"""Pastes text at the cursor: save clipboard, set text, wait for the keys, last check,
Ctrl+V, restore clipboard. Or, with copy_only(), just leaves the text on the clipboard.

With confirm on (the "paste_confirm" setting), the Ctrl+V goes out with the text only
promised to Windows ("delayed rendering"): Windows asks Blurt for it when an app really
pastes, and that request is the confirmation (see _ConfirmedPaste).

Uses the Windows clipboard functions directly through ctypes.
"""

import ctypes
import time
from ctypes import wintypes

from app.focus import foreground_root
from app.hotkey import KEYEVENTF_KEYUP, send_keys

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# Clipboard managers (Ditto, Win+V) open the clipboard to read every change, so
# right after a change it can be busy for a moment. Keep trying this long (0.5s).
OPEN_ATTEMPTS = 50
OPEN_RETRY_SECONDS = 0.01
HWND_MESSAGE = wintypes.HWND(-3)
# A clipboard bigger than this isn't copied into memory to be put back after a paste; the
# dictation replaces it. A 4K screenshot is about 33 MB, so normal copies are always restored.
MAX_SAVE_BYTES = 256 * 1024 * 1024
VK_LCONTROL = 0xA2
VK_V = 0x56

# Formats whose data is a Windows drawing handle, not plain memory. We can't copy
# these, and Windows rebuilds the common ones (like CF_BITMAP from CF_DIB) anyway.
GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}


def _is_copyable(fmt):
    # 0x200-0x3FF are private and GDI ranges: skip them too.
    return fmt not in GDI_FORMATS and not (0x200 <= fmt <= 0x3FF)


user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL

# For the confirmed paste: Blurt's own clipboard window and its messages.
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.MsgWaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD,
]
user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
user32.GetClipboardOwner.argtypes = []
user32.GetClipboardOwner.restype = wintypes.HWND
user32.GetOpenClipboardWindow.argtypes = []
user32.GetOpenClipboardWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WM_RENDERFORMAT = 0x0305  # an app pastes: hand the promised text over now
WM_RENDERALLFORMATS = 0x0306  # our window is closing: hand over everything still owed
WM_DESTROYCLIPBOARD = 0x0307  # someone emptied the clipboard: it isn't ours anymore
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
CONFIRM_SECONDS = 1.5  # nobody asked for the text this long after Ctrl+V: the app didn't take it
NO_WINDOW_SECONDS = 0.5  # a request without a window counts only this soon after Ctrl+V
WINDOW_CLASS = "BlurtClipboardOwner"

# Markers that keep pasted text out of clipboard history. Each is a format with a
# 4-byte 0 as its data. The first two are for Windows (Win+V history, cloud clipboard).
# The last two are for clipboard managers: Ditto skips a change carrying either one.
CF_NO_HISTORY = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
CF_NO_CLOUD = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")
CF_NO_MONITOR = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
CF_VIEWER_IGNORE = user32.RegisterClipboardFormatW("Clipboard Viewer Ignore")
PRIVATE_MARKERS = (CF_NO_HISTORY, CF_NO_CLOUD, CF_NO_MONITOR, CF_VIEWER_IGNORE)


# What password managers and browsers put next to a copied password. The Dictionary
# panel never shows or adds clipboard text carrying one of them (see is_hidden).
SENSITIVE_MARKERS = {
    "ExcludeClipboardContentFromMonitorProcessing": CF_NO_MONITOR,
    "CanIncludeInClipboardHistory": CF_NO_HISTORY,
    "CanUploadToCloudClipboard": CF_NO_CLOUD,
}


