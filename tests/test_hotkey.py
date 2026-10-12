"""Tests for the hold-to-talk hook: every rule, for Ctrl+Win and the four other shortcuts
(M14), the golden sequences from before M14, pause, and quitting. No real keyboard: fake
key events go straight into the hook's decision code, and injected keys are recorded
instead of sent.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_hotkey -v
"""

import contextlib
import ctypes
import io
import itertools
import queue
import random
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock

from app import hotkey as hotkey_module
from app.hotkey import FAMILY_KEYS, KBDLLHOOKSTRUCT, KEYEVENTF_KEYUP, VK_DUMMY, Hotkey
from app.shortcut import ALLOWED, DEFAULT, FAMILIES
from app.shortcut import text as shortcut_text
from tools import hotkey_golden

LCTRL, LWIN, D = 0xA2, 0x5B, 0x44


class Keyboard:
    """Presses keys into the hook and keeps track of what Windows sees.

    Windows sees a key event when the hook lets it through, and every key the
    hook injects. `windows` is that list of (vk, "down" or "up").

    Keys the hook injects while handling a key are recorded BEFORE that key, because
    that's the order Windows uses: it passes the key on only after the hook returns
    (AutoHotkey's hook.cpp relies on this for its menu mask; M14 bugs 1 and 2 came
    from a dummy tap that landed before the Alt press it was meant to cover).
    """

    def __init__(self, test, shortcut=None):
        self.events = queue.Queue()
        self.hotkey = Hotkey(self.events) if shortcut is None else Hotkey(self.events, shortcut=shortcut)
        self.windows = []
        for name, fake in (("send_keys", self._injected), ("_windows_thinks_held", self.windows_thinks_held)):
            patch = mock.patch.object(hotkey_module, name, fake)
            patch.start()
            test.addCleanup(patch.stop)

    def windows_thinks_held(self, vk):
        """Like GetAsyncKeyState: down if the last event Windows saw for this key was a press."""
        seen = [what for key, what in self.windows if key == vk]
        return bool(seen) and seen[-1] == "down"

    def missed_up(self, vk):
        """A release the hook never saw (the Ctrl+Alt+Del screen), but Windows did."""
        self.windows.append((vk, "up"))

    def _injected(self, keys):
        for key in keys:
            self.windows.append((key[0], "up" if key[1] & KEYEVENTF_KEYUP else "down"))

    def _key(self, vk, down):
        kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=0)
        swallowed = self.hotkey._handle(kb, down)
        if not swallowed:
            self.windows.append((vk, "down" if down else "up"))
        return swallowed

    def down(self, vk):
        return self._key(vk, True)

    def up(self, vk):
        return self._key(vk, False)

    def sent(self):
        """The event kinds the hook sent to the worker, leaving out the stuck-key checks and debug lines."""
        kinds = []
        while not self.events.empty():
            kind = self.events.get()[0]
            if kind not in ("check_stuck", "debug"):
                kinds.append(kind)
        return kinds

    def balanced(self, vk):
        """Windows saw exactly as many releases as presses of this key."""
        downs = sum(1 for key, what in self.windows if key == vk and what == "down")
        ups = sum(1 for key, what in self.windows if key == vk and what == "up")
        return downs == ups


class NotPausedTest(unittest.TestCase):
    def test_ctrl_then_win_records(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        self.assertTrue(kb.down(LWIN))  # hidden
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.balanced(LWIN))


