"""Tests for the API key window and what it's wired to (app/ui/key_window.py): the check,
saving, the language, pausing, and the key rows and notifications in Settings and the tray.

Nothing is shown on screen, focus is never moved, Groq and Credential Manager are
faked, and config.json is a temporary copy. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_key_window -v
"""

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import main
from app import keystore
from app.config import DEFAULTS
from app.ui import key_window as key_module
from app.ui import tray as tray_module
from app.ui.settings_panel import SettingsPanel
from app.ui.tray import Tray
from tests import real_fonts
from tests.test_usage import meters_at

app = QApplication.instance() or QApplication([])
KEY = "gsk_FAKEkeyForTheKeyWindowTests0123456789"


class OffScreenKeyWindow(key_module.KeyWindow):
    """The real window, never drawn on the screen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)


def wait_for(condition, seconds=2.0):
    """Runs Qt's queued calls (the check's answer comes from another thread) until condition() holds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


class KeyFlowTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(key_module, "KeyWindow", OffScreenKeyWindow),
            mock.patch.object(key_module.win32, "set_foreground", lambda hwnd: False),
            mock.patch.object(key_module.win32, "foreground_window", lambda: 0),
            mock.patch.object(key_module, "CLOSE_DELAY_MS", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.applied = []
        self.pause = main.PauseReasons(self.applied.append)
        self.pause.set("no_key", True)
        self.answer = "works"
        self.checked = []
        self.saved = []
        self.languages = []
        self.new_keys = []
        self.release = threading.Event()
        self.release.set()

        def check(key):
            self.release.wait(2)
            self.checked.append(key)
            return self.answer

        self.flow = key_module.KeyFlow(self.pause, on_new_key=self.new_key, focus_home=lambda: None,
                                       check=check, save=self.saved.append, set_language=self.languages.append,
                                       current_language=lambda: self.language)
        self.language = "en"
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(self.flow.close)

    def new_key(self, key):
        self.new_keys.append(key)
        self.pause.set("no_key", False)

    def type_and_save(self, key=KEY):
        window = self.flow.window
        window.field.setText(key)
        window._edited()
        window.save_button.click()

    def test_opening_pauses_and_closing_resumes_only_that(self):
        self.pause.set("no_key", False)
        self.pause.user_toggle(True)
        self.flow.open()
        self.assertIn("key_window", self.pause.reasons)
        self.flow.close()
        self.assertTrue(wait_for(lambda: not self.flow.is_open()))
        self.assertEqual(self.pause.reasons, {"user"})  # your own pause stays

    def test_a_working_key_is_saved_and_used(self):
        self.flow.open()
        self.type_and_save()
        self.assertTrue(wait_for(lambda: not self.flow.is_open()))
        self.assertEqual(self.checked, [KEY])
        self.assertEqual(self.saved, [KEY])
        self.assertEqual(self.new_keys, [KEY])
        self.assertEqual(self.flow.key_source, "windows")
        self.assertFalse(self.pause.paused)
        self.assertEqual(self.languages, [])  # English was already set: config.json isn't touched

    def test_a_rejected_key_is_not_saved(self):
        self.answer = "rejected"
        self.flow.open()
        self.type_and_save()
        self.assertTrue(wait_for(lambda: self.flow.window.status == "rejected"))
        self.assertEqual(self.saved, [])
        self.assertTrue(self.pause.paused)
        self.flow.window.field.setText(KEY + "x")  # editing clears the old answer
        self.flow.window._edited()
        self.assertEqual(self.flow.window.status, "empty")

    def test_save_anyway_skips_the_second_check(self):
        self.answer = "unreachable"
        self.flow.open()
        self.type_and_save()
        self.assertTrue(wait_for(lambda: self.flow.window.status == "unreachable"))
        self.assertEqual(self.flow.window.save_button.text(), "Save anyway")
        self.flow.window.save_button.click()
        self.assertTrue(wait_for(lambda: not self.flow.is_open()))
        self.assertEqual(self.checked, [KEY])
        self.assertEqual(self.saved, [KEY])

    def test_the_language_is_saved_with_the_key(self):
        self.flow.open()
        self.flow.window.language_buttons["auto"].click()
        self.type_and_save()
        self.assertTrue(wait_for(lambda: not self.flow.is_open()))
        self.assertEqual(self.languages, ["auto"])

    def test_a_hand_set_language_stays(self):
        self.language = "bn"
        self.flow.open()
        buttons = self.flow.window.language_buttons
        self.assertEqual(buttons["bn"].text(), "Bengali")
        self.assertTrue(buttons["bn"].isChecked())
        self.type_and_save()
        self.assertTrue(wait_for(lambda: not self.flow.is_open()))
        self.assertEqual(self.languages, [])  # not switched to English behind your back

    def test_credential_manager_saying_no(self):
        def broken(key):
            raise keystore.KeyStoreError("couldn't save the API key (Windows error 5)")
        self.flow._save = broken
        self.flow.open()
        self.type_and_save()
        self.assertTrue(wait_for(lambda: self.flow.window.status == "save_failed"))
        self.assertEqual(self.new_keys, [])
        self.assertTrue(self.pause.paused)

    def test_closing_while_checking_ignores_the_answer(self):
        self.release.clear()  # the check waits
        self.flow.open()
        self.type_and_save()
        self.assertEqual(self.flow.window.status, "checking")
        self.flow.close()
        self.release.set()
        self.assertTrue(wait_for(lambda: bool(self.checked)))
        wait_for(lambda: False, 0.1)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.pause.reasons, {"no_key"})

    def test_opening_twice_keeps_one_window(self):
        self.flow.open()
        window = self.flow.window
        self.flow.open()
        self.assertIs(self.flow.window, window)

    def test_first_run_or_change(self):
        self.flow.open()
        self.assertEqual(self.flow.window.later_button.text(), "Not now")
        self.flow.close()
        wait_for(lambda: not self.flow.is_open())
        self.flow.key_source = "env"
        self.flow.open()
        self.assertEqual(self.flow.window.later_button.text(), "Cancel")

    def test_paste_key_cleans_the_clipboard_text(self):
        clipboard = mock.Mock()  # a fake: your real clipboard is never touched
        with mock.patch.object(key_module.QGuiApplication, "clipboard", lambda: clipboard):
            self.flow.open()
            clipboard.text.return_value = f"  {KEY[:20]}\n{KEY[20:]} "
            self.flow.window.paste_key()
            self.assertEqual(self.flow.window.key(), KEY)
            self.assertEqual(self.flow.window.status, "empty")
            clipboard.text.return_value = "sk-not-groq-123456"
            self.flow.window.paste_key()
            self.assertEqual(self.flow.window.status, "not_groq")
            self.assertTrue(self.flow.window.save_button.isEnabled())  # a warning, never a block

    def test_save_is_off_with_no_key(self):
        self.flow.open()
        self.assertFalse(self.flow.window.save_button.isEnabled())

    def test_impossible_keys_are_refused_without_sending(self):
        self.answer = "unreachable"  # even "Save anyway" must not store them
        self.flow.open()
        for key in ("gsk_" + "a" * keystore.MAX_KEY_LENGTH, "gsk_café123456789", "gsk_abc\x07defghij"):
            self.type_and_save(key)
            self.assertEqual(self.flow.window.status, "invalid", repr(key[:12]))
            self.assertFalse(self.flow.window.save_button.isEnabled())
            self.flow._save_requested(key, "en")  # Enter, or a click that got through anyway
            self.assertEqual(self.flow.window.status, "invalid")
        wait_for(lambda: False, 0.1)
        self.assertEqual(self.checked, [])
        self.assertEqual(self.saved, [])
        self.type_and_save()  # a real key still works after
        self.assertTrue(wait_for(lambda: self.flow.window.status == "unreachable"))

    def test_a_pasted_document_is_cut_and_refused(self):
        clipboard = mock.Mock()  # a fake: your real clipboard is never touched
        with mock.patch.object(key_module.QGuiApplication, "clipboard", lambda: clipboard):
            self.flow.open()
            clipboard.text.return_value = "Dear team, " * 5000
            self.flow.window.paste_key()
        self.assertEqual(len(self.flow.window.key()), keystore.MAX_KEY_LENGTH + 1)
        self.assertEqual(self.flow.window.status, "invalid")
        self.assertFalse(self.flow.window.save_button.isEnabled())

    def test_open_again_restores_a_minimized_window(self):
        self.flow.open()
        window = self.flow.window
        window.showMinimized()
        self.flow.open()  # what Resume and Ctrl+Win now do while it's open
        self.assertIs(self.flow.window, window)
        self.assertFalse(window.isMinimized())


