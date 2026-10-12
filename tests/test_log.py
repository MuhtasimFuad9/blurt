"""Tests for the log file used when there's no console (log.py).

Temporary folders only. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_log -v
"""

import contextlib
import ctypes
import inspect
import io
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import log
from app import pipeline as pipeline_module


def isolate_start(test):
    """log.start() sets process-wide things (M17): the error hooks and the user folder forms.
    Put them back after the test, so other tests never run with them."""
    for target, name in ((sys, "excepthook"), (sys, "unraisablehook"), (threading, "excepthook"),
                         (log, "_profile_patterns")):
        patcher = mock.patch.object(target, name, getattr(target, name) if name != "_profile_patterns" else [])
        patcher.start()
        test.addCleanup(patcher.stop)


class LogFileTest(unittest.TestCase):
    def setUp(self):
        isolate_start(self)
        self.path = Path(tempfile.mkdtemp()) / "logs" / "app.log"
        self.old = self.path.with_name("app.log.1")

    def make(self, max_bytes=log.MAX_BYTES):
        log_file = log.LogFile(self.path, max_bytes)
        self.addCleanup(log_file.close)
        return log_file

    def test_lines_get_a_timestamp_and_the_folder_is_made(self):
        log_file = self.make()
        print("hello", file=log_file)
        print("half", end="", file=log_file)
        print(" a line", file=log_file)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d hello$")
        self.assertTrue(lines[1].endswith(" half a line"))

    def test_capped_with_one_older_file(self):
        log_file = self.make(max_bytes=2000)
        for i in range(500):
            print(f"line {i:04d} " + "x" * 40, file=log_file)
        self.assertLessEqual(self.path.stat().st_size, 2000)
        self.assertLessEqual(self.old.stat().st_size, 2000)
        self.assertFalse(self.path.with_name("app.log.2").exists())
        self.assertIn("line 0499", self.path.read_text(encoding="utf-8"))  # the newest line is kept

    def test_a_new_file_starts_with_the_version(self):
        # M19: app.log after a rotation, sent on its own, still says which version wrote it.
        log_file = self.make(max_bytes=2000)
        for i in range(500):
            print(f"line {i:04d} " + "x" * 40, file=log_file)
        first = self.path.read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(first, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d --- Blurt \S+ "
                                r"\(continued, older lines are in app\.log\.1\) ---$")
        self.assertIn(log.VERSION, first)
        self.assertLessEqual(self.path.stat().st_size, 2000)  # the header counts toward the cap
        self.assertLessEqual(self.old.stat().st_size, 2000)

    def test_keys_and_the_user_folder_stay_hidden_across_a_rotation(self):
        # M19: the line that makes app.log rotate is redacted like any other, in both files.
        log.set_profile(LONG, SHORT)
        log_file = self.make(max_bytes=2000)
        for i in range(100):
            print(f"line {i:02d} {LONG}\\AppData {FAKE_KEY}", file=log_file)
            if self.old.exists():
                break  # stop right after the line that started the new file
        new = self.path.read_text(encoding="utf-8")
        old = self.old.read_text(encoding="utf-8")
        self.assertEqual(len(new.splitlines()), 2)  # the header, then that line
        for text in (new, old):
            self.assertIn("%USERPROFILE%\\AppData [key hidden]", text)
            self.assertNotIn("LongUserName", text)
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("gsk_", text)

    def test_an_unwritable_log_never_crashes(self):
        self.path.parent.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.write_bytes(b"")  # a file where the logs folder should be
        log_file = self.make()
        print("nobody will read this", file=log_file)
        log_file.flush()

    def test_a_blocked_rename_keeps_logging(self):
        log_file = self.make(max_bytes=500)
        with mock.patch.object(log.os, "replace", side_effect=PermissionError("in use")) as replace:
            for i in range(40):
                print(f"line {i:02d} " + "x" * 40, file=log_file)
            self.assertEqual(replace.call_count, 1)  # not retried on every line
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("line 00", text)
        self.assertIn("line 39", text)  # nothing dropped while app.log couldn't be renamed
        self.assertNotIn("continued", text)  # M19: still the same file, so no version header
        with mock.patch.object(log.time, "monotonic", return_value=time.monotonic() + log.ROTATE_RETRY_SECONDS):
            print("after the lock", file=log_file)
        self.assertTrue(self.old.exists())  # tried again later, and it worked
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertIn("(continued, older lines are in app.log.1)", lines[0])  # now it's a new file
        self.assertTrue(lines[1].endswith(" after the lock"))

    def test_odd_characters_never_crash_print(self):
        log_file = self.make()
        print("mic \udc81 name", file=log_file)  # a lone surrogate: strict UTF-8 can't write it
        print("next line", file=log_file)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("mic ? name", text)
        self.assertIn("next line", text)

    def test_start_without_a_console_writes_to_the_file(self):
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), \
                mock.patch.object(log, "_to_file", False):
            self.assertTrue(log.start(self.path))
            print("from print")
            print("from stderr", file=sys.stderr)
            self.assertTrue(log.hides_text())
            sys.stdout.close()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("started", text)
        self.assertRegex(text, r"--- Blurt \S+ started \(Windows \d+\.\d+\.\d+\) ---")  # M19: which Windows
        self.assertIn("from print", text)
        self.assertIn("from stderr", text)

    def test_dictation_text_is_hidden_only_when_logging_to_a_file(self):
        with mock.patch.object(log, "_to_file", True):
            self.assertEqual(pipeline_module.shown("Hello there, friend."), "(hidden, 3 words)")
        with mock.patch.object(log, "_to_file", False), mock.patch.object(log.paths, "INSTALLED", False):
            self.assertEqual(pipeline_module.shown("Hello there."), "Hello there.")


