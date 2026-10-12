"""Tests for the smaller M10 Hardening fixes: the clipboard save limit, the cleanup length
guard, and dictation text hidden from the console when installed.

No real clipboard, no network. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_hardening -v
"""

import contextlib
import io
import re
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import groq
import httpx
import numpy as np

from app import api, keystore, log
from app import pipeline as pipeline_module
from app.cleanup import clean_up, too_long
from app.config import DEFAULTS
from app.paster import _collect, is_hidden
from app.usage import UsageTracker
from tests.test_hallucination import spoken

MB = 1024 * 1024


class ClipboardLimitTests(unittest.TestCase):
    def collect(self, sizes, limit):
        read = []

        def reader(fmt):
            read.append(fmt)
            return b"x" * sizes[fmt]

        return _collect(sizes, sizes.get, reader, limit), read

    def test_everything_under_the_limit_is_saved(self):
        saved, _ = self.collect({1: 10, 13: 20, 49: 30}, limit=100)
        self.assertEqual([(fmt, len(data)) for fmt, data in saved], [(1, 10), (13, 20), (49, 30)])

    def test_over_the_limit_stops_before_reading_more(self):
        with self.assertRaises(OSError) as caught:
            self.collect({1: 60, 13: 60, 49: 1}, limit=100)
        self.assertIn("too big", str(caught.exception))

    def test_a_4k_screenshot_is_restored(self):
        screenshot = 3840 * 2160 * 4 + 40  # a DIB: pixels plus its header
        saved, _ = self.collect({8: screenshot, 17: screenshot}, limit=256 * MB)
        self.assertEqual(len(saved), 2)

    def test_a_format_that_cant_be_read_is_skipped(self):
        saved = _collect([1, 2], lambda fmt: 5, lambda fmt: None if fmt == 1 else b"ok", 100)
        self.assertEqual(saved, [(2, b"ok")])

    def test_text_left_on_the_clipboard_hides_from_the_dictionary_panel(self):
        # copy_only marks only CanUploadToCloudClipboard = 0; the panel counts that as private.
        self.assertTrue(is_hidden({"CanUploadToCloudClipboard": (0).to_bytes(4, "little")}))