class _Clipboard:
    """Opens the clipboard, retrying if another app has it open. Use with "with".

    It uses a hidden window that is destroyed afterwards. Windows wants a window as
    the clipboard owner, and destroying it means nobody waits on us for messages.
    attempts=1 tries once (for the GUI thread, which must never wait).
    """

    def __init__(self, attempts=OPEN_ATTEMPTS):
        self.attempts = attempts

    def __enter__(self):
        self.hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
        for attempt in range(self.attempts):
            if user32.OpenClipboard(self.hwnd):
                return self
            if attempt < self.attempts - 1:  # no wait after the last try (attempts=1 never waits)
                time.sleep(OPEN_RETRY_SECONDS)
        user32.DestroyWindow(self.hwnd)
        raise OSError("clipboard is busy (another app has it open)")

    def __exit__(self, *exc):
        user32.CloseClipboard()
        user32.DestroyWindow(self.hwnd)


def _read_bytes(handle):
    size = kernel32.GlobalSize(handle)
    if not size:
        return None
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _set_bytes(fmt, data):
    """Puts one format on the (open) clipboard. Returns True if Windows took it."""
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)  # only free it if Windows didn't take it
        return False
    return True  # Windows owns the memory now, so it stays after our process exits


def _collect(formats, size_of, read, limit=MAX_SAVE_BYTES):
    """(format, bytes) for each format read(fmt) can give. Raises OSError, before reading
    more, once the total would pass limit. size_of(fmt) is the size without reading it."""
    saved, total = [], 0
    for fmt in formats:
        total += size_of(fmt)
        if total > limit:
            raise OSError(f"it's too big to put back afterwards (over {limit // (1024 * 1024)} MB)")
        data = read(fmt)
        if data is not None:
            saved.append((fmt, data))
    return saved


def _save_clipboard():
    """Returns a list of (format, bytes) for everything we can copy.

    Raises OSError if the clipboard is busy or bigger than MAX_SAVE_BYTES.
    """
    def formats():
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if _is_copyable(fmt):
                yield fmt
            fmt = user32.EnumClipboardFormats(fmt)

    def size_of(fmt):
        handle = user32.GetClipboardData(fmt)
        return kernel32.GlobalSize(handle) if handle else 0

    def read(fmt):
        handle = user32.GetClipboardData(fmt)
        return _read_bytes(handle) if handle else None

    with _Clipboard():
        return _collect(formats(), size_of, read)


def _windows_text(text):
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _mark_private(already=()):
    """Adds the markers that keep this clipboard content out of every history (clipboard open)."""
    for fmt in PRIVATE_MARKERS:
        if fmt not in already:
            _set_bytes(fmt, (0).to_bytes(4, "little"))


def _set_text(text, allow_history=False):
    """Puts text on the clipboard. Returns the clipboard's change counter afterwards.

    Raises OSError if the clipboard is busy or Windows refused the text.
    allow_history=False keeps it out of Win+V history and clipboard managers like Ditto.
    allow_history=True lets it into those, but never into the cloud clipboard (synced to
    your other devices through Microsoft).
    """
    with _Clipboard():
        user32.EmptyClipboard()
        if not _set_bytes(CF_UNICODETEXT, (_windows_text(text) + "\0").encode("utf-16-le")):
            raise OSError("Windows didn't accept the text")
        if allow_history:
            _set_bytes(CF_NO_CLOUD, (0).to_bytes(4, "little"))
        else:
            _mark_private()
    return user32.GetClipboardSequenceNumber()


def read_text():
    """The text on the clipboard right now, or None (no text, or the clipboard is busy)."""
    try:
        with _Clipboard():
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            data = _read_bytes(handle) if handle else None
    except OSError:
        return None
    if data is None:
        return None
    return data.decode("utf-16-le", errors="replace").split("\0", 1)[0]


def is_hidden(markers):
    """True if the clipboard content is marked as private (a copied password, for example).

    markers is {marker name: its data as bytes} for the SENSITIVE_MARKERS that are on
    the clipboard. Hidden when ExcludeClipboardContentFromMonitorProcessing is there at
    all, or CanIncludeInClipboardHistory or CanUploadToCloudClipboard says 0 (not
    allowed). Anything unreadable counts as 0, so a doubt always hides the text.
    Our own pasted dictations (and the clipboard restored after a paste) carry these
    markers too, so they count as hidden. That's fine: copy the word again.
    """
    if "ExcludeClipboardContentFromMonitorProcessing" in markers:
        return True
    for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
        if name in markers:
            data = markers[name] or b""
            if len(data) < 4 or int.from_bytes(data[:4], "little") == 0:
                return True
    return False