class MissedReleaseTest(unittest.TestCase):
    def test_win_alone_after_a_missed_ctrl_release_opens_start(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        kb.missed_up(LCTRL)  # released on the secure screen
        self.assertFalse(kb.down(LWIN))  # Windows sees Win: the Start menu works
        kb.up(LWIN)
        self.assertEqual(kb.sent(), [])  # no dictation
        self.assertNotIn(LCTRL, kb.hotkey._held)

    def test_paste_does_not_wait_for_a_missed_release(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        kb.missed_up(LCTRL)
        self.assertEqual(kb.hotkey.modifiers_held(), set())

    def test_ctrl_win_still_records_after_a_missed_release(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        kb.missed_up(LCTRL)
        kb.down(LCTRL)
        self.assertTrue(kb.down(LWIN))
        self.assertEqual(kb.hotkey.modifiers_held(), {LCTRL, LWIN})  # hidden Win still counts as held
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.balanced(LWIN))


LKEY = 0x4C


class MissedWinTest(unittest.TestCase):
    """M14 bug 3: Win+L with Win still held when the lock screen appears. The hook never sees
    Win's release (it happens on the lock screen), but Windows does. A Win the hook isn't
    hiding is visible to Windows, so the hook asks Windows about it on the next Win or partner
    press, like Ctrl, Shift and Alt, and forgets it."""

    def lock_with_win(self, kb):
        """Plain Win+L (Win is visible: it isn't the shortcut), Win released on the lock screen."""
        self.assertFalse(kb.down(LWIN))
        self.assertFalse(kb.down(LKEY))
        kb.missed_up(LWIN)
        kb.up(LKEY)
        kb.hotkey.debug = True

    def debug_lines(self, kb):
        return [detail for kind, _, detail in list(kb.events.queue) if kind == "debug"]

    def test_partner_taps_after_the_lock_start_nothing(self):  # the user's repro, every shortcut
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                self.lock_with_win(kb)
                after_lock = len(kb.windows)
                for _ in range(3):
                    for vk in partners(shortcut):
                        self.assertFalse(kb.down(vk))
                    for vk in partners(shortcut):
                        self.assertFalse(kb.up(vk))
                self.assertEqual(self.debug_lines(kb), ["forgot Left Win (its release was missed)"])
                self.assertEqual(kb.sent().count("start"), 0)
                self.assertNotIn(LWIN, kb.hotkey._held)
                # Windows saw only the partner keys themselves: no dummy, no second Win release.
                self.assertEqual({key for key, _ in kb.windows[after_lock:]}, set(partners(shortcut)))
                self.assertFalse(kb.hotkey._mask_pending)
                for vk in {key for key, _ in kb.windows}:
                    self.assertTrue(kb.balanced(vk))

    def test_typing_d_afterwards_is_a_plain_d(self):
        kb = Keyboard(self)
        self.lock_with_win(kb)
        kb.down(LCTRL)
        kb.up(LCTRL)
        before = len(kb.windows)
        self.assertFalse(kb.down(D))  # not replayed as Win+D: the desktop isn't shown
        kb.up(D)
        self.assertEqual(kb.windows[before:], [(D, "down"), (D, "up")])
        self.assertEqual(kb.sent(), [])

    def test_win_alone_afterwards_reaches_windows(self):
        kb = Keyboard(self)
        self.lock_with_win(kb)
        kb.down(LCTRL)
        kb.up(LCTRL)
        before = len(kb.windows)
        self.assertFalse(kb.down(LWIN))
        self.assertFalse(kb.up(LWIN))
        self.assertEqual(kb.windows[before:], [(LWIN, "down"), (LWIN, "up")])  # the Start menu opens
        self.assertEqual(kb.sent(), [])

    def test_the_shortcut_works_afterwards(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                self.lock_with_win(kb)
                for vk in partners(shortcut):
                    kb.down(vk)
                self.assertTrue(kb.down(LWIN))
                kb.up(LWIN)
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop"])
                self.assertTrue(kb.balanced(LWIN))

    def test_without_a_partner_press_the_rule_waits(self):
        """The check runs on Win and partner presses only, so other keys after the lock leave
        the forgotten Win alone, and do nothing odd with it either (Win isn't hidden)."""
        kb = Keyboard(self)
        self.lock_with_win(kb)
        before = len(kb.windows)
        kb.down(D)
        kb.up(D)
        self.assertIn(LWIN, kb.hotkey._held)
        self.assertEqual(kb.windows[before:], [(D, "down"), (D, "up")])
        kb.down(LCTRL)
        self.assertNotIn(LWIN, kb.hotkey._held)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), [])

    def test_a_missed_ctrl_and_a_missed_win_together(self):
        kb = Keyboard(self)
        kb.down(LSHIFT)
        kb.down(LCTRL)
        self.assertFalse(kb.down(LWIN))  # Ctrl+Shift+Win isn't Ctrl+Win: Win stays visible
        kb.up(LSHIFT)
        kb.missed_up(LCTRL)
        kb.missed_up(LWIN)
        kb.hotkey.debug = True
        kb.down(RCTRL)
        kb.up(RCTRL)
        self.assertEqual(sorted(self.debug_lines(kb)), ["forgot Left Ctrl (its release was missed)",
                                                        "forgot Left Win (its release was missed)"])
        self.assertEqual(kb.sent(), [])
        self.assertEqual(kb.hotkey._held, set())
        self.assertEqual(kb.hotkey.modifiers_held(), set())

    def test_a_hidden_win_is_never_asked_about(self):
        """A hidden Win looks up to Windows on purpose (swallowed, or released for Windows by R2).
        Pressing the partner key on the other side must not forget it mid-dictation."""
        for win_first in (False, True):
            with self.subTest(win_first=win_first):
                kb = Keyboard(self)
                for vk in ((LWIN, LCTRL) if win_first else (LCTRL, LWIN)):
                    kb.down(vk)
                self.assertFalse(kb.windows_thinks_held(LWIN))
                kb.down(RCTRL)
                self.assertIn(LWIN, kb.hotkey._held)
                self.assertTrue(kb.hotkey.recording)
                kb.up(RCTRL)
                self.assertTrue(kb.hotkey.recording)  # Left Ctrl and Win are still held
                self.assertTrue(kb.up(LWIN))  # still hidden: its release is swallowed
                kb.up(LCTRL)
                self.assertEqual(kb.sent(), ["start", "stop"])
                self.assertTrue(kb.balanced(LWIN))

    def test_a_win_tap_clears_a_missed_hidden_win(self):
        """The known limit, and what the README tells people to do about it: the shortcut held
        through a lock (Win hidden, so it can't be checked), both releases missed. Afterwards
        the partner key alone starts a recording; one Win tap clears it."""
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for vk in partners(shortcut):
                    kb.down(vk)
                self.assertTrue(kb.down(LWIN))  # hidden (R1): Windows never saw it
                for vk in partners(shortcut):
                    kb.missed_up(vk)  # released on the lock screen; the hidden Win's release too
                self.assertEqual(kb.sent(), ["start"])

                def partner_taps():
                    for _ in range(2):
                        for vk in partners(shortcut):
                            kb.down(vk)
                        for vk in partners(shortcut):
                            kb.up(vk)
                    return kb.sent()

                self.assertIn("start", partner_taps())  # the symptom: the partner alone records
                kb.down(LWIN)  # the fix: one Win tap
                kb.up(LWIN)
                self.assertEqual(partner_taps(), [])
                self.assertEqual(kb.hotkey._held, set())
                self.assertEqual(kb.hotkey._win_hidden, set())
                self.assertFalse(kb.hotkey.recording)

    def test_windows_wrongly_saying_up_only_forgets_win(self):
        """If Windows' answer were wrong (GetAsyncKeyState returns 0 when it fails), a Win the
        user really holds is forgotten: no dictation, and Windows gets its real release."""
        kb = Keyboard(self)
        kb.down(LWIN)
        with mock.patch.object(hotkey_module, "_windows_thinks_held", lambda vk: False):
            self.assertFalse(kb.down(LCTRL))
        self.assertFalse(kb.up(LCTRL))
        self.assertFalse(kb.up(LWIN))
        self.assertEqual(kb.windows, [(LWIN, "down"), (LCTRL, "down"), (LCTRL, "up"), (LWIN, "up")])
        self.assertEqual(kb.sent(), [])

    def test_alt_after_the_lock_is_a_plain_alt_tap(self):
        """Alt+Win: the forgotten Win means R2 never runs, so no dummy and no pending mask. Alt
        reaches Windows as a plain tap (which in Notepad shows its key tips, as it always does)."""
        kb = Keyboard(self, frozenset({"Alt", "Win"}))
        self.lock_with_win(kb)
        before = len(kb.windows)
        kb.down(LALT)
        self.assertFalse(kb.hotkey._mask_pending)
        kb.up(LALT)
        self.assertEqual(kb.windows[before:], [(LALT, "down"), (LALT, "up")])
        self.assertEqual(kb.sent(), [])

    def test_the_mask_flag_is_never_set_when_the_check_runs(self):
        """_handle uses a pending mask before anything else, and the flag is only set after the
        check (by R2 or R6, later in the same key event), so the check never runs with it set."""
        original = Hotkey._drop_missed_releases
        seen = []

        def checked(hotkey, current):
            seen.append(hotkey._mask_pending)
            original(hotkey, current)

        rng = random.Random(1717)
        cases = [hotkey_golden.random_case(rng) for _ in range(300)]
        with mock.patch.object(Hotkey, "_drop_missed_releases", checked):
            for shortcut in ALLOWED:
                for case in cases:
                    hotkey_golden.Replay(lambda events: Hotkey(events, shortcut=shortcut)).run(case)
        self.assertGreater(len(seen), 1000)
        self.assertNotIn(True, seen)


class PausedTest(unittest.TestCase):
    def setUp(self):
        self.kb = Keyboard(self)
        self.kb.hotkey.paused = True

    def test_ctrl_then_win_is_swallowed_and_only_hints(self):
        kb = self.kb
        kb.down(LCTRL)
        self.assertTrue(kb.down(LWIN))  # Windows never sees it: no taskbar badges, no Start menu
        self.assertTrue(kb.up(LWIN))  # ...and never its release either
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])
        self.assertFalse(kb.hotkey.recording)
        self.assertNotIn((LWIN, "down"), kb.windows)
        self.assertTrue(kb.balanced(LWIN))
        self.assertTrue(kb.balanced(LCTRL))

    def test_win_then_ctrl_still_releases_win_with_the_dummy_key(self):
        kb = self.kb
        self.assertFalse(kb.down(LWIN))  # alone, Win goes to Windows
        kb.down(LCTRL)
        # Windows is told Win was released, after a dummy key so Start doesn't open
        # (then Ctrl itself, which the hook lets through).
        self.assertEqual(
            kb.windows[-4:], [(VK_DUMMY, "down"), (VK_DUMMY, "up"), (LWIN, "up"), (LCTRL, "down")]
        )
        self.assertTrue(kb.up(LWIN))  # already released for Windows: the real release is swallowed
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])
        self.assertTrue(kb.balanced(LWIN))

    def test_shortcut_still_works(self):
        kb = self.kb
        kb.down(LCTRL)
        kb.down(LWIN)
        self.assertTrue(kb.down(D))  # held back and replayed after Win
        self.assertEqual(kb.windows[-2:], [(LWIN, "down"), (D, "down")])
        kb.up(D)
        self.assertFalse(kb.up(LWIN))  # Windows saw Win go down, so it gets the release
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])  # no "cancel": nothing was recording
        for vk in (LWIN, LCTRL, D):
            self.assertTrue(kb.balanced(vk))

    def test_win_auto_repeat_hints_once(self):
        kb = self.kb
        kb.down(LCTRL)
        for _ in range(5):
            kb.down(LWIN)
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])
        self.assertTrue(kb.balanced(LWIN))

    def test_resume_takes_effect_on_the_next_press(self):
        kb = self.kb
        kb.down(LCTRL)
        kb.down(LWIN)
        kb.hotkey.paused = False  # resumed while still holding
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])  # no "stop" for a recording that never started
        kb.down(LCTRL)
        kb.down(LWIN)
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.balanced(LWIN))


class PauseDuringRecordingTest(unittest.TestCase):
    def test_recording_finishes_normally(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        kb.down(LWIN)
        kb.hotkey.paused = True  # e.g. from the tray, mid-dictation
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.balanced(LWIN))


class ConsoleCloseTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.quit_app = mock.Mock(side_effect=lambda: self.log.append("quit through Qt"))
        self.hotkey = Hotkey(queue.Queue(), on_console_close=self.quit_app)
        self.hotkey._exit = lambda code: self.log.append(f"exit {code}")
        for name in ("stop", "uninstall", "release_stuck_modifiers"):
            patch = mock.patch.object(self.hotkey, name, side_effect=lambda name=name: self.log.append(name))
            patch.start()
            self.addCleanup(patch.stop)

    def ctrl_c(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.hotkey._on_console_ctrl(0))

    def test_first_ctrl_c_quits_through_qt(self):
        self.ctrl_c()
        self.assertEqual(self.log, ["quit through Qt"])

    def test_second_ctrl_c_unhooks_repairs_keys_and_exits(self):
        self.ctrl_c()
        self.ctrl_c()  # the GUI never got to it
        self.assertEqual(self.log, ["quit through Qt", "stop", "uninstall", "release_stuck_modifiers", "exit 1"])

    def test_waits_briefly_for_the_hook_thread_to_unhook_itself(self):
        self.hotkey._hook = 1234  # still installed, and the hook thread doesn't answer
        with mock.patch.object(hotkey_module, "FORCE_QUIT_HOOK_WAIT", 0.05):
            started = time.monotonic()
            self.ctrl_c()
            self.ctrl_c()
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertEqual(self.log[-3:], ["uninstall", "release_stuck_modifiers", "exit 1"])