class KeyBlocksTest(unittest.TestCase):
    """Resume and Ctrl+Win while dictation is paused because of the key (main.py)."""

    def setUp(self):
        self.pause = main.PauseReasons(lambda paused: None)
        self.opened = []
        self.notified = []
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def resume(self):
        main.press_pause_button(self.pause, False, lambda: self.opened.append(True))

    def press(self, window_open):
        main.press_while_paused(self.pause, window_open, lambda: self.opened.append(True),
                                lambda: self.notified.append(True))

    def test_resume_with_the_key_window_open_brings_it_forward(self):
        self.pause.set("key_window", True)
        self.resume()
        self.assertEqual(self.opened, [True])
        self.assertEqual(self.pause.reasons, {"key_window"})

    def test_resume_with_no_key_lifts_your_pause_and_opens_the_window(self):
        self.pause.set("no_key", True)
        self.pause.user_toggle(True)
        self.resume()
        self.assertEqual(self.opened, [True])
        self.assertEqual(self.pause.reasons, {"no_key"})  # a saved key now resumes for real

    def test_plain_pause_and_resume_never_open_it(self):
        main.press_pause_button(self.pause, True, lambda: self.opened.append(True))
        self.assertEqual(self.pause.reasons, {"user"})
        self.resume()
        self.assertFalse(self.pause.paused)
        self.assertEqual(self.opened, [])

    def test_ctrl_win_while_paused(self):
        self.pause.set("key_window", True)
        self.press(window_open=True)
        self.assertEqual((self.opened, self.notified), ([True], []))
        self.pause.set("key_window", False)
        self.pause.set("no_key", True)
        self.press(window_open=False)
        self.assertEqual((self.opened, self.notified), ([True], [True]))
        self.pause.set("no_key", False)
        self.pause.user_toggle(True)
        self.press(window_open=False)  # your own pause: only the pill's pause hint
        self.assertEqual((self.opened, self.notified), ([True], [True]))


