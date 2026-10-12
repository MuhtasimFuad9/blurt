"""Tests for hiding the quiet pill while a fullscreen app has focus (M18).

- looks_fullscreen: the decision alone, from the window's rect, style and monitor.
- fullscreen_app_active: the same with fake Windows calls (no real windows are read).
- The pill and the cancel notice: idle and hover hide in fullscreen, dictation feedback
  and the notice still show.

Nothing is shown on screen (WA_DontShowOnScreen), on the default settings (a temporary
config.json that doesn't exist). Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_fullscreen -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.ui import pill as pill_module
from app.ui import win32
from app.ui.notice import CancelNotice
from app.ui.pill import Pill

app = QApplication.instance() or QApplication([])

MONITOR = (0, 0, 1920, 1080)  # the primary monitor; its work area ends at 1032 (the taskbar)
TITLE_BAR = win32.WS_CAPTION | 0x00010000 | 0x00020000 | 0x00040000  # plus maximize, minimize, sizing
NO_TITLE_BAR = 0x00010000 | 0x00020000  # the title bar style stripped, as browsers do in fullscreen


class LooksFullscreenTest(unittest.TestCase):
    def check(self, window, zoomed, style, on_primary=True, monitor=MONITOR):
        return win32.looks_fullscreen(window, monitor, on_primary, zoomed, style)

    def test_a_window_that_is_exactly_the_monitor(self):
        self.assertTrue(self.check(MONITOR, zoomed=False, style=NO_TITLE_BAR))  # VLC, a borderless game

    def test_maximized_without_a_title_bar(self):
        # A maximized browser that went fullscreen (Chrome, Opera, Firefox): the M18 bug.
        self.assertTrue(self.check(MONITOR, zoomed=True, style=NO_TITLE_BAR))

    def test_maximized_with_a_title_bar_never_counts(self):
        # Exactly the monitor: rule 2 would have said yes; it was dropped (no app needed it).
        self.assertFalse(self.check(MONITOR, zoomed=True, style=TITLE_BAR))
        # Normal taskbar: the frame overhangs, but the bottom stops above the taskbar.
        self.assertFalse(self.check((-8, -8, 1928, 1040), zoomed=True, style=TITLE_BAR))
        # Auto-hide taskbar: covers the whole monitor, still a normal maximized window.
        self.assertFalse(self.check((-8, -8, 1928, 1088), zoomed=True, style=TITLE_BAR))

    def test_a_normal_window(self):
        self.assertFalse(self.check((100, 100, 1200, 800), zoomed=False, style=TITLE_BAR))
        self.assertFalse(self.check((100, 100, 1200, 800), zoomed=False, style=NO_TITLE_BAR))

    def test_only_the_primary_monitor_counts(self):
        self.assertFalse(self.check(MONITOR, zoomed=False, style=NO_TITLE_BAR, on_primary=False))
        left_monitor = (-1920, 0, 0, 1080)  # a second monitor left of the primary one
        self.assertFalse(self.check(left_monitor, zoomed=False, style=NO_TITLE_BAR, on_primary=False,
                                    monitor=left_monitor))

    def test_one_pixel_short_on_any_edge(self):
        for window in ((1, 0, 1920, 1080), (0, 1, 1920, 1080), (0, 0, 1919, 1080), (0, 0, 1920, 1079)):
            with self.subTest(window=window):
                self.assertFalse(self.check(window, zoomed=False, style=NO_TITLE_BAR))
                self.assertFalse(self.check(window, zoomed=True, style=NO_TITLE_BAR))

    def test_bigger_than_the_monitor(self):
        # Not maximized and overhanging every edge: still covers it (unchanged since M3).
        self.assertTrue(self.check((-8, -8, 1928, 1088), zoomed=False, style=TITLE_BAR))
        self.assertTrue(self.check((-8, -8, 1928, 1088), zoomed=False, style=NO_TITLE_BAR))


class FakeUser32:
    """Answers the calls fullscreen_app_active makes, for one active window."""

    def __init__(self, hwnd=100, class_name="Chrome_WidgetWin_1", rect=MONITOR, zoomed=True,
                 style=NO_TITLE_BAR, primary=True):
        self.hwnd = hwnd
        self.class_name = class_name
        self.rect = rect
        self.zoomed = zoomed
        self.style = style
        self.primary = primary
        self.windows_read = False
        self.style_index = None

    def GetForegroundWindow(self):
        self.windows_read = True
        return self.hwnd

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.class_name
        return len(self.class_name)

    def MonitorFromWindow(self, hwnd, flags):
        return 7

    def GetMonitorInfoW(self, monitor, info_ref):
        info = info_ref._obj
        info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom = MONITOR
        info.dwFlags = win32.MONITORINFOF_PRIMARY if self.primary else 0
        return True

    def GetWindowRect(self, hwnd, rect_ref):
        rect = rect_ref._obj
        rect.left, rect.top, rect.right, rect.bottom = self.rect
        return True

    def IsZoomed(self, hwnd):
        return self.zoomed

    def GetWindowLongPtrW(self, hwnd, index):
        self.style_index = index
        return self.style


class FakeShell32:
    def __init__(self, state=5, result=0):  # 5 = QUNS_ACCEPTS_NOTIFICATIONS, a normal desktop
        self.state = state
        self.result = result

    def SHQueryUserNotificationState(self, state_ref):
        state_ref._obj.value = self.state
        return self.result


class FullscreenAppActiveTest(unittest.TestCase):
    OWN = 55

    def active(self, user32=None, shell32=None):
        user32 = user32 or FakeUser32()
        with mock.patch.object(win32, "user32", user32), \
                mock.patch.object(win32, "shell32", shell32 or FakeShell32()):
            return win32.fullscreen_app_active(self.OWN)

    def test_exclusive_fullscreen_and_presentation_mode(self):
        for state in (win32.QUNS_RUNNING_D3D_FULL_SCREEN, win32.QUNS_PRESENTATION_MODE):
            with self.subTest(state=state):
                user32 = FakeUser32(rect=(100, 100, 1200, 800))  # the window alone wouldn't count
                self.assertTrue(self.active(user32, FakeShell32(state)))
                self.assertFalse(user32.windows_read)  # decided without looking at windows

    def test_busy_alone_is_not_fullscreen(self):
        # QUNS_BUSY (2) is monitor-blind, so it isn't used: a normal window stays not fullscreen.
        self.assertFalse(self.active(FakeUser32(rect=(100, 100, 1200, 800)), FakeShell32(2)))

    def test_maximized_browser_in_fullscreen(self):
        user32 = FakeUser32()  # maximized, no title bar, exactly the monitor
        self.assertTrue(self.active(user32))
        self.assertEqual(user32.style_index, win32.GWL_STYLE)

    def test_a_failed_query_still_checks_the_window(self):
        self.assertTrue(self.active(FakeUser32(), FakeShell32(result=-2147467259)))  # E_FAIL

    def test_own_window_desktop_and_taskbar(self):
        self.assertFalse(self.active(FakeUser32(hwnd=self.OWN)))
        for class_name in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            with self.subTest(class_name=class_name):
                self.assertFalse(self.active(FakeUser32(class_name=class_name, zoomed=False)))

    def test_no_active_window(self):
        self.assertFalse(self.active(FakeUser32(hwnd=None)))

    def test_maximized_notepad_with_auto_hide_taskbar(self):
        user32 = FakeUser32(class_name="Notepad", rect=(-8, -8, 1928, 1088), style=TITLE_BAR)
        self.assertFalse(self.active(user32))

    def test_secondary_monitor(self):
        self.assertFalse(self.active(FakeUser32(primary=False)))


class FullscreenPillTest(unittest.TestCase):
    """The real pill, started, with the fullscreen check faked and driven by hand."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        missing = Path(folder.name) / "config.json"  # not there: the defaults
        self.fullscreen = False
        for patch in (mock.patch.object(pill_module, "CONFIG_PATH", missing),
                      mock.patch("app.config.CONFIG_PATH", missing),
                      mock.patch.object(pill_module.win32, "fullscreen_app_active",
                                        lambda own_hwnd: self.fullscreen)):
            patch.start()
            self.addCleanup(patch.stop)
        self.pill = Pill()
        self.pill.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.pill.deleteLater)
        self.addCleanup(self.pill.hide)
        self.pill.start()
        for timer in (self.pill._hover_timer, self.pill._fullscreen_timer, self.pill._config_timer):
            timer.stop()  # the tests poll by hand; the real cursor never hovers the pill
        self.assertTrue(self.pill.isVisible())

    def go_fullscreen(self, on=True):
        self.fullscreen = on
        self.pill._poll_fullscreen()

    def test_the_default_is_on(self):
        self.assertTrue(self.pill.settings["hide_in_fullscreen"])

    def test_idle_hides_and_comes_back(self):
        self.go_fullscreen()
        self.assertEqual(self.pill._state, "idle")
        self.assertFalse(self.pill.isVisible())
        self.go_fullscreen(False)
        self.assertTrue(self.pill.isVisible())

    def test_hover_hides(self):
        self.pill._hovered = True
        self.pill._refresh()
        self.assertEqual(self.pill._state, "hover")
        self.go_fullscreen()
        self.assertFalse(self.pill.isVisible())

    def test_dictation_feedback_still_shows(self):
        self.go_fullscreen()
        for result in ("done", "copied", "error"):
            with self.subTest(result=result):
                self.assertFalse(self.pill.isVisible())  # idle, hidden
                self.pill.recording_started()
                self.assertEqual(self.pill._state, "recording")
                self.assertTrue(self.pill.isVisible())
                self.pill.recording_ended(True)
                self.assertEqual(self.pill._state, "processing")
                self.assertTrue(self.pill.isVisible())
                self.pill.job_finished(result)
                self.assertEqual(self.pill._state, result)
                self.assertTrue(self.pill.isVisible())
                self.pill._clear_result()  # its hold is over: back to idle, hidden again
                self.assertEqual(self.pill._state, "idle")
                self.assertFalse(self.pill.isVisible())

    def test_the_setting_off_keeps_it_visible(self):
        self.pill._settings = dict(self.pill._settings, hide_in_fullscreen=False)
        self.go_fullscreen()
        self.assertTrue(self.pill._fullscreen)
        self.assertTrue(self.pill.isVisible())

    def test_the_cancel_notice_still_shows(self):
        notice = CancelNotice(self.pill)
        notice.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(notice.deleteLater)
        self.addCleanup(notice.hide_now)
        self.go_fullscreen()
        self.pill.recording_started()
        self.pill.recording_ended(False)  # cancelled by a stray key: nothing to process
        self.assertEqual(self.pill._state, "idle")
        self.assertFalse(self.pill.isVisible())
        notice.show_notice()  # it answers a key press, like the "!", so fullscreen doesn't hide it
        self.assertTrue(notice.isVisible())


if __name__ == "__main__":
    unittest.main()
