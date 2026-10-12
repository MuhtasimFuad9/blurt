"""Where the app's printed lines go, and the filter that keeps API keys out of them.

With a console (running main.py from a terminal), print() works as always.
Without one (the installed app, or pythonw.exe from source) Python has nowhere
to print, so start() sends stdout and stderr to logs\\app.log in the user
folder: one timestamp per line, at most MAX_BYTES, then it becomes app.log.1
(the one before that is deleted). So the logs never take more than about 2 MB,
unless another program keeps a log file open so it can't be renamed: then
app.log keeps growing, and the rename is tried again every minute.
Crashes on any thread end up there too: Python's own crash reports are written
to stderr, which is now the log. A hard crash (Python itself dies, in Qt or a
Windows call) can't print anything: faulthandler writes it to logs\\crash.log,
and the next start copies it into app.log (watch_for_crashes).

Either way, every line passes through redact() first: anything that looks like
a Groq key (gsk_...) and the key the app actually uses (add_secret) are
replaced by "[key hidden]". So the key can't reach the console, the log file or
a printed error, even by mistake. Your user folder (C:\\Users\\<your name>, and its
short form like C:\\Users\\LONGNA~1) becomes %USERPROFILE%, so a path in an error
message doesn't put your Windows user name in the log (set_profile).

Dictation text is kept out of the log file by pipeline.shown(), which asks
hides_text() here, and so are error messages that could quote it
(describe_exception, print_exception, and api.describe_for_log for Groq's errors).
"""

import ctypes
import faulthandler
import os
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

from app import paths
from app.version import VERSION

MAX_BYTES = 1_000_000
ROTATE_RETRY_SECONDS = 60.0  # if app.log can't be renamed (another program has it open), try again after this
HIDDEN = "[key hidden]"
KEY_PATTERN = re.compile(r"gsk_[A-Za-z0-9_\-]{8,}")
MIN_SECRET_LENGTH = 8  # shorter "secrets" would hide normal words
PROFILE = "%USERPROFILE%"
# After the user folder comes the rest of a path, a quote, a space or the end of the line.
# Anything else means it's a longer name (C:\Users\Sam vs C:\Users\Samantha): left alone.
PATH_END = r"(?=[\\/'\"\s]|$)"
SHORT_PATH_CHARS = 1024
CRASH_REPORT_BYTES = 64 * 1024  # how much of crash.log the next start copies into app.log (its end)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetShortPathNameW.restype = wintypes.DWORD

_secrets = set()
_secrets_lock = threading.Lock()
_profile_patterns = []  # set_profile(): the user folder's forms, as regular expressions
_to_file = False
_crash_file = None  # crash.log while faulthandler writes to it (watch_for_crashes)


def add_secret(value):
    """Hide this exact text from every output from now on (the API key in use)."""
    if value and len(value) >= MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(value)


def short_path(path):
    """Windows' 8.3 short form of path (like C:\\Users\\LONGNA~1), or None if it has none
    (short names off on that drive, or the folder doesn't exist)."""
    buffer = ctypes.create_unicode_buffer(SHORT_PATH_CHARS)
    length = kernel32.GetShortPathNameW(path, buffer, SHORT_PATH_CHARS)
    if length == 0 or length >= SHORT_PATH_CHARS:
        return None
    return buffer.value


def set_profile(long_path, short=None):
    """From now on, show the user folder as %USERPROFILE% in every output. long_path is the
    folder (C:\\Users\\Sam), short its 8.3 form; short is ignored when it's the same folder name.
    Each form is matched in any case, with single or doubled backslashes (an OSError's message
    doubles them), and only where the folder name ends."""
    forms = []
    for form in (long_path, short):
        form = (form or "").rstrip("\\/")
        if form and form.lower() not in (f.lower() for f in forms):
            forms.append(form)
    patterns = []
    for form in sorted(forms, key=len, reverse=True):
        for variant in (form.replace("\\", "\\\\"), form):
            patterns.append(re.compile(re.escape(variant) + PATH_END, re.IGNORECASE))
    with _secrets_lock:
        _profile_patterns[:] = patterns


def find_profile(environ=None):
    """(user folder, its short form or None), for set_profile."""
    environ = os.environ if environ is None else environ
    long_path = environ.get("USERPROFILE") or str(Path.home())
    short = short_path(long_path)
    if short is not None and short.lower() == long_path.rstrip("\\/").lower():
        short = None
    return long_path, short