def clipboard_sequence():
    """Windows' clipboard change counter. Goes up whenever anything is copied. Very cheap."""
    return user32.GetClipboardSequenceNumber()


def read_text_for_dictionary():
    """What the Dictionary panel may offer to add: ("text", text), ("hidden", None), ("none", None)
    or ("busy", None).

    Tries to open the clipboard only once, so the GUI thread never waits; a busy
    clipboard gives ("busy", None) and the panel tries again on its next check.
    """
    try:
        with _Clipboard(attempts=1):
            markers = {}
            for name, fmt in SENSITIVE_MARKERS.items():
                if user32.IsClipboardFormatAvailable(fmt):
                    handle = user32.GetClipboardData(fmt)
                    markers[name] = _read_bytes(handle) if handle else None
            if is_hidden(markers):
                return "hidden", None
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            data = _read_bytes(handle) if handle else None
    except OSError:
        return "busy", None
    if data is None:
        return "none", None
    return "text", data.decode("utf-16-le", errors="replace").split("\0", 1)[0]


def copy_only(text):
    """Leaves text on the clipboard for you to paste yourself. Returns True only if it's really there.

    It's allowed into Win+V history (not the cloud clipboard), so it survives if you copy
    something else first.
    The text is read back afterwards; if it isn't there, it's set once more.
    """
    for attempt in range(2):
        try:
            _set_text(text, allow_history=True)
        except OSError as e:
            print(f"copy: couldn't use the clipboard ({e})")
            continue
        if read_text() == _windows_text(text):
            return True
        print("copy: the clipboard didn't hold the text" + (", trying once more" if attempt == 0 else ""))
    return False


def _put_back(saved):
    """Replaces the clipboard's content with the saved formats (the clipboard must be open)."""
    user32.EmptyClipboard()
    formats = {fmt for fmt, _ in saved}
    for fmt, data in saved:
        _set_bytes(fmt, data)
    if saved:
        # It's already in your history from when you copied it; don't add it twice.
        _mark_private(formats)


def _restore_clipboard(saved):
    with _Clipboard():
        _put_back(saved)


def _wait_for_keys_released(hotkey, timeout):
    deadline = time.monotonic() + timeout
    while hotkey.modifiers_held():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


# ---- The confirmed paste (M17 item 12, "paste_confirm") ----

CTRL_V = [(VK_LCONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_LCONTROL, KEYEVENTF_KEYUP)]
KEY_UPS = [(VK_V, KEYEVENTF_KEYUP), (VK_LCONTROL, KEYEVENTF_KEYUP)]
PUMP_STEP_MS = 10  # longest single wait while answering Windows

# What a confirmed paste ended as (paste_outcome), plus the ways it can end before Ctrl+V.
CONFIRMED = "confirmed"  # the app you dictated into asked for the text
CONFIRMED_NO_WINDOW = "confirmed without a window"  # see paste_outcome
NOT_CONFIRMED = "not confirmed"  # another program (or one Windows can't name) asked first
NOT_TAKEN = "not taken"  # nobody asked within CONFIRM_SECONDS
REPLACED = "replaced"  # you copied something else before anyone asked
BUSY = "busy"  # the clipboard couldn't be opened for the promise
UNPROMISED = "unpromised"  # Windows didn't list the promised text
REFUSED = "refused"  # Windows refused the key presses (see send_keys)

COPIED_ELSEWHERE = "Couldn't paste (you copied something else at the same moment). Press Copy."


