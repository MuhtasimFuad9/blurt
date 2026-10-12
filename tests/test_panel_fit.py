"""Tests that the panels fit on the screen (M16).

Settings and Usage get shorter and scroll when the screen is too small; History and
Dictionary stay exactly as they were. The screens are numbers, not real monitors:
set_anchor(center x, top of the hover pill, top of the screen), with the hover pill's
top 94px above the screen's bottom (a 48px taskbar, the 12px gap and the 34px pill).
Nothing is shown on screen, and every file is a temporary copy. Measured with the
real Windows fonts only (tests/real_fonts.py). Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_panel_fit -v
"""

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFont, QWheelEvent
from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.history import HistoryStore
from app.ui.dictionary_panel import DictionaryPanel
from app.ui.history_panel import HistoryPanel
from app.ui.panel import UsagePanel
from app.ui.settings_panel import SettingsPanel
from app.usage import UsageTracker, parse_rate_limit
from tests import real_fonts

app = QApplication.instance() or QApplication([])

ROOT = Path(__file__).resolve().parent.parent
SCREENS = (614, 720, 768, 800, 864, 1080)  # logical heights: 1366 x 768 at 125% up to 1080 at 100%
CENTER_X = 960
PILL_ABOVE_BOTTOM = 94
RATE_LIMIT = ("Rate limit reached for model `whisper-large-v3-turbo` on requests per minute (RPM): "
              "Limit 20, Used 20, Requested 1. Please try again in 30s.")


class FakeAutostart:
    """Start with Windows as in the installed app, without the registry."""

    def available(self):
        return True

    def enabled(self):
        return False

    def set_enabled(self, on):
        raise OSError("not in tests")


def anchor(panel, height, screen_top=0):
    """Put the panel above the hover pill of a screen `height` tall whose top is screen_top."""
    panel.set_anchor(CENTER_X, screen_top + height - PILL_ABOVE_BOTTOM, screen_top)


def card_top(panel):
    """The card's top edge on the screen."""
    return panel.y() + panel._card_rect().top() * panel._settings["scale"]


def open_panel(panel):
    panel._open = True  # like open_panel(), without showing a window
    panel._on_open()
    if hasattr(panel, "_live_timer"):
        panel._live_timer.stop()
    return panel


