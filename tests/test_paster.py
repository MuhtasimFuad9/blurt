"""Tests for paste()'s last-moment check and what it returns (M17, P1 part A).

The clipboard and SendInput are always faked: these tests never touch your real
clipboard (any use of it fails the test) and never send a key. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_paster -v
"""

import contextlib
import io
import unittest
from unittest import mock

from app import hotkey as hotkey_module
from app import paster as paster_module
from app import pipeline as pipeline_module
from app.hotkey import KEYEVENTF_KEYUP, send_keys
from app.paster import VK_LCONTROL, VK_V, paste
from app.pipeline import Pipeline
from tests.test_hallucination import CONFIG, FakeTracker

CTRL_V = [(VK_LCONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_LCONTROL, KEYEVENTF_KEYUP)]
KEY_UPS = [(VK_V, KEYEVENTF_KEYUP), (VK_LCONTROL, KEYEVENTF_KEYUP)]


class FakeKeys:
    """The hotkey, as paste() sees it: held for `held` checks, then released."""

    def __init__(self, events, held=0):
        self.events = events
        self.held = held

    def modifiers_held(self):
        if self.held:
            self.held -= 1
            return True
        self.events.append("keys released")
        return False


class FakeClipboard(unittest.TestCase):
    """Fakes every clipboard and keyboard call paste() makes, and records them in order."""

    def setUp(self):
        self.events = []
        self.sent = 4  # what the fake SendInput returns for Ctrl+V
        self.copy_works = True

        def refuse_real_clipboard(*args, **kwargs):
            raise AssertionError("a test tried to open the real clipboard")

        def send(keys):
            self.events.append(("send_keys", keys))
            return self.sent if keys == CTRL_V else len(keys)

        def copy(text):
            self.events.append(("copy_only", text))
            return self.copy_works

        for name, fake in [
            ("_Clipboard", refuse_real_clipboard),
            ("_save_clipboard", lambda: self.events.append("save") or [(13, b"old\0")]),
            ("_set_text", lambda text: self.events.append(("set_text", text)) or 7),
            ("_restore_clipboard", lambda saved: self.events.append("restore")),
            ("clipboard_sequence", lambda: 7),  # nobody copied anything meanwhile
            ("send_keys", send),
            ("copy_only", copy),
        ]:
            patcher = mock.patch.object(paster_module, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(paster_module.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def paste(self, ready=None, held=0, key_timeout=10.0):
        def checked():
            self.events.append("ready")
            return ready
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = paste("Hello.", FakeKeys(self.events, held), restore_ms=0, key_timeout=key_timeout,
                           ready=None if ready is None else checked)
        self.output = out.getvalue()
        return result

    def keys_sent(self):
        return [event[1] for event in self.events if isinstance(event, tuple) and event[0] == "send_keys"]


class PasteTest(FakeClipboard):
    def test_pasted_after_the_last_check(self):
        self.assertEqual(self.paste(ready=True, held=3), "pasted")
        self.assertEqual(self.events, ["save", ("set_text", "Hello."), "keys released", "ready",
                                       ("send_keys", CTRL_V), "restore"])

    def test_no_check_given_pastes_as_before(self):
        self.assertEqual(self.paste(), "pasted")
        self.assertEqual(self.keys_sent(), [CTRL_V])

    def test_last_check_says_no(self):
        self.assertEqual(self.paste(ready=False), "copied")
        self.assertEqual(self.keys_sent(), [])  # no Ctrl+V
        self.assertEqual(self.events[-2:], ["ready", ("copy_only", "Hello.")])  # a normal copy, in Win+V
        self.assertNotIn("restore", self.events)  # your old clipboard isn't put back over it

    def test_last_check_says_no_and_the_copy_fails(self):
        self.copy_works = False
        self.assertEqual(self.paste(ready=False), "error")

    def test_sendinput_refusal_is_caught_but_an_admin_window_is_not(self):
        # SendInput's count only catches Windows refusing input outright (it took 0 of 4 here).
        self.sent = 0
        self.assertEqual(self.paste(ready=True), "copied")
        self.assertEqual(self.keys_sent(), [CTRL_V])  # tried once, nothing to release
        self.assertIn(("copy_only", "Hello."), self.events)
        self.assertNotIn("restore", self.events)
        self.assertIn("Windows took 0 of the 4 key presses", self.output)

    def test_an_admin_window_still_counts_as_pasted(self):
        # The known limit: an app running as administrator drops our Ctrl+V (UIPI), but
        # Microsoft's SendInput documentation says neither the count nor GetLastError shows
        # that. SendInput reports all 4 taken, so paste() can only say "pasted". Part B
        # (item 12, delayed rendering) is what would catch this case; it isn't built.
        self.sent = 4
        self.assertEqual(self.paste(ready=True), "pasted")

    def test_half_sent_ctrl_v_releases_the_keys(self):
        self.sent = 2
        self.assertEqual(self.paste(ready=True), "copied")
        self.assertEqual(self.keys_sent(), [CTRL_V, KEY_UPS])  # Ctrl and V never left held down

    def test_keys_still_held(self):
        self.assertEqual(self.paste(ready=True, held=10**9, key_timeout=0.05), "held")
        self.assertNotIn("ready", self.events)
        self.assertEqual(self.keys_sent(), [])

    def test_clipboard_refused_the_text(self):
        def fail(text):
            raise OSError("clipboard is busy")
        with mock.patch.object(paster_module, "_set_text", fail):
            self.assertEqual(self.paste(ready=True), "error")
        self.assertNotIn("ready", self.events)
        self.assertEqual(self.keys_sent(), [])


class SendKeysCountTest(unittest.TestCase):
    def test_returns_what_sendinput_took(self):
        with mock.patch.object(hotkey_module, "user32") as user32:  # never the real SendInput
            user32.SendInput.return_value = 4
            self.assertEqual(send_keys(CTRL_V), 4)
            user32.SendInput.return_value = 0
            self.assertEqual(send_keys(CTRL_V), 0)
        self.assertEqual(user32.SendInput.call_args.args[0], 4)


class DeliverTest(FakeClipboard):
    """The pipeline's _deliver with the real paste(): the window and text field checks at the last moment."""

    def setUp(self):
        super().setUp()
        self.settings = {"paste_mode": "always", "debug_paste": False}
        self.windows = []  # foreground_root() answers, in order
        self.fields = []  # find_text_field() answers, in order
        for name, fake in [
            ("load_config", lambda: self.settings),
            ("foreground_root", lambda: self.windows.pop(0)),
            ("window_exe", lambda hwnd: {1: "notepad.exe", 2: "Discord.exe"}[hwnd]),
            ("find_text_field", self.find_text_field),
            ("copy_only", paster_module.copy_only),  # the pipeline's own name for it: the fake too
        ]:
            patcher = mock.patch.object(pipeline_module, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pipeline = Pipeline(client=None, hotkey=FakeKeys(self.events, held=2),
                                 config=dict(CONFIG, clipboard_restore_ms=0), tracker=FakeTracker())

    def find_text_field(self, settings):
        self.events.append("text field check")
        return self.fields.pop(0), "test"

    def deliver(self, target=1):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = self.pipeline._deliver("Hello.", target)
        self.output = out.getvalue()
        return result

    def test_same_window_pastes(self):
        self.windows = [1, 1]  # at the start, and at the last moment
        self.assertEqual(self.deliver(), "pasted")
        self.assertEqual(self.keys_sent(), [CTRL_V])

    def test_window_changed_during_the_key_wait(self):
        self.windows = [1, 2]  # still Notepad at first, Discord by the last moment
        self.assertEqual(self.deliver(), "copied")
        self.assertEqual(self.keys_sent(), [])  # no Ctrl+V
        self.assertIn(("copy_only", "Hello."), self.events)
        self.assertIn("switched windows just before pasting (notepad.exe -> Discord.exe)", self.output)
        self.assertIn("copied to clipboard, not pasted (you switched windows)", self.output)

    def test_not_a_text_field_at_the_last_moment(self):
        self.settings["paste_mode"] = "smart"
        self.windows = [1, 1]
        self.fields = [False]
        self.assertEqual(self.deliver(), "copied")
        self.assertEqual(self.keys_sent(), [])
        # The check runs once, after the keys are released, right before Ctrl+V would go out.
        self.assertEqual(self.events.count("text field check"), 1)
        self.assertLess(self.events.index("keys released"), self.events.index("text field check"))
        self.assertIn("copied to clipboard, not pasted (no text field focused)", self.output)

    def test_a_text_field_is_checked_once_and_pasted(self):
        self.settings["paste_mode"] = "smart"
        self.windows = [1, 1]
        self.fields = [True]
        self.assertEqual(self.deliver(), "pasted")
        self.assertEqual(self.events.count("text field check"), 1)
        self.assertLess(self.events.index("text field check"), self.events.index(("send_keys", CTRL_V)))

    def test_sendinput_returned_0(self):
        self.sent = 0
        self.windows = [1, 1]
        self.assertEqual(self.deliver(), "copied")
        self.assertIn(("copy_only", "Hello."), self.events)
        self.assertIn("copied to clipboard, not pasted (Windows refused the key presses)", self.output)

    def test_switched_before_the_paste_starts_never_touches_the_clipboard_twice(self):
        self.windows = [2]  # the early check, as before: copied without saving your clipboard
        self.assertEqual(self.deliver(), "copied")
        self.assertEqual(self.events, [("copy_only", "Hello.")])

    def test_keys_held(self):
        self.pipeline.hotkey = FakeKeys(self.events, held=10**9)
        self.windows = [1]
        with mock.patch.object(pipeline_module, "paste", lambda *a, **k: paste(*a, **dict(k, key_timeout=0.05))):
            self.assertEqual(self.deliver(), "held")
        self.assertEqual(self.keys_sent(), [])
        self.assertIn("keys still held", self.output)


if __name__ == "__main__":
    unittest.main()
