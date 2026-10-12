"""Tests for the tray: the 80% notification, its testing switch, and giving focus back.

No real tray icon clicks, Windows focus or notifications: those parts are faked.
Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_tray -v
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.ui import tray as tray_module
from app.ui.tray import ICON_SIZES, Tray, tray_icon
from tests.test_usage import meters_at

app = QApplication.instance() or QApplication([])


class FakeTracker:
    def __init__(self):
        self.meters = meters_at()
        self.rate_limit = None  # snapshot()["rate_limit"]: the last 429, while it's in force

    def snapshot(self):
        return {"meters": self.meters, "rate_limit": self.rate_limit}


def cap(kind, name, wait, left=None, until=10_000.0):
    """A snapshot()["rate_limit"]: Groq asked to wait `wait` seconds, `left` of them are left."""
    left = wait if left is None else left
    return {"kind": kind, "type": "TPD" if kind == "cleanup" else "ASD", "name": name,
            "until": until, "wait": wait, "retry_in": left}


class TrayTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config.json"
        self.config.write_text('{\n  "debug_fake_limit_warning": null\n}\n')
        for patch in (
            mock.patch.object(tray_module, "CONFIG_PATH", self.config),
            mock.patch("app.config.CONFIG_PATH", self.config),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.tracker = FakeTracker()
        self.pill = mock.Mock()
        self.on_pause = mock.Mock()
        self.tray = Tray(self.tracker, self.pill, on_pause=self.on_pause, on_quit=mock.Mock())
        self.shown = []
        self.tray._icon.showMessage = lambda title, text, *rest: self.shown.append((title, text))
        self.tray._icon.show = lambda: None  # no real icon in the tray while testing
        self.addCleanup(self.tray.hide)

    def check(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.tray._check_limits()
        return out.getvalue()

    def test_notifies_once_when_a_meter_crosses_80(self):
        self.tray.start()
        self.tracker.meters = meters_at(cleanup_tokens=81)
        self.check()
        self.check()
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.shown[0][0], "Groq limit almost used up")
        self.assertIn("Cleanup tokens, daily limit: 81%", self.shown[0][1])

    def test_already_high_at_startup_is_quiet(self):
        self.tracker.meters = meters_at(audio_day=90)
        self.tray.start()
        self.check()
        self.assertEqual(self.shown, [])

    def test_fake_warning_fires_once_and_switches_itself_off(self):
        self.tray.start()
        self.check()  # config.json unchanged: nothing
        self.config.write_text('{\n  "debug_fake_limit_warning": "audio_hour"\n}\n')
        out = self.check()
        self.assertIn("FAKE LIMIT WARNING for audio_hour", out)
        self.assertEqual(len(self.shown), 1)
        self.assertIn("Audio, hourly limit: 85%", self.shown[0][1])
        self.assertEqual(self.config.read_text(), '{\n  "debug_fake_limit_warning": null\n}\n')
        self.check()
        self.check()  # the real meter is back to 0%: armed again, nothing shown
        self.assertEqual(len(self.shown), 1)

    def test_fake_warning_with_a_wrong_name_explains(self):
        self.tray.start()
        self.config.write_text('{"debug_fake_limit_warning": "tokens"}')
        out = self.check()
        self.assertIn("isn't a meter", out)
        self.assertEqual(self.shown, [])

    def test_pause_menu_follows_the_state(self):
        self.tray._pause_action.trigger()
        self.on_pause.assert_called_once_with(True)
        self.tray.set_paused(True)
        self.assertEqual(self.tray._pause_action.text(), "Resume dictation")
        self.assertEqual(self.tray._icon.toolTip(), "Blurt: paused")

    def test_notification_click_opens_usage(self):
        self.tray.start()
        self.tracker.meters = meters_at(cleanup_tokens=81)
        self.check()
        self.tray._icon.messageClicked.emit()
        self.pill.open_panel.assert_called_once_with("usage")

    def test_config_problem_notification(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tray.show_config_problem("config.json has a mistake on line 3: probably a missing comma.")
        self.assertEqual(self.shown, [("Running on default settings",
                                       "config.json has a mistake on line 3: probably a missing comma.")])
        self.tray._icon.messageClicked.emit()
        self.pill.open_panel.assert_called_once_with("settings")  # shows the mistake, with Open in Notepad

    def test_the_shortcut_in_the_tooltip_and_notification(self):
        self.assertEqual(self.tray._icon.toolTip(), "Blurt (hold Ctrl+Win)")  # the default, as before M14
        with contextlib.redirect_stdout(io.StringIO()):
            self.tray.show_already_running()
        self.assertEqual(self.shown[-1][1], "Hold Ctrl+Win to dictate. Its icon is next to the clock.")
        self.tray.set_shortcut("Alt+Shift+Win")
        self.assertEqual(self.tray._icon.toolTip(), "Blurt (hold Alt+Shift+Win)")
        with contextlib.redirect_stdout(io.StringIO()):
            self.tray.show_already_running()
        self.assertEqual(self.shown[-1][1], "Hold Alt+Shift+Win to dictate. Its icon is next to the clock.")
        tray = Tray(self.tracker, self.pill, on_pause=self.on_pause, on_quit=mock.Mock(), hotkey="Alt+Win")
        self.addCleanup(tray.hide)
        self.assertEqual(tray._icon.toolTip(), "Blurt (hold Alt+Win)")

    # ---- the "Groq limit used up" notification ----

    def test_transcription_cap_with_history_on(self):
        self.tray.start()
        self.tracker.rate_limit = cap("transcription", "audio seconds per day", 2460)
        out = self.check()
        self.assertEqual(self.shown, [("Groq limit used up", "Groq's free limit is used up (audio seconds per day). "
                                       "Dictation works again in about 41m. Until then your dictations wait in "
                                       "History for Retry.")])
        self.assertIn("[tray] notification: Groq limit used up.", out)
        self.tray._icon.messageClicked.emit()
        self.pill.open_panel.assert_called_once_with("usage")  # the countdown is there

    def test_transcription_cap_with_history_off(self):
        self.config.write_text('{"history": false}')
        self.tray.start()
        self.tracker.rate_limit = cap("transcription", "audio seconds per day", 2460)
        self.check()
        self.assertEqual(len(self.shown), 1)
        self.assertTrue(self.shown[0][1].endswith("Until then your dictations can't be kept, because History is off."))

    def test_cleanup_cap(self):
        self.tray.start()
        self.tracker.rate_limit = cap("cleanup", "tokens per day", 2460)
        self.check()
        self.assertEqual(self.shown, [("Groq limit used up", "Groq's free limit is used up (tokens per day). "
                                       "Until it refills (about 41m), Blurt pastes your words without cleanup.")])

    def test_cap_once_per_limit(self):
        self.tray.start()
        self.tracker.rate_limit = cap("cleanup", "tokens per day", 2460)
        self.check()
        self.tracker.rate_limit = cap("cleanup", "tokens per day", 2460, left=2458)  # the next 2s poll
        self.check()
        self.tracker.rate_limit = cap("cleanup", "tokens per day", 2400, until=10_060.0)  # hit again later
        self.check()
        self.assertEqual(len(self.shown), 1)

    def test_no_cap_notification_under_60s(self):
        self.tray.start()
        for wait in (2, 20, 30, 59):
            self.tracker.rate_limit = cap("transcription", "requests per minute", wait, until=10_000.0 + wait * 100)
            self.check()
        self.assertEqual(self.shown, [])

    # ---- the quiet mic notification (main.py's QuietMic decides when) ----

    def test_quiet_mic_notification_opens_sound_settings(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tray.show_quiet_mic()
        self.assertEqual(self.shown, [("Blurt didn't hear you", "If you were speaking, your mic may be too quiet: "
                                       "raise its input volume in Windows Sound settings. Click here to open them.")])
        with mock.patch.object(tray_module.QDesktopServices, "openUrl") as open_url:
            self.tray._icon.messageClicked.emit()
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), "ms-settings:sound")
        self.pill.open_panel.assert_not_called()


class FocusBackTest(unittest.TestCase):
    """_give_focus_back with Windows' focus faked. Windows: 1 = ours, 2 = taskbar, 3 = Notepad, 4 = Opera."""

    def setUp(self):
        self.foreground = 1
        self.set_to = []

        def set_foreground(hwnd):
            if not hwnd:
                return False  # like the real one: no window, nothing happens
            self.set_to.append(hwnd)
            self.foreground = hwnd
            return True

        patches = [
            mock.patch.object(tray_module.win32, "foreground_window", lambda: self.foreground),
            mock.patch.object(tray_module.win32, "is_own_window", lambda hwnd: hwnd == 1),
            mock.patch.object(tray_module.win32, "is_taskbar", lambda hwnd: hwnd == 2),
            mock.patch.object(tray_module.win32, "set_foreground", set_foreground),
            mock.patch.object(tray_module, "window_exe", lambda hwnd: "notepad.exe"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.tray = Tray(FakeTracker(), mock.Mock(), on_pause=mock.Mock(), on_quit=mock.Mock())
        self.addCleanup(self.tray.hide)

    def give_back(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.tray._give_focus_back()
        return out.getvalue()

    def test_remembers_your_window_not_ours_or_the_taskbar(self):
        for hwnd in (3, 2, 1):  # Notepad, then the tray click, then our menu
            self.foreground = hwnd
            self.tray._remember_window()
        self.assertEqual(self.tray._last_window, 3)

    def test_menu_closed_on_our_window_gives_focus_back(self):
        self.tray._last_window = 3
        self.foreground = 1
        out = self.give_back()
        self.assertEqual(self.set_to, [3])
        self.assertNotIn("FOCUS STOLEN", out)

    def test_menu_closed_on_the_taskbar_gives_focus_back(self):
        self.tray._last_window = 3
        self.foreground = 2
        self.give_back()
        self.assertEqual(self.set_to, [3])

    def test_clicking_another_window_to_close_the_menu_is_left_alone(self):
        self.tray._last_window = 3
        self.foreground = 4
        self.give_back()
        self.assertEqual(self.set_to, [])

    def test_warns_if_focus_stays_with_us(self):
        self.tray._last_window = None  # no window to go back to
        self.foreground = 1
        self.assertIn("FOCUS STOLEN", self.give_back())


class IconTest(unittest.TestCase):
    def test_every_size_for_both_looks(self):
        for paused in (False, True):
            sizes = {size.width() for size in tray_icon(paused).availableSizes()}
            self.assertEqual(sizes, set(ICON_SIZES))


if __name__ == "__main__":
    unittest.main()