def redact(text):
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
        profile_patterns = list(_profile_patterns)
    for secret in secrets:
        text = text.replace(secret, HIDDEN)
    for pattern in profile_patterns:
        text = pattern.sub(PROFILE, text)
    return KEY_PATTERN.sub(HIDDEN, text)


def hides_text():
    """True when printed lines may be kept (a log file, or the installed app): then dictation
    text is shown only as a word count."""
    return _to_file or paths.INSTALLED


# Error messages written by Python or a library can quote a value: int() repeats its input,
# a UnicodeEncodeError names the character, pydantic shows input_value. On the dictation path
# that value could be dictated text or cleanup output. So wherever output is kept, an
# unexpected error is shown by its class and the code line it came from, never its message.

def describe_exception(error):
    """One line about an unexpected error: "ValueError: <message>" on a console from source,
    "ValueError at cleanup.py:120 (message hidden)" wherever output is kept."""
    name = type(error).__name__
    if not hides_text():
        return f"{name}: {error}"
    frames = traceback.extract_tb(error.__traceback__)
    where = f" at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else ""
    return f"{name}{where} (message hidden)"


def print_exception(error):
    """traceback.print_exc() for an error that was caught. Wherever output is kept, the stack
    (files, lines, code) is printed but the message isn't, and chained errors are left out."""
    if not hides_text():
        traceback.print_exception(error)
        return
    stack = "".join(traceback.format_tb(error.__traceback__))
    print(f"Traceback (most recent call last):\n{stack}{type(error).__name__} (message hidden)", file=sys.stderr)


# Errors nobody caught are printed by Python itself, message included: on the main thread
# (and in Qt's slots, which report through sys.excepthook), on other threads, and inside a
# callback Windows calls (the keyboard hook: "Exception ignored ..."). start() sends all
# three through print_exception instead. A hook must never raise, so each swallows its own errors.

def _excepthook(kind, error, tb):
    try:
        if error is None:
            print(f"{kind.__name__} (no details)", file=sys.stderr)
            return
        if error.__traceback__ is None and tb is not None:
            error = error.with_traceback(tb)
        print_exception(error)
    except Exception:
        pass


def _thread_excepthook(args):
    try:
        if args.exc_type is SystemExit:  # like Python's own: a thread ending with SystemExit is quiet
            return
        name = args.thread.name if args.thread is not None else "a thread"
        print(f"Exception in thread {name}:", file=sys.stderr)
        _excepthook(args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass


def _unraisablehook(unraisable):
    try:
        # Only Python's own description and the kind of object, never its repr (it could hold text).
        where = unraisable.err_msg or "Exception ignored in"
        print(f"{where}: {type(unraisable.object).__name__}", file=sys.stderr)
        _excepthook(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)
    except Exception:
        pass


def catch_uncaught_errors():
    """Uncaught errors, anywhere, are printed through print_exception from now on."""
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook


class _Lines:
    """A stream print() can write to. Collects whole lines, redacts them, hands them on."""

    def __init__(self):
        self._lock = threading.Lock()
        self._partial = ""

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            lines = (self._partial + text).split("\n")
            self._partial = lines.pop()
            for line in lines:
                self._emit(redact(line) + "\n")
        return len(text)

    def flush(self):
        with self._lock:
            if self._partial:
                self._emit(redact(self._partial))
                self._partial = ""
            self._flush()

    def isatty(self):
        return False

    def _emit(self, text):
        raise NotImplementedError

    def _flush(self):
        pass


class RedactingStream(_Lines):
    """The console, with keys hidden."""

    def __init__(self, stream):
        super().__init__()
        self._stream = stream
        self.encoding = getattr(stream, "encoding", "utf-8")

    def _emit(self, text):
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError, UnicodeError):
            pass  # a closed or odd console must never crash the app

    def _flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass


