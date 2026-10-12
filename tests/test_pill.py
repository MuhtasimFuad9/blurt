"""Tests for the pill's hover menu label, which names the hold-to-talk shortcut (M14), and
the "Didn't hear you" label above the "!" (M17).

The pill is made but never shown, on the default settings (a temporary config.json
that doesn't exist). Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_pill -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from app.micname import NO_MIC, NO_MIC_TEXT
from app.ui import pill as pill_module
from app.ui.pill import QUIET_TEXT, Pill

app = QApplication.instance() or QApplication([])


class PillTest(unittest.TestCase):
    """Makes pills on the default settings (no tests of its own)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        missing = Path(folder.name) / "config.json"  # not there: the defaults
        for patch in (mock.patch.object(pill_module, "CONFIG_PATH", missing),
                      mock.patch("app.config.CONFIG_PATH", missing)):
            patch.start()
            self.addCleanup(patch.stop)

    def pill(self, **kwargs):
        pill = Pill(**kwargs)
        self.addCleanup(pill.deleteLater)
        return pill


class MenuLabelTest(PillTest):
    def test_the_default_is_unchanged(self):
        # Measured with the pill from before M14 on the default settings: a 250 x 34 hover
        # pill in a 282 x 94 window (the mockup's 222 grew with the menu paddings in M3).
        for pill in (self.pill(), self.pill(hotkey="Ctrl+Win")):
            self.assertEqual(pill._menu_label, "Ctrl + Win")
            self.assertEqual(pill._sizes["hover"], (250, 34))
            self.assertEqual((pill.width(), pill.height()), (282, 94))

    def test_the_hover_pill_fits_the_label(self):
        default = self.pill()
        widths = {}
        for hotkey in ("Alt+Win", "Ctrl+Alt+Win", "Ctrl+Shift+Win", "Alt+Shift+Win"):
            pill = self.pill(hotkey=hotkey)
            widths[hotkey] = pill._sizes["hover"][0]
            self.assertGreater(pill.width(), widths[hotkey])  # the window has room for it
        self.assertLess(widths["Alt+Win"], default._sizes["hover"][0])  # "Alt" is shorter than "Ctrl"
        self.assertLess(widths["Alt+Win"], widths["Ctrl+Alt+Win"])
        self.assertGreater(widths["Ctrl+Shift+Win"], 250)

    def test_set_shortcut_changes_it_live(self):
        pill = self.pill()
        pill.set_shortcut("Ctrl+Shift+Win")
        self.assertEqual(pill._menu_label, "Ctrl + Shift + Win")
        self.assertGreater(pill._sizes["hover"][0], 250)
        pill.set_shortcut("Ctrl+Win")
        self.assertEqual(pill._sizes["hover"], (250, 34))
        self.assertEqual((pill.width(), pill.height()), (282, 94))


class QuietLabelTest(PillTest):
    """A dictation the silence check dropped ("quiet"): the "!" with "Didn't hear you" above it."""

    def quiet_pill(self, mic=lambda: "Realtek Audio"):
        pill = self.pill(mic_source=mic)
        pill._show = lambda: None  # never put the window on screen while testing
        pill.recording_ended(True)  # one dictation in the pipeline
        return pill

    def click(self, pill, point):
        """Press and release the left button at a design-pixel point."""
        scale = pill._settings["scale"]
        event = mock.Mock()
        event.position.return_value = QPointF(point.x() * scale, point.y() * scale)
        with mock.patch.object(pill_module.QTimer, "singleShot"):  # no focus check later
            pill.mousePressEvent(event)
            pill.mouseReleaseEvent(event)

    def test_quiet_shows_the_alert_with_its_label(self):
        pill = self.quiet_pill()
        pill.job_finished("quiet", 0)
        self.assertEqual(pill._state, "error")
        self.assertEqual(pill._label_wanted(), (QUIET_TEXT, "mic"))
        self.assertEqual(QUIET_TEXT, "Didn't hear you")
        self.assertEqual(pill._label_display(pill._label_wanted())[0], QUIET_TEXT)  # fits, not cut with "…"
        self.assertEqual(pill._result_timer.interval(), pill._settings["error_hold_ms"])  # held like "!" (4s)
        self.assertEqual(pill._settings["error_hold_ms"], 4000)

    def test_the_label_stays_while_the_alert_does(self):
        pill = self.quiet_pill()
        pill.job_finished("quiet", 0)
        hold = pill._settings["mic_label_hold_ms"]
        with mock.patch.object(pill_module, "_now_ms", return_value=pill_module._now_ms() + hold + 1000):
            self.assertEqual(pill._label_wanted(), (QUIET_TEXT, "mic"))  # a mic name would be gone by now
        pill._clear_result()  # error_hold_ms is over
        self.assertEqual(pill._state, "idle")
        self.assertIsNone(pill._label_wanted())

    def test_a_click_clears_it_and_opens_nothing(self):
        pill = self.quiet_pill()
        pill._open_panel = mock.Mock(return_value=False)
        pill.job_finished("quiet", 0)
        self.assertEqual(pill._state, "error")
        self.click(pill, pill._pill_rect().center())
        self.assertEqual(pill._state, "idle")
        pill._open_panel.assert_not_called()  # nothing was saved to History

    def test_nothing_and_no_mic_are_unchanged(self):
        pill = self.quiet_pill()
        pill.job_finished("nothing", 0)
        self.assertEqual(pill._state, "idle")  # nothing worth pasting: no "!", as before
        pill.show_error()
        self.assertEqual(pill._label_wanted(), (NO_MIC_TEXT, "mic_off"))
        pill.recording_ended(True)
        pill.job_finished("quiet", 0)
        self.assertEqual(pill._label_wanted(), (QUIET_TEXT, "mic"))  # the newer reason wins
        self.assertIsNot(pill._error_note, NO_MIC)

    def test_no_label_when_mic_labels_are_off(self):
        pill = self.quiet_pill()
        pill._settings = dict(pill._settings, mic_label=False)
        pill.job_finished("quiet", 0)
        self.assertEqual(pill._state, "error")  # the "!" still shows
        self.assertIsNone(pill._label_wanted())


if __name__ == "__main__":
    unittest.main()