def wheel(panel, notches):
    """Turn the wheel over the middle of the panel: negative = down (towards the footer)."""
    center = QPointF(panel.width() / 2, panel.height() / 2)
    event = QWheelEvent(center, center, QPoint(0, 0), QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    panel.wheelEvent(event)
    return event.isAccepted()


class RealFontsTest(unittest.TestCase):
    """Every measuring test in M16 depends on this one."""

    def test_the_panels_are_measured_with_the_real_windows_fonts(self):
        self.assertEqual(real_fonts.problems(), [])

    def test_the_range_rejects_arial_and_a_missing_font(self):
        # So the check can tell the real font from another one on this PC.
        pinned = dict(real_fonts.WIDTHS, **{"line height": real_fonts.LINE_HEIGHT})
        for families in (["Arial"], ["No Such Font Blurt"]):
            font = real_fonts.panel_font()
            font.setFamilies(families)
            for name, value in real_fonts.measure(font).items():
                self.assertTrue(real_fonts.out_of_range(value, pinned[name]), f"{families} {name} {value:.3f}")

    def test_the_pinned_font_is_the_settings_row_font(self):
        self.assertEqual((real_fonts.PX, real_fonts.WEIGHT), (13, QFont.Weight.Medium))


class MeasuringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real_fonts.require_real_fonts()

    def folder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def assert_on_screen(self, panel, screen_top, what):
        self.assertGreaterEqual(panel.y(), screen_top, f"{what}: window top")
        self.assertGreaterEqual(card_top(panel), screen_top, f"{what}: card top")


class SettingsFitTest(MeasuringTest):
    """Settings with the rows installed users see (the API key and Start with Windows)."""

    def settings(self, state="fine", scale=1.0):
        folder = self.folder()
        text = (ROOT / "config.example.json").read_text(encoding="utf-8")
        if state == "broken":  # a real mistake: a missing comma
            text = text.replace('"keep_mic_open": true,', '"keep_mic_open": true', 1)
        path = folder / "config.json"
        path.write_text(text, encoding="utf-8")
        panel = SettingsPanel(path, key_status=lambda: "windows", on_change_key=lambda: None,
                              autostart=FakeAutostart())
        self.addCleanup(panel.deleteLater)
        panel.set_settings(dict(DEFAULTS["pill"], scale=scale))
        open_panel(panel)
        if state == "list":
            panel._open_shortcut_list(True)
        return panel

    def test_every_state_fits_every_screen(self):
        for state in ("fine", "broken", "list"):
            panel = self.settings(state)
            for height in SCREENS:
                anchor(panel, height)
                self.assert_on_screen(panel, 0, f"{state} on {height}")

    def test_it_fits_at_scale_125_on_768(self):
        for state in ("fine", "broken", "list"):
            panel = self.settings(state, scale=1.25)
            anchor(panel, 768)
            self.assert_on_screen(panel, 0, f"{state} at 1.25")
            if state != "list":  # the list open is short enough to fit
                self.assertGreater(panel._max_scroll(), 0, state)

    def test_a_screen_that_starts_at_40(self):
        # A taskbar at the top of the screen (or a monitor above): the available area starts at 40.
        for state in ("fine", "broken", "list"):
            panel = self.settings(state)
            for height in SCREENS:
                anchor(panel, height, screen_top=40)
                self.assert_on_screen(panel, 40, f"{state} on {height} from 40")

    def test_1080_is_pinned(self):
        for state in ("fine", "broken", "list"):
            panel = self.settings(state)
            unknown = (panel.width(), panel.height(), panel._card_rect())
            anchor(panel, 1080)
            self.assertEqual((panel.width(), panel.height()), (348, 857), state)
            self.assertEqual((panel.width(), panel.height(), panel._card_rect()), unknown, state)
            self.assertEqual(panel._max_scroll(), 0, state)

    def test_it_scrolls_only_when_it_doesnt_fit(self):
        panel = self.settings("fine")
        for height, scrolls in ((614, True), (720, True), (768, True), (800, False), (864, False), (1080, False)):
            anchor(panel, height)
            self.assertEqual(panel._max_scroll() > 0, scrolls, height)

    def test_the_whole_content_can_be_scrolled_into_view(self):
        panel = self.settings("broken")
        anchor(panel, 614)
        view = panel._view_rect()
        self.assertAlmostEqual(panel._max_scroll() + view.height(), panel._content_height())
        panel._scroll_to(panel._max_scroll(), animate=False)
        footer, quit_rect = panel._layout()["footer"]
        self.assertAlmostEqual(footer.bottom(), view.bottom())
        self.assertTrue(view.contains(quit_rect.center()))

    def test_more_or_less_room_resizes_the_window(self):
        panel = self.settings("broken")
        anchor(panel, 1080)
        self.assertEqual(panel.height(), 857)
        anchor(panel, 720)
        self.assertEqual(panel.height(), 652)
        panel._scroll_to(panel._max_scroll(), animate=False)
        anchor(panel, 864)
        self.assertEqual(panel.height(), 796)
        self.assertEqual(panel._scroll.value, panel._max_scroll())  # back in range
        anchor(panel, 1080)
        self.assertEqual((panel.height(), panel._scroll.value), (857, 0))

    def test_where_the_card_fits_it_sits_exactly_where_it_did(self):
        # Only the see-through window above it gets shorter: the card keeps its place on screen.
        panel = self.settings("fine")
        anchor(panel, 1080)
        reference = card_top(panel) - (1080 - PILL_ABOVE_BOTTOM)  # relative to the pill
        for height in (800, 864):
            anchor(panel, height)
            self.assertEqual(panel._max_scroll(), 0)
            self.assertAlmostEqual(card_top(panel) - (height - PILL_ABOVE_BOTTOM), reference, places=6)


class UsageFitTest(MeasuringTest):
    def usage(self, banner):
        tracker = UsageTracker(":memory:", DEFAULTS["groq_limits"], clock=lambda: 1_000_000_000.0)
        self.addCleanup(tracker.close)
        if banner:
            tracker.log_failure("transcription", 120.0, 429, rate_limit=parse_rate_limit(RATE_LIMIT))
        panel = UsagePanel(tracker)
        self.addCleanup(panel.deleteLater)
        return open_panel(panel)

    def test_it_fits_every_screen_with_and_without_the_banner(self):
        for banner in (False, True):
            panel = self.usage(banner)
            for height in SCREENS:
                anchor(panel, height)
                self.assert_on_screen(panel, 0, f"banner {banner} on {height}")
                anchor(panel, height, screen_top=40)
                self.assert_on_screen(panel, 40, f"banner {banner} on {height} from 40")

    def test_it_only_changes_on_the_614_screen(self):
        for banner in (False, True):
            panel = self.usage(banner)
            unknown = (panel.width(), panel.height(), panel._card_rect())
            for height in SCREENS[1:]:
                anchor(panel, height)
                self.assertEqual((panel.width(), panel.height(), panel._card_rect()), unknown, height)
                self.assertEqual(panel._max_scroll(), 0)
            anchor(panel, 614)
            self.assertLess(panel.height(), unknown[1])
            self.assertGreater(panel._max_scroll(), 0)

    def test_the_wheel_scrolls_it_on_614_and_does_nothing_where_it_fits(self):
        panel = self.usage(banner=True)
        anchor(panel, 1080)
        self.assertFalse(wheel(panel, -1))
        self.assertEqual(panel._scroll.end, 0)
        anchor(panel, 614)
        self.assertTrue(wheel(panel, -1))
        self.assertEqual(panel._scroll.end, min(60, panel._max_scroll()))
        self.assertTrue(wheel(panel, -10))
        self.assertEqual(panel._scroll.end, panel._max_scroll())

    def test_the_banner_going_away_keeps_the_scroll_in_range(self):
        panel = self.usage(banner=True)
        anchor(panel, 614)
        panel._scroll_to(panel._max_scroll(), animate=False)
        panel._snapshot = dict(panel._snapshot, rate_limit=None)
        panel._fit_scroll()
        self.assertEqual(panel._scroll.value, panel._max_scroll())
        self.assertLess(panel._max_scroll(), 78)

    def test_it_opens_at_the_top(self):
        panel = self.usage(banner=True)
        anchor(panel, 614)
        panel._scroll_to(panel._max_scroll(), animate=False)
        panel._open = False
        open_panel(panel)
        self.assertEqual(panel._scroll.value, 0)


class HistoryAndDictionaryTest(MeasuringTest):
    """They don't fit the screen (FITS_SCREEN is off); they must not need to on the 614 screen."""

    def panels(self):
        folder = self.folder()
        store = HistoryStore(folder / "history.db", folder / "failed")
        self.addCleanup(store.close)
        history = HistoryPanel(store)
        terms = folder / "dictionary.txt"
        terms.write_text("Groq\n", encoding="utf-8")
        dictionary = DictionaryPanel(terms)
        for panel in (history, dictionary):
            self.addCleanup(panel.deleteLater)
        return {"history": history, "dictionary": dictionary}

    def test_their_tallest_card_fits_the_614_screen(self):
        for name, panel in self.panels().items():
            anchor(panel, 614)
            _, top, _ = panel._margins()
            # The window is sized for the tallest card, which sits right under its top margin.
            self.assertGreaterEqual(panel.y() + top * panel._settings["scale"], 0, name)

    def test_a_screen_top_changes_nothing(self):
        for name, panel in self.panels().items():
            self.assertFalse(panel.FITS_SCREEN, name)
            panel.set_anchor(CENTER_X, 1080 - PILL_ABOVE_BOTTOM)
            before = (panel.width(), panel.height(), panel.pos(), panel._card_rect())
            for screen_top in (0, 40, 500):
                panel.set_anchor(CENTER_X, 1080 - PILL_ABOVE_BOTTOM, screen_top)
                self.assertEqual((panel.width(), panel.height(), panel.pos(), panel._card_rect()), before, name)


if __name__ == "__main__":
    unittest.main()
