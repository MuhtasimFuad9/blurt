"""Tests for the confirmed paste (M17 item 12, "paste_confirm"): delayed rendering.

Windows is always faked (FakeWindows): one clipboard, Blurt's clipboard window, and apps
that paste or copy at scripted times. These tests never touch your real clipboard (the
real user32 and kernel32 are swapped for objects that fail the test) and never send a key.
Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_paste_confirm -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from app import config as config_module
from app import paster as paster_module
from app import pipeline as pipeline_module
from app.paster import (
    CONFIRMED, CONFIRMED_NO_WINDOW, COPIED_ELSEWHERE, CTRL_V, KEY_UPS, NOT_CONFIRMED, NOT_TAKEN, REPLACED,
    WM_DESTROYCLIPBOARD, WM_RENDERALLFORMATS, WM_RENDERFORMAT, CF_UNICODETEXT, paste, paste_outcome,
)
from app.pipeline import Heard, Pipeline
from tests.test_hallucination import CONFIG, FakeTracker
from tests.test_paster import FakeKeys

SENT = 10.0  # when Ctrl+V went out, in the outcome table tests
TARGET = 1  # the window you dictated into
TARGET_PID = 42  # its process
OTHER_PID = 77  # another program (a clipboard manager, say)


class OutcomeTableTest(unittest.TestCase):
    """paste_outcome: events and times in, outcome out."""

    def outcome(self, request=None, replaced_at=None, target_pid=TARGET_PID):
        return paste_outcome(request, replaced_at, SENT, target_pid)

    def test_confirmed_by_pid(self):
        # Notepad (probe run 2): asked 1ms after Ctrl+V, through another window of its own process.
        self.assertEqual(self.outcome((SENT + 0.001, TARGET_PID, True)), CONFIRMED)
        # Matched by process, so being in front isn't needed for this rule.
        self.assertEqual(self.outcome((SENT + 0.001, TARGET_PID, False)), CONFIRMED)

    def test_confirmed_without_a_window(self):
        # Windows Terminal (probe run 5): no window, 2ms after Ctrl+V, the target in front.
        self.assertEqual(self.outcome((SENT + 0.002, 0, True)), CONFIRMED_NO_WINDOW)
        self.assertEqual(self.outcome((SENT + 0.5, 0, True)), CONFIRMED_NO_WINDOW)  # 500ms is still in

    def test_no_window_request_after_500ms(self):
        self.assertEqual(self.outcome((SENT + 0.501, 0, True)), NOT_CONFIRMED)
        self.assertEqual(self.outcome((SENT + 1.2, 0, True)), NOT_CONFIRMED)

    def test_no_window_request_with_the_target_not_in_front(self):
        self.assertEqual(self.outcome((SENT + 0.002, 0, False)), NOT_CONFIRMED)

    def test_a_window_of_another_process(self):
        # Also a clipboard manager reading at once: Blurt must hand the text over, so the
        # app's paste afterwards can't be seen.
        self.assertEqual(self.outcome((SENT + 0.002, OTHER_PID, True)), NOT_CONFIRMED)

    def test_nobody_asks_within_1_5s(self):
        self.assertEqual(self.outcome(None), NOT_TAKEN)
        self.assertEqual(self.outcome((SENT + 1.6, TARGET_PID, True)), NOT_TAKEN)  # too late to count

    def test_you_copied_something_else(self):
        self.assertEqual(self.outcome(None, replaced_at=SENT + 0.2), REPLACED)
        # After another program read it: your copy is what's on the clipboard, not ours.
        self.assertEqual(self.outcome((SENT + 0.002, OTHER_PID, True), replaced_at=SENT + 0.1), REPLACED)
        # After the app took the text: it's pasted (the restore then keeps your copy).
        self.assertEqual(self.outcome((SENT + 0.002, TARGET_PID, True), replaced_at=SENT + 0.1), CONFIRMED)

    def test_no_target(self):
        for request in [(SENT + 0.002, TARGET_PID, False), (SENT + 0.002, 0, False), (SENT + 0.002, 0, True)]:
            with self.subTest(request=request):
                self.assertEqual(self.outcome(request, target_pid=0), NOT_CONFIRMED)
        self.assertEqual(self.outcome(None, target_pid=0), NOT_TAKEN)


class Forbidden:
    """Stands in for the real user32 and kernel32: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"a test reached the real Windows call {name}")


