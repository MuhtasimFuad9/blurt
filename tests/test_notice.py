"""Tests for the cancel notice (M17 item 14): "Dictation cancelled: another key was pressed",
in its own window in the idle pill's spot, that never takes focus and scales like the pill.

Nothing is shown on screen (WA_DontShowOnScreen), on the default settings (a temporary
config.json that doesn't exist). The scaling test runs a child Python per display scale
(QT_SCALE_FACTOR, which Qt applies like a Windows display scale) with the real fonts.
Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_notice -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import main
from app.ui import notice as notice_module
from app.ui import pill as pill_module
from app.ui import win32
from app.ui.notice import CancelNotice
from app.ui.pill import Pill, UiBridge
from tests import real_fonts

app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


class CancelNoticeWantedTest(unittest.TestCase):
    def test_only_after_a_real_hold(self):
        self.assertFalse(main.cancel_notice_wanted(0.0))
        self.assertFalse(main.cancel_notice_wanted(0.29))  # a quick Ctrl+Win+D: no notice
        self.assertTrue(main.cancel_notice_wanted(0.3))  # MIN_HOLD_SECONDS, the tap limit
        self.assertTrue(main.cancel_notice_wanted(5.0))


class NoticeTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        missing = Path(folder.name) / "config.json"  # not there: the defaults
        for patch in (mock.patch.object(pill_module, "CONFIG_PATH", missing),
                      mock.patch("app.config.CONFIG_PATH", missing)):
            patch.start()
            self.addCleanup(patch.stop)
        self.pill = Pill()
        self.pill.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.pill.deleteLater)
        self.notice = CancelNotice(self.pill)
        self.notice.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.notice.deleteLater)
        self.addCleanup(self.notice.hide_now)

    def test_the_text(self):
        self.assertEqual(notice_module.TEXT, "Dictation cancelled: another key was pressed")
        self.assertNotIn("—", notice_module.TEXT)

    def test_it_can_never_take_focus(self):
        flags = self.notice.windowFlags()
        for flag in (Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.FramelessWindowHint, Qt.WindowType.Tool):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(self.notice.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(self.notice.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.notice.show_notice()
        style = win32.user32.GetWindowLongPtrW(int(self.notice.winId()), win32.GWL_EXSTYLE)
        for bit in (win32.WS_EX_NOACTIVATE, win32.WS_EX_TOOLWINDOW, win32.WS_EX_TRANSPARENT):
            self.assertTrue(style & bit, hex(bit))  # never active, no taskbar button, clicks pass through
        self.assertFalse(style & win32.WS_EX_APPWINDOW)

    def test_the_bridge_signal_shows_it(self):
        bridge = UiBridge()
        bridge.dictation_cancelled.connect(self.notice.show_notice, Qt.ConnectionType.QueuedConnection)
        bridge.dictation_cancelled.emit()
        self.assertTrue(wait_for(self.notice.isVisible))

    def test_it_sits_on_the_idle_pill(self):
        self.notice.show_notice()
        center_x, bottom = self.pill.idle_spot()
        rect = self.notice.notice_rect_on_screen()
        self.assertAlmostEqual(rect.center().x(), center_x, delta=0.5)
        self.assertAlmostEqual(rect.bottom(), bottom, delta=1)
        pill_rect = self.pill._pill_rect()  # the idle pill, in design pixels inside its window
        self.assertAlmostEqual(self.pill.y() + pill_rect.bottom() * self.pill.settings["scale"], bottom, delta=1)

    def test_it_goes_away_when_the_pill_needs_its_spot(self):
        self.notice.show_notice()
        self.pill.recording_started()  # a new dictation
        self.assertFalse(self.notice.isVisible())
        self.pill.recording_ended(False)
        self.notice.show_notice()
        self.pill._hovered = True  # the hover menu
        self.pill._refresh()
        self.assertFalse(self.notice.isVisible())

    def test_it_hides_by_itself(self):
        self.pill._settings = dict(self.pill.settings, error_hold_ms=30, content_in_ms=0, content_out_ms=0)
        self.notice.show_notice()
        self.assertTrue(self.notice.isVisible())
        self.assertTrue(wait_for(lambda: not self.notice.isVisible()))


CHILD = r"""
import json, sys
sys.path.insert(0, ".")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from tests import real_fonts
from app.ui.notice import CancelNotice
from app.ui.pill import Pill
from PySide6.QtGui import QGuiApplication
pill = Pill()
pill.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
notice = CancelNotice(pill)
notice.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
notice.show_notice()
app.processEvents()
s = pill.settings
rect = notice.notice_rect_on_screen()
area = QGuiApplication.primaryScreen().availableGeometry()
center_x, bottom = pill.idle_spot()
print(json.dumps({
    "fonts": real_fonts.problems(),
    "ratio": notice.devicePixelRatioF(),
    "pill_ratio": pill.devicePixelRatioF(),
    "center": [rect.center().x(), center_x],
    "bottom": [rect.bottom(), bottom],
    "rect": [rect.left(), rect.top(), rect.right(), rect.bottom()],
    "gap": s["gap_above_taskbar"],
    "area": [area.x(), area.y(), area.width(), area.height()],
    "content": s["mic_label_padding_left"] + s["mic_label_icon_size"] + s["mic_label_icon_gap"]
               + notice._text_w + s["mic_label_padding_right"],
    "notice_w": notice._notice_w,
    "icon_pixels": notice.icon_pixel_size(),
    "icon_size": s["mic_label_icon_size"],
}))
"""


class ScalingTest(unittest.TestCase):
    """At 100%, 125% and 150% display scaling the notice sits on the idle pill, fits the screen,
    and its text and icon are drawn for the real pixel ratio, the same way as the pill."""

    @classmethod
    def setUpClass(cls):
        real_fonts.require_real_fonts()

    def measure(self, factor):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, QT_SCALE_FACTOR=str(factor), BLURT_HOME=home)
            env.pop("QT_QPA_PLATFORM", None)
            result = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=env, capture_output=True,
                                    text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_every_scale(self):
        for factor in (1.0, 1.25, 1.5):
            with self.subTest(scale=f"{factor:.0%}"):
                got = self.measure(factor)
                self.assertEqual(got["fonts"], [])
                self.assertAlmostEqual(got["ratio"], factor)
                self.assertAlmostEqual(got["pill_ratio"], factor)
                self.assertAlmostEqual(got["center"][0], got["center"][1], delta=0.5)
                self.assertAlmostEqual(got["bottom"][0], got["bottom"][1], delta=1)
                # The visible notice (the window around it is see-through shadow room, like the pill's).
                left, top, right, bottom = got["rect"]
                ax, ay, aw, ah = got["area"]
                self.assertGreaterEqual(left, ax)
                self.assertLessEqual(right, ax + aw)
                self.assertGreaterEqual(top, ay)
                self.assertAlmostEqual(bottom, ay + ah - got["gap"], delta=1)  # 12px above the taskbar
                self.assertLessEqual(got["content"], got["notice_w"])  # the text fits
                self.assertEqual(got["icon_pixels"], round(got["icon_size"] * factor))  # sharp, not stretched


if __name__ == "__main__":
    unittest.main()