def paste_outcome(request, replaced_at, sent_at, target_pid):
    """The outcome table of a confirmed paste: what happened after Blurt's Ctrl+V. Times in seconds.

    request: the first time Windows asked Blurt for the text, as (time, pid of the window
    that had the clipboard open then, or 0 when it had none, whether the window you
    dictated into was in front), or None. Only the first one counts: once the text is
    handed over, Windows gives it to later readers without asking.
    replaced_at: when someone else emptied the clipboard (you copied something), or None.
    sent_at: when Ctrl+V was sent. target_pid: the process of the window you dictated
    into, 0 if there's none (then nothing can be confirmed).

    - CONFIRMED: the asking window belongs to the target's process. By process, not by
      window: Notepad reads the clipboard through another window than the one in front.
    - CONFIRMED_NO_WINDOW: the asker had no window (Windows Terminal reads it that way),
      the target was still in front, and it asked within NO_WINDOW_SECONDS of Ctrl+V.
    - NOT_CONFIRMED: any other request (a window of another process, a request without a
      window that's too late or with the target not in front, no target).
    - NOT_TAKEN: nobody asked within CONFIRM_SECONDS (an admin window, P1).
    - REPLACED: your copy replaced Blurt's text before anyone asked, or after someone
      else did: Ctrl+V probably pasted your copy, so it isn't counted as pasted.
    """
    if request is None or request[0] - sent_at > CONFIRM_SECONDS:
        return REPLACED if replaced_at is not None else NOT_TAKEN
    when, pid, in_front = request
    if replaced_at is not None and replaced_at < when:
        return REPLACED
    if target_pid:
        if pid == target_pid:
            return CONFIRMED
        if pid == 0 and in_front and when - sent_at <= NO_WINDOW_SECONDS:
            return CONFIRMED_NO_WINDOW
    return REPLACED if replaced_at is not None else NOT_CONFIRMED


def _window_pid(hwnd):
    """The process id a window belongs to, 0 for no window."""
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_owners = {}  # Blurt's clipboard window -> its _ConfirmedPaste, while the window exists


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    owner = _owners.get(hwnd)
    if owner is not None and message in (WM_RENDERFORMAT, WM_RENDERALLFORMATS, WM_DESTROYCLIPBOARD):
        owner.on_message(message, wparam)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


class _Windows:
    """The Windows calls a confirmed paste makes. The tests replace it (_WINDOWS) with a fake."""

    def __init__(self):
        self._registered = False

    def create_window(self, owner):
        """A hidden message-only window that owns the promised text. Returns it, or None."""
        instance = kernel32.GetModuleHandleW(None)
        if not self._registered:
            window_class = WNDCLASSW(lpfnWndProc=_window_proc, hInstance=instance, lpszClassName=WINDOW_CLASS)
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                return None
            self._registered = True
        hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, instance, None)
        if hwnd:
            _owners[hwnd] = owner
        return hwnd

    def destroy_window(self, hwnd):
        user32.DestroyWindow(hwnd)  # Windows first sends WM_RENDERALLFORMATS if it was the owner
        _owners.pop(hwnd, None)

    def open_clipboard(self, hwnd):
        return bool(user32.OpenClipboard(hwnd))

    def close_clipboard(self):
        user32.CloseClipboard()

    def empty_clipboard(self):
        user32.EmptyClipboard()

    def promise_text(self):
        """Puts the text on the (open) clipboard as a promise. True if Windows lists it now."""
        # With no data SetClipboardData returns NULL even when it worked (probe run 1), so ask instead.
        user32.SetClipboardData(CF_UNICODETEXT, None)
        return bool(user32.IsClipboardFormatAvailable(CF_UNICODETEXT))

    def give_text(self, data):
        return _set_bytes(CF_UNICODETEXT, data)

    def mark_private(self):
        _mark_private()

    def put_back(self, saved):
        _put_back(saved)

    def sequence(self):
        return user32.GetClipboardSequenceNumber()

    def owner(self):
        return user32.GetClipboardOwner() or 0

    def requester_pid(self):
        """The process asking for the text: the one whose window has the clipboard open. 0 for none."""
        return _window_pid(user32.GetOpenClipboardWindow())

    def window_pid(self, hwnd):
        return _window_pid(hwnd)

    def foreground_root(self):
        return foreground_root()

    def send_keys(self, keys):
        return send_keys(keys)

    def now(self):
        return time.monotonic()

    def pump(self, timeout_ms):
        """Waits up to timeout_ms, answering every message that comes (Windows' requests too)."""
        user32.MsgWaitForMultipleObjects(0, None, False, max(0, int(timeout_ms)), QS_ALLINPUT)
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