class FakeWindows:
    """Windows as a confirmed paste sees it. The clock only moves in pump(), and the
    script runs there: (seconds after Ctrl+V, action, argument), in order."""

    HWND = 500  # Blurt's clipboard window

    def __init__(self):
        self.clock = 100.0
        self.sequence_number = 7
        self.owner_hwnd = 0
        self.content = {CF_UNICODETEXT: b"old"}  # None means promised, not handed over
        self.open_by = None
        self.alive = False
        self.run = None
        self.script = []
        self.sent_at = None
        self.sent = 4  # what SendInput returns for Ctrl+V
        self.busy = 0  # this many open_clipboard calls fail first
        self.promise_ok = True
        self.requester = 0
        self.rendering = False
        self.front = TARGET
        self.pids = {TARGET: TARGET_PID}
        self.events = []
        self.gives = 0
        self.pumps = 0
        self.app_got = None
        self.restored_at = None

    # ---- the scripted world ----

    def app_pastes(self, pid):
        """An app opens the clipboard and asks for the text, through a window of process pid (0: none)."""
        self.events.append(("request", pid))
        self.requester, self.open_by, self.rendering = pid, "app", True
        if CF_UNICODETEXT in self.content and self.content[CF_UNICODETEXT] is None:
            self.run.on_message(WM_RENDERFORMAT, CF_UNICODETEXT)
        self.app_got = self.content.get(CF_UNICODETEXT)
        self.requester, self.open_by, self.rendering = 0, None, False

    def you_copy(self, _=None):
        """You copy something: your app empties the clipboard, so Blurt's window loses it."""
        self.events.append("you copy")
        if self.owner_hwnd == self.HWND:
            self.run.on_message(WM_DESTROYCLIPBOARD, 0)
        self.owner_hwnd = 999
        self.content = {CF_UNICODETEXT: b"yours"}
        self.sequence_number += 1

    def early_set(self, text):
        """paster._set_text: the normal private copy, before the key wait."""
        self.events.append(("set_text", text))
        self.owner_hwnd = 0  # its helper window is destroyed right away
        self.content = {CF_UNICODETEXT: b"early"}
        self.sequence_number += 1
        return self.sequence_number

    # ---- what _ConfirmedPaste calls ----

    def create_window(self, run):
        self.run, self.alive = run, True
        self.events.append("window")
        return self.HWND

    def destroy_window(self, hwnd):
        self.events.append("close window")
        if self.owner_hwnd == hwnd:
            self.run.on_message(WM_RENDERALLFORMATS, 0)  # sent even when nothing is owed (probe run 2)
            self.owner_hwnd = 0
        self.alive = False

    def open_clipboard(self, hwnd):
        if self.busy:
            self.busy -= 1
            return False
        assert self.open_by is None, "opened twice"
        self.open_by = hwnd
        return True

    def close_clipboard(self):
        self.open_by = None

    def empty_clipboard(self):
        assert self.open_by == self.HWND
        if self.owner_hwnd == self.HWND:
            self.run.on_message(WM_DESTROYCLIPBOARD, 0)  # Windows tells the owner, even when it's us
        self.events.append("empty")
        self.owner_hwnd = self.HWND
        self.content = {}
        self.sequence_number += 1

    def promise_text(self):
        self.events.append("promise")
        if self.promise_ok:
            self.content[CF_UNICODETEXT] = None
        return self.promise_ok

    def give_text(self, data):
        self.gives += 1
        self.content[CF_UNICODETEXT] = data
        if not self.rendering:
            self.sequence_number += 1  # in our own open it's a new copy; in a request it isn't (probe runs 4, 5)
        return True

    def mark_private(self):
        self.content["markers"] = True

    def put_back(self, saved):
        self.empty_clipboard()
        self.content = dict(saved)
        self.restored_at = self.clock
        self.events.append("restore")

    def sequence(self):
        return self.sequence_number

    def owner(self):
        return self.owner_hwnd

    def requester_pid(self):
        return self.requester

    def window_pid(self, hwnd):
        return self.pids.get(hwnd, 0)

    def foreground_root(self):
        return self.front

    def send_keys(self, keys):
        self.events.append(("keys", keys))
        if keys == CTRL_V:
            self.sent_at = self.clock
            return self.sent
        return len(keys)

    def now(self):
        return self.clock

    def pump(self, timeout_ms):
        assert self.alive, "pump() without Blurt's window"
        self.pumps += 1
        self.clock += max(timeout_ms, 1) / 1000
        while self.script and self.sent_at is not None and self.clock >= self.sent_at + self.script[0][0]:
            _, action, argument = self.script.pop(0)
            action(argument)


