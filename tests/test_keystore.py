"""Tests for the API key: Credential Manager, the lookup order, the key check, and that
the key never shows up in the log file, the console or an error message.

Uses the real Credential Manager with throwaway entries ("Blurt (unit test ...)"),
deleted afterwards; your real key and the sandbox's test key are never touched.
Groq is faked (httpx.MockTransport), so no network. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_keystore -v
"""

import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
import uuid
from pathlib import Path
from unittest import mock

import httpx

from app import keystore, log, paths
from app.transcribe import warm_up

FAKE_KEY = "gsk_FAKEtestKEY0123456789abcdefghijklmnopqrstuvwxyzABCD"
ROOT = Path(__file__).resolve().parent.parent


def throwaway_name():
    return f"{paths.APP_ID} (unit test {uuid.uuid4().hex[:8]})/groq_api_key"


def fake_groq(status=200, body=None, fail=False):
    """A Groq client whose every request gets this answer (or a connection error)."""
    def handler(request):
        if fail:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(status, json=body if body is not None else {"object": "list", "data": []})
    return keystore.make_client(FAKE_KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


class CredentialManagerTest(unittest.TestCase):
    def setUp(self):
        self.name = throwaway_name()
        self.addCleanup(keystore.delete_key, self.name)

    def test_save_read_replace_delete(self):
        self.assertIsNone(keystore.read_saved_key(self.name))
        keystore.save_key("gsk_first_key_123456", self.name)
        self.assertEqual(keystore.read_saved_key(self.name), "gsk_first_key_123456")
        keystore.save_key("gsk_second_key_123456", self.name)
        self.assertEqual(keystore.read_saved_key(self.name), "gsk_second_key_123456")
        self.assertTrue(keystore.delete_key(self.name))
        self.assertIsNone(keystore.read_saved_key(self.name))
        self.assertFalse(keystore.delete_key(self.name))  # already gone: fine

    def test_names_for_real_and_test_runs_differ(self):
        self.assertEqual(keystore.REAL_CREDENTIAL_NAME, "Blurt/groq_api_key")
        self.assertNotEqual(keystore.TEST_CREDENTIAL_NAME, keystore.REAL_CREDENTIAL_NAME)


class LookupOrderTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.env_path = self.folder / ".env"
        for name, value in [("ENV_PATH", self.env_path), ("INSTALLED", False)]:
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.saved = None
        patch = mock.patch.object(keystore, "read_saved_key", lambda name=None: self.saved)
        patch.start()
        self.addCleanup(patch.stop)

    def test_nothing_anywhere(self):
        self.assertEqual(keystore.find_key(), (None, None))

    def test_env_file_from_source(self):
        self.env_path.write_text("GROQ_API_KEY=gsk_from_env_file_1234\n", encoding="utf-8")
        self.assertEqual(keystore.find_key(), ("gsk_from_env_file_1234", "env"))

    def test_saved_key_wins_over_env(self):
        self.env_path.write_text("GROQ_API_KEY=gsk_from_env_file_1234\n", encoding="utf-8")
        self.saved = "gsk_saved_in_windows_1234"
        self.assertEqual(keystore.find_key(), ("gsk_saved_in_windows_1234", "windows"))

    def test_installed_ignores_env(self):
        self.env_path.write_text("GROQ_API_KEY=gsk_from_env_file_1234\n", encoding="utf-8")
        with mock.patch.object(paths, "INSTALLED", True):
            self.assertEqual(keystore.find_key(), (None, None))

    def test_the_environment_is_left_alone(self):
        self.env_path.write_text("GROQ_API_KEY=gsk_from_env_file_1234\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_stray_variable_1234"}):
            self.assertEqual(keystore.find_key()[0], "gsk_from_env_file_1234")
            self.assertEqual(os.environ["GROQ_API_KEY"], "gsk_stray_variable_1234")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            keystore.find_key()
            self.assertNotIn("GROQ_API_KEY", os.environ)  # read without being put in the environment

    def test_a_broken_credential_store_falls_back(self):
        def broken(name=None):
            raise keystore.KeyStoreError("couldn't read the saved API key (Windows error 5)")
        self.env_path.write_text("GROQ_API_KEY=gsk_from_env_file_1234\n", encoding="utf-8")
        with mock.patch.object(keystore, "read_saved_key", broken), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(keystore.find_key(), ("gsk_from_env_file_1234", "env"))


class CheckKeyTest(unittest.TestCase):
    def test_answers(self):
        self.assertEqual(keystore.check_key(FAKE_KEY, fake_groq(200)), "works")
        self.assertEqual(keystore.check_key(FAKE_KEY, fake_groq(401, {"error": {"message": "Invalid API Key"}})),
                         "rejected")
        self.assertEqual(keystore.check_key(FAKE_KEY, fake_groq(403, {"error": {"message": "Forbidden"}})),
                         "rejected")
        self.assertEqual(keystore.check_key(FAKE_KEY, fake_groq(500, {"error": {"message": "oops"}})),
                         "unreachable")
        self.assertEqual(keystore.check_key(FAKE_KEY, fake_groq(fail=True)), "unreachable")

    def test_cleaning_pasted_text(self):
        self.assertEqual(keystore.clean_key("  gsk_abc\r\n def \t"), "gsk_abcdef")
        self.assertEqual(keystore.clean_key(None), "")
        self.assertTrue(keystore.looks_like_groq_key("gsk_abc"))
        self.assertFalse(keystore.looks_like_groq_key("sk-abc"))


