"""Shows what Windows reports about the active window, to check the pill's fullscreen
detection (M18).

It doesn't change anything and never presses keys. Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\fullscreen_probe.py

Put apps in and out of fullscreen. A line is printed whenever what's reported
changes. Press Ctrl+C to stop, then copy the output into the chat.
Window titles are never read, only the window's class name.

Each line shows:
- class:    the Windows class of the active window
- zoomed:   Windows says it's maximized (IsZoomed)
- caption:  it has the title bar style (WS_CAPTION)
- rect:     the window's rectangle, mon: its monitor's (left, top, right, bottom, real pixels)
- covers:   the window covers its whole monitor
- exact:    the window is exactly its monitor
- primary:  that monitor is the primary one (where the pill sits)
- quns:     SHQueryUserNotificationState's answer (3 = exclusive fullscreen game, 4 = presentation mode)
- old:      what the app decides today (fullscreen_app_active in app/ui/win32.py)
- rule1:    covers the primary monitor, and is not maximized or has no title bar style
- rule2:    maximized with the title bar style, and exactly the primary monitor
- new:      the app's rule since M18: quns 3 or 4, or win32.looks_fullscreen (rule1; rule2 was dropped)
"""

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ui import win32  # noqa: E402

POLL_SECONDS = 0.5
GWL_STYLE = -16
WS_CAPTION = 0x00C00000  # the title bar style (WS_BORDER | WS_DLGFRAME)
# The real app runs per-monitor DPI aware (Qt does that), so Windows gives it real pixels.
# Without this, Windows would scale the numbers on a display that isn't at 100%.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

user32 = win32.user32
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL


def _rect(r):
    return (r.left, r.top, r.right, r.bottom)


def _quns():
    state = ctypes.c_int(0)
    if win32.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return None
    return state.value


def _yes(value):
    return "True" if value else "False"


def describe():
    """One line about the active window right now (no title, no app name)."""
    quns = _quns()
    quns_hit = quns in (win32.QUNS_RUNNING_D3D_FULL_SCREEN, win32.QUNS_PRESENTATION_MODE)
    old = win32.fullscreen_app_active(0)
    rule1 = rule2 = looks = False

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        details = "class=(no active window)"
    else:
        class_name = win32._class_name(hwnd)
        zoomed = bool(user32.IsZoomed(hwnd))
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        caption = (style & WS_CAPTION) == WS_CAPTION
        window = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(window))
        details = (f"class={class_name} zoomed={_yes(zoomed)} caption={_yes(caption)} "
                   f"rect={_rect(window)}")

        monitor = user32.MonitorFromWindow(hwnd, win32.MONITOR_DEFAULTTONULL)
        info = win32.MONITORINFO(cbSize=ctypes.sizeof(win32.MONITORINFO))
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            details += " mon=(none)"
        else:
            screen = info.rcMonitor
            primary = bool(info.dwFlags & win32.MONITORINFOF_PRIMARY)
            covers = (window.left <= screen.left and window.top <= screen.top
                      and window.right >= screen.right and window.bottom >= screen.bottom)
            exact = _rect(window) == _rect(screen)
            details += (f" mon={_rect(screen)} covers={_yes(covers)} exact={_yes(exact)} "
                        f"primary={_yes(primary)}")
            counts = primary and class_name not in win32.DESKTOP_CLASSES
            rule1 = counts and covers and (not zoomed or not caption)
            rule2 = counts and zoomed and caption and exact
            looks = class_name not in win32.DESKTOP_CLASSES and win32.looks_fullscreen(
                _rect(window), _rect(screen), primary, zoomed, style)

    new = quns_hit or looks
    return (f"{details} quns={quns} old={_yes(old)} rule1={_yes(rule1)} rule2={_yes(rule2)} "
            f"new={_yes(new)}")


def main():
    if not user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        print("(couldn't switch to per-monitor DPI awareness; at 100% scaling that changes nothing)")
    print("Watching the active window. Put apps in and out of fullscreen. Ctrl+C stops.\n")
    last = None
    try:
        while True:
            line = describe()
            if line != last:
                print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)
                last = line
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
