"""Smart paste: is the thing with keyboard focus a text field?

Asks UI Automation (the accessibility layer screen readers use) about the
focused element. Only a clear "yes, editable" counts: anything unsure means
"don't paste", because the text then just waits on the clipboard.

Checked in order, first match decides:
1. The app is in paste_never_apps: no.
2. The app is in paste_always_apps: yes.
3. UI Automation: read-only: no. Edit, or supports text editing, or an
   editable Document: yes.
4. Anything else, an error or a timeout: no.

COM note: comtypes sets up COM for whichever thread imports it first. Qt owns
COM on the main thread, so comtypes is only ever imported here, lazily, from a
background thread (the pipeline or the warm-up thread), in multithreaded mode.
"""

import ctypes
import sys
import threading
import time
from ctypes import wintypes

from app.config import load_config

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
ole32.CoIncrementMTAUsage.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
ole32.CoIncrementMTAUsage.restype = ctypes.c_long

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOT = 2
COINIT_MULTITHREADED = 0

UIA_TIMEOUT_MS = 300  # a frozen app must never hold up the paste for long
CHROME_RETRY_SECONDS = 0.15

# UI Automation numbers (from the Windows SDK, UIAutomationClient.h)
UIA_EDIT = 50004
UIA_DOCUMENT = 50030
UIA_IsValuePatternAvailablePropertyId = 30043
UIA_ValueIsReadOnlyPropertyId = 30046
UIA_IsTextEditPatternAvailablePropertyId = 30149
CONTROL_NAMES = {
    50000: "Button", 50004: "Edit", 50020: "Text", 50025: "Custom", 50026: "Group",
    50030: "Document", 50032: "Window", 50033: "Pane",
}

_setup_lock = threading.Lock()
_automation = None
_comtypes = None


def foreground_exe():
    """The exe name of the app in front, like "opera.exe". "" if unknown."""
    return window_exe(user32.GetForegroundWindow())


def foreground_root():
    """The top-level window of the app in front, as a number (0 if none). Safe on any thread (no COM).

    GetAncestor(GA_ROOT) climbs from the active window to its outermost window, so an
    app that swaps its inner windows (tabs, panes) still gives the same answer.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def window_exe(hwnd):
    """The exe name of the app a window belongs to. "" if unknown. Safe on any thread (no COM)."""
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""  # e.g. an app running as administrator
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def _get_automation():
    """Creates the UI Automation object once. Never call this on the main (Qt) thread."""
    global _automation, _comtypes
    with _setup_lock:
        if _automation is None:
            sys.coinit_flags = COINIT_MULTITHREADED  # read by comtypes when it's imported
            import comtypes
            import comtypes.client

            # Keep multithreaded COM alive for the whole run, so any background
            # thread can use the automation object without its own setup.
            cookie = ctypes.c_void_p()
            ole32.CoIncrementMTAUsage(ctypes.byref(cookie))

            if not getattr(sys, "frozen", False):
                # Writes the UI Automation wrapper into comtypes/gen the first time. The
                # installed app has it built in (tools/build.py makes it before bundling).
                comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient

            automation = comtypes.client.CreateObject(
                UIAutomationClient.CUIAutomation8, interface=UIAutomationClient.IUIAutomation2
            )
            automation.ConnectionTimeout = UIA_TIMEOUT_MS
            automation.TransactionTimeout = UIA_TIMEOUT_MS
            _comtypes = comtypes
            _automation = automation
    return _automation


def _prop(element, prop_id):
    try:
        return element.GetCurrentPropertyValue(prop_id)
    except _comtypes.COMError:
        return None


def _ask_uia():
    """(is a text field, description, framework) for the focused element."""
    element = _get_automation().GetFocusedElement()
    kind = element.CurrentControlType
    framework = element.CurrentFrameworkId or ""
    has_value = bool(_prop(element, UIA_IsValuePatternAvailablePropertyId))
    text_edit = bool(_prop(element, UIA_IsTextEditPatternAvailablePropertyId))
    read_only = has_value and bool(_prop(element, UIA_ValueIsReadOnlyPropertyId))

    supports = [name for name, ok in (("TextEdit", text_edit), ("Value", has_value)) if ok]
    description = (
        f"{CONTROL_NAMES.get(kind, kind)} (fw={framework or '-'}, "
        f"supports={'+'.join(supports) or 'none'}{', read-only' if read_only else ''})"
    )
    if read_only:
        return False, description, framework
    if kind == UIA_EDIT or text_edit or (kind == UIA_DOCUMENT and has_value):
        return True, description, framework
    return False, description, framework


def find_text_field(config):
    """(paste or not, reason). Runs on the pipeline thread."""
    exe = foreground_exe()
    name = exe.lower()
    if name in {app.lower() for app in config["paste_never_apps"]}:
        return False, f"{exe} is in paste_never_apps"
    if name in {app.lower() for app in config["paste_always_apps"]}:
        return True, f"{exe} is in paste_always_apps"

    try:
        is_field, description, framework = _ask_uia()
        if not is_field and framework == "Chrome":
            # Chromium apps build their accessibility info on demand; ask once more.
            time.sleep(CHROME_RETRY_SECONDS)
            is_field, description, framework = _ask_uia()
    except Exception as e:  # timeouts, apps closing mid-question, admin windows...
        return False, f"{exe or 'unknown app'}: unsure ({e})"
    verdict = "text field" if is_field else "not a text field"
    return is_field, f"{exe or 'unknown app'}: {verdict}, {description}"


def warm_up():
    """Ask once when recording starts, so Chromium apps are ready by paste time.

    Runs on its own short-lived thread. Failures don't matter here.
    """
    try:
        if load_config()["paste_mode"] != "smart":
            return
        _get_automation().GetFocusedElement().CurrentControlType
    except Exception:
        pass