_WINDOWS = _Windows()


class _ConfirmedPaste:
    """Blurt's clipboard window for one confirmed paste: steps 4 and 5 of paste().

    The no-blocking rule: while this window owns the promised text, the pipeline thread
    never waits without answering Windows: no time.sleep, no UI Automation, no clipboard
    retries with plain sleeps. Every wait is api.pump(), which answers requests as they
    come, and answering only copies the text into Windows memory. paste() always closes
    the window afterwards; Windows then asks for anything still owed (WM_RENDERALLFORMATS),
    which is answered too, so no app waits on Blurt and the clipboard is never left empty.
    """

    def __init__(self, api, text, target):
        self.api = api
        self.data = (_windows_text(text) + "\0").encode("utf-16-le")
        self.target = target or 0
        self.target_pid = api.window_pid(target) if target else 0
        self.hwnd = None
        self.owed = False  # the text is promised and not handed over yet
        self.request = None  # the first request: (time, asker's pid or 0, target in front)
        self.replaced_at = None  # when someone else emptied the clipboard
        self.sent_at = None  # when Ctrl+V went out
        self.sent = 0  # how many of the 4 key presses Windows took
        self.sequence = None  # the clipboard's change counter after the promise
        self.restored = None  # True: your clipboard is back; False: kept your new copy; "busy"
        self.error = None  # an error while answering Windows (answered anyway)
        self._restoring = False  # our own EmptyClipboard when putting yours back isn't "someone else"

    def on_message(self, message, wparam):
        """Windows' clipboard messages to our window. Never raises: an app may be waiting."""
        try:
            if message == WM_RENDERFORMAT:
                # The asker has the clipboard open right now, so its window says who it is.
                when = self.api.now()
                pid = self.api.requester_pid()
                in_front = bool(self.target) and self.api.foreground_root() == self.target
                if wparam == CF_UNICODETEXT and self.owed:
                    self.owed = not self.api.give_text(self.data)
                    # Only a request that got the text counts (a failed hand-over gave the app nothing).
                    if not self.owed and self.request is None:
                        self.request = (when, pid, in_front)
            elif message == WM_RENDERALLFORMATS:
                # Only what's still owed: handing it over again counts as a new copy (probe run 4).
                if self.owed and self._open():
                    try:
                        if self.api.owner() == self.hwnd:
                            self.owed = not self.api.give_text(self.data)
                    finally:
                        self.api.close_clipboard()
            elif message == WM_DESTROYCLIPBOARD:
                if not self._restoring and self.replaced_at is None:
                    self.replaced_at = self.api.now()
        except Exception as e:
            self.error = e

    def _open(self):
        """Opens the clipboard for our window, retrying for up to 0.5s while answering Windows."""
        for attempt in range(OPEN_ATTEMPTS):
            if self.api.open_clipboard(self.hwnd):
                return True
            if attempt < OPEN_ATTEMPTS - 1:
                self.api.pump(OPEN_RETRY_SECONDS * 1000)
        return False

    def _pump_until(self, deadline, done=lambda: False):
        while not done():
            left = deadline - self.api.now()
            if left <= 0:
                return
            self.api.pump(min(left * 1000, PUMP_STEP_MS))

    def run(self, early_sequence, saved, restore_ms):
        """Steps 4 and 5. Returns BUSY, UNPROMISED, REPLACED, REFUSED or a paste_outcome()."""
        # Step 4: promise the text, unless you copied something after the early set.
        if not self._open():
            return BUSY
        try:
            # Nobody can change the clipboard while it's open, so this compare can't be raced.
            if self.api.sequence() != early_sequence:
                return REPLACED  # closed without emptying it: your copy stays, no Ctrl+V
            self.api.empty_clipboard()
            self.owed = self.api.promise_text()
            if not self.owed:
                self.api.give_text(self.data)  # never leave the clipboard empty
            self.api.mark_private()
        finally:
            self.api.close_clipboard()
        # Read after closing: while it's open the number isn't final yet (probe runs 4 and 5).
        self.sequence = self.api.sequence()
        if not self.owed:
            return UNPROMISED
        if self.api.owner() != self.hwnd or self.replaced_at is not None:
            return REPLACED  # you copied something in the moment between closing and reading

        self.sent = self.api.send_keys(CTRL_V)
        self.sent_at = self.api.now()
        if self.sent < len(CTRL_V):
            if self.sent:
                # Not expected, but a half-sent Ctrl+V must not leave Ctrl or V held down.
                self.api.send_keys(KEY_UPS)
            return REFUSED

        # Step 5: answer Windows until the text is asked for, you copy something, or time is up.
        self._pump_until(self.sent_at + CONFIRM_SECONDS,
                         lambda: self.request is not None or self.replaced_at is not None)
        outcome = paste_outcome(self.request, self.replaced_at, self.sent_at, self.target_pid)
        if outcome in (CONFIRMED, CONFIRMED_NO_WINDOW) and saved is not None:
            self.restored = self._restore(saved, restore_ms)
        return outcome

    def _restore(self, saved, restore_ms):
        """Puts your clipboard back: the app has the text, and at least restore_ms after Ctrl+V.
        Not if you copied something else meanwhile: then the number changed or the clipboard
        isn't ours anymore (handing the text over leaves both as they were, probe runs 4 and 5)."""
        self._pump_until(self.sent_at + restore_ms / 1000)
        if not self._open():
            return "busy"
        try:
            if self.api.sequence() != self.sequence or self.api.owner() != self.hwnd or self.replaced_at is not None:
                return False
            self._restoring = True
            self.api.put_back(saved)
            self.owed = False
            return True
        finally:
            self._restoring = False
            self.api.close_clipboard()