class ConfirmedPasteTest(unittest.TestCase):
    """paste() with confirm on, through FakeWindows."""

    def setUp(self):
        self.win = FakeWindows()
        self.copies = []

        def no_sleep_while_owned(seconds):
            if self.win.alive:
                raise AssertionError("time.sleep while Blurt's window owns the promised text")

        def copy(text):
            if self.win.alive:
                raise AssertionError("copy_only while Blurt's window still exists")
            self.copies.append(text)
            self.win.events.append("copy_only")
            return True

        for target, name, fake in [
            (paster_module, "user32", Forbidden()),
            (paster_module, "kernel32", Forbidden()),
            (paster_module, "_WINDOWS", self.win),
            (paster_module, "_save_clipboard", lambda: [(CF_UNICODETEXT, b"old")]),
            (paster_module, "_set_text", self.win.early_set),
            (paster_module, "copy_only", copy),
            (paster_module.time, "sleep", no_sleep_while_owned),
        ]:
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def paste(self, script=(), target=TARGET, ready=True, confirm=True):
        self.win.script = list(script)
        self.info = {}
        events = self.win.events

        def checked():
            events.append("ready")
            return ready if not callable(ready) else ready()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = paste("Hello.", FakeKeys(events), restore_ms=300, ready=checked, target=target,
                           confirm=confirm, info=self.info)
        self.output = out.getvalue()
        return result

    def keys_sent(self):
        return [event[1] for event in self.win.events if isinstance(event, tuple) and event[0] == "keys"]

    # ---- confirmed ----

    def test_confirmed_by_pid_puts_your_clipboard_back(self):
        self.assertEqual(self.paste([(0.001, self.win.app_pastes, TARGET_PID)]), "pasted")
        self.assertIn("paste: pasted (confirmed)\n", self.output)
        self.assertEqual(self.win.app_got, "Hello.".encode("utf-16-le") + b"\0\0")  # the app got the text
        self.assertEqual(self.win.content, {CF_UNICODETEXT: b"old"})  # your clipboard is back
        self.assertGreaterEqual(self.win.restored_at, self.win.sent_at + 0.3)  # not before restore_ms
        self.assertEqual(self.copies, [])
        # Steps 1 to 5 in order: early set, key wait, last check, then the window, the promise and Ctrl+V.
        events = [e for e in self.win.events if e not in ("empty",)]
        self.assertEqual(events, [("set_text", "Hello."), "keys released", "ready", "window", "promise",
                                  ("keys", CTRL_V), ("request", TARGET_PID), "restore", "close window"])

    def test_confirmed_without_a_window(self):
        self.assertEqual(self.paste([(0.002, self.win.app_pastes, 0)]), "pasted")
        self.assertIn("paste: pasted (confirmed, the app read it without a window)\n", self.output)
        self.assertNotIn("paste: pasted (confirmed)\n", self.output)  # a test can tell which rule fired
        self.assertEqual(self.win.content, {CF_UNICODETEXT: b"old"})

    def test_the_exit_only_hands_over_what_is_still_owed(self):
        self.paste([(0.001, self.win.app_pastes, TARGET_PID)])
        self.assertEqual(self.win.gives, 1)  # the request only, nothing again on closing

    def test_you_copy_after_the_app_took_it_keeps_your_copy(self):
        self.assertEqual(self.paste([(0.001, self.win.app_pastes, TARGET_PID), (0.1, self.win.you_copy, None)]),
                         "pasted")
        self.assertEqual(self.win.content, {CF_UNICODETEXT: b"yours"})  # not put back over yours
        self.assertNotIn("restore", self.win.events)

    # ---- not confirmed: the copy icon, the dictation as a normal copy ----

    def test_nobody_asks_within_1_5s(self):
        self.assertEqual(self.paste([]), "copied")
        self.assertIn("paste: the app didn't take the text, so it's on your clipboard", self.output)
        self.assertEqual(self.info, {"reason": "the app didn't take the text"})
        self.assertEqual(self.copies, ["Hello."])
        self.assertGreaterEqual(self.win.clock - self.win.sent_at, 1.5)
        # Closing the window handed the text over once, before the normal copy.
        self.assertEqual(self.win.gives, 1)
        self.assertLess(self.win.events.index("close window"), self.win.events.index("copy_only"))
        self.assertNotIn("restore", self.win.events)

    def test_no_window_request_after_500ms(self):
        self.assertEqual(self.paste([(0.6, self.win.app_pastes, 0)]), "copied")
        self.assertEqual(self.info, {"reason": "not confirmed"})
        self.assertNotIn("restore", self.win.events)

    def test_no_window_request_with_the_target_not_in_front(self):
        self.win.front = 2
        self.assertEqual(self.paste([(0.002, self.win.app_pastes, 0)]), "copied")
        self.assertEqual(self.info, {"reason": "not confirmed"})

    def test_a_clipboard_manager_reading_at_once(self):
        self.assertEqual(self.paste([(0.001, self.win.app_pastes, OTHER_PID)]), "copied")
        self.assertEqual(self.win.app_got, "Hello.".encode("utf-16-le") + b"\0\0")  # it was handed over
        self.assertIn("paste: not confirmed", self.output)
        self.assertEqual(self.copies, ["Hello."])  # the dictation stays, as a normal copy
        self.assertNotIn("restore", self.win.events)

    def test_no_target(self):
        for target in (None, 0):
            with self.subTest(target=target):
                self.setUp()
                self.assertEqual(self.paste([(0.001, self.win.app_pastes, TARGET_PID)], target=target), "copied")
                self.assertEqual(self.info, {"reason": "not confirmed"})

    # ---- you copied something else: a failed paste ----

    def test_you_copied_before_the_promise(self):
        # Between the early set and step 4: the compare finds a new number, with the clipboard open.
        result = self.paste(ready=lambda: self.win.you_copy() or True)
        self.assertEqual(result, "error")
        self.assertEqual(self.keys_sent(), [])  # no Ctrl+V
        self.assertNotIn("empty", self.win.events)  # closed without emptying it
        self.assertEqual(self.win.content, {CF_UNICODETEXT: b"yours"})
        self.assertEqual(self.info, {"error": COPIED_ELSEWHERE})
        self.assertIn("paste: you copied something else before the app took the text, so it wasn't pasted. "
                      "Your copy stays on the clipboard; the dictation is in History.", self.output)
        self.assertEqual(self.copies, [])

    def test_you_copied_after_ctrl_v_before_anyone_asked(self):
        self.assertEqual(self.paste([(0.05, self.win.you_copy, None)]), "error")
        self.assertEqual(self.win.content, {CF_UNICODETEXT: b"yours"})  # your copy stays
        self.assertEqual(self.info, {"error": COPIED_ELSEWHERE})
        self.assertNotIn("restore", self.win.events)  # your older clipboard isn't put back either
        self.assertEqual(self.copies, [])

    # ---- the no-blocking rule ----

    def test_busy_clipboard_waits_by_answering_windows(self):
        # setUp's time.sleep fails while the window owns anything; the retries must pump.
        self.win.busy = 3
        self.assertEqual(self.paste([(0.001, self.win.app_pastes, TARGET_PID)]), "pasted")
        self.assertGreaterEqual(self.win.pumps, 3)

    def test_message_handler_never_raises(self):
        def broken(data):
            raise MemoryError("no memory")
        self.win.give_text = broken
        # The app got nothing, so it isn't confirmed: the dictation ends up as a normal copy.
        self.assertEqual(self.paste([(0.001, self.win.app_pastes, TARGET_PID)]), "copied")
        self.assertIn("a clipboard request went wrong (MemoryError)", self.output)
        self.assertNotIn("no memory", self.output)  # the class only, never the message
        self.assertEqual(self.copies, ["Hello."])

    # ---- the ways it falls back ----

    def test_sendinput_refused(self):
        self.win.sent = 0
        self.assertEqual(self.paste(), "copied")
        self.assertEqual(self.keys_sent(), [CTRL_V])
        self.assertIn("Windows took 0 of the 4 key presses", self.output)
        self.assertLess(self.win.events.index("close window"), self.win.events.index("copy_only"))

    def test_half_sent_ctrl_v_releases_the_keys(self):
        self.win.sent = 2
        self.assertEqual(self.paste(), "copied")
        self.assertEqual(self.keys_sent(), [CTRL_V, KEY_UPS])

    def test_last_check_says_no_makes_no_window(self):
        self.assertEqual(self.paste(ready=False), "copied")
        self.assertNotIn("window", self.win.events)
        self.assertEqual(self.keys_sent(), [])

    def test_confirm_off_is_the_checkpoint_b_paste(self):
        with mock.patch.object(paster_module, "send_keys", lambda keys: len(keys)), \
                mock.patch.object(paster_module, "clipboard_sequence", lambda: self.win.sequence_number), \
                mock.patch.object(paster_module, "_restore_clipboard", lambda saved: self.win.events.append("old restore")):
            self.assertEqual(self.paste(confirm=False), "pasted")
        self.assertNotIn("window", self.win.events)
        self.assertIn("old restore", self.win.events)