class EnvironmentCantRedirectTest(unittest.TestCase):
    """M16 S2: GROQ_BASE_URL and GROQ_CUSTOM_HEADERS are read by the Groq SDK itself. Neither may
    change where the key goes or what's sent with it. If a Groq SDK upgrade renames the private
    attribute make_client empties, this fails."""

    EVIL = {"GROQ_BASE_URL": "https://example.invalid/steal",
            "GROQ_CUSTOM_HEADERS": "X-Evil: 1\nAuthorization: Bearer someone-elses-key"}

    def sent_request(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"object": "list", "data": []})

        with mock.patch.dict(os.environ, self.EVIL):
            client = keystore.make_client(FAKE_KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            client.models.list()
            client.copy().models.list()  # a copy (with_options) must stay clean too
        return seen

    def test_the_request_goes_to_groq_with_our_key_only(self):
        for request in self.sent_request():
            self.assertEqual(request.url.host, "api.groq.com")
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.headers["authorization"], f"Bearer {FAKE_KEY}")
            self.assertNotIn("x-evil", request.headers)

    def test_proxies_are_still_honored(self):
        # The SDK's own httpx client reads HTTPS_PROXY and SSL_CERT_FILE (trust_env); we keep it.
        self.assertTrue(keystore.make_client(FAKE_KEY)._client.trust_env)


class GroqLogIgnoredTest(unittest.TestCase):
    """M17: GROQ_LOG=debug would make the SDK log every request (transcript, prompt, dictionary).
    It's read once, when groq is imported, so this needs a fresh Python: a child process."""

    PROBE = ("import logging, os, app.keystore, groq; "
             "print(logging.getLogger('groq').level, logging.getLogger('httpx').level, "
             "len(logging.getLogger().handlers), 'GROQ_LOG' in os.environ)")

    def run_probe(self, value):
        env = dict(os.environ, GROQ_LOG=value)
        result = subprocess.run([sys.executable, "-c", self.PROBE], cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_groq_log_is_ignored(self):
        for value in ("debug", "info"):
            with self.subTest(value=value):
                # Logger levels stay NOTSET (0), no handler is added, and the variable is gone.
                self.assertEqual(self.run_probe(value), ["0", "0", "0", "False"])

    def test_the_probe_would_see_it(self):
        # Without app first, the same probe shows the SDK turning its logging on.
        env = dict(os.environ, GROQ_LOG="debug")
        probe = "import logging, groq; print(logging.getLogger('groq').level, len(logging.getLogger().handlers))"
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.stdout.split(), [str(logging.DEBUG), "1"])


class KeyNeverPrintedTest(unittest.TestCase):
    """Save a fake key, make everything that can go wrong go wrong, and search the log file."""

    def setUp(self):
        self.name = throwaway_name()
        self.addCleanup(keystore.delete_key, self.name)
        self.log_path = Path(tempfile.mkdtemp()) / "logs" / "app.log"
        self.log_file = log.LogFile(self.log_path)
        self.addCleanup(self.log_file.close)
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = self.log_file
        self.addCleanup(lambda: (setattr(sys, "stdout", old[0]), setattr(sys, "stderr", old[1])))

    def test_errors_never_put_the_key_in_the_log(self):
        keystore.save_key(FAKE_KEY, self.name)
        key = keystore.read_saved_key(self.name)
        self.assertEqual(key, FAKE_KEY)

        # Groq answering with the key in its error message (the worst case).
        echo = {"error": {"message": f"Invalid API Key: {FAKE_KEY}"}}
        self.assertEqual(keystore.check_key(key, fake_groq(401, echo)), "rejected")
        client = fake_groq(401, echo)
        try:
            client.models.list()
        except Exception as e:
            print(f"request failed: {e}")
            traceback.print_exc()
        # The warm-up with a rejected key, and with no connection.
        warm_up(client)
        warm_up(fake_groq(fail=True))
        # A crash whose message holds the key, as Python would report it.
        try:
            raise RuntimeError(f"something went wrong with {FAKE_KEY}")
        except RuntimeError:
            traceback.print_exc()
        # A key that doesn't look like a Groq key is hidden too, once it's in use.
        odd_key = "not-a-groq-shaped-secret-987654"
        log.add_secret(odd_key)
        print(f"oops: {odd_key}")
        # And printing it straight out.
        print(key)
        print("the end, half a line then", end="")
        print(f" {FAKE_KEY[:30]}", end="")
        print(FAKE_KEY[30:])
        sys.stdout.flush()

        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("request failed", text)
        self.assertIn("RuntimeError", text)
        self.assertIn(log.HIDDEN, text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_KEY[4:], text)
        self.assertNotIn(odd_key, text)

    def test_keystore_errors_never_contain_the_key(self):
        with mock.patch.object(keystore.advapi32, "CredWriteW", lambda *a: False):
            with self.assertRaises(keystore.KeyStoreError) as caught:
                keystore.save_key(FAKE_KEY, self.name)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIsNone(keystore.read_saved_key(self.name))

    def test_the_console_hides_it_too(self):
        console = io.StringIO()
        stream = log.RedactingStream(console)
        stream.write(f"key is {FAKE_KEY}\n")
        stream.flush()
        self.assertNotIn(FAKE_KEY, console.getvalue())
        self.assertIn(log.HIDDEN, console.getvalue())


if __name__ == "__main__":
    unittest.main()