LONG = "C:\\Users\\LongUserName"
SHORT = "C:\\Users\\LONGUS~1"
FAKE_KEY = "gsk_" + "Ab3" * 16  # shaped like a key, not a real one


class UserFolderTest(unittest.TestCase):
    """M17: the user folder never reaches the log by name. Faked folders only, never this PC's."""

    def setUp(self):
        isolate_start(self)
        log.set_profile(LONG, SHORT)

    def test_the_long_form(self):
        self.assertEqual(log.redact(f"created config.json in {LONG}\\AppData\\Local\\Blurt"),
                         "created config.json in %USERPROFILE%\\AppData\\Local\\Blurt")
        self.assertEqual(log.redact(f"couldn't create {LONG}"), "couldn't create %USERPROFILE%")

    def test_doubled_backslashes(self):
        try:
            open(f"{LONG}\\no such folder 1234\\x.txt", encoding="utf-8")
        except OSError as e:
            message = str(e)  # what the app prints: Python doubles the backslashes in it
        self.assertIn("C:\\\\Users\\\\LongUserName", message)
        self.assertNotIn("LongUserName", log.redact(f"couldn't read it: {message}"))
        self.assertIn("%USERPROFILE%\\\\no such folder 1234", log.redact(message))

    def test_the_short_form(self):
        self.assertEqual(log.redact(f"{SHORT}\\AppData\\Local\\Temp"), "%USERPROFILE%\\AppData\\Local\\Temp")
        doubled = SHORT.replace("\\", "\\\\") + "\\\\AppData"
        self.assertEqual(log.redact(f"'{doubled}'"), "'%USERPROFILE%\\\\AppData'")

    def test_any_case(self):
        for text in (LONG.lower(), LONG.upper(), "c:\\users\\longUSERname", SHORT.lower(), "c:\\Users\\Longus~1"):
            with self.subTest(text=text):
                self.assertEqual(log.redact(f"in {text}\\x"), "in %USERPROFILE%\\x")

    def test_only_where_the_folder_name_ends(self):
        for text in (f"{LONG}X\\AppData", f"{SHORT}0\\AppData", f"{LONG}.old"):
            with self.subTest(text=text):
                self.assertEqual(log.redact(text), text)
        for ending in ("/", "'", '"', " ", ""):
            with self.subTest(ending=ending):
                self.assertEqual(log.redact(LONG + ending), "%USERPROFILE%" + ending)

    def test_the_short_form_comes_from_windows_only_when_different(self):
        environ = {"USERPROFILE": LONG}
        with mock.patch.object(log, "short_path", return_value=SHORT):
            self.assertEqual(log.find_profile(environ), (LONG, SHORT))
        for same in (LONG, LONG.upper(), None):  # short names off, or the call failed
            with self.subTest(short=same), mock.patch.object(log, "short_path", return_value=same):
                self.assertEqual(log.find_profile(environ), (LONG, None))

    def test_a_failed_windows_call_gives_no_short_form(self):
        with mock.patch.object(log.kernel32, "GetShortPathNameW", return_value=0):
            self.assertIsNone(log.short_path(LONG))

    def test_a_missing_folder_has_no_short_form(self):
        self.assertIsNone(log.short_path("C:\\no such folder 1234\\really"))

    def test_keys_are_still_hidden(self):
        self.assertEqual(log.redact(f"{LONG} gsk_abcdefghijklmnop"), "%USERPROFILE% [key hidden]")