class PipelineConfirmTest(unittest.TestCase):
    """What the pipeline does with paste()'s confirmed results."""

    def setUp(self):
        self.settings = {"paste_mode": "always", "debug_paste": False, "paste_confirm": True, "history": True}
        self.calls = []
        self.added = []
        for name, fake in [
            ("load_config", lambda: self.settings),
            ("load_terms", lambda: []),
            ("foreground_root", lambda: TARGET),
            ("audio_stats", lambda *args: (0.05, 0.001, 1500)),
        ]:
            patcher = mock.patch.object(pipeline_module, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        history = mock.Mock()
        history.add.side_effect = lambda status, **fields: self.added.append((status, fields)) or 1
        history.audio_path.return_value = None
        self.pipeline = Pipeline(client=None, hotkey=None, config=dict(CONFIG, clipboard_restore_ms=300),
                                 tracker=FakeTracker(), history=history)
        heard = Heard(raw="hello there", cleaned="Hello there.", cleanup="ok")
        heard.text = "Hello there."
        mock.patch.object(self.pipeline, "_hear", return_value=heard).start()
        self.addCleanup(mock.patch.stopall)

    def fake_paste(self, result, info=None):
        def paste_(text, hotkey, **kwargs):
            self.calls.append(kwargs)
            kwargs["info"].update(info or {})
            return result
        mock.patch.object(pipeline_module, "paste", paste_).start()

    def dictate(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = self.pipeline._process(np.zeros(16000, dtype=np.int16), 0.0, TARGET)
        self.output = out.getvalue()
        return result

    def test_passes_the_target_and_the_setting(self):
        self.fake_paste("pasted")
        self.assertEqual(self.dictate(), "done")
        self.assertEqual((self.calls[0]["target"], self.calls[0]["confirm"]), (TARGET, True))
        self.settings["paste_confirm"] = False
        self.dictate()
        self.assertFalse(self.calls[1]["confirm"])

    def test_a_settings_dict_without_the_key_is_unconfirmed(self):
        del self.settings["paste_confirm"]
        self.fake_paste("pasted")
        self.dictate()
        self.assertFalse(self.calls[0]["confirm"])

    def test_you_copied_something_else(self):
        self.fake_paste("error", {"error": COPIED_ELSEWHERE})
        self.assertEqual(self.dictate(), "error")  # the pill's "!"
        status, fields = self.added[-1]
        self.assertEqual((status, fields["error"]), ("failed", COPIED_ELSEWHERE))
        self.assertEqual(fields["cleaned"], "Hello there.")  # the dictation is in History

    def test_other_errors_keep_their_messages(self):
        self.fake_paste("error")
        self.dictate()
        self.assertEqual(self.added[-1][1]["error"], pipeline_module.PASTE_FAILED)
        self.fake_paste("pasted")  # and a stale reason never sticks to the next dictation
        self.dictate()
        self.assertIsNone(self.added[-1][1]["error"])

    def test_not_confirmed_shows_the_copy_icon_with_its_reason(self):
        self.fake_paste("copied", {"reason": "the app didn't take the text"})
        self.assertEqual(self.dictate(), "copied")
        self.assertEqual(self.added[-1][0], "copied")
        self.assertIn("copied to clipboard, not pasted (the app didn't take the text)", self.output)


class ConfigTest(unittest.TestCase):
    def test_on_by_default(self):
        self.assertIs(config_module.DEFAULTS["paste_confirm"], True)

    def test_an_older_config_json_gets_it_without_being_written(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_bytes(json.dumps({"paste_mode": "smart"}).encode("utf-8"))
            before = path.read_bytes()
            self.assertIs(config_module.load_config(path)["paste_confirm"], True)
            self.assertEqual(path.read_bytes(), before)

    def test_false_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_bytes(b'{"paste_confirm": false}')
            self.assertIs(config_module.load_config(path)["paste_confirm"], False)


if __name__ == "__main__":
    unittest.main()