class FakeResponse:
    def __init__(self, text):
        self.headers = {}
        self._text = text

    def parse(self):
        message = SimpleNamespace(content=self._text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeTracker:
    def log_cleanup(self, latency_ms, usage, headers):
        return 300


class CleanupGuardTests(unittest.TestCase):
    def run_cleanup(self, raw, answer):
        with mock.patch("app.api.request", return_value=(FakeResponse(answer), 400.0)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = clean_up(client=None, tracker=FakeTracker(), raw=raw, terms=[])
        return result, out.getvalue()

    def test_normal_cleanup_is_used(self):
        (cleaned, status, _), _ = self.run_cleanup("um what time is it", "What time is it?")
        self.assertEqual((cleaned, status), ("What time is it?", "ok"))

    def test_an_answer_instead_of_a_cleanup_is_not_used(self):
        answer = ("It is currently 3:30 PM in Lisbon. If you need the time in another city, let me know "
                  "and I can convert it for you, or set a reminder for later today.")
        (cleaned, status, tokens), output = self.run_cleanup("what time is it", answer)
        self.assertEqual((cleaned, status, tokens), (None, "failed", 300))  # the raw transcript is pasted
        self.assertIn("much longer", output)
        self.assertNotIn("Lisbon", output)  # the text itself isn't printed

    def test_the_limit(self):
        raw = "one two three four five"
        self.assertFalse(too_long(raw, " ".join(["w"] * 20)))  # 2 x 5 + 10
        self.assertTrue(too_long(raw, " ".join(["w"] * 21)))
        self.assertFalse(too_long("hi", "Hi, this is a short but longer sentence now."))


class HiddenTextTests(unittest.TestCase):
    def test_from_source_the_text_is_printed(self):
        with mock.patch.object(pipeline_module, "hides_text", lambda: False):
            self.assertEqual(pipeline_module.shown("Hello there."), "Hello there.")

    def test_installed_only_the_word_count_is_printed(self):
        with mock.patch.object(pipeline_module, "hides_text", lambda: True):
            self.assertEqual(pipeline_module.shown("Hello there, friend."), "(hidden, 3 words)")


# M16 S4: Groq's error body (and Python's own error messages) never reach the console or log
# file as they are, because they can quote what was sent.
SENTENCE = "please remind me about the dentist on thursday"  # stands in for dictated text
FAKE_KEY = "gsk_" + "Zz9" * 16  # shaped like a key, not a real one
REAL_401 = {"error": {"message": "Invalid API Key", "type": "invalid_request_error", "code": "invalid_api_key"}}


def groq_answering(status, body):
    def handler(request):
        return httpx.Response(status, json=body)
    return keystore.make_client(FAKE_KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def status_error(status, body):
    try:
        groq_answering(status, body).models.list()
    except groq.APIStatusError as e:
        return e
    raise AssertionError("expected a Groq error")


class ErrorLogTests(unittest.TestCase):
    def setUp(self):
        # Four tests reach the real api.request, which reads config.json's debug_fake_rate_limit.
        # Without this, a value set there would be used up by a test (and written back as null).
        no_fake = mock.patch("app.api._take_fake_rate_limit", return_value=None)
        no_fake.start()
        self.addCleanup(no_fake.stop)

    def tracker(self):
        tracker = UsageTracker(":memory:", DEFAULTS["groq_limits"])
        self.addCleanup(tracker.close)
        return tracker

    def test_a_real_groq_body_keeps_only_status_type_and_code(self):
        self.assertEqual(api.describe_for_log(status_error(401, REAL_401)),
                         "Groq error 401 (invalid_request_error, invalid_api_key)")

    def test_the_message_never_gets_through(self):
        body = {"error": {"message": f"could not process '{SENTENCE}' for {FAKE_KEY}",
                          "type": "invalid_request_error", "code": "bad_input", "failed_generation": SENTENCE}}
        line = api.describe_for_log(status_error(400, body))
        self.assertEqual(line, "Groq error 400 (invalid_request_error, bad_input)")

    def test_odd_bodies(self):
        self.assertEqual(api.describe_for_log(status_error(500, ["not", "a", "dict"])), "Groq error 500")
        self.assertEqual(api.describe_for_log(status_error(502, {"error": "just text " + SENTENCE})), "Groq error 502")
        evil = {"error": {"type": "has spaces: " + SENTENCE + " " + "x" * 200, "code": 42}}
        line = api.describe_for_log(status_error(400, evil))
        self.assertNotIn(" ", line.split("(", 1)[1])  # only plain word characters inside the brackets
        self.assertLessEqual(len(line), len("Groq error 400 ()") + api.MAX_ERROR_FIELD)

    def test_other_groq_errors(self):
        request = httpx.Request("GET", "https://api.groq.com/openai/v1/models")
        self.assertEqual(api.describe_for_log(groq.APIConnectionError(request=request)), "APIConnectionError")
        self.assertEqual(api.describe_for_log(groq.APITimeoutError(request=request)), "APITimeoutError")
        response = httpx.Response(200, request=request)
        validation = groq.APIResponseValidationError(response, body={"text": SENTENCE})
        self.assertEqual(api.describe_for_log(validation), "Groq error (APIResponseValidationError)")

    def test_our_rate_limit_message_is_kept(self):
        limited = api.RateLimited("cleanup", {"name": "requests per minute", "retry_in": 20.0})
        self.assertEqual(api.describe_for_log(limited), "cleanup rate limited (requests per minute), try again in 20s")

    def test_unexpected_errors_hide_their_message_wherever_output_is_kept(self):
        try:
            int(SENTENCE)  # Python repeats the input in its message
        except ValueError as e:
            error = e
        with mock.patch.object(log, "hides_text", lambda: True):
            line = api.describe_for_log(error)
        self.assertNotIn(SENTENCE, line)
        self.assertRegex(line, r"^ValueError at test_hardening\.py:\d+ \(message hidden\)$")
        with mock.patch.object(log, "hides_text", lambda: False):
            self.assertIn(SENTENCE, api.describe_for_log(error))  # a console from source: full message

    def test_the_crash_report_hides_the_message_but_keeps_the_stack(self):
        try:
            int(SENTENCE)
        except ValueError as e:
            error = e
        with mock.patch.object(log, "hides_text", lambda: True), \
                contextlib.redirect_stderr(io.StringIO()) as out:
            log.print_exception(error)
        report = out.getvalue()
        self.assertNotIn(SENTENCE, report)
        self.assertIn("test_hardening.py", report)
        self.assertIn("ValueError (message hidden)", report)

    def test_cleanup_logs_no_body(self):
        body = {"error": {"message": f"bad request: {SENTENCE}", "type": "invalid_request_error", "code": "bad_input"}}
        tracker = self.tracker()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cleaned, status, _ = clean_up(groq_answering(400, body), tracker, SENTENCE, [])
        self.assertEqual((cleaned, status), (None, "failed"))
        self.assertIn("cleanup error: Groq error 400 (invalid_request_error, bad_input)", out.getvalue())
        self.assertNotIn("dentist", out.getvalue())

    def test_transcription_logs_no_body(self):
        body = {"error": {"message": f"bad audio after '{SENTENCE}'", "type": "invalid_request_error",
                          "code": "bad_audio"}}
        tracker = self.tracker()
        pipeline = pipeline_module.Pipeline(groq_answering(400, body), None, {"language": "en"}, tracker)
        audio = np.zeros(16000, dtype=np.int16)
        with mock.patch.object(pipeline_module, "load_terms", lambda: []), \
                mock.patch.object(pipeline_module, "load_config", lambda: {"language": "en"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            heard = pipeline._hear(audio, pipeline_module.to_wav_bytes(audio), 0.1, 1000)
        self.assertEqual((heard.problem, heard.error), ("failed", "Groq error 400"))
        self.assertIn("transcription failed: Groq error 400 (invalid_request_error, bad_audio)", out.getvalue())
        self.assertNotIn("dentist", out.getvalue())

    # A 429 takes another path: api.request turns it into our own RateLimited, whose message is
    # built from the limit's name and the wait. Groq's real 429 message has the same shape as
    # this one; the made-up sentence is added to its message and to another body field.
    RATE_LIMIT_BODY = {"error": {
        "message": "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_fake` on requests "
                   f"per minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 30s. Seen: {SENTENCE}",
        "type": "requests", "code": "rate_limit_exceeded", "failed_generation": SENTENCE}}

    def test_a_cleanup_429_logs_no_body(self):
        tracker = self.tracker()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cleaned, status, _ = clean_up(groq_answering(429, self.RATE_LIMIT_BODY), tracker, "hello there", [])
        self.assertEqual((cleaned, status), (None, "limited"))
        self.assertIn("cleanup: cleanup rate limited (requests per minute), try again in 30s", out.getvalue())
        self.assertNotIn("dentist", out.getvalue())
        self.assertNotIn("org_fake", out.getvalue())

    def test_a_transcription_429_logs_no_body(self):
        tracker = self.tracker()
        pipeline = pipeline_module.Pipeline(groq_answering(429, self.RATE_LIMIT_BODY), None, {"language": "en"},
                                            tracker)
        audio = np.zeros(16000, dtype=np.int16)
        with mock.patch.object(pipeline_module, "load_terms", lambda: []), \
                mock.patch.object(pipeline_module, "load_config", lambda: {"language": "en"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            heard = pipeline._hear(audio, pipeline_module.to_wav_bytes(audio), 0.1, 1000)
        self.assertEqual((heard.problem, heard.error), ("limited", "Rate limit: requests per minute"))
        self.assertIn("transcription failed: transcription rate limited (requests per minute), try again in 30s",
                      out.getvalue())
        self.assertNotIn("dentist", out.getvalue())
        self.assertNotIn("org_fake", out.getvalue())

    def test_a_crashed_dictation_hides_the_message_in_the_log(self):
        tracker = self.tracker()
        pipeline = pipeline_module.Pipeline(None, None, {}, tracker)

        def crash(*args):
            raise ValueError(f"invalid literal for int() with base 10: '{SENTENCE}'")

        with mock.patch.object(pipeline, "_process", crash), \
                mock.patch.object(log, "hides_text", lambda: True), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            pipeline._run_job("dictation", np.zeros(1600, dtype=np.int16), 0.0)
        self.assertNotIn("dentist", out.getvalue() + err.getvalue())
        self.assertIn("ValueError (message hidden)", err.getvalue())
        self.assertIn("dictation failed", out.getvalue())

    def history_save_error(self, keep):
        """M19: History couldn't save a dictation, with an error that quotes its text."""
        def add(*args, **fields):
            raise ValueError(f"can't store '{SENTENCE}'")

        pipeline = pipeline_module.Pipeline(None, None, {}, self.tracker(), history=SimpleNamespace(add=add))
        with mock.patch.object(pipeline_module, "load_config", lambda: {"history": True}), \
                mock.patch.object(log, "hides_text", lambda: keep), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            pipeline._save_entry("pasted", raw=SENTENCE, cleaned=SENTENCE)
        return out.getvalue()

    def test_a_history_save_error_hides_its_message_in_the_log(self):
        text = self.history_save_error(keep=True)
        self.assertNotIn("dentist", text)
        self.assertRegex(text, r"history: couldn't save this dictation: ValueError at test_hardening\.py:\d+ "
                               r"\(message hidden\)")

    def test_a_history_save_error_keeps_its_message_on_a_console(self):
        self.assertIn(SENTENCE, self.history_save_error(keep=False))


class TimingLogTests(unittest.TestCase):
    """M19: how long each part of a dictation took, so a slow Groq can be told from a slow paste.
    Groq and the paste are faked; nothing is sent or pasted."""

    def setUp(self):
        for name, value in [("load_terms", lambda: []), ("load_config", lambda: {"language": "en"})]:
            patcher = mock.patch.object(pipeline_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        tracker = UsageTracker(":memory:", DEFAULTS["groq_limits"])
        self.addCleanup(tracker.close)
        self.pipeline = pipeline_module.Pipeline(object(), None, dict(DEFAULTS), tracker)

    def dictate(self, keep, transcribe=None):
        """One dictation of SENTENCE, 5 seconds after the recording ended. Returns everything printed."""
        transcribe = transcribe or mock.Mock(return_value=(SENTENCE, 10.0))
        with mock.patch.object(pipeline_module, "transcribe", transcribe), \
                mock.patch.object(pipeline_module, "clean_up", return_value=(SENTENCE, "ok", 300)), \
                mock.patch.object(self.pipeline, "_deliver", return_value="pasted"), \
                mock.patch.object(pipeline_module, "hides_text", lambda: keep), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.pipeline._process(spoken(1500), time.perf_counter() - 5.0)
        return out.getvalue() + err.getvalue()

    def test_a_failed_transcription_says_how_long_it_took(self):
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")
        limited = api.RateLimited("transcription", {"name": "requests per minute", "retry_in": 30.0})
        for error, name in [(groq.APIConnectionError(request=request), "APIConnectionError"),
                            (limited, r"transcription rate limited \(requests per minute\), try again in 30s")]:
            with self.subTest(error=type(error).__name__):
                text = self.dictate(keep=False, transcribe=mock.Mock(side_effect=error))
                self.assertRegex(text, rf"transcription failed: {name} \(after \d+\.\d\ds\)")

    def test_a_dictation_says_how_long_the_paste_took(self):
        text = self.dictate(keep=False)
        self.assertRegex(text, r"transcribe \d+\.\d\ds \| cleanup \d+\.\d\ds \| total \d+\.\d\ds")
        found = re.search(r"paste (\d+\.\d\d)s \| whole dictation (\d+\.\d\d)s since the recording ended\n", text)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(float(found[2]), 5.0)  # counted from when the recording ended
        self.assertLess(float(found[2]), 60.0)

    def test_the_timing_lines_never_show_dictated_text_in_the_log(self):
        text = self.dictate(keep=True)
        self.assertIn("paste ", text)  # the new line is there...
        self.assertIn("RAW:     (hidden, 8 words)", text)
        self.assertNotIn("dentist", text)  # ...and the dictated text is nowhere


if __name__ == "__main__":
    unittest.main()