class UncaughtErrorTest(unittest.TestCase):
    """M17: errors nobody catches print their stack but not their message wherever output is kept."""

    def setUp(self):
        isolate_start(self)
        log.catch_uncaught_errors()

    def error(self):
        message = " ".join(["secret", "dictated", "words"])  # a stack shows code lines, so not written out there
        try:
            raise ValueError(message)
        except ValueError as e:
            return e

    def output(self, call, keep):
        err = io.StringIO()
        with mock.patch.object(log, "_to_file", keep), mock.patch.object(log.paths, "INSTALLED", False), \
                contextlib.redirect_stderr(err):
            call()
        return err.getvalue()

    def calls(self):
        error = self.error()
        return {
            "main thread or Qt": lambda: sys.excepthook(ValueError, error, error.__traceback__),
            "another thread": lambda: threading.excepthook(
                SimpleNamespace(exc_type=ValueError, exc_value=error, exc_traceback=error.__traceback__,
                                thread=threading.current_thread())),
            "keyboard hook callback": lambda: sys.unraisablehook(
                SimpleNamespace(exc_type=ValueError, exc_value=error, exc_traceback=error.__traceback__,
                                err_msg="Exception ignored on calling ctypes callback function",
                                object=lambda: "secret dictated words")),
        }

    def test_the_message_is_hidden_when_output_is_kept(self):
        for where, call in self.calls().items():
            with self.subTest(where=where):
                text = self.output(call, keep=True)
                self.assertIn("ValueError (message hidden)", text)
                self.assertIn("test_log.py", text)  # the stack is still there
                self.assertNotIn("secret", text)

    def test_the_message_stays_on_a_console(self):
        for where, call in self.calls().items():
            with self.subTest(where=where):
                self.assertIn("secret dictated words", self.output(call, keep=False))

    def test_a_hook_never_raises(self):
        broken = SimpleNamespace()  # none of the fields Python passes
        sys.excepthook(None, None, None)
        threading.excepthook(broken)
        sys.unraisablehook(broken)

    def test_a_thread_ending_with_systemexit_is_quiet(self):
        args = SimpleNamespace(exc_type=SystemExit, exc_value=SystemExit(), exc_traceback=None, thread=None)
        self.assertEqual(self.output(lambda: threading.excepthook(args), keep=True), "")

    def test_main_prints_caught_crashes_the_same_way(self):
        # run_worker and shutdown used traceback.print_exc(), which prints the message.
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("print_exc(", source)
        self.assertEqual(source.count("log.print_exception(e)"), 2)
        # M19: the worker printed a hook error as "hook error: {repr}", message included.
        self.assertFalse("hook error: {detail}" in source)

    def hook_error_output(self, keep):
        """M19: a key event whose handling raises, through the hook's real entry point, then
        printed by the worker's report_hook_error. Returns everything printed."""
        import main
        from app import hotkey as hotkey_module

        hotkey = hotkey_module.Hotkey(queue.Queue())
        kb = hotkey_module.KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0, flags=0, time=0, dwExtraInfo=0)
        with mock.patch.object(hotkey, "_handle", side_effect=self.error()), \
                mock.patch.object(hotkey_module.user32, "CallNextHookEx", lambda *args: 0):
            hotkey._on_key(hotkey_module.HC_ACTION, hotkey_module.WM_KEYDOWN, ctypes.addressof(kb))
        kind, _, detail = hotkey.events.get_nowait()
        self.assertEqual(kind, "hook_error")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            text = self.output(lambda: main.report_hook_error(detail), keep)
        return out.getvalue() + text

    def test_a_hook_error_hides_its_message_when_output_is_kept(self):
        text = self.hook_error_output(keep=True)
        self.assertIn("keyboard hook error", text)
        self.assertIn("ValueError (message hidden)", text)
        self.assertIn("hotkey.py", text)  # the stack: where in the hook it went wrong
        self.assertNotIn("secret", text)

    def test_a_hook_error_keeps_its_message_on_a_console(self):
        self.assertIn("secret dictated words", self.hook_error_output(keep=False))


PROJECT = Path(__file__).resolve().parent.parent
CHILD_TIMEOUT_SECONDS = 60  # a child that hangs fails the test instead of hanging the run
# A child Blurt, cut down to the log: argv[1] is the project folder, argv[2] what to do.
CHILD = """\
import sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] != "console":
    sys.stdout = sys.stderr = None  # like the installed app: no console, so the log file
from app import log
log.start()
if sys.argv[2] in ("watch", "clean", "crash", "console"):
    log.watch_for_crashes()
if sys.argv[2] == "clean":
    log.stop_watching_for_crashes()
    log.stop_watching_for_crashes()  # a second stop is harmless
if sys.argv[2] == "crash":
    import faulthandler
    faulthandler._sigsegv()  # a real hard crash; CPython turns off Windows' crash dialog for it
"""