def _paste_confirmed(text, saved, sequence, target, restore_ms, info):
    """Steps 4 and 5 through Blurt's clipboard window. Returns (result, None), or (None,
    sequence) when the confirmation couldn't be set up and a plain Ctrl+V goes out instead."""
    api = _WINDOWS
    run = _ConfirmedPaste(api, text, target)
    run.hwnd = api.create_window(run)
    if not run.hwnd:
        print("paste: couldn't make Blurt's clipboard window, so this paste isn't confirmed")
        return None, sequence
    try:
        outcome = run.run(sequence, saved, restore_ms)
    finally:
        api.destroy_window(run.hwnd)  # anything still owed is handed over first
    # The window is closed and nothing is owed: plain waits (copy_only) are fine again.
    if run.error is not None:
        print(f"paste: a clipboard request went wrong ({type(run.error).__name__}); it was answered on closing")

    if outcome == BUSY:
        print("paste: the clipboard was busy, so this paste isn't confirmed")
        return None, sequence
    if outcome == UNPROMISED:
        print("paste: Windows didn't list the promised text, so this paste isn't confirmed")
        return None, run.sequence
    if outcome == REPLACED:
        print("paste: you copied something else before the app took the text, so it wasn't pasted. "
              "Your copy stays on the clipboard; the dictation is in History.")
        info["error"] = COPIED_ELSEWHERE
        return "error", None
    if outcome == REFUSED:
        print(f"paste: Windows took {run.sent} of the {len(CTRL_V)} key presses, so it didn't paste")
        return ("copied" if copy_only(text) else "error"), None
    if outcome in (CONFIRMED, CONFIRMED_NO_WINDOW):
        print("paste: pasted (confirmed)" if outcome == CONFIRMED
              else "paste: pasted (confirmed, the app read it without a window)")
        if run.restored == "busy":
            print("paste: couldn't restore the clipboard (it was busy)")
        return "pasted", None
    if outcome == NOT_TAKEN:
        print("paste: the app didn't take the text, so it's on your clipboard")
        info["reason"] = "the app didn't take the text"
    else:
        print("paste: not confirmed (another program read the text first, or Windows couldn't "
              "tell which), so it's on your clipboard")
        info["reason"] = "not confirmed"
    return ("copied" if copy_only(text) else "error"), None