class FakeTracker:
    def __init__(self):
        self.connection = None

    def snapshot(self):
        return {"meters": meters_at(), "connection": self.connection}


class TrayKeyTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = Path(folder.name) / "config.json"
        config.write_text('{\n  "debug_fake_limit_warning": null\n}\n')
        patch = mock.patch.object(tray_module, "CONFIG_PATH", config)
        patch.start()
        self.addCleanup(patch.stop)
        self.tracker = FakeTracker()
        self.on_key = mock.Mock()
        self.tray = Tray(self.tracker, mock.Mock(), on_pause=mock.Mock(), on_quit=mock.Mock(), on_key=self.on_key)
        self.shown = []
        self.tray._icon.showMessage = lambda title, text, *rest: self.shown.append(title)
        self.tray._icon.show = lambda: None
        self.addCleanup(self.tray.hide)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_missing_key_notification_at_most_every_30s(self):
        clock = [1000.0]
        with mock.patch.object(tray_module.time, "monotonic", lambda: clock[0]):
            self.tray.show_missing_key()
            clock[0] += 10
            self.tray.show_missing_key()
            clock[0] += 25
            self.tray.show_missing_key()
        self.assertEqual(self.shown, ["Add your Groq API key", "Add your Groq API key"])
        self.tray._icon.messageClicked.emit()
        self.on_key.assert_called_once()

    def test_rejected_key_notifies_once_per_change(self):
        self.tray.start()
        self.tracker.connection = "key_rejected"
        self.tray._check_limits()
        self.tray._check_limits()
        self.assertEqual(self.shown, ["Groq rejected your API key"])
        self.tray._icon.messageClicked.emit()
        self.on_key.assert_called_once()
        self.tracker.connection = "connected"
        self.tray._check_limits()
        self.tracker.connection = "key_rejected"
        self.tray._check_limits()
        self.assertEqual(len(self.shown), 2)

    def test_no_rejected_notification_without_a_key(self):
        self.tray.set_key_missing(True)
        self.tracker.connection = "key_rejected"
        self.tray._check_limits()
        self.assertEqual(self.shown, [])

    def test_menu_and_tooltip(self):
        self.tray.set_key_missing(True)
        self.assertEqual(self.tray._key_action.text(), "Add API key")
        self.assertEqual(self.tray._icon.toolTip(), "Blurt: add your Groq API key")
        self.tray._key_action.trigger()
        self.on_key.assert_called_once()
        self.tray.set_key_missing(False)
        self.assertEqual(self.tray._key_action.text(), "Change API key")


class SettingsKeyRowTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text('{\n  "language": "en"\n}\n', encoding="utf-8")
        self.status = "env"
        self.on_change = mock.Mock()
        self.panel = SettingsPanel(self.path, key_status=lambda: self.status, on_change_key=self.on_change)
        self.panel.set_settings(dict(DEFAULTS["pill"]))
        self.addCleanup(self.panel.deleteLater)
        self.panel._open = True
        self.panel._on_open()
        self.addCleanup(self.panel._live_timer.stop)

    def key_row(self):
        return next(row for row in self.panel._groups()[0] if row[0] == "api_key")

    def test_says_where_the_key_comes_from(self):
        for status, text in [("env", "From the .env file"), ("windows", "Saved in Windows"),
                             (None, "None yet: dictation is paused"), ("rejected", "Rejected by Groq: change it")]:
            self.status = status
            self.assertEqual(self.key_row()[3], text)
        self.status = None
        self.assertEqual(self.panel._key_button_text(), "Add")

    def test_the_button_opens_the_key_window_even_when_config_is_broken(self):
        self.path.write_text('{\n  "language": "en"\n  "history": true\n}\n', encoding="utf-8")
        self.panel.reload()
        self.assertIsNone(self.panel._config)
        button = self.panel._layout()["rows"]["api_key"][2]
        target = self.panel._content_target(button.center())
        self.assertEqual(target, "api_key")
        with contextlib.redirect_stdout(io.StringIO()):
            self.panel._content_release(target)
        self.on_change.assert_called_once()

    def test_no_row_without_a_key_status(self):
        panel = SettingsPanel(self.path)
        self.addCleanup(panel.deleteLater)
        self.assertNotIn("api_key", [row[0] for row in panel._groups()[0]])


