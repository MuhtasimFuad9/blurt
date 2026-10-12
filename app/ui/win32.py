"""Windows tricks the pill needs that Qt can't do on its own.

- Never become the active window (so pastes still go to your document).
- Let the mouse pass straight through the pill when it's idle.
- Stay on top without activating.
- Notice when a fullscreen app (video, game) has focus.
- Give focus back to your window after the tray menu closes.
"""

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000  # the title bar style (WS_BORDER | WS_DLGFRAME)
WS_EX_TRANSPARENT = 0x00000020  # mouse clicks pass through to the window below
WS_EX_TOOLWINDOW = 0x00000080  # no taskbar button, not in Alt+Tab
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000  # clicking never makes it the active window

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3

MONITOR_DEFAULTTONULL = 0
MONITORINFOF_PRIMARY = 1

# SHQueryUserNotificationState answers
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # exclusive fullscreen game
QUNS_PRESENTATION_MODE = 4

# Windows that cover the whole screen but aren't "fullscreen apps"
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# The taskbar and the tray's overflow (^) flyout. Clicking the tray icon makes one of
# these the active window, so they never count as "the window you were working in".
TASKBAR_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Xaml_WindowedPopupClass",
}

LONG_PTR = ctypes.c_ssize_t


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
shell32.SHQueryUserNotificationState.restype = ctypes.c_long
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04


def _set_ex_style(hwnd, add=0, remove=0):
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    new_style = (style | add) & ~remove
    if new_style != style:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)


def make_no_activate(hwnd):
    """The window can never become active, and stays off the taskbar and Alt+Tab."""
    _set_ex_style(hwnd, add=WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, remove=WS_EX_APPWINDOW)


def set_click_through(hwnd, on):
    _set_ex_style(hwnd, add=WS_EX_TRANSPARENT if on else 0, remove=0 if on else WS_EX_TRANSPARENT)


def keep_on_top(hwnd):
    """Move to the top of the always-on-top windows, without activating."""
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def message_id(msg_address):
    """The message number from a Windows MSG, given its memory address (from Qt's nativeEvent)."""
    return wintypes.MSG.from_address(msg_address).message


def foreground_window():
    return user32.GetForegroundWindow()


def mouse_button_down():
    """True while any mouse button is held, anywhere on screen.

    The Usage panel never has focus, so it gets no "you lost focus" event when
    you click elsewhere. Checking the buttons is how it notices a click outside.
    """
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON))


def set_foreground(hwnd):
    """Make hwnd the active window. Windows only allows it right after the user clicked us (the tray menu)."""
    return bool(hwnd) and bool(user32.IsWindow(hwnd)) and bool(user32.SetForegroundWindow(hwnd))


def is_own_window(hwnd):
    """True if the window belongs to this app (the pill, a panel, the tray's hidden window, a menu)."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def is_taskbar(hwnd):
    return _class_name(hwnd) in TASKBAR_CLASSES


def _class_name(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def looks_fullscreen(window_rect, monitor_rect, on_primary, zoomed, style):
    """True if a window with these facts is a fullscreen app on the primary monitor.

    Rects are (left, top, right, bottom) in real pixels; style is the window's GWL_STYLE.
    The window has to cover its whole monitor. A maximized window can still be fullscreen:
    Chrome, Opera and Firefox, maximized before they go fullscreen, stay maximized (IsZoomed
    keeps saying so) but drop the title bar style. A normal maximized window (Notepad,
    Explorer) keeps its title bar, and with an auto-hide taskbar it covers the monitor too
    (its invisible frame hangs over each edge), so a maximized window only counts without it.
    """
    if not on_primary:
        return False
    left, top, right, bottom = window_rect
    screen_left, screen_top, screen_right, screen_bottom = monitor_rect
    covers = left <= screen_left and top <= screen_top and right >= screen_right and bottom >= screen_bottom
    has_title_bar = (style & WS_CAPTION) == WS_CAPTION
    return covers and (not zoomed or not has_title_bar)


def fullscreen_app_active(own_hwnd):
    """True if the active window is a fullscreen app on the primary monitor."""
    state = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        if state.value in (QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE):
            return True

    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd == own_hwnd or _class_name(hwnd) in DESKTOP_CLASSES:
        return False

    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL)
    if not monitor:
        return False
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    # No early return for maximized windows: a browser that was maximized stays "maximized"
    # in fullscreen (M18), so looks_fullscreen decides with the title bar style instead.
    screen = info.rcMonitor
    return looks_fullscreen(
        (rect.left, rect.top, rect.right, rect.bottom),
        (screen.left, screen.top, screen.right, screen.bottom),
        on_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        zoomed=bool(user32.IsZoomed(hwnd)),
        style=user32.GetWindowLongPtrW(hwnd, GWL_STYLE),
    )
