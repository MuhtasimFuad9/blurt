"""Tests for the Settings panel's clicks, slider drag and live refresh (app/ui/settings_panel.py).

A temporary config.json only; the real one is never touched. Nothing is shown on
screen. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_settings_panel -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from app import shortcut
from app.config import DEFAULTS, ConfigError, load_config, set_setting
from app.ui import settings_panel as settings_module
from app.ui.panel import SCROLL_STEP
from app.ui.settings_panel import SettingsPanel
from tests import real_fonts

app = QApplication.instance() or QApplication([])

CONFIG = """{
  "keep_mic_open": true,
  "language": "en",
  "paste_mode": "smart",
  "history": true,
  "pill": {
    "scale": 1.0,
    "idle_opacity": 0.22,
    "mic_label": true
  }
}
"""


class SettingsPanelTest(unittest.TestCase):
    def make_autostart(self):
        return None  # no Start with Windows row (StartWithWindowsTest has one)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.on_quit = mock.Mock()
        self.panel = SettingsPanel(self.path, on_quit=self.on_quit, autostart=self.make_autostart())
        self.panel.set_settings(dict(DEFAULTS["pill"]))  # scale 1: widget pixels are design pixels
        self.addCleanup(self.panel.deleteLater)
        self.open()

    def open(self):
        self.panel._open = True  # like open_panel(), without showing a window
        self.panel._on_open()
        self.addCleanup(self.panel._live_timer.stop)

    def click(self, target):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.panel._content_release(target)
        return out.getvalue()

    def settings(self):
        return load_config(self.path)

    def mouse(self, kind, pos, buttons=Qt.MouseButton.LeftButton):
        event = QMouseEvent(kind, QPointF(pos), QPointF(pos), Qt.MouseButton.LeftButton, buttons,
                            Qt.KeyboardModifier.NoModifier)
        handler = {QEvent.Type.MouseButtonPress: self.panel.mousePressEvent,
                   QEvent.Type.MouseMove: self.panel.mouseMoveEvent,
                   QEvent.Type.MouseButtonRelease: self.panel.mouseReleaseEvent}[kind]
        with contextlib.redirect_stdout(io.StringIO()):
            handler(event)

    def control(self, name):
        return self.panel._layout()["rows"][name][2]

    # ---- clicks ----

    def test_a_toggle_writes_only_that_value(self):
        self.click("history")
        self.assertEqual(self.path.read_text(encoding="utf-8"), CONFIG.replace('"history": true', '"history": false'))
        self.click("history")
        self.assertEqual(self.path.read_text(encoding="utf-8"), CONFIG)

    def test_mic_name_is_in_the_pill_section(self):
        self.click("mic_label")
        self.assertFalse(self.settings()["pill"]["mic_label"])

    def test_a_choice_writes_and_the_current_one_writes_nothing(self):
        self.click(("language", "auto"))
        self.assertEqual(self.settings()["language"], "auto")
        before = self.path.stat().st_mtime_ns
        self.click(("language", "auto"))
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_clicks_hit_the_right_controls(self):
        toggle = self.control("history")
        self.assertEqual(self.panel._content_target(toggle.center()), "history")
        segments = self.panel._segment_rects("paste_mode", self.control("paste_mode"))
        self.assertEqual(self.panel._content_target(segments[1][2].center()), ("paste_mode", "always"))

    # ---- a broken config.json ----

    def break_file(self):
        self.path.write_text(CONFIG.replace('"en",', '"en"'), encoding="utf-8")
        self.panel.reload()
        return self.path.read_bytes()

    def test_broken_file_locks_every_control(self):
        self.break_file()
        for name in ("history", "keep_mic_open", "mic_label", "idle_opacity", "language", "hotkey"):
            self.assertIsNone(self.panel._content_target(self.control(name).center()), name)
        self.assertEqual(self.panel._content_target(self.panel._banner_button_rect().center()), "banner_notepad")

    def test_broken_file_is_never_written_even_if_a_click_gets_through(self):
        before = self.break_file()
        out = self.click("history")  # the second guard: set_setting refuses
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("not saved", out)
        self.assertTrue(self.panel._note.startswith("Not saved"))

    def test_fixing_the_file_unlocks_it(self):
        self.break_file()
        self.path.write_text(CONFIG, encoding="utf-8")
        self.panel._poll()
        self.assertIsNone(self.panel._problem)
        self.assertEqual(self.panel._content_target(self.control("history").center()), "history")

    # ---- live refresh ----

    def test_a_notepad_edit_shows_up(self):
        set_setting("history", False, path=self.path)  # "Notepad" saves
        self.panel._poll()
        self.assertFalse(self.panel._value("history"))

    # ---- the slider ----

    def test_drag_writes_once_on_release_and_refresh_never_moves_it(self):
        track = self.control("idle_opacity")
        self.mouse(QEvent.Type.MouseButtonPress, track.center())
        self.assertIsNotNone(self.panel._drag)
        # Dragged past the panel's right edge: it stops at 100%.
        self.mouse(QEvent.Type.MouseMove, QPointF(track.right() + 200, track.center().y()))
        self.assertEqual(self.panel._drag, 1.0)
        # Meanwhile config.json changes (Notepad): the 0.5s refresh must not move the slider.
        set_setting("language", "auto", path=self.path)
        set_setting("idle_opacity", 0.9, section="pill", path=self.path)
        self.panel._poll()
        self.assertEqual(self.panel._opacity(), 1.0)
        self.assertEqual(self.settings()["pill"]["idle_opacity"], 0.9)  # nothing written while dragging
        self.mouse(QEvent.Type.MouseButtonRelease, QPointF(track.right() + 200, track.center().y()),
                   Qt.MouseButton.NoButton)
        self.assertIsNone(self.panel._drag)
        settings = self.settings()
        self.assertEqual(settings["pill"]["idle_opacity"], 1.0)
        self.assertEqual(settings["language"], "auto")  # the other edit is kept

    def test_drag_to_the_far_left_stops_at_the_minimum(self):
        track = self.control("idle_opacity")
        self.mouse(QEvent.Type.MouseButtonPress, track.center())
        self.mouse(QEvent.Type.MouseMove, QPointF(track.left() - 300, track.center().y()))
        self.mouse(QEvent.Type.MouseButtonRelease, QPointF(track.left() - 300, track.center().y()),
                   Qt.MouseButton.NoButton)
        self.assertEqual(self.settings()["pill"]["idle_opacity"], settings_module.OPACITY_MIN)

    # ---- Quit, Notepad, a busy file ----

    def test_quit_takes_two_clicks(self):
        self.click("quit")
        self.on_quit.assert_not_called()
        self.assertTrue(self.panel._quit_armed_until)
        self.click("quit")
        self.on_quit.assert_called_once()

    def test_quit_disarms_after_3_seconds(self):
        self.click("quit")
        self.panel._quit_armed_until = 1.0  # long ago
        self.panel._poll()
        self.assertFalse(self.panel._quit_armed_until)
        self.click("quit")
        self.on_quit.assert_not_called()

    def test_open_in_notepad(self):
        with mock.patch.object(settings_module.subprocess, "Popen") as popen:
            self.click("notepad")
        popen.assert_called_once_with(["notepad.exe", str(self.path)])
        self.assertFalse(self.panel.is_open)

    def test_a_busy_file_says_so_and_keeps_running(self):
        busy = ConfigError("config.json is busy (another program has it open), so the change wasn't saved.")
        with mock.patch.object(settings_module, "set_setting", side_effect=busy):
            self.click("history")
        self.assertEqual(self.panel._note, "Not saved: config.json is busy, try again")
        self.assertTrue(self.settings()["history"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), json.loads(CONFIG))

    def test_the_footer_shows_the_version(self):
        painted = []
        with mock.patch.object(self.panel, "_text", side_effect=lambda painter, rect, text, *a, **k: painted.append(text)):
            self.panel._paint_footer(mock.Mock(), *self.panel._layout()["footer"])
        self.assertIn(f"Blurt {settings_module.VERSION} · More in config.json", painted)

    def test_no_start_with_windows_row_without_autostart(self):
        self.assertNotIn("start_with_windows", self.panel._layout()["rows"])

    # ---- the Shortcut row (M14) ----

    def hint(self, name):
        return dict((row, hint) for group in self.panel._groups() for row, _, _, hint in group)[name]

    def shortcut_parts(self):
        return self.panel._shortcut_rects(self.panel._layout()["rows"]["hotkey"][1])

    def option(self, text):
        """Where to click to pick a shortcut in the open list."""
        rects = {value: rect for value, _, rect in self.shortcut_parts()["options"]}
        return rects[text].center()

    def target(self, pos):
        return self.panel._content_target(pos)

    def test_the_row_comes_first_and_names_the_shortcut(self):
        self.assertEqual(self.panel._groups()[0][0][0], "hotkey")  # no API key row in these tests
        self.assertEqual(self.hint("hotkey"), "Hold Ctrl + Win to dictate")
        self.assertEqual(self.target(self.control("hotkey").center()), "hotkey")

    def test_change_opens_the_list_and_cancel_writes_nothing(self):
        before = self.path.read_bytes()
        self.click("hotkey")
        self.assertTrue(self.panel._shortcut_open)
        self.assertEqual([name for name, *_ in self.panel._groups()[0]], ["hotkey"])  # the only card now
        self.assertEqual(len(self.panel._groups()), 1)
        self.assertEqual(self.panel._subtitle(), settings_module.SHORTCUT_SUBTITLE)
        self.assertEqual(self.panel._shortcut_pick, "Ctrl+Win")  # the one in use
        self.assertIsNone(self.target(self.shortcut_parts()["save"].center()))  # Save is off
        self.click(self.target(self.option("Alt+Win")))
        self.assertEqual(self.target(self.shortcut_parts()["save"].center()), "hotkey_save")
        self.click(self.target(self.shortcut_parts()["cancel"].center()))
        self.assertFalse(self.panel._shortcut_open)
        self.assertIsNone(self.panel._shortcut_pick)
        self.assertEqual(self.panel._subtitle(), "Changes apply right away")
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_writes_only_the_hotkey_in_its_canonical_form(self):
        self.click("hotkey")
        for text in ("Ctrl+Alt+Win", "Alt+Shift+Win"):  # the last pick counts
            self.click(self.target(self.option(text)))
        self.click(self.target(self.shortcut_parts()["save"].center()))
        self.assertFalse(self.panel._shortcut_open)
        # CONFIG has no hotkey (like every config.json from before M14): it's added, the rest untouched.
        expected = json.loads(CONFIG)
        expected["hotkey"] = "Alt+Shift+Win"
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith(CONFIG[:-3]))  # only added at the end
        self.assertEqual(self.hint("hotkey"), "Hold Alt + Shift + Win to dictate")

    def test_every_option_can_be_picked(self):
        self.click("hotkey")
        for families in shortcut.ALLOWED:
            text = shortcut.text(families)
            self.assertEqual(self.target(self.option(text)), ("hotkey_option", text))

    def test_the_list_is_shorter_than_the_closed_panel(self):
        # So the window (sized once for the tallest state) never has to grow for it.
        closed = self.panel._content_height()
        self.click("hotkey")
        self.assertLess(self.panel._content_height(), closed)
        self.assertLessEqual(self.panel._content_height(), self.panel._max_content_height())

    def test_closing_the_panel_drops_the_pick(self):
        self.click("hotkey")
        self.click(self.target(self.option("Alt+Win")))
        self.panel._on_close()
        self.panel._on_open()
        self.assertFalse(self.panel._shortcut_open)
        self.assertIsNone(self.panel._shortcut_pick)
        self.assertEqual(self.settings()["hotkey"], "Ctrl+Win")

    def write_hotkey(self, value):
        set_setting("hotkey", value, path=self.path)  # "Notepad" or config_tool writes it
        self.panel._poll()

    def test_a_value_that_is_not_allowed_shows_ctrl_win_with_a_warning(self):
        for value in ("Ctrl+Shift", 5, "Ctrl+Q"):
            with self.subTest(value=value):
                self.write_hotkey(value)
                self.assertTrue(self.panel._hotkey_problem)
                self.assertEqual(self.hint("hotkey"), "Not allowed in config.json: using Ctrl + Win")
                # Save is on for Ctrl+Win too, so picking it writes a clean value.
                self.click("hotkey")
                self.assertEqual(self.target(self.shortcut_parts()["save"].center()), "hotkey_save")
                self.click("hotkey_save")
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["hotkey"], "Ctrl+Win")
                self.assertFalse(self.panel._hotkey_problem)
                self.assertEqual(self.hint("hotkey"), "Hold Ctrl + Win to dictate")

    def test_an_allowed_value_in_another_form_is_fine(self):
        self.write_hotkey("win + ALT")
        self.assertFalse(self.panel._hotkey_problem)
        self.assertEqual(self.hint("hotkey"), "Hold Alt + Win to dictate")

    def test_a_notepad_edit_while_the_list_is_open_keeps_the_pick(self):
        self.click("hotkey")
        self.click(self.target(self.option("Ctrl+Shift+Win")))
        self.write_hotkey("Alt+Win")
        self.assertTrue(self.panel._shortcut_open)
        self.assertEqual(self.panel._shortcut_pick, "Ctrl+Shift+Win")
        self.assertEqual(self.panel._hotkey(), "Alt+Win")  # "in use" follows the file

    def test_the_file_breaking_while_the_list_is_open_closes_it(self):
        self.click("hotkey")
        self.break_file()
        self.assertFalse(self.panel._shortcut_open)
        self.assertEqual(self.hint("hotkey"), settings_module.SHORTCUT_BROKEN_HINT)
        self.assertIsNone(self.target(self.control("hotkey").center()))

    def test_a_busy_file_keeps_the_list_open(self):
        self.click("hotkey")
        self.click(self.target(self.option("Alt+Win")))
        busy = ConfigError("config.json is busy (another program has it open), so the change wasn't saved.")
        with mock.patch.object(settings_module, "set_setting", side_effect=busy):
            self.click("hotkey_save")
        self.assertEqual(self.panel._note, "Not saved: config.json is busy, try again")
        self.assertTrue(self.panel._shortcut_open)
        self.assertEqual(self.panel._shortcut_pick, "Alt+Win")
        self.assertEqual(self.settings()["hotkey"], "Ctrl+Win")

    def test_save_reaches_the_hook_the_same_way_as_a_notepad_edit(self):
        """End to end: the panel only writes config.json. The worker's ConfigWatcher sees it
        and main.apply_new_hotkey asks the hook, which switches once no key is held."""
        import main
        from app.config import ConfigWatcher
        from tests.test_hotkey import Keyboard

        watcher = ConfigWatcher(self.path)
        kb = Keyboard(self)
        kb.down(LCTRL)  # held from before Save
        self.click("hotkey")
        self.click(self.target(self.option("Alt+Win")))
        self.click("hotkey_save")
        changes = []
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(main.apply_new_hotkey(watcher.changed(), kb.hotkey, changes.append))
        self.assertEqual(changes, ["Alt+Win"])  # the pill's label and the tray's tooltip
        self.assertEqual(kb.hotkey.requested, {"Alt", "Win"})
        self.assertTrue(kb.down(LWIN))  # Ctrl still held from before: the old Ctrl+Win, once
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        kb.down(LALT)  # nothing held: the new shortcut from here on
        self.assertEqual(kb.hotkey.shortcut, {"Alt", "Win"})
        self.assertTrue(kb.down(LWIN))
        kb.up(LWIN)
        kb.up(LALT)
        self.assertEqual(kb.sent(), ["start", "stop"])


LCTRL, LALT, LWIN = 0xA2, 0xA4, 0x5B


class FakeAutostart:
    """Start with Windows without the registry."""

    def __init__(self, on=False, installed=True, fail=False):
        self.on, self.installed, self.fail = on, installed, fail
        self.writes = []

    def available(self):
        return self.installed

    def enabled(self):
        return self.on

    def set_enabled(self, on):
        if self.fail:
            raise PermissionError("access denied")
        self.writes.append(on)
        self.on = on


class StartWithWindowsTest(SettingsPanelTest):
    """Every test above again with the Start with Windows row (the rows below it move), plus its own."""

    def make_autostart(self):
        self.autostart = FakeAutostart(on=True)
        return self.autostart

    def test_no_start_with_windows_row_without_autostart(self):
        self.assertIn("start_with_windows", self.panel._layout()["rows"])  # this panel has one

    def test_the_row_is_last_in_the_first_card(self):
        names = [name for name, *_ in self.panel._groups()[0]]
        self.assertEqual(names[-1], "start_with_windows")

    def test_the_toggle_turns_it_off_and_on(self):
        target = self.panel._content_target(self.control("start_with_windows").center())
        self.assertEqual(target, "start_with_windows")
        self.click(target)
        self.assertFalse(self.panel._autostart_on)
        self.click(target)
        self.assertEqual(self.autostart.writes, [False, True])
        self.assertTrue(self.panel._autostart_on)

    def test_it_never_touches_config_json(self):
        before = self.path.read_bytes()
        self.click("start_with_windows")
        self.assertEqual(self.path.read_bytes(), before)

    def test_it_works_while_config_json_is_broken(self):
        self.break_file()
        self.assertEqual(self.panel._content_target(self.control("start_with_windows").center()), "start_with_windows")
        self.click("start_with_windows")
        self.assertEqual(self.autostart.writes, [False])

    def test_task_manager_turning_it_off_shows_up(self):
        self.autostart.on = False  # "Task Manager" disables it
        self.panel._poll()
        self.assertFalse(self.panel._autostart_on)

    def test_a_failed_change_says_so(self):
        self.autostart.fail = True
        out = self.click("start_with_windows")
        self.assertIn("not changed", out)
        self.assertEqual(self.panel._note, "Not saved: couldn't change Start with Windows")
        self.assertTrue(self.panel._autostart_on)


class StartWithWindowsFromSourceTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "config.json"
        path.write_text(CONFIG, encoding="utf-8")
        self.autostart = FakeAutostart(on=True, installed=False)
        self.panel = SettingsPanel(path, autostart=self.autostart)
        self.panel.set_settings(dict(DEFAULTS["pill"]))
        self.addCleanup(self.panel.deleteLater)
        self.panel._open = True
        self.panel._on_open()
        self.addCleanup(self.panel._live_timer.stop)

    def test_shown_locked_with_its_hint(self):
        row = dict((name, hint) for name, _, _, hint in self.panel._groups()[0])
        self.assertEqual(row["start_with_windows"], settings_module.AUTOSTART_SOURCE_HINT)
        control = self.panel._layout()["rows"]["start_with_windows"][2]
        self.assertIsNone(self.panel._content_target(control.center()))

    def test_it_reads_off_and_never_asks_the_registry(self):
        self.assertFalse(self.panel._autostart_on)  # even though the fake says on
        self.assertEqual(self.autostart.writes, [])


class ScrolledSettingsTest(unittest.TestCase):
    """M16: on a small screen the card stops at the screen's top and the content scrolls.
    The rows installed users see (API key, Start with Windows), on the 614px screen (1366 x 768
    at 125%), with real mouse and wheel events. Measured with the real Windows fonts only."""

    SCREEN = 614

    @classmethod
    def setUpClass(cls):
        real_fonts.require_real_fonts()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.on_quit = mock.Mock()
        self.on_change_key = mock.Mock()
        self.panel = SettingsPanel(self.path, on_quit=self.on_quit, key_status=lambda: "windows",
                                   on_change_key=self.on_change_key, autostart=FakeAutostart(on=True))
        self.panel.set_settings(dict(DEFAULTS["pill"]))  # scale 1: widget pixels are design pixels
        self.addCleanup(self.panel.deleteLater)
        self.anchor(self.SCREEN)
        self.panel._open = True  # like open_panel(), without showing a window
        self.panel._on_open()
        self.addCleanup(self.panel._live_timer.stop)
        self.assertGreater(self.panel._max_scroll(), 100)  # it really scrolls here

    def anchor(self, height):
        self.panel.set_anchor(960, height - 94, 0)  # a 48px taskbar, the gap and the 34px hover pill

    def scroll(self, value):
        self.panel._scroll_to(value, animate=False)
        return self.panel._scroll.value

    def mouse(self, kind, pos, buttons=Qt.MouseButton.LeftButton):
        event = QMouseEvent(kind, QPointF(pos), QPointF(pos), Qt.MouseButton.LeftButton, buttons,
                            Qt.KeyboardModifier.NoModifier)
        handler = {QEvent.Type.MouseButtonPress: self.panel.mousePressEvent,
                   QEvent.Type.MouseMove: self.panel.mouseMoveEvent,
                   QEvent.Type.MouseButtonRelease: self.panel.mouseReleaseEvent}[kind]
        with contextlib.redirect_stdout(io.StringIO()):
            handler(event)

    def press_and_release(self, pos):
        self.mouse(QEvent.Type.MouseButtonPress, pos)
        self.mouse(QEvent.Type.MouseButtonRelease, pos, Qt.MouseButton.NoButton)

    def wheel(self, notches):
        """Negative = down, towards the footer."""
        center = QPointF(self.panel.width() / 2, self.panel.height() / 2)
        event = QWheelEvent(center, center, QPoint(0, 0), QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.panel.wheelEvent(event)
        return event.isAccepted()

    def control(self, name):
        return self.panel._layout()["rows"][name][2]

    def visible(self, pos):
        view = self.panel._view_rect()
        return view.top() <= pos.y() <= view.bottom()

    def settings(self):
        return load_config(self.path)

    def assert_height_fresh(self, what):
        """The scroll's idea of the content height (_total_h, kept by _fit_scroll) must match the
        real one: a stale, taller _total_h shows a scroll bar and faded edges on a card that fits."""
        self.assertEqual(self.panel._total_h, self.panel._content_height(), what)

    # ---- the wheel ----

    def test_the_wheel_scrolls_to_the_end_and_no_further(self):
        self.assertTrue(self.wheel(-1))
        self.assertEqual(self.panel._scroll.end, SCROLL_STEP)
        self.wheel(-20)
        self.assertEqual(self.panel._scroll.end, self.panel._max_scroll())
        self.wheel(20)
        self.assertEqual(self.panel._scroll.end, 0)

    def test_the_wheel_does_nothing_when_everything_fits(self):
        self.anchor(1080)
        self.assertEqual(self.panel._max_scroll(), 0)
        self.assertFalse(self.wheel(-1))
        self.assertEqual(self.panel._scroll.end, 0)

    def test_the_wheel_does_nothing_while_the_slider_is_held(self):
        at = self.scroll(self.panel._max_scroll())
        track = self.control("idle_opacity")
        self.mouse(QEvent.Type.MouseButtonPress, track.center())
        self.assertIsNotNone(self.panel._drag)
        self.assertTrue(self.wheel(3))  # swallowed
        self.assertEqual(self.panel._scroll.end, at)
        self.mouse(QEvent.Type.MouseButtonRelease, track.center(), Qt.MouseButton.NoButton)
        self.assertTrue(self.wheel(1))  # released: it scrolls again
        self.assertEqual(self.panel._scroll.end, at - SCROLL_STEP)

    # ---- clicks after scrolling ----

    def test_quit_at_the_end_takes_two_clicks(self):
        self.scroll(self.panel._max_scroll())
        quit_rect = self.panel._layout()["footer"][1]
        self.assertTrue(self.visible(quit_rect.center()))
        self.press_and_release(quit_rect.center())
        self.on_quit.assert_not_called()
        self.press_and_release(self.panel._layout()["footer"][1].center())  # "Quit?" is a bit wider
        self.on_quit.assert_called_once()

    def test_the_slider_saves_the_value_under_the_mouse(self):
        self.scroll(self.panel._max_scroll())
        track = self.control("idle_opacity")
        self.assertTrue(self.visible(track.center()))
        start = QPointF(track.left() + 0.25 * track.width(), track.center().y())
        end = QPointF(track.left() + 0.75 * track.width(), track.center().y())
        self.mouse(QEvent.Type.MouseButtonPress, start)
        self.mouse(QEvent.Type.MouseMove, end)
        self.mouse(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)
        expected = round(settings_module.OPACITY_MIN + 0.75 * (settings_module.OPACITY_MAX - settings_module.OPACITY_MIN), 2)
        self.assertEqual(self.settings()["pill"]["idle_opacity"], expected)

    def test_change_after_scrolling_then_alt_win_and_save(self):
        unscrolled = self.control("hotkey").center()
        self.scroll(30)
        button = self.control("hotkey").center()
        self.assertEqual(button.y(), unscrolled.y() - 30)
        self.assertTrue(self.visible(button))
        self.press_and_release(button)
        self.assertTrue(self.panel._shortcut_open)
        self.assert_height_fresh("after Change")
        self.assertEqual(self.panel._scroll.value, 0)  # the list fits: back in range
        parts = self.panel._shortcut_rects(self.panel._layout()["rows"]["hotkey"][1])
        self.press_and_release(dict((text, rect) for text, _, rect in parts["options"])["Alt+Win"].center())
        self.press_and_release(parts["save"].center())
        self.assertFalse(self.panel._shortcut_open)
        self.assert_height_fresh("after Save")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["hotkey"], "Alt+Win")

    def test_the_banners_open_in_notepad_after_scrolling(self):
        self.path.write_text(CONFIG.replace('"en",', '"en"'), encoding="utf-8")
        self.panel.reload()
        unscrolled = self.panel._banner_button_rect().center()
        self.scroll(40)
        button = self.panel._banner_button_rect().center()
        self.assertEqual(button.y(), unscrolled.y() - 40)
        self.assertTrue(self.visible(button))
        with mock.patch.object(settings_module.subprocess, "Popen") as popen:
            self.press_and_release(button)
        popen.assert_called_once_with(["notepad.exe", str(self.path)])

    def test_a_click_where_a_row_used_to_be_doesnt_toggle_it(self):
        old = self.control("history").center()
        self.scroll(60)
        self.assertNotEqual(self.panel._content_target(old), "history")
        self.press_and_release(old)
        self.assertTrue(self.settings()["history"])
        self.press_and_release(self.control("history").center())  # where it is now
        self.assertFalse(self.settings()["history"])

    def test_the_header_hides_a_row_scrolled_under_it(self):
        # Scroll the API key's Change button up behind the header (10px above the visible part).
        view_top = self.panel._view_rect().top()
        self.scroll(self.control("api_key").center().y() - (view_top - 10))
        button = self.control("api_key").center()
        self.assertLess(button.y(), view_top)
        self.assertFalse(self.panel._close_rect().contains(button))
        self.assertFalse(self.panel._notepad_rect().contains(button))
        self.assertIsNone(self.panel._content_target(button))
        self.press_and_release(button)
        self.on_change_key.assert_not_called()
        self.assertTrue(self.panel.is_open)

    def test_below_the_card_is_not_a_hidden_row_either(self):
        # The footer, unscrolled, is below the visible part: Quit can't be clicked there.
        quit_rect = self.panel._layout()["footer"][1]
        self.assertFalse(self.visible(quit_rect.center()))
        self.assertIsNone(self.panel._content_target(quit_rect.center()))

    # ---- the content changing while scrolled ----

    def test_opening_the_list_at_the_bottom_brings_the_scroll_back(self):
        self.scroll(self.panel._max_scroll())
        self.panel._content_release("hotkey")
        self.assertTrue(self.panel._shortcut_open)
        self.assert_height_fresh("after Change")
        self.assertLessEqual(self.panel._scroll.value, self.panel._max_scroll())
        self.panel._content_release("hotkey_cancel")
        self.assertFalse(self.panel._shortcut_open)
        self.assert_height_fresh("after Cancel")

    def test_the_file_breaking_and_getting_fixed_while_scrolled(self):
        fine_max = self.scroll(self.panel._max_scroll())
        self.path.write_text(CONFIG.replace('"en",', '"en"'), encoding="utf-8")
        self.panel._poll()  # the banner appears: more to scroll, the position is kept
        self.assertIsNotNone(self.panel._problem)
        self.assert_height_fresh("after the file broke")
        self.assertGreater(self.panel._max_scroll(), fine_max)
        self.assertEqual(self.panel._scroll.value, fine_max)
        self.scroll(self.panel._max_scroll())
        self.path.write_text(CONFIG, encoding="utf-8")
        self.panel._poll()  # it goes away: back in range
        self.assertIsNone(self.panel._problem)
        self.assert_height_fresh("after the file was fixed")
        self.assertEqual(self.panel._max_scroll(), fine_max)
        self.assertEqual(self.panel._scroll.value, fine_max)

    def test_it_opens_at_the_top(self):
        self.scroll(self.panel._max_scroll())
        self.panel._on_close()
        self.panel._on_open()
        self.assertEqual(self.panel._scroll.value, 0)


if __name__ == "__main__":
    unittest.main()
