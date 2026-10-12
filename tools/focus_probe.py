"""Shows what Windows reports about the focused element, to plan Smart paste.

It doesn't change anything and never presses keys. Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\focus_probe.py

Click into different apps and fields. A line is printed whenever what's focused
changes. Press Ctrl+C to stop, then copy the output into the chat.

Each line shows three signals:
- class:  the Windows class of the focused control (classic text boxes are "Edit", "RichEdit...")
- caret:  whether the app created a real Windows text caret
- uia:    what UI Automation (the accessibility layer screen readers use) says:
          control type, class, framework, read-only, and whether it supports
          text (Text), text editing (TextEdit) and a value (Value)
"""

import ctypes
import sys
import time
from ctypes import wintypes

sys.coinit_flags = 0  # multithreaded COM, like the real app's pipeline thread will use

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as uia  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

POLL_SECONDS = 0.5
UIA_TIMEOUT_MS = 300
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# UI Automation property numbers (from the Windows SDK, UIAutomationClient.h)
UIA_LocalizedControlTypePropertyId = 30004
UIA_IsTextPatternAvailablePropertyId = 30040
UIA_IsValuePatternAvailablePropertyId = 30043
UIA_ValueIsReadOnlyPropertyId = 30046
UIA_AriaRolePropertyId = 30101
UIA_IsTextEditPatternAvailablePropertyId = 30149

CONTROL_TYPES = {
    50000: "Button", 50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink", 50007: "ListItem",
    50008: "List", 50018: "Tab", 50019: "TabItem", 50020: "Text", 50023: "Tree", 50024: "TreeItem",
    50025: "Custom", 50026: "Group", 50030: "Document", 50032: "Window", 50033: "Pane",
}


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def class_name(hwnd):
    if not hwnd:
        return "-"
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def exe_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"pid {pid} (no access)"
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return f"pid {pid}"
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def win32_signals():
    """(exe, focused control class, has caret) from plain Windows functions."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "-", "-", False
    pid = wintypes.DWORD()
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return exe_name(pid.value), "?", False
    return exe_name(pid.value), class_name(info.hwndFocus), bool(info.hwndCaret)


def make_automation():
    automation = comtypes.client.CreateObject(uia.CUIAutomation8, interface=uia.IUIAutomation2)
    # Don't hang on a frozen app.
    automation.ConnectionTimeout = UIA_TIMEOUT_MS
    automation.TransactionTimeout = UIA_TIMEOUT_MS
    return automation


def prop(element, prop_id):
    try:
        return element.GetCurrentPropertyValue(prop_id)
    except comtypes.COMError:
        return None


def uia_signals(automation):
    """A short description of the focused element, and how long asking took."""
    t0 = time.perf_counter()
    try:
        element = automation.GetFocusedElement()
    except comtypes.COMError as e:
        return f"error {e.hresult & 0xFFFFFFFF:#010x}", (time.perf_counter() - t0) * 1000
    try:
        type_id = element.CurrentControlType
        kind = CONTROL_TYPES.get(type_id) or f"{type_id} ({prop(element, UIA_LocalizedControlTypePropertyId)})"
        uia_class = element.CurrentClassName or "-"
        if len(uia_class) > 40:
            uia_class = uia_class[:40] + "..."  # web pages can have very long class lists
        parts = [
            kind,
            f"class={uia_class}",
            f"fw={element.CurrentFrameworkId or '-'}",
        ]
        role = prop(element, UIA_AriaRolePropertyId)
        if role:
            parts.append(f"aria={role}")
        supports = [
            name
            for name, prop_id in (
                ("Text", UIA_IsTextPatternAvailablePropertyId),
                ("TextEdit", UIA_IsTextEditPatternAvailablePropertyId),
                ("Value", UIA_IsValuePatternAvailablePropertyId),
            )
            if prop(element, prop_id)
        ]
        parts.append("supports=" + ("+".join(supports) or "none"))
        if "Value" in supports:
            parts.append(f"readonly={bool(prop(element, UIA_ValueIsReadOnlyPropertyId))}")
        parts.append(f"focusable={bool(element.CurrentIsKeyboardFocusable)}")
        text = " ".join(parts)
    except comtypes.COMError as e:
        text = f"error reading element {e.hresult & 0xFFFFFFFF:#010x}"
    return text, (time.perf_counter() - t0) * 1000


def main():
    automation = make_automation()
    print("Watching the focused element. Click around; Ctrl+C to stop.\n")
    last = None
    while True:
        exe, focus_class, caret = win32_signals()
        uia_text, ms = uia_signals(automation)
        snapshot = (exe, focus_class, caret, uia_text)
        if snapshot != last:
            last = snapshot
            print(f"{time.strftime('%H:%M:%S')}  {exe}")
            print(f"    class={focus_class}  caret={'yes' if caret else 'no'}")
            print(f"    uia: {uia_text}  ({ms:.0f}ms)")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