def paste(text, hotkey, restore_ms=300, key_timeout=10.0, ready=None, target=None, confirm=False, info=None):
    """Pastes text into whatever app has focus.

    ready() is the last check, run after the keys are released and right before Ctrl+V
    (the pipeline's: still the same window, still a text field). If it returns False,
    nothing is pasted and the text is left on the clipboard as a normal copy (in Win+V
    history, like copy_only), instead of the clipboard you had before.

    confirm (the "paste_confirm" setting): Ctrl+V goes out with the text only promised,
    and "pasted" then means the app asked for it (see paste_outcome). target is the
    window you dictated into (its process must be the one asking). info, a dict, gets
    "reason" (why "copied") and "error" (History's message for "error") when there's one.

    Returns:
    - "pasted": confirm off: Windows took all 4 key presses of Ctrl+V, whether the app
      really pasted is unknown (an admin window drops them without telling). Confirm on:
      the app asked for the text.
    - "copied": not pasted, the text is on the clipboard as a normal copy (ready() said
      no, Windows refused the key presses, or confirm on: the paste wasn't confirmed).
    - "held": not pasted, the keys were still held after key_timeout. The text is on the
      clipboard, kept out of Win+V history.
    - "error": not pasted, and the text couldn't be put on the clipboard, or (confirm on)
      you copied something else before the app took it: your copy stays.
    """
    info = {} if info is None else info
    try:
        saved = _save_clipboard()
    except OSError as e:
        print(f"paste: couldn't read the clipboard ({e}); it won't be restored")
        saved = None

    # Set the text first, so it's on the clipboard even if we never paste.
    try:
        sequence = _set_text(text)
    except OSError as e:
        print(f"paste: couldn't put the text on the clipboard ({e})")
        return "error"

    # Otherwise our Ctrl+V becomes Win+Ctrl+V or Shift+Ctrl+V.
    if not _wait_for_keys_released(hotkey, key_timeout):
        print("paste: keys still held after 10s, so I didn't paste. The text is on your clipboard.")
        return "held"

    if ready is not None and not ready():
        return "copied" if copy_only(text) else "error"

    if confirm:
        result, sequence = _paste_confirmed(text, saved, sequence, target, restore_ms, info)
        if result is not None:
            return result

    keys = CTRL_V
    sent = send_keys(keys)
    if sent < len(keys):
        # Windows refused the key presses (see send_keys). This never catches an admin window.
        print(f"paste: Windows took {sent} of the {len(keys)} key presses, so it didn't paste")
        if sent:
            # Not expected, but a half-sent Ctrl+V must not leave Ctrl or V held down.
            # If Ctrl and V both went down, the app may have pasted anyway.
            send_keys(KEY_UPS)
        return "copied" if copy_only(text) else "error"

    if saved is not None:
        time.sleep(restore_ms / 1000)  # give the app time to read the clipboard
        # If you copied something new in the meantime, keep that instead.
        if clipboard_sequence() == sequence:
            try:
                _restore_clipboard(saved)
            except OSError as e:
                print(f"paste: couldn't restore the clipboard ({e})")
    return "pasted"