class GoldenTest(unittest.TestCase):
    """The configurable hook, on Ctrl+Win, does exactly what the hook from before M14 did
    (tests/golden/hotkey_golden.json, recorded with that hook: 3,051 key sequences), except in
    the 164 sequences with a missed release of a Win the hook wasn't hiding (M14 bug 3). Those
    must match up to where the missed-Win rule fired, be no worse after it, be exactly the
    pinned set, and give exactly the output saved in hotkey_golden_missed_win.json after
    the user's bug 3 retests. The numbers are written here too, not only in tools/hotkey_golden.py."""

    def assert_golden(self, make=None):
        result = hotkey_golden.Comparison(make)
        self.assertEqual([(case, why) for case, _, _, why in result.differences[:3]], [])
        self.assertEqual(result.matched, 2887)
        self.assertEqual(len(result.missed_win), 164)
        self.assertEqual(result.missed_win_sha256(),
                         "cbaff7a0835335b581e250daec8752403d33968859086a7fd979a9e1db63d2c0")
        # The fixed hook reproduces the saved output of those 164 exactly.
        self.assertEqual([[case, got] for case, _, got in result.missed_win], hotkey_golden.load_missed_win())
        self.assertEqual(result.problems(), [])
        return result

    def test_the_golden_file_is_never_rewritten(self):
        self.assertEqual(hotkey_golden.golden_sha256(),
                         "47ddb46b0b90a29c2b1c4bae471bce1d4bb7b13417eb69cf73c6fe222a5bd038")
        self.assertEqual(hotkey_golden.GOLDEN_SHA256, hotkey_golden.golden_sha256())

    def test_the_missed_win_file_is_never_rewritten(self):
        self.assertEqual(hotkey_golden.file_sha256(hotkey_golden.MISSED_WIN_PATH),
                         "18271bb3801a023cc2ae1845cd92c867971c684ca26aaf2216630dda1fe48067")
        self.assertEqual(hotkey_golden.MISSED_WIN_FILE_SHA256,
                         hotkey_golden.file_sha256(hotkey_golden.MISSED_WIN_PATH))
        self.assertEqual(len(hotkey_golden.load_missed_win()), 164)

    def test_save_missed_win_refuses_to_overwrite(self):
        before = hotkey_golden.MISSED_WIN_PATH.read_bytes()
        out = io.StringIO()
        with mock.patch.object(hotkey_golden.sys, "argv", ["hotkey_golden.py", "save-missed-win"]), \
                mock.patch.object(hotkey_golden, "Comparison") as comparison, contextlib.redirect_stdout(out):
            self.assertEqual(hotkey_golden.main(), 1)
        comparison.assert_not_called()
        self.assertIn("never rewritten", out.getvalue())
        self.assertEqual(hotkey_golden.MISSED_WIN_PATH.read_bytes(), before)

    def test_default_matches_the_old_hook(self):
        result = self.assert_golden()
        for case, _, _ in result.missed_win:  # all random ones, each with a missed Win release
            self.assertNotIn(case, hotkey_golden.HAND_CASES)
            self.assertTrue("~LW" in case.split() or "~RW" in case.split(), case)

    def test_ctrl_win_passed_explicitly_matches_too(self):
        self.assert_golden(lambda events: Hotkey(events, debug=True, shortcut=DEFAULT))

    def test_the_check_fails_without_the_rule(self):
        """With the old _drop_missed_releases (Ctrl, Shift and Alt only), the 164 match the file
        exactly again, so the pinned count fails: the exception can't quietly go stale."""

        def old_rule(hotkey, current):
            for vk in list(hotkey._held & hotkey_module.VISIBLE_MODIFIERS):
                if vk != current and not hotkey_module._windows_thinks_held(vk):
                    hotkey._held.discard(vk)
                    hotkey._debug(f"forgot {hotkey_module.MODIFIER_NAMES[vk]} (its release was missed)")

        with mock.patch.object(Hotkey, "_drop_missed_releases", old_rule):
            result = hotkey_golden.Comparison()
        self.assertEqual((result.matched, len(result.missed_win), len(result.differences)), (3051, 0, 0))
        self.assertEqual(result.problems(), ["0 missed-Win cases, expected exactly 164"])

    def test_the_check_fails_if_a_hidden_win_is_forgotten_too(self):
        """A rule that also forgot a hidden Win (Windows thinks it's up on purpose) would break
        dictations, and the check sees it."""

        def too_eager(hotkey, current):
            for vk in list(hotkey._held & (hotkey_module.VISIBLE_MODIFIERS | hotkey_module.WIN_KEYS)):
                if vk != current and not hotkey_module._windows_thinks_held(vk):
                    hotkey._held.discard(vk)
                    hotkey._debug(f"forgot {hotkey_module.MODIFIER_NAMES[vk]} (its release was missed)")

        with mock.patch.object(Hotkey, "_drop_missed_releases", too_eager):
            result = hotkey_golden.Comparison()
        self.assertTrue(result.differences)
        self.assertTrue(result.problems())


LALT, RALT, LSHIFT, RSHIFT, RCTRL, RWIN = 0xA4, 0xA5, 0xA0, 0xA1, 0xA3, 0x5C
LEFT_KEY = {"Ctrl": LCTRL, "Alt": LALT, "Shift": LSHIFT, "Win": LWIN}
RIGHT_KEY = {"Ctrl": RCTRL, "Alt": RALT, "Shift": RSHIFT, "Win": RWIN}


def partners(shortcut):
    """The shortcut's keys other than Win (left side), in the canonical order."""
    return [LEFT_KEY[name] for name in FAMILIES if name in shortcut and name != "Win"]


def outsiders(shortcut):
    """Modifiers that aren't in the shortcut."""
    return [LEFT_KEY[name] for name in ("Ctrl", "Alt", "Shift") if name not in shortcut]


def masked(shortcut):
    return "Alt" in shortcut or "Shift" in shortcut


def dummy_taps(kb):
    return kb.windows.count((VK_DUMMY, "down"))