class ConflictNoteTest(unittest.TestCase):
    """M17: Windows gives no error when two apps watch the same keys, so the Welcome window says it."""

    @classmethod
    def setUpClass(cls):
        real_fonts.require_real_fonts()  # sizes below are measured, so only with the real fonts

    def window(self, first_run):
        window = OffScreenKeyWindow(first_run=first_run)
        self.addCleanup(window.deleteLater)
        window.show()
        app.processEvents()
        self.addCleanup(window.close)
        return window

    def test_only_the_first_run_window_has_it(self):
        welcome = self.window(True)
        self.assertEqual(welcome.conflict_note.title.text(), "Close other dictation apps first")
        self.assertEqual(welcome.conflict_note.body.text(),
                         "Apps like WisprFlow use the same keys as Blurt. If both are open, Blurt may stop working.")
        self.assertIn("WisprFlow", welcome.conflict_note.body.text())
        for text in (welcome.conflict_note.title.text(), welcome.conflict_note.body.text()):
            self.assertNotIn("—", text)
        self.assertIsNone(self.window(False).conflict_note)

    def test_it_wraps_inside_the_window_and_the_window_still_fits(self):
        welcome = self.window(True)
        note = welcome.conflict_note
        self.assertTrue(note.body.wordWrap())
        self.assertLessEqual(note.geometry().right(), welcome.width() - key_module.PAD)
        for label in (note.title, note.body):  # the text stays inside the callout too
            self.assertLessEqual(label.mapTo(welcome, label.rect().bottomRight()).x(), note.geometry().right())
        self.assertEqual(welcome.width(), key_module.WIDTH)
        # A 1366 x 768 laptop with a 48px taskbar has 720px; measured 468px tall with real fonts
        # (450 before the Dictionary tip, M17 item 14).
        self.assertLessEqual(welcome.height(), 480)

    def test_it_is_a_warm_callout_right_above_the_buttons(self):
        welcome = self.window(True)
        note = welcome.conflict_note
        self.assertEqual(note.objectName(), "callout")
        self.assertIn(f"QFrame#callout {{ background: {key_module.C_WARM}; border: 1px solid {key_module.C_AMBER};",
                      key_module.STYLE)
        icon = note.findChild(key_module.QLabel, "callout_icon")
        self.assertFalse(icon.pixmap().isNull())
        self.assertLess(icon.geometry().right(), note.title.geometry().left())  # the icon is on the left
        # Below the Language card, above Not now and Save.
        language = welcome.language_buttons["en"].parentWidget().parentWidget()
        self.assertGreater(note.geometry().top(), language.geometry().bottom())
        self.assertLess(note.geometry().bottom(), welcome.save_button.geometry().top())


class DictionaryTipTest(unittest.TestCase):
    """M17 item 14: the Welcome window says names and rare words go in the Dictionary."""

    @classmethod
    def setUpClass(cls):
        real_fonts.require_real_fonts()  # sizes below are measured, so only with the real fonts

    def window(self, first_run):
        window = OffScreenKeyWindow(first_run=first_run)
        self.addCleanup(window.deleteLater)
        window.show()
        app.processEvents()
        self.addCleanup(window.close)
        return window

    def test_only_the_first_run_window_has_it(self):
        welcome = self.window(True)
        self.assertEqual(welcome.dictionary_tip.text(), "Misspelled names or rare words? Add them in Dictionary.")
        self.assertNotIn("—", welcome.dictionary_tip.text())
        change = self.window(False)
        self.assertIsNone(change.dictionary_tip)
        self.assertEqual(change.height(), 349)  # the change-key window is unchanged

    def test_one_line_in_the_language_card_and_the_window_still_fits(self):
        welcome = self.window(True)
        tip = welcome.dictionary_tip
        language = welcome.language_buttons["en"].parentWidget().parentWidget()
        self.assertIs(tip.parentWidget(), language)  # in the Language card
        hint = [label for label in language.findChildren(key_module.QLabel, "hint") if label is not tip][0]
        self.assertGreater(tip.geometry().top(), hint.geometry().bottom())  # under "Auto-detect can guess wrong..."
        self.assertLessEqual(tip.height(), hint.height() + 1)  # one line, like the hint above it
        self.assertLessEqual(tip.geometry().right(), language.width())
        self.assertEqual(welcome.width(), key_module.WIDTH)
        self.assertLessEqual(welcome.height(), 480)  # measured 468


if __name__ == "__main__":
    unittest.main()