class LogFile(_Lines):
    """logs\\app.log: timestamped lines, size-capped. Never raises; if the file can't be
    written, the lines are just dropped."""

    encoding = "utf-8"

    def __init__(self, path, max_bytes=MAX_BYTES):
        super().__init__()
        self.path = path
        self.max_bytes = max_bytes
        self._file = None
        self._rotate_after = 0.0  # a failed rename is tried again from this time (monotonic)

    def _open(self):
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # errors="replace": a character UTF-8 can't hold becomes "?" instead of an error
            self._file = open(self.path, "a", encoding="utf-8", errors="replace", newline="\r\n")
        return self._file

    def _close_file(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                file.close()
            except (OSError, ValueError):
                pass

    def _rotate(self):
        """app.log becomes app.log.1. If another program holds either file open, the rename
        fails: keep writing to app.log and try again later, rather than drop every line.
        Returns True when it was renamed, so the next line starts a new app.log."""
        now = time.monotonic()
        if now < self._rotate_after:
            return False
        self._close_file()
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            self._rotate_after = now + ROTATE_RETRY_SECONDS
            return False
        return True

    def _emit(self, text):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S ")
        line = stamp + text if text.endswith("\n") else stamp + text + "\n"
        try:
            log = self._open()
            if log.tell() + len(line.encode("utf-8", "replace")) > self.max_bytes and log.tell() > 0:
                rotated = self._rotate()
                log = self._open()
                if rotated:
                    # So a new app.log, sent on its own, still says which version wrote it.
                    log.write(f"{stamp}--- {paths.APP_DISPLAY_NAME} {VERSION} (continued, older lines "
                              f"are in {self.path.name}.1) ---\n")
            log.write(line)
            log.flush()
        except (OSError, ValueError):  # ValueError: an odd character, or a closed file
            self._close_file()

    def close(self):
        with self._lock:
            self._close_file()


def windows_version():
    """Like "Windows 10.0.26200", for the start line. Windows 11 says 10.0 too; the build tells them apart."""
    version = sys.getwindowsversion()
    return f"Windows {version.major}.{version.minor}.{version.build}"


def start(log_path=None):
    """Call first thing in main(). No console: output goes to the log file. With one, the
    console gets the key filter. Returns True when writing to the log file."""
    global _to_file
    set_profile(*find_profile())
    catch_uncaught_errors()
    if sys.stdout is None or sys.stderr is None:
        log = LogFile(log_path or paths.LOG_PATH)
        sys.stdout = sys.stderr = log
        _to_file = True
        print(f"--- {paths.APP_DISPLAY_NAME} {VERSION} started ({windows_version()}) ---")
    else:
        if not isinstance(sys.stdout, _Lines):
            sys.stdout = RedactingStream(sys.stdout)
        if not isinstance(sys.stderr, _Lines):
            sys.stderr = RedactingStream(sys.stderr)
    return _to_file


# A hard crash (an access violation in Qt, comtypes or a Windows call) ends Python before it can
# print anything. faulthandler still writes where each thread was to crash.log: the kind of crash,
# thread ids, and file:line plus function names (no values, no messages). A clean quit deletes the
# file, so a crash.log with something in it means the last run never got to "Bye.". Windows can
# also report some errors that were handled (COM's RPC_E_* codes) this way; a clean quit drops those.

def _crash_report(path):
    """The end of crash.log (at most CRASH_REPORT_BYTES) as text, "" if it's missing."""
    try:
        with open(path, "rb") as file:
            size = file.seek(0, os.SEEK_END)
            file.seek(max(0, size - CRASH_REPORT_BYTES))
            text = file.read().decode("utf-8", "replace")
    except OSError:
        return ""
    if size > CRASH_REPORT_BYTES:
        text = text.partition("\n")[2]  # its first line was cut off
    return text


def watch_for_crashes():
    """Call once the single-instance lock is ours: a second copy must never read or empty the
    running copy's crash.log. Only when writing to the log file (on a console a crash shows
    there). A crash.log the last run left is copied into app.log first, through print, so every
    line is redacted and timestamped; then this run gets a new, empty one. Never raises."""
    global _crash_file
    if not _to_file or _crash_file is not None:
        return
    report = _crash_report(paths.CRASH_PATH)
    if report.strip():
        print("--- the last run ended in a hard crash. What Python saw: ---")
        for line in report.splitlines():
            print(line)
    file = None
    try:
        paths.CRASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        file = open(paths.CRASH_PATH, "w", encoding="utf-8")
        faulthandler.enable(file, all_threads=True)
    except Exception as e:
        print(f"crash watch is off: {describe_exception(e)}")
        if file is not None:
            file.close()
        return
    _crash_file = file  # kept open for the life of the process: faulthandler writes to it


def stop_watching_for_crashes():
    """Just before "Bye.": a clean quit, so this run's crash.log is deleted. Does nothing if
    watch_for_crashes didn't start watching. Never raises."""
    global _crash_file
    file, _crash_file = _crash_file, None
    if file is None:
        return
    for step in (faulthandler.disable, file.close, lambda: Path(file.name).unlink(missing_ok=True)):
        try:
            step()
        except Exception:
            pass