class HardCrashTest(unittest.TestCase):
    """M19: a hard crash's report (crash.log) lands in the next run's app.log. Each run is a
    child process: faulthandler is process-wide, and one test really crashes. Its USERPROFILE
    is a temporary folder holding the child's script, and BLURT_HOME a folder inside it."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.home = self.folder / "home"
        self.log_path = self.home / "logs" / "app.log"
        self.crash_path = self.home / "logs" / "crash.log"
        self.script = self.folder / "crash_child.py"
        self.script.write_text(CHILD, encoding="utf-8")

    def run_child(self, what):
        env = dict(os.environ, BLURT_HOME=str(self.home), USERPROFILE=str(self.folder))
        env.pop("PYTHONFAULTHANDLER", None)
        return subprocess.run([sys.executable, str(self.script), str(PROJECT), what], env=env,
                              capture_output=True, timeout=CHILD_TIMEOUT_SECONDS,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def app_log(self):
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""

    def test_a_hard_crash_is_in_the_next_app_log(self):
        crashed = self.run_child("crash")
        self.assertNotEqual(crashed.returncode, 0)
        self.assertGreater(self.crash_path.stat().st_size, 0)
        self.assertNotIn("hard crash", self.app_log())

        self.assertEqual(self.run_child("watch").returncode, 0)
        text = self.app_log()
        self.assertEqual(text.count("--- the last run ended in a hard crash. What Python saw: ---"), 1)
        self.assertIn("Fatal Python error: Segmentation fault", text)
        self.assertIn("%USERPROFILE%\\crash_child.py", text)  # the frame: file and line
        self.assertNotIn(str(self.folder).lower(), text.lower())
        self.assertEqual(text.count("Segmentation fault"), 1)  # copied once
        for line in text.splitlines():
            self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")  # every line went through print
        self.assertEqual(self.crash_path.stat().st_size, 0)  # this run's own, still empty

    def test_a_clean_quit_leaves_no_crash_log(self):
        self.assertEqual(self.run_child("clean").returncode, 0)
        self.assertFalse(self.crash_path.exists())
        self.assertIn("started", self.app_log())

    def test_an_empty_crash_log_is_not_reported(self):
        # Ended in Task Manager: nothing crashed, so crash.log is still empty.
        self.crash_path.parent.mkdir(parents=True)
        self.crash_path.write_bytes(b"")
        self.assertEqual(self.run_child("watch").returncode, 0)
        self.assertNotIn("hard crash", self.app_log())

    def test_a_second_copy_never_touches_crash_log(self):
        # log.start() alone is what a second copy runs before it quits.
        self.crash_path.parent.mkdir(parents=True)
        self.crash_path.write_bytes(b"the running copy's report\n")
        self.assertEqual(self.run_child("start").returncode, 0)
        self.assertEqual(self.crash_path.read_bytes(), b"the running copy's report\n")
        self.assertNotIn("hard crash", self.app_log())

    def test_no_crash_watch_on_a_console(self):
        self.assertEqual(self.run_child("console").returncode, 0)
        self.assertFalse(self.crash_path.exists())
        self.assertFalse(self.log_path.exists())

    def test_only_the_end_of_a_big_crash_log_is_copied(self):
        self.crash_path.parent.mkdir(parents=True)
        lines = [f"line {i:04d} " + "x" * 40 for i in range(3000)]  # about 150 KB
        self.crash_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.run_child("watch").returncode, 0)
        copied = [line for line in self.app_log().splitlines() if " line " in line]
        self.assertTrue(copied[-1].endswith(lines[-1]))
        self.assertLessEqual(len(copied) * (len(lines[0]) + 1), log.CRASH_REPORT_BYTES)
        for line in copied:
            self.assertRegex(line, r" line \d{4} x{40}$")  # no line cut in half

    def test_main_watches_only_with_the_lock_and_stops_just_before_bye(self):
        source = (PROJECT / "main.py").read_text(encoding="utf-8")
        claim = source.index("if not claim_single_instance():")
        watch = source.index("log.watch_for_crashes()")
        self.assertEqual(source.count("log.watch_for_crashes()"), 1)
        self.assertGreater(watch, source.index("return", claim))  # a second copy has returned by then
        stop = source.index("log.stop_watching_for_crashes()")
        self.assertEqual(source[stop:source.index('print("Bye.")')].count("\n"), 1)
        self.assertNotIn("crash", inspect.getsource(log.start))

    def test_stop_without_watching_does_nothing(self):
        with mock.patch.object(log, "_crash_file", None), \
                mock.patch.object(log.faulthandler, "disable") as disable:
            log.stop_watching_for_crashes()
        disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