class EveryShortcutTest(unittest.TestCase):
    """Rules R1 to R11 and the new mask rule (R15), for each of the five shortcuts."""

    def all_balanced(self, kb):
        for vk in {key for key, _ in kb.windows}:
            self.assertTrue(kb.balanced(vk), f"key 0x{vk:02X} unbalanced: {kb.windows}")

    def test_partners_then_win_records_and_hides_win(self):  # R1, R4, R15
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for vk in partners(shortcut):
                    kb.down(vk)
                self.assertTrue(kb.down(LWIN))
                self.assertTrue(kb.up(LWIN))
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop"])
                self.assertNotIn(LWIN, [key for key, _ in kb.windows])  # Windows never saw Win at all
                self.assertEqual(dummy_taps(kb), 1 if masked(shortcut) else 0)
                self.all_balanced(kb)

    def test_win_first_is_released_with_the_dummy_key(self):  # R2, R4, R15
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                self.assertFalse(kb.down(LWIN))  # alone, Win goes to Windows
                *first, last = partners(shortcut)
                for vk in first:
                    kb.down(vk)
                kb.down(last)
                # Win's release, after a dummy so Start doesn't open, reaches Windows before the
                # key that completed the shortcut: so that dummy can't cover that key.
                self.assertEqual(kb.windows[-4:], [(VK_DUMMY, "down"), (VK_DUMMY, "up"), (LWIN, "up"),
                                                   (last, "down")])
                self.assertTrue(kb.up(LWIN))  # already released for Windows
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop"])
                if masked(shortcut):
                    # A second dummy, sent with the next key event (here Win's hidden release),
                    # lands while the last key is down.
                    after = kb.windows[kb.windows.index((last, "down")):]
                    self.assertEqual(after[:3], [(last, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up")])
                self.assertEqual(dummy_taps(kb), 2 if masked(shortcut) else 1)
                self.all_balanced(kb)

    def test_alt_pressed_last_is_masked(self):  # M14 bug 1: Alt+Win, Win then Alt
        for paused in (False, True):
            with self.subTest(paused=paused):
                kb = Keyboard(self, frozenset({"Alt", "Win"}))
                kb.hotkey.paused = paused
                kb.down(LWIN)
                kb.down(LALT)
                kb.up(LALT)
                kb.up(LWIN)
                self.assertEqual(kb.windows, [(LWIN, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up"), (LWIN, "up"),
                                              (LALT, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up"), (LALT, "up")])
                self.assertEqual(kb.sent(), ["paused_press"] if paused else ["start", "stop"])

    def test_alt_shift_win_never_leaves_alt_shift_alone(self):  # M14 bug 2: the layout switch
        for order in ((LWIN, LALT, LSHIFT), (LWIN, LSHIFT, LALT), (LALT, LWIN, LSHIFT), (LSHIFT, LWIN, LALT)):
            with self.subTest(order=[hotkey_module.MODIFIER_NAMES[vk] for vk in order]):
                kb = Keyboard(self, frozenset({"Alt", "Shift", "Win"}))
                for vk in order:
                    kb.down(vk)
                kb.up(LALT)  # the first release: a dummy must reach Windows while both are down
                seen = kb.windows
                both_down = max(seen.index((LALT, "down")), seen.index((LSHIFT, "down")))
                self.assertIn((VK_DUMMY, "down"), seen[both_down:seen.index((LALT, "up"))])

    def test_win_in_the_middle(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                first, *rest = partners(shortcut)
                if not rest:
                    continue
                kb.down(first)
                self.assertFalse(kb.down(LWIN))  # not the whole shortcut yet: Windows sees Win
                kb.down(rest[0])
                self.assertEqual(kb.sent(), ["start"])
                kb.up(rest[0])
                kb.up(LWIN)
                kb.up(first)
                self.assertEqual(kb.sent(), ["stop"])
                self.all_balanced(kb)

    def test_repeats_start_once(self):  # R3
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for vk in partners(shortcut):
                    kb.down(vk)
                for _ in range(5):
                    self.assertTrue(kb.down(LWIN))
                kb.up(LWIN)
                last = partners(shortcut)[-1]
                kb.down(LWIN)
                for _ in range(3):
                    kb.down(LWIN)
                kb.up(LWIN)
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop", "start", "stop"])
                self.assertTrue(kb.balanced(LWIN))
                self.assertTrue(kb.balanced(last))

    def test_a_third_key_cancels_and_reaches_windows_after_win(self):  # R5
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for vk in partners(shortcut):
                    kb.down(vk)
                kb.down(LWIN)
                self.assertTrue(kb.down(D))  # held back and replayed after Win
                self.assertEqual(kb.windows[-2:], [(LWIN, "down"), (D, "down")])
                kb.up(D)
                self.assertFalse(kb.up(LWIN))  # Windows saw Win go down, so it gets the release
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "cancel"])
                self.all_balanced(kb)

    def test_a_modifier_outside_the_shortcut_is_a_third_key(self):  # R5
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for other in outsiders(shortcut):
                    kb = Keyboard(self, shortcut)
                    for vk in partners(shortcut):
                        kb.down(vk)
                    kb.down(LWIN)
                    self.assertTrue(kb.down(other))
                    self.assertEqual(kb.windows[-2:], [(LWIN, "down"), (other, "down")])
                    for vk in (other, LWIN, *partners(shortcut)):
                        kb.up(vk)
                    self.assertEqual(kb.sent(), ["start", "cancel"])
                    self.all_balanced(kb)

    def test_an_extra_modifier_held_first_means_another_shortcut(self):  # R7
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for other in outsiders(shortcut):
                    kb = Keyboard(self, shortcut)
                    kb.down(other)
                    for vk in partners(shortcut):
                        kb.down(vk)
                    self.assertFalse(kb.down(LWIN))  # goes to Windows untouched
                    for vk in (LWIN, *partners(shortcut), other):
                        kb.up(vk)
                    self.assertEqual(kb.sent(), [])
                    self.assertEqual(dummy_taps(kb), 0)
                    self.all_balanced(kb)

    def test_releasing_any_key_of_the_shortcut_stops(self):  # R8
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                for released in [LWIN] + partners(shortcut):
                    kb = Keyboard(self, shortcut)
                    keys = partners(shortcut) + [LWIN]
                    for vk in keys:
                        kb.down(vk)
                    kb.up(released)
                    self.assertEqual(kb.sent(), ["start", "stop"])
                    for vk in keys:
                        if vk != released:
                            kb.up(vk)
                    self.assertEqual(kb.sent(), [])
                    self.all_balanced(kb)

    def test_the_other_side_of_a_shortcut_key_does_not_cancel(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                for vk in partners(shortcut):
                    kb.down(vk)
                kb.down(LWIN)
                other_side = RIGHT_KEY[[name for name in FAMILIES if name in shortcut][0]]
                self.assertFalse(kb.down(other_side))
                kb.up(other_side)
                self.assertTrue(kb.hotkey.recording)
                self.assertEqual(kb.sent(), ["start"])

    def test_pressing_a_partner_again_starts_again(self):  # R6
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                first = partners(shortcut)[0]
                for vk in partners(shortcut):
                    kb.down(vk)
                kb.down(LWIN)
                kb.up(first)
                kb.down(first)  # Win still held and hidden: nothing to release
                pressed_again = len(kb.windows) - 1
                for vk in [LWIN] + partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop", "start", "stop"])
                self.assertEqual(dummy_taps(kb), 2 if masked(shortcut) else 0)
                if masked(shortcut):  # the second dummy comes after the new press, before its release
                    self.assertEqual(kb.windows[pressed_again:pressed_again + 2],
                                     [(first, "down"), (VK_DUMMY, "down")])
                self.all_balanced(kb)

    def test_paused_hints_and_still_hides_win(self):  # R9
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                kb.hotkey.paused = True
                for vk in partners(shortcut):
                    kb.down(vk)
                self.assertTrue(kb.down(LWIN))
                self.assertTrue(kb.up(LWIN))
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["paused_press"])
                self.assertEqual(dummy_taps(kb), 1 if masked(shortcut) else 0)
                self.all_balanced(kb)

    def test_a_missed_partner_release_is_forgotten(self):  # R11
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                first = partners(shortcut)[0]
                for vk in partners(shortcut):
                    kb.down(vk)
                kb.missed_up(first)  # released on the Ctrl+Alt+Del screen
                self.assertFalse(kb.down(LWIN))  # Windows sees Win: the Start menu works
                kb.up(LWIN)
                for vk in partners(shortcut)[1:]:
                    kb.up(vk)
                self.assertEqual(kb.sent(), [])
                self.assertEqual(kb.hotkey.modifiers_held(), set())  # a paste wouldn't wait

    def test_the_old_default_does_nothing_with_another_shortcut(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = Keyboard(self, shortcut)
                if shortcut == DEFAULT:
                    continue
                kb.down(LCTRL)
                self.assertFalse(kb.down(LWIN))
                kb.up(LWIN)
                kb.up(LCTRL)
                self.assertEqual(kb.sent(), [])
                self.assertEqual(kb.windows, [(LCTRL, "down"), (LWIN, "down"), (LWIN, "up"), (LCTRL, "up")])

    def test_alt_is_masked_while_it_is_down(self):  # R15
        kb = Keyboard(self, frozenset({"Alt", "Win"}))
        kb.down(LALT)
        kb.down(LWIN)
        kb.up(LWIN)
        kb.up(LALT)
        # Windows sees Alt used with another key, so releasing it can't open the menu bar.
        self.assertEqual(kb.windows, [(LALT, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up"), (LALT, "up")])


class InvariantTest(unittest.TestCase):
    """Random key sequences (the golden file's generator) through each shortcut: Windows' view
    stays balanced, a recording only starts on exactly the shortcut, and always ends.

    Left out: sequences where the hook saw a release Windows didn't ("!"), which is what the
    stuck-key repair is for, and, after running, those where Windows got a release of a Win the
    hook was hiding ("~LW" while hidden): nothing can tell the hook, a known limit.
    A missed release of a Win the hook wasn't hiding is included since M14 bug 3's fix."""

    CASES = 600

    def replay(self, shortcut, case):
        replay = hotkey_golden.Replay(lambda events: Hotkey(events, debug=True, shortcut=shortcut))
        replay.run(case)
        return replay, [families for _, families in replay.starts]

    def test_invariants(self):
        rng = random.Random(1414)
        cases = [case for case in (hotkey_golden.random_case(rng) for _ in range(self.CASES * 2))
                 if "!" not in case][:self.CASES]
        self.assertEqual(len(cases), self.CASES)
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                checked = with_missed_win = 0
                for case in cases:
                    replay, starts = self.replay(shortcut, case)
                    if replay.missed_hidden_win:
                        continue
                    checked += 1
                    with_missed_win += replay.missed_visible_win is not None
                    state = {}
                    for kind, vk, down, _, _ in replay.windows:
                        if not down:
                            self.assertTrue(state.get(vk), f"release without a press: {case}")
                        state[vk] = down
                    self.assertFalse(any(state.values()), f"Windows thinks a key is held: {case}")
                    for held in starts:
                        self.assertEqual(held, set(shortcut), f"started without exactly the shortcut: {case}")
                    kinds = [k for k in replay.worker if k in ("start", "stop", "cancel")]
                    for first, second in zip(kinds, kinds[1:]):
                        self.assertFalse(first == "start" and second == "start", case)
                    self.assertFalse(replay.hotkey.recording, case)
                self.assertGreater(checked, 590)  # measured: 594 to 599 of 600
                self.assertGreater(with_missed_win, 45)  # measured: 51 to 56, which the old rule failed 4 to 19 of


ALT_SHIFT_KEYS = FAMILY_KEYS["Alt"] | FAMILY_KEYS["Shift"]
SHORT_NAMES = {"Ctrl": "LC", "Alt": "LA", "Shift": "LS", "Win": "LW"}


def unmasked_keys(shortcut, case):
    """Alt or Shift keys that were held when the shortcut started (or hinted while paused), but
    that Windows saw go down and up with no other key pressed in between. Such a release opens
    the app's menu bar (Alt) or switches the keyboard layout (Alt+Shift): M14 bugs 1 and 2.

    "Another key" is the dummy key or any key that isn't a modifier. A release the hook never
    saw ("~", the secure screen) is skipped: nothing can be sent before it."""
    replay = hotkey_golden.Replay(lambda events: Hotkey(events, debug=True, shortcut=shortcut))
    marks = []  # (position in Windows' view, Alt/Shift keys Windows thinks are down) at each start
    missed = set()  # positions of releases only Windows saw
    step = replay.step

    def checked_step(token):
        before = len(replay.worker)
        step(token)
        if token.startswith("~"):
            missed.add(len(replay.windows) - 1)
        for kind in replay.worker[before:]:
            if kind in ("start", "paused_press"):
                marks.append((len(replay.windows),
                              [vk for vk in ALT_SHIFT_KEYS if replay.windows_thinks_held(vk)]))

    replay.step = checked_step
    replay.run(case)
    seen = replay.windows
    found = []
    for at, held in marks:
        for vk in held:
            pressed = max(i for i in range(at) if seen[i][1] == vk and seen[i][2])
            releases = [i for i in range(at, len(seen)) if seen[i][1] == vk and not seen[i][2]]
            if not releases or releases[0] in missed:
                continue
            between = seen[pressed + 1:releases[0]]
            if not any(down and (key == VK_DUMMY or key not in hotkey_module.MODIFIER_NAMES)
                       for _, key, down, _, _ in between):
                found.append(hotkey_module.MODIFIER_NAMES[vk])
    return found


class MaskInvariantTest(unittest.TestCase):
    """Every Alt or Shift key held for the shortcut gets a key pressed while it's down in Windows,
    so releasing it can't open a menu or switch the layout (R15). Keys injected while the hook
    handles a key reach Windows before that key (see Keyboard), which the replay models."""

    def every_order(self, shortcut):
        keys = [SHORT_NAMES[name] for name in FAMILIES if name in shortcut]
        for presses in itertools.permutations(keys):
            for releases in itertools.permutations(keys):
                for paused in ("", "P "):
                    yield (paused + " ".join("+" + k for k in presses) + " "
                           + " ".join("-" + k for k in releases))
        for partner in keys[:-1]:  # R6: a partner released and pressed again, Win still held
            yield f"{' '.join('+' + k for k in keys)} -{partner} +{partner} " + " ".join("-" + k for k in keys)

    def test_every_key_order(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                cases = list(self.every_order(shortcut))
                failing = [(case, keys) for case in cases if (keys := unmasked_keys(shortcut, case))]
                self.assertFalse(failing, f"{len(failing)} of {len(cases)} sequences leave Alt or Shift "
                                          f"unmasked, e.g. {failing[:2]}")

    def test_random_sequences(self):
        rng = random.Random(1515)
        cases = [case for case in (hotkey_golden.random_case(rng) for _ in range(800)) if "!" not in case][:600]
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                failing = [case for case in cases if unmasked_keys(shortcut, case)]
                self.assertFalse(failing, f"{len(failing)} of {len(cases)} random sequences leave Alt or "
                                          f"Shift unmasked, e.g. {failing[:2]}")


WM_KEYUP = 0x0101
VK_V = 0x56


class RealPathKeyboard(Keyboard):
    """Like Keyboard, but every event goes through the hook's real entry point (_on_key), and
    every key the hook injects comes back through it too, flagged as injected, before it
    reaches Windows (Windows calls low-level hooks for injected keys as well, from inside
    SendInput: AutoHotkey's source says its injected keys call "the keybd hook recursively")."""

    def __init__(self, test, shortcut=None):
        super().__init__(test, shortcut)
        patch = mock.patch.object(hotkey_module.user32, "CallNextHookEx", lambda *args: 0)
        patch.start()
        test.addCleanup(patch.stop)

    def _call_hook(self, vk, down, flags=0):
        kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=0)
        w_param = hotkey_module.WM_KEYDOWN if down else WM_KEYUP
        return self.hotkey._on_key(hotkey_module.HC_ACTION, w_param, ctypes.addressof(kb)) == 1

    def _injected(self, keys):
        for key in keys:
            down = not key[1] & KEYEVENTF_KEYUP
            self._call_hook(key[0], down, hotkey_module.LLKHF_INJECTED)
            self.windows.append((key[0], "down" if down else "up"))

    def inject(self, keys):
        """Keys injected from outside the hook: the paste's Ctrl+V, the stuck-key repair, other programs."""
        self._injected(keys)

    def _key(self, vk, down):
        swallowed = self._call_hook(vk, down)
        if not swallowed:
            self.windows.append((vk, "down" if down else "up"))
        return swallowed


class MaskFlagTest(unittest.TestCase):
    """The mask sent on the next key event (R15, _mask_pending): only real key events send it,
    it's sent once, and it can't outlive a shortcut change."""

    def test_our_own_injected_keys_never_send_the_mask(self):
        kb = RealPathKeyboard(self, frozenset({"Alt", "Win"}))
        kb.down(LWIN)
        kb.down(LALT)  # R2's own dummy and Win release went back through the hook, flagged as injected
        self.assertTrue(kb.hotkey._mask_pending)
        self.assertEqual(kb.windows, [(LWIN, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up"), (LWIN, "up"),
                                      (LALT, "down")])
        # Every kind of key we inject, arriving while Alt is held, leaves the flag alone.
        kb.inject([(VK_DUMMY, 0), (VK_DUMMY, KEYEVENTF_KEYUP)])  # R1's dummy, the stuck-key repair's
        kb.inject([(LWIN, KEYEVENTF_KEYUP)])  # a Win release (R2, the stuck-key repair)
        kb.inject([(LWIN, 0), (D, 0), (D, KEYEVENTF_KEYUP), (LWIN, KEYEVENTF_KEYUP)])  # an R5 replay
        kb.inject([(LCTRL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (LCTRL, KEYEVENTF_KEYUP)])  # a paste
        self.assertTrue(kb.hotkey._mask_pending)
        self.assertEqual(dummy_taps(kb), 2)  # R2's and the injected one above, no mask yet
        kb.up(LALT)  # the first real key event: the mask lands right before Alt's release
        self.assertFalse(kb.hotkey._mask_pending)
        self.assertEqual(kb.windows[-3:], [(VK_DUMMY, "down"), (VK_DUMMY, "up"), (LALT, "up")])
        kb.up(LWIN)
        self.assertEqual(kb.sent(), ["start", "stop"])

    def test_the_real_path_matches_the_fake_one(self):
        """_on_key with injected keys fed back gives Windows the same view as calling _handle."""
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                results = []
                # One after the other: each keyboard replaces the module's send_keys with its own.
                for make in (Keyboard, RealPathKeyboard):
                    kb = make(self, shortcut)
                    for vk in [LWIN] + partners(shortcut) + [D]:
                        kb.down(vk)
                    for vk in [D, LWIN] + partners(shortcut):
                        kb.up(vk)
                    results.append((kb.windows, kb.sent()))
                self.assertEqual(results[0], results[1])
                self.assertIn((VK_DUMMY, "down"), results[0][0])

    def test_sent_once_even_while_alt_repeats(self):
        kb = Keyboard(self, frozenset({"Alt", "Win"}))
        kb.down(LWIN)
        kb.down(LALT)
        for _ in range(3):
            kb.down(LALT)  # auto-repeat: the first one carries the mask
        kb.up(LALT)
        kb.up(LWIN)
        self.assertEqual(kb.windows[4:], [(LALT, "down"), (VK_DUMMY, "down"), (VK_DUMMY, "up"), (LALT, "down"),
                                          (LALT, "down"), (LALT, "down"), (LALT, "up")])

    def test_a_missed_alt_release_leaves_one_harmless_tap(self):
        """Alt+Win, Win then Alt, and both released on the lock screen: the hook sees no key event
        before the next one after unlocking, so the pending mask goes out then, once, BEFORE that
        key (here a Win tap), never between its press and release: Start still opens."""
        kb = Keyboard(self, frozenset({"Alt", "Win"}))
        kb.down(LWIN)
        kb.down(LALT)
        kb.missed_up(LALT)  # Windows saw Alt go up; Win was already released for Windows by R2
        self.assertTrue(kb.hotkey._mask_pending)
        before = len(kb.windows)
        self.assertFalse(kb.down(LWIN))  # after unlocking, the user taps Win
        self.assertFalse(kb.up(LWIN))
        self.assertEqual(kb.windows[before:], [(VK_DUMMY, "down"), (VK_DUMMY, "up"), (LWIN, "down"), (LWIN, "up")])
        self.assertFalse(kb.hotkey._mask_pending)
        kb.down(D)
        kb.up(D)
        self.assertEqual(dummy_taps(kb), 2)  # R2's and the late mask, nothing more
        self.assertEqual(kb.sent(), ["start", "stop"])
        for vk in (LWIN, LALT, D):
            self.assertTrue(kb.balanced(vk))

    def test_a_shortcut_change_waits_for_the_mask(self):
        kb = Keyboard(self, frozenset({"Alt", "Win"}))
        kb.down(LWIN)
        kb.down(LALT)
        kb.hotkey.set_shortcut(DEFAULT)
        kb.up(LALT)  # the mask goes out under the old shortcut; nothing switches while Win is held
        self.assertEqual(kb.windows[-3:], [(VK_DUMMY, "down"), (VK_DUMMY, "up"), (LALT, "up")])
        self.assertEqual(kb.hotkey.shortcut, {"Alt", "Win"})
        kb.up(LWIN)
        kb.down(D)  # idle: switches now
        self.assertEqual(kb.hotkey.shortcut, DEFAULT)

    def test_never_switches_while_the_flag_is_set(self):
        """_apply_requested runs after the flag is used on every key event, and the flag is only
        set while the shortcut is held (not idle), so a switch never sees it set."""
        original = Hotkey._apply_requested
        checked = []

        def apply_requested(hotkey):
            checked.append(hotkey._mask_pending)
            original(hotkey)

        rng = random.Random(1616)
        cases = [hotkey_golden.random_case(rng) for _ in range(300)]
        with mock.patch.object(Hotkey, "_apply_requested", apply_requested):
            for shortcut in ALLOWED:
                for case in cases:
                    replay = hotkey_golden.Replay(lambda events: Hotkey(events, shortcut=shortcut))
                    step = replay.step

                    def switching_step(token, replay=replay, step=step):
                        replay.hotkey.set_shortcut(rng.choice(ALLOWED))
                        step(token)

                    replay.step = switching_step
                    replay.run(case)
        self.assertGreater(len(checked), 1000)
        self.assertNotIn(True, checked)


class SetShortcutTest(unittest.TestCase):
    """Changing the shortcut while the app runs (the worker thread asks, the hook thread switches)."""

    def test_refuses_a_shortcut_that_is_not_allowed(self):
        hotkey = Hotkey(queue.Queue())
        for families in ({"Shift", "Win"}, {"Ctrl", "Shift"}, {"Win"}, {"Ctrl", "Alt", "Shift", "Win"}):
            with self.assertRaises(ValueError):
                hotkey.set_shortcut(families)
        with self.assertRaises(ValueError):
            Hotkey(queue.Queue(), shortcut={"Ctrl", "Alt"})

    def test_switches_on_the_next_key_event_when_idle(self):
        kb = Keyboard(self)
        kb.hotkey.set_shortcut({"Alt", "Win"})
        self.assertEqual(kb.hotkey.requested, {"Alt", "Win"})
        self.assertEqual(kb.hotkey.shortcut, DEFAULT)  # not before a key event
        kb.down(LALT)  # idle: switches first, then handles the key
        self.assertEqual(kb.hotkey.shortcut, {"Alt", "Win"})
        self.assertTrue(kb.down(LWIN))
        kb.up(LWIN)
        kb.up(LALT)
        self.assertEqual(kb.sent(), ["start", "stop"])

    def test_never_switches_in_the_middle_of_a_recording(self):
        kb = Keyboard(self)
        kb.down(LCTRL)
        kb.down(LWIN)
        kb.hotkey.set_shortcut({"Alt", "Win"})
        kb.down(LCTRL)  # a repeat
        self.assertEqual(kb.hotkey.shortcut, DEFAULT)
        kb.up(LWIN)  # the old shortcut's rules finish it: Win's release stays hidden
        self.assertEqual(kb.hotkey.shortcut, DEFAULT)  # Ctrl is still held
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.balanced(LWIN))
        kb.down(LCTRL)  # the next sequence uses the new shortcut
        self.assertEqual(kb.hotkey.shortcut, {"Alt", "Win"})
        self.assertFalse(kb.down(LWIN))
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), [])

    def test_the_latest_request_wins(self):
        kb = Keyboard(self)
        for families in ({"Alt", "Win"}, {"Ctrl", "Shift", "Win"}, {"Ctrl", "Alt", "Win"}):
            kb.hotkey.set_shortcut(families)
        kb.down(D)
        kb.up(D)
        self.assertEqual(kb.hotkey.shortcut, {"Ctrl", "Alt", "Win"})

    def test_asking_from_another_thread_while_keys_arrive(self):
        """The worker thread keeps changing its mind while the hook handles keys. Nothing is
        lost or mixed: every sequence is handled by one whole shortcut, Windows' view stays
        balanced, and the hook ends up on the last request."""
        kb = Keyboard(self)
        stop = threading.Event()
        last = []

        def worker():
            rng = random.Random(7)
            while not stop.is_set():
                families = rng.choice(ALLOWED)
                kb.hotkey.set_shortcut(families)
                last[:] = [families]

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            deadline = time.monotonic() + 1.0
            rounds = 0
            while time.monotonic() < deadline or rounds < 200:
                rules = kb.hotkey._rules
                keys = [LEFT_KEY[name] for name in FAMILIES if name in rules.families and name != "Win"]
                for vk in keys + [LWIN]:
                    kb.down(vk)
                for vk in [LWIN] + keys:
                    kb.up(vk)
                rounds += 1
        finally:
            stop.set()
            thread.join()
        for vk in {key for key, _ in kb.windows}:
            self.assertTrue(kb.balanced(vk))
        kinds = [kind for kind in kb.sent() if kind in ("start", "stop", "cancel")]
        self.assertEqual(kinds.count("start"), kinds.count("stop") + kinds.count("cancel"))
        kb.down(D)  # idle: the hook takes the last request
        kb.up(D)
        self.assertEqual(kb.hotkey.shortcut, last[0])


class ApplyNewHotkeyTest(unittest.TestCase):
    """main.apply_new_hotkey: the worker passes a changed config.json to the hook, pill and tray."""

    def test_a_new_hotkey_is_passed_on_once(self):
        import main

        hotkey = Hotkey(queue.Queue())
        changes = []
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(main.apply_new_hotkey({"hotkey": "Alt+Win"}, hotkey, changes.append))
            self.assertFalse(main.apply_new_hotkey({"hotkey": "Alt+Win"}, hotkey, changes.append))
        self.assertEqual(hotkey.requested, {"Alt", "Win"})
        self.assertEqual(changes, ["Alt+Win"])


SPACE, Z_KEY, A_KEY, LEFT_ARROW = 0x20, 0x5A, 0x41, 0x25


class TimedKeyboard(Keyboard):
    """A Keyboard with the hook on a fake clock, so a test can wait past a recording's first second."""

    def __init__(self, test, shortcut=None):
        super().__init__(test, shortcut)
        self.clock = hotkey_golden.FakeTime()
        patch = mock.patch.object(hotkey_module, "time", self.clock)
        patch.start()
        test.addCleanup(patch.stop)

    def wait(self, seconds):
        self.clock.now += seconds

    def hold_shortcut(self):
        for vk in partners(self.hotkey.shortcut):
            self.down(vk)
        self.down(LWIN)

    def release_shortcut(self):
        self.up(LWIN)
        for vk in partners(self.hotkey.shortcut):
            self.up(vk)

    def all_events(self):
        events = []
        while not self.events.empty():
            events.append(self.events.get()[0])
        return events


class GraceTest(unittest.TestCase):
    """M17 item 14: another key in a recording's first second still cancels it (you meant a
    Windows shortcut); after that, other keys are swallowed and the dictation is kept."""

    def all_balanced(self, kb):
        for vk in {key for key, _ in kb.windows}:
            self.assertTrue(kb.balanced(vk), f"key 0x{vk:02X} unbalanced: {kb.windows}")

    def test_inside_the_first_second_a_third_key_still_cancels(self):  # R5, unchanged
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = TimedKeyboard(self, shortcut)
                kb.hold_shortcut()
                kb.wait(0.99)
                self.assertTrue(kb.down(D))  # held back and replayed after Win
                self.assertEqual(kb.windows[-2:], [(LWIN, "down"), (D, "down")])
                kb.up(D)
                self.assertFalse(kb.up(LWIN))  # Windows saw Win go down, so it gets the release
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "cancel"])
                self.all_balanced(kb)

    def test_after_the_first_second_other_keys_are_swallowed(self):
        for shortcut in ALLOWED:
            for key in (D, LEFT_ARROW, SPACE, Z_KEY, A_KEY, *outsiders(shortcut)):
                with self.subTest(shortcut=shortcut_text(shortcut), key=f"0x{key:02X}"):
                    kb = TimedKeyboard(self, shortcut)
                    kb.hold_shortcut()
                    kb.wait(1.0)
                    self.assertTrue(kb.down(key))
                    self.assertTrue(kb.up(key))
                    self.assertTrue(kb.hotkey.recording)
                    kb.wait(2.0)
                    kb.release_shortcut()
                    self.assertEqual(kb.sent(), ["start", "stop"])  # kept, never cancelled
                    seen = [vk for vk, _ in kb.windows]
                    self.assertNotIn(key, seen)
                    self.assertNotIn(LWIN, seen)  # Win stayed hidden: no replay
                    self.assertEqual(kb.hotkey._swallowed, set())
                    self.all_balanced(kb)

    def test_auto_repeat_and_a_release_after_the_shortcut_stay_hidden(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.wait(1.5)
        for _ in range(5):
            self.assertTrue(kb.down(A_KEY))  # the press and its auto-repeats
        kb.release_shortcut()  # A still held: the dictation stops and is kept
        self.assertEqual(kb.sent(), ["start", "stop"])
        self.assertTrue(kb.down(A_KEY))  # still repeating after the shortcut was let go
        self.assertTrue(kb.up(A_KEY))
        self.assertNotIn(A_KEY, [vk for vk, _ in kb.windows])
        self.assertFalse(kb.down(A_KEY))  # a new press after its release is typed normally
        self.assertFalse(kb.up(A_KEY))
        self.all_balanced(kb)

    def test_alt_never_reaches_windows_so_no_menu_bar(self):
        kb = TimedKeyboard(self)  # Ctrl+Win
        kb.hold_shortcut()
        kb.wait(2.0)
        self.assertTrue(kb.down(LALT))
        self.assertTrue(kb.up(LALT))
        kb.release_shortcut()
        self.assertNotIn(LALT, [vk for vk, _ in kb.windows])
        self.assertEqual(kb.sent(), ["start", "stop"])

    def test_a_modifier_let_go_last_is_not_stuck(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.wait(2.0)
        self.assertTrue(kb.down(LSHIFT))
        kb.release_shortcut()
        self.assertIn("stop", kb.all_events())
        self.assertEqual(kb.hotkey.modifiers_held(), set())  # the paste doesn't wait for a key Windows never saw
        self.assertTrue(kb.up(LSHIFT))
        self.assertEqual(kb.all_events(), ["check_stuck"])  # the last modifier let go
        before = list(kb.windows)
        with contextlib.redirect_stdout(io.StringIO()):
            kb.hotkey.release_stuck_modifiers()
        self.assertEqual(kb.windows, before)  # nothing to repair
        self.all_balanced(kb)

    def test_a_missed_release_never_eats_a_later_press(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.wait(2.0)
        self.assertTrue(kb.down(A_KEY))  # swallowed; its release is then missed (the secure screen)
        kb.release_shortcut()
        self.assertFalse(kb.down(D))  # typing goes on
        self.assertFalse(kb.up(D))
        self.assertFalse(kb.down(A_KEY))  # a new press of A reaches Windows
        self.assertFalse(kb.up(A_KEY))
        self.assertNotIn(A_KEY, kb.hotkey._swallowed)
        self.all_balanced(kb)

    def test_known_limit_the_same_key_as_the_very_next_press(self):
        """Nothing tells a missed release from an auto-repeat when the same key is the very next
        press, so that one press is swallowed too, and its release ends it. It needs a release
        missed on the secure screen with no key pressed since: rare."""
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.wait(2.0)
        self.assertTrue(kb.down(A_KEY))  # its release is missed
        kb.release_shortcut()
        self.assertTrue(kb.down(A_KEY))  # looks like a repeat
        self.assertTrue(kb.up(A_KEY))
        self.assertFalse(kb.down(A_KEY))  # from then on, normal
        self.assertFalse(kb.up(A_KEY))

    def test_a_new_recording_gets_its_own_first_second(self):  # R6
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = TimedKeyboard(self, shortcut)
                kb.hold_shortcut()
                kb.wait(5.0)
                first = partners(shortcut)[0]
                kb.up(first)  # stop: kept
                kb.down(first)  # Win still held and hidden: a new recording
                kb.wait(0.5)
                self.assertTrue(kb.down(D))  # inside the new recording's first second: cancel, replay
                self.assertIn((D, "down"), kb.windows)
                kb.up(D)
                self.assertFalse(kb.up(LWIN))  # replayed, so Windows gets its release
                for vk in partners(shortcut):
                    kb.up(vk)
                self.assertEqual(kb.sent(), ["start", "stop", "start", "cancel"])
                self.all_balanced(kb)

    def test_paused_shortcuts_still_work_after_a_second(self):  # R9
        kb = TimedKeyboard(self)
        kb.hotkey.paused = True
        kb.hold_shortcut()
        kb.wait(3.0)
        self.assertTrue(kb.down(D))  # nothing is recording: Win replayed before D, as today
        self.assertEqual(kb.windows[-2:], [(LWIN, "down"), (D, "down")])
        kb.up(D)
        kb.up(LWIN)
        kb.up(LCTRL)
        self.assertEqual(kb.sent(), ["paused_press"])
        self.all_balanced(kb)


class SwallowInvariantTest(unittest.TestCase):
    """InvariantTest's and MaskInvariantTest's random sequences with the first second set to 0,
    so every other key pressed during a recording is swallowed. A "~" release of a key the hook
    was swallowing (Windows gets a release it never saw pressed: the secure screen) is skipped,
    like a missed release of a hidden Win."""

    CASES = 600

    def setUp(self):
        patch = mock.patch.object(hotkey_module, "CANCEL_GRACE_SECONDS", 0.0)
        patch.start()
        self.addCleanup(patch.stop)

    def run_case(self, shortcut, case):
        replay = hotkey_golden.Replay(lambda events: Hotkey(events, debug=True, shortcut=shortcut))
        missed = []
        step = replay.step

        def checked_step(token):
            if token[0] == "~" and hotkey_golden.KEYS[token[1:]][0] in replay.hotkey._swallowed:
                missed.append(token)
            step(token)

        replay.step = checked_step
        replay.run(case)
        return replay, bool(missed)

    def test_invariants(self):
        rng = random.Random(1414)
        cases = [case for case in (hotkey_golden.random_case(rng) for _ in range(self.CASES * 2))
                 if "!" not in case][:self.CASES]
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                checked = with_swallowing = 0
                for case in cases:
                    replay, missed_swallowed = self.run_case(shortcut, case)
                    if replay.missed_hidden_win or missed_swallowed:
                        continue
                    checked += 1
                    with_swallowing += any(kind.startswith("debug:held back a key") for kind in replay.worker)
                    state = {}
                    for kind, vk, down, _, _ in replay.windows:
                        if not down:
                            self.assertTrue(state.get(vk), f"release without a press: {case}")
                        state[vk] = down
                    self.assertFalse(any(state.values()), f"Windows thinks a key is held: {case}")
                    for _, held in replay.starts:
                        self.assertEqual(held, set(shortcut), f"started without exactly the shortcut: {case}")
                    kinds = [k for k in replay.worker if k in ("start", "stop", "cancel")]
                    self.assertNotIn("cancel", kinds, case)
                    for first, second in zip(kinds, kinds[1:]):
                        self.assertFalse(first == "start" and second == "start", case)
                    self.assertFalse(replay.hotkey.recording, case)
                    self.assertEqual(replay.hotkey._swallowed, set(), case)
                # Measured: 588 to 599 of 600 checked; 6 (Alt+Shift+Win) to 48 (Ctrl+Win) swallowed a key.
                self.assertGreater(checked, 580, f"only {checked} checked")
                self.assertGreaterEqual(with_swallowing, 5, f"only {with_swallowing} swallowed a key")

    def test_alt_and_shift_stay_masked(self):
        rng = random.Random(1515)
        cases = [case for case in (hotkey_golden.random_case(rng) for _ in range(800)) if "!" not in case][:600]
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                failing = [case for case in cases if unmasked_keys(shortcut, case)]
                self.assertFalse(failing, f"{len(failing)} of {len(cases)} random sequences leave Alt or "
                                          f"Shift unmasked, e.g. {failing[:2]}")


class ReleaseCheckTest(unittest.TestCase):
    """M17 item 14: the hook thread's timer ends a recording whose release the hook missed
    (the secure screen, an admin window, a removed hook), so it's transcribed."""

    def all_balanced(self, kb):
        for vk in {key for key, _ in kb.windows}:
            self.assertTrue(kb.balanced(vk), f"key 0x{vk:02X} unbalanced: {kb.windows}")

    def test_a_missed_release_stops_and_keeps_the_recording(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = TimedKeyboard(self, shortcut)
                kb.hold_shortcut()
                kb.wait(2.0)
                kb.hotkey._check_release()  # Windows says the shortcut's keys are held: noted
                self.assertTrue(kb.hotkey.recording)
                for vk in partners(shortcut):
                    kb.missed_up(vk)  # let go on the Ctrl+Alt+Del screen: only Windows saw it
                kb.hotkey._check_release()
                self.assertFalse(kb.hotkey.recording)
                self.assertEqual(kb.sent(), ["start", "stop"])  # a stop: the worker transcribes it
                self.assertEqual(kb.hotkey._held, set())
                self.assertEqual(kb.hotkey.modifiers_held(), set())  # the paste won't wait for a phantom Win
                self.assertEqual(kb.hotkey._swallowed, {LWIN})  # Windows never saw it go down
                self.all_balanced(kb)

    def test_after_it_a_lone_partner_starts_nothing_and_the_shortcut_works(self):
        for shortcut in ALLOWED:
            with self.subTest(shortcut=shortcut_text(shortcut)):
                kb = TimedKeyboard(self, shortcut)
                kb.hold_shortcut()
                kb.wait(2.0)
                kb.hotkey._check_release()
                for vk in partners(shortcut):
                    kb.missed_up(vk)
                kb.hotkey._check_release()
                kb.sent()
                for vk in partners(shortcut):  # the hidden Win was let go on that screen too
                    kb.down(vk)
                self.assertEqual(kb.sent(), [])  # no dictation from the partner keys alone
                for vk in partners(shortcut):
                    kb.up(vk)
                kb.hold_shortcut()
                kb.release_shortcut()
                self.assertEqual(kb.sent(), ["start", "stop"])
                self.all_balanced(kb)

    def test_the_next_win_press_is_a_new_press(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()  # Win is the last key pressed
        kb.wait(2.0)
        kb.hotkey._check_release()
        kb.missed_up(LCTRL)
        kb.hotkey._check_release()
        self.assertFalse(kb.down(LWIN))  # Win alone reaches Windows: the Start menu works
        self.assertFalse(kb.up(LWIN))
        self.assertNotIn(LWIN, kb.hotkey._swallowed)
        self.all_balanced(kb)

    def test_a_win_still_held_keeps_its_release_hidden(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.wait(2.0)
        kb.hotkey._check_release()
        kb.missed_up(LCTRL)  # Ctrl let go on the secure screen, Win still held
        kb.hotkey._check_release()
        self.assertTrue(kb.up(LWIN))  # Windows never saw it go down, so no release either
        self.assertNotIn(LWIN, [vk for vk, _ in kb.windows])
        self.all_balanced(kb)

    def test_still_held_nothing_happens(self):
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        for _ in range(3):
            kb.wait(0.25)
            kb.hotkey._check_release()
        self.assertTrue(kb.hotkey.recording)
        self.assertEqual(kb.sent(), ["start"])

    def test_never_fires_for_a_key_windows_never_reported_held(self):
        """Another app swallowing Ctrl: Windows never sees it, so it can't be "released"."""
        kb = TimedKeyboard(self)
        kb.hold_shortcut()
        kb.windows.remove((LCTRL, "down"))  # the other app's hook took it
        for _ in range(3):
            kb.wait(0.25)
            kb.hotkey._check_release()
        self.assertTrue(kb.hotkey.recording)
        self.assertEqual(kb.sent(), ["start"])

    def test_nothing_while_idle_or_paused(self):
        kb = TimedKeyboard(self)
        kb.hotkey._check_release()
        kb.hotkey.paused = True
        kb.hold_shortcut()
        kb.hotkey._check_release()
        kb.missed_up(LCTRL)
        kb.hotkey._check_release()
        self.assertEqual(kb.sent(), ["paused_press"])

    def fake_messages(self, hotkey, messages):
        """run_loop with GetMessageW handing out (message, hWnd, wParam) tuples, then quitting.

        Each message is built in a separate MSG and copied into run_loop's as raw bytes, the way
        Windows fills it: so run_loop only ever sees MSG's real fields, and a wrong field name
        there fails here too (the first version read msg.hwnd, which crashed the sandbox run)."""
        pending = list(messages)
        dispatched = []

        def get_message(msg_ref, *args):
            if not pending:
                return 0
            source = wintypes.MSG()
            source.message, source.hWnd, source.wParam = pending.pop(0)
            ctypes.memmove(ctypes.addressof(msg_ref._obj), ctypes.addressof(source), ctypes.sizeof(source))
            return 1

        patches = [mock.patch.object(hotkey_module.user32, "GetMessageW", get_message),
                   mock.patch.object(hotkey_module.user32, "TranslateMessage", lambda ref: 0),
                   mock.patch.object(hotkey_module.user32, "DispatchMessageW",
                                     lambda ref: dispatched.append(ref._obj.wParam) or 0)]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            hotkey.run_loop()
        return dispatched

    def test_run_loop_runs_it_for_its_own_timer_only(self):
        hotkey = Hotkey(queue.Queue())
        hotkey._timer_id = 77
        calls = []
        with mock.patch.object(hotkey, "_check_release", lambda: calls.append("check")):
            dispatched = self.fake_messages(hotkey, [(hotkey_module.WM_TIMER, None, 77),
                                                     (hotkey_module.WM_TIMER, None, 5),
                                                     (0x0401, None, 77)])
        self.assertEqual(calls, ["check"])
        self.assertEqual(dispatched, [5, 77])  # other messages are dispatched as before

    def test_an_error_in_it_becomes_a_hook_error(self):
        hotkey = Hotkey(queue.Queue())
        hotkey._timer_id = 77

        def broken():
            raise RuntimeError("boom")

        with mock.patch.object(hotkey, "_check_release", broken):
            self.fake_messages(hotkey, [(hotkey_module.WM_TIMER, None, 77)] * 2)
        events = [hotkey.events.get() for _ in range(hotkey.events.qsize())]
        self.assertEqual([kind for kind, _, _ in events], ["hook_error", "hook_error"])  # the loop kept going
        for _, _, detail in events:
            # M19: the error itself, so the worker prints its stack and can hide its message.
            self.assertIsInstance(detail, RuntimeError)

    def test_install_sets_the_timer_and_uninstall_kills_it(self):
        hotkey = Hotkey(queue.Queue())
        log = []
        patches = [
            mock.patch.object(hotkey_module.user32, "SetWindowsHookExW", lambda *args: 1234),
            mock.patch.object(hotkey_module.user32, "UnhookWindowsHookEx", lambda hook: log.append("unhook") or 1),
            mock.patch.object(hotkey_module.user32, "SetTimer",
                              lambda hwnd, timer_id, ms, proc: log.append(("set", hwnd, ms)) or 77),
            mock.patch.object(hotkey_module.user32, "KillTimer",
                              lambda hwnd, timer_id: log.append(("kill", hwnd, timer_id)) or 1),
            mock.patch.object(hotkey_module.kernel32, "SetConsoleCtrlHandler", lambda *args: 1),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            hotkey.install()
            self.assertEqual(hotkey._timer_id, 77)
            hotkey.uninstall()
            hotkey.uninstall()  # safe twice
        self.assertEqual(log, [("set", None, hotkey_module.RELEASE_CHECK_MS), ("kill", None, 77), "unhook"])
        self.assertEqual(hotkey._timer_id, 0)


class GoldenMarginTest(unittest.TestCase):
    """The golden check passes unchanged because no golden sequence presses a third key late in a
    recording: this proves it, so the unchanged numbers really show the first second is today's code."""

    def test_every_golden_third_key_comes_well_inside_the_first_second(self):
        latest = 0.0

        class Timed(Hotkey):
            def _handle_other(self, kb, is_down, now):
                nonlocal latest
                if is_down and self.recording:
                    latest = max(latest, now - self._recording_since)
                return super()._handle_other(kb, is_down, now)

        for case, _ in hotkey_golden.load():
            hotkey_golden.Replay(lambda events: Timed(events, debug=True)).run(case)
        self.assertGreater(latest, 0.1)  # measured 0.18s: the file does press third keys mid-recording
        self.assertLess(latest, hotkey_module.CANCEL_GRACE_SECONDS / 2)


if __name__ == "__main__":
    unittest.main()
