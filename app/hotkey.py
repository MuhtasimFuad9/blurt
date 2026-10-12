"""Hold-to-talk (Ctrl+Win by default) using a Windows low-level keyboard hook.

Windows calls our hook function for every key press in the whole system.
The shortcut is one of the five in app/shortcut.py. Every one includes Win,
plus one or two of Ctrl, Alt and Shift (its "partner" keys).
Rules for the hook callback:
- During the shortcut, the Win key is hidden from Windows (so no taskbar number
  badges and no Start menu). Otherwise every key is passed on untouched, except:
- Once a recording is CANCEL_GRACE_SECONDS old, any other key pressed is hidden from
  Windows too (its press, its repeats and its release), so a stray key never cancels a
  long dictation. Within that first second another key still cancels (a Windows shortcut
  like Ctrl+Win+D was meant).
- While recording, a timer on the hook thread asks Windows whether the shortcut is still
  held, and ends a recording whose release the hook missed (see _check_release).
- Windows' view must always stay balanced: every Win press Windows saw gets
  exactly one release, and a press it never saw gets no release.
- A shortcut with Alt or Shift gets a dummy key tap while its keys are held, so
  releasing them can't open an app's menu bar or switch the keyboard layout.
- Keys injected while the hook handles a key reach Windows BEFORE that key: Windows
  only passes the key on once the hook returns. (AutoHotkey relies on the same order:
  "the OS will see that the keystroke occurred while ALT or WIN is still down because
  we haven't done CallNextHookEx() yet".) So a dummy tap sent while a partner key's
  press is being handled lands before that press and doesn't cover it.
- It does no slow work: it updates a few variables, sometimes injects keys
  (only when their order matters), and puts events on a queue for the worker.
  Windows removes slow hooks.
"""

import ctypes
import os
import queue
import time
from ctypes import wintypes

from app.shortcut import ALLOWED, DEFAULT
from app.shortcut import text as shortcut_text

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Windows constants
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
WM_TIMER = 0x0113
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK_DUMMY = 0xE8  # unassigned key, used to stop the Start menu from opening

MODIFIER_NAMES = {
    0xA0: "Left Shift",
    0xA1: "Right Shift",
    0xA2: "Left Ctrl",
    0xA3: "Right Ctrl",
    0xA4: "Left Alt",
    0xA5: "Right Alt",
    0x5B: "Left Win",
    0x5C: "Right Win",
}
CTRL_KEYS = {0xA2, 0xA3}
WIN_KEYS = {0x5B, 0x5C}
SHIFT_ALT_KEYS = {0xA0, 0xA1, 0xA4, 0xA5}
FAMILY_KEYS = {"Ctrl": frozenset(CTRL_KEYS), "Alt": frozenset({0xA4, 0xA5}),
               "Shift": frozenset({0xA0, 0xA1}), "Win": frozenset(WIN_KEYS)}
# Never hidden as part of the shortcut, so Windows' own view of these is right (see _drop_missed_releases;
# a Win key is too, while it isn't hidden). The one exception: one pressed during a recording past its
# first second is swallowed (see _handle_other), and then Windows rightly thinks it's up.
VISIBLE_MODIFIERS = CTRL_KEYS | SHIFT_ALT_KEYS
EXTENDED_KEYS = {0xA3, 0xA5, 0x5B, 0x5C}  # SendInput needs a flag for these
OTHER_KEY_NAMES = {0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x73: "F4"}

# A modifier pressed by software (for example our own Ctrl+V paste) this recently
# is treated as intentional, not stuck.
INJECTED_GRACE_SECONDS = 1.0

# Another key this soon after a recording started cancels it (you meant a Windows shortcut).
# Later, other keys are swallowed until the shortcut is released, so the dictation is kept.
CANCEL_GRACE_SECONDS = 1.0

# While recording, how often the hook thread asks Windows whether the shortcut's other keys
# are still held (see _check_release).
RELEASE_CHECK_MS = 250

# With debug on, report key events that reached the hook this late. The clock
# only ticks every ~16ms, so smaller numbers can't be measured anyway.
HOOK_LAG_WARN_MS = 30

# A second Ctrl+C gives the hook thread this long to unhook itself before we do it.
FORCE_QUIT_HOOK_WAIT = 1.0

# Types that are pointer-sized on 64-bit Python
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


# SendInput needs the full INPUT union (mouse, keyboard, hardware) so the size is right.
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
CONSOLE_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

# Exact argument and return types. Without these, 64-bit Python can crash randomly.
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ULONG_PTR
user32.KillTimer.argtypes = [wintypes.HWND, ULONG_PTR]
user32.KillTimer.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount.argtypes = []
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.SetConsoleCtrlHandler.argtypes = [CONSOLE_HANDLER, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL


def _windows_thinks_held(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def send_keys(keys):
    """Inject key events, in order, in one call. keys is a list of (vk, flags) or (vk, flags, scan).

    Returns how many events Windows took (SendInput's count). Fewer than len(keys) only
    catches Windows refusing input outright: Microsoft's documentation names input blocked
    by another thread, and a UAC prompt or the lock screen in front is expected to refuse
    it too (not tested here). It says nothing about an app running as administrator: the
    documentation says a UIPI block shows in neither the count nor GetLastError, so Ctrl+V
    into an admin window still counts as sent.
    """
    inputs = (INPUT * len(keys))()
    for i, key in enumerate(keys):
        vk, flags = key[0], key[1]
        scan = key[2] if len(key) > 2 else 0
        if vk in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return user32.SendInput(len(keys), inputs, ctypes.sizeof(INPUT))


def _key_name(vk):
    if vk in MODIFIER_NAMES:
        return MODIFIER_NAMES[vk]
    if vk in OTHER_KEY_NAMES:
        return OTHER_KEY_NAMES[vk]
    if 0x30 <= vk <= 0x5A:
        return chr(vk)
    return f"key 0x{vk:02X}"


def send_dummy_key():
    """Tap an unassigned key so Windows thinks Win was used in a shortcut (no Start menu)."""
    send_keys([(VK_DUMMY, 0), (VK_DUMMY, KEYEVENTF_KEYUP)])


class _Rules:
    """What the hook needs to know about one shortcut, worked out once. Never changed after
    it's made, so the hook can swap the whole thing in one step (see Hotkey.set_shortcut)."""

    def __init__(self, families):
        self.families = frozenset(families)
        partner_names = [name for name in ("Ctrl", "Alt", "Shift") if name in self.families]
        self.partners = tuple(FAMILY_KEYS[name] for name in partner_names)  # each one must be held
        self.partner_keys = frozenset().union(*self.partners)
        self.partner_text = shortcut_text(partner_names)  # "Ctrl", "Alt+Shift"
        # Modifiers that aren't in the shortcut: holding one means it's another shortcut.
        self.others = frozenset().union(*(FAMILY_KEYS[name] for name in ("Ctrl", "Alt", "Shift")
                                          if name not in self.families))
        # Alt or Shift pressed and released alone can open a menu bar or switch the layout.
        self.mask = "Alt" in self.families or "Shift" in self.families


class Hotkey:
    """Puts (event, timestamp, detail) tuples on the queue.

    Events: "start", "stop", "cancel", "paused_press", "check_stuck", "hook_error", "debug".

    While paused, the shortcut is handled exactly the same (Win is still hidden, so no
    Start menu, and shortcuts like Ctrl+Win+D still work), but it sends
    "paused_press" instead of "start", so nothing is recorded.
    """

    def __init__(self, events: queue.Queue, debug=False, on_console_close=None, shortcut=DEFAULT):
        self.events = events
        self.debug = debug
        self.recording = False
        # Set from the GUI thread; the hook only reads it when a shortcut press begins,
        # so a recording already going when you pause finishes normally.
        self.paused = False
        # The shortcut in use: only the hook thread changes it (see set_shortcut).
        self._rules = _Rules(self._checked(shortcut))
        self._requested = self._rules.families  # only set_shortcut changes it
        # Ctrl+C or closing the console window. Default: stop the hook (which quits the app).
        self._on_console_close = on_console_close or self.stop
        self._console_presses = 0
        self._exit = os._exit  # replaced in tests
        self._held = set()  # modifiers physically held right now
        self._win_hidden = set()  # Win keys held, but Windows thinks they're up
        self._last_down = None  # most recent key pressed (only it can auto-repeat)
        self._press_count = {vk: 0 for vk in MODIFIER_NAMES}
        self._injected_at = {}  # modifier -> when software last pressed it
        # A partner key completed a shortcut with Alt or Shift: send the dummy tap at the start of
        # the next real key event, so it lands after that partner's press (see _handle). Hook thread only.
        self._mask_pending = False
        self._recording_since = 0.0  # when the current recording started (perf_counter)
        # Keys whose press Windows never saw, so their repeats and their release stay hidden too.
        self._swallowed = set()
        # Index (in _rules.partners) of each shortcut key family Windows reported held during
        # this recording (see _check_release).
        self._release_seen = set()
        self._timer_id = 0
        self._hook = None
        self._thread_id = None
        # Keep references so Python never garbage-collects the callbacks.
        self._hook_proc = HOOKPROC(self._on_key)
        self._console_proc = CONSOLE_HANDLER(self._on_console_ctrl)

    # ---- runs inside the hook: keep it tiny ----

    def _on_key(self, n_code, w_param, l_param):
        swallow = False
        try:
            if n_code == HC_ACTION:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if kb.flags & LLKHF_INJECTED:
                    self._track_injected(kb.vkCode, is_down)
                else:
                    if self.debug:
                        self._check_lag(kb)
                    swallow = self._handle(kb, is_down)
        except Exception as e:
            swallow = False  # on any error, let the key through
            self.events.put(("hook_error", time.perf_counter(), e))  # main.report_hook_error prints it
        if swallow:
            return 1  # Windows never sees this key event
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _track_injected(self, vk, is_down):
        if vk in MODIFIER_NAMES:
            if is_down:
                self._injected_at[vk] = time.perf_counter()
            else:
                self._injected_at.pop(vk, None)

    def _check_lag(self, kb):
        # kb.time is when Windows created the key event. Wrapping subtraction,
        # because the millisecond counter rolls over every 49 days.
        lag = (kernel32.GetTickCount() - kb.time) & 0xFFFFFFFF
        if HOOK_LAG_WARN_MS < lag < 60_000:
            self._debug(f"hook lag: {lag}ms on {_key_name(kb.vkCode)}")

    def _debug(self, message):
        if self.debug:
            self.events.put(("debug", time.perf_counter(), message))

    def _combo_held(self):
        """Win and every partner key are held (other keys may be held too)."""
        return bool(self._held & WIN_KEYS) and all(self._held & keys for keys in self._rules.partners)

    def _exact_partners_held(self):
        """Every partner key is held, and no modifier outside the shortcut (Ctrl+Shift+Win
        isn't Ctrl+Win: that one goes to Windows untouched)."""
        rules = self._rules
        return all(self._held & keys for keys in rules.partners) and not self._held & rules.others

    def _mask_partners(self):
        """A dummy key tap while the partner keys are down, so Windows counts them as used in
        a shortcut: releasing Alt then can't open the app's menu bar, and Alt+Shift can't switch
        the keyboard layout. Ctrl+Win needs none (Ctrl alone does nothing)."""
        if self._rules.mask:
            send_dummy_key()
            self._debug(f"sent the dummy key so {self._rules.partner_text} can't act alone")

    def _idle(self):
        return not self._held and not self._win_hidden and not self.recording

    def _apply_requested(self):
        """Switch to a shortcut set_shortcut asked for, but only between key sequences:
        nothing held, Win not hidden, not recording. Runs on the hook thread only."""
        requested = self._requested  # read once: another thread may replace it any moment
        if requested != self._rules.families and self._idle():
            self._rules = _Rules(requested)
            self._debug(f"shortcut is now {shortcut_text(requested)}")

    def _start_recording(self, now):
        if self.paused:
            self.events.put(("paused_press", now, None))
        elif not self.recording:
            self.recording = True
            self._recording_since = now
            self._release_seen.clear()
            self.events.put(("start", now, None))

    def _handle(self, kb, is_down):
        """Returns True if the key should be swallowed (hidden from Windows).

        Only real key events get here: _on_key sends keys flagged as injected (ours: the
        dummy key, Win releases and replays, the stuck-key repair, the paste's Ctrl+V, and
        other programs') to _track_injected instead.
        """
        now = time.perf_counter()
        vk = kb.vkCode
        if self._mask_pending:
            # The partner key that completed the shortcut is down in Windows by now, and this
            # key (at the latest its release) hasn't reached Windows yet: the tap lands in between.
            self._mask_pending = False
            self._mask_partners()
        self._apply_requested()
        rules = self._rules

        repeat = False
        was_last = vk == self._last_down
        if is_down:
            # The keyboard only auto-repeats the most recent key. A "held" key that
            # isn't the most recent one means we missed its release: treat it as new.
            # (A swallowed key that auto-repeats is a repeat too, even if it's no longer in _held.)
            repeat = (vk in self._held or vk in self._swallowed) and was_last
            self._last_down = vk
            if not repeat and vk in self._held:
                self._held.discard(vk)
                self._win_hidden.discard(vk)
            if not repeat and vk in MODIFIER_NAMES:
                self._held.add(vk)
                self._press_count[vk] += 1
        elif vk in MODIFIER_NAMES:
            self._held.discard(vk)

        hidden_before = False
        if vk in self._swallowed:
            if not is_down:
                self._swallowed.discard(vk)  # Windows never saw it go down, so it gets no release
                hidden_before = True
            elif was_last:
                hidden_before = True  # an auto-repeat of a swallowed key
            else:
                # A new press: its release was missed (the Ctrl+Alt+Del screen). Handle it normally,
                # so no key is ever eaten twice.
                self._swallowed.discard(vk)

        if is_down and not repeat and (vk in WIN_KEYS or vk in rules.partner_keys):
            self._drop_missed_releases(vk)  # before deciding anything about the shortcut
        if hidden_before:
            swallow = True
        elif vk in WIN_KEYS:
            swallow = self._handle_win(vk, is_down, repeat, now)
        elif vk in rules.partner_keys:
            # A partner key never triggers a replay (rule 5); it can only start a recording.
            if is_down and not repeat:
                self._handle_partner_down(now)
            swallow = False
        else:
            swallow = self._handle_other(kb, is_down, now)

        if not is_down:
            if self.recording and not self._combo_held():
                self.recording = False
                self.events.put(("stop", now, None))
            if vk in MODIFIER_NAMES and not self._held:
                # All modifiers released: have the worker check Windows agrees.
                self.events.put(("check_stuck", now, None))
        return swallow

    def _drop_missed_releases(self, current):
        """Forget modifier presses whose release the hook never saw.

        Hooks don't see keys on the secure screen (Ctrl+Alt+Del, UAC prompts, the lock
        screen), so a release can go missing and the hook would think the key is still down.
        A missed Ctrl: Win alone would start a dictation and hide the Start menu. A missed
        Win (Win+L, Win released on the lock screen, M14 bug 3): every Ctrl press would start
        one, and typing D would replay Win+D.

        Ctrl, Shift and Alt are never hidden, and neither is a Win the hook isn't hiding right
        now: Windows saw those go down, so if Windows says one is up, it really is up. A hidden
        Win looks up to Windows on purpose, so it can't be checked (a known limit). A Ctrl, Shift
        or Alt swallowed during a long recording (_handle_other) looks up too, so it may be
        forgotten here while it's still held: harmless, since Windows never saw it, and its
        release stays hidden through _swallowed. The key being
        handled right now is skipped: Microsoft's LowLevelKeyboardProc page says the hook is
        "called before the asynchronous state of the key is updated".
        """
        for vk in list(self._held & (VISIBLE_MODIFIERS | WIN_KEYS)):
            if vk in self._win_hidden:
                continue
            if vk != current and not _windows_thinks_held(vk):
                self._held.discard(vk)
                self._debug(f"forgot {MODIFIER_NAMES[vk]} (its release was missed)")

    def _handle_win(self, vk, is_down, repeat, now):
        if not is_down:
            # Rule 4: Windows never saw a hidden Win go down, so it gets no release.
            hidden = vk in self._win_hidden
            self._win_hidden.discard(vk)
            return hidden
        if repeat:
            # Rule 3: repeats of a hidden Win stay hidden.
            return vk in self._win_hidden
        # Rule 1: the partner keys already held (and nothing else), so hide this Win completely.
        if self._exact_partners_held():
            self._win_hidden.add(vk)
            self._debug(f"hid {MODIFIER_NAMES[vk]} ({self._rules.partner_text} was held first)")
            self._mask_partners()
            self._start_recording(now)
            return True
        return False

    def _handle_partner_down(self, now):
        if not self._held & WIN_KEYS or not self._exact_partners_held() or self.recording:
            return
        # Rule 2: Win was pressed first, so Windows saw it go down. Tell Windows it
        # was released, with the dummy key first so the Start menu doesn't open.
        # These injected keys reach Windows before the partner key being handled, so
        # they can't cover it: that's the job of the mask on the next key event.
        visible = [w for w in self._held & WIN_KEYS if w not in self._win_hidden]
        if visible:
            send_keys(
                [(VK_DUMMY, 0), (VK_DUMMY, KEYEVENTF_KEYUP)]
                + [(w, KEYEVENTF_KEYUP) for w in visible]
            )
            self._win_hidden.update(visible)
            names = ", ".join(MODIFIER_NAMES[w] for w in visible)
            self._debug(f"released {names} to Windows with dummy key (Win was pressed first)")
        # Rule 6 (Win already hidden, e.g. a partner released and pressed again): nothing to release.
        # Rule 15: mask the partner keys, but only once this one has reached Windows (see _handle).
        if self._rules.mask:
            self._mask_pending = True
        self._start_recording(now)

    def _handle_other(self, kb, is_down, now):
        if not is_down:
            return False
        if self.recording and now - self._recording_since >= CANCEL_GRACE_SECONDS:
            # A long dictation is never thrown away: hide this key (its press, repeats and
            # release) from Windows until it's let go, and keep recording. Win stays hidden too.
            self._swallowed.add(kb.vkCode)
            self._debug("held back a key: the recording is past its first second")
            return True
        # Any other key this early in a recording means the user wants a Windows shortcut.
        if self.recording:
            self.recording = False
            self.events.put(("cancel", now, None))
        if not self._win_hidden:
            return False
        # Rule 5: give Windows the hidden Win back, then this key, in that order.
        # Hold this key back and send both ourselves, in one call, so the order
        # never depends on where injected keys land relative to the key being handled.
        wins = list(self._win_hidden)
        flags = KEYEVENTF_EXTENDEDKEY if kb.flags & LLKHF_EXTENDED else 0
        send_keys([(w, 0) for w in wins] + [(kb.vkCode, flags, kb.scanCode)])
        self._win_hidden.clear()
        names = ", ".join(MODIFIER_NAMES[w] for w in wins)
        self._debug(f"replayed {names} before {_key_name(kb.vkCode)}")
        return True

    def _check_release(self):
        """Ends a recording whose release the hook never saw, so it's transcribed instead of
        running on until the next press. Runs on the hook thread every RELEASE_CHECK_MS (run_loop).

        The hook misses releases on the secure screen (Ctrl+Alt+Del, UAC prompts, the lock
        screen), while an admin window is in front, and after Windows removes a slow hook.
        Windows still knows: the shortcut's Ctrl, Alt and Shift are never hidden, so if Windows
        reported one held earlier in this recording and now says every key of it is up, it was
        let go. (GetAsyncKeyState also says "up" while the secure screen is showing, which
        stops the recording right there, also fine.) A key Windows never reported held (another
        app swallowing it) is never treated as released.
        """
        if not self.recording:
            return
        for index, keys in enumerate(self._rules.partners):
            held = self._held & keys
            if any(_windows_thinks_held(vk) for vk in held):
                self._release_seen.add(index)
            elif index in self._release_seen:
                # Forget every Ctrl, Alt or Shift Windows says is up (the other partner of a
                # three-key shortcut too): their releases were missed the same way.
                for vk in list(self._held & VISIBLE_MODIFIERS):
                    if not _windows_thinks_held(vk):
                        self._held.discard(vk)
                # Windows never saw a hidden Win go down, so its release (if it comes) stays hidden,
                # and until then it no longer counts as held (a lone Ctrl can't start a dictation).
                for vk in self._win_hidden:
                    self._held.discard(vk)
                    self._swallowed.add(vk)
                self._win_hidden.clear()
                # You let go (most likely of everything, on the secure screen), so nothing is
                # auto-repeating: the next press of any key, Win too, is a new press.
                self._last_down = None
                self.recording = False
                now = time.perf_counter()
                self.events.put(("stop", now, None))
                self._debug("stopped: Windows says the shortcut was let go (the hook missed the release)")
                return

    def _on_release_timer(self):
        try:
            self._check_release()
        except Exception as e:
            self.events.put(("hook_error", time.perf_counter(), e))

    def _on_console_ctrl(self, ctrl_type):
        # Windows runs this on a thread of its own, so it works even if the GUI is frozen.
        # Ctrl+C or closing the terminal: quit the app (main.py does it in order).
        # A second Ctrl+C means that's stuck: quit right away instead.
        self._console_presses += 1
        if self._console_presses == 1:
            self._on_console_close()
        else:
            self.force_quit()
        return True

    def force_quit(self):
        """The normal quit is stuck (e.g. the GUI froze): unhook, repair stuck keys, and exit now.

        A dictation still processing is dropped.
        """
        print("Second Ctrl+C: quitting right away.")
        self.stop()  # the hook thread unhooks and repairs keys itself, if it's still running
        deadline = time.monotonic() + FORCE_QUIT_HOOK_WAIT
        while self._hook and time.monotonic() < deadline:
            time.sleep(0.02)
        self.uninstall()  # does nothing if the hook thread already did it
        self.release_stuck_modifiers()
        self._exit(1)

    # ---- called from other threads ----

    @staticmethod
    def _checked(families):
        families = frozenset(families)
        if families not in ALLOWED:
            raise ValueError(f"not an allowed shortcut: {sorted(families)}")
        return families

    @property
    def shortcut(self):
        """The shortcut the hook uses right now (a set like {"Ctrl", "Win"})."""
        return self._rules.families

    @property
    def requested(self):
        """The shortcut last asked for with set_shortcut (the hook may not have switched yet)."""
        return self._requested

    def set_shortcut(self, families):
        """Ask the hook to use another shortcut. Called from the worker thread (config.json changed).

        The handover needs no lock: this thread only ever replaces _requested (one reference,
        written in one step, which Python never shows half done), and only the hook thread
        reads it and replaces _rules, with a new object that is never changed afterwards.
        Each has one writer, so nothing can be lost or mixed: the hook always ends up on the
        latest request. It switches at the start of the next key event when nothing is held
        and nothing is recording, never in the middle of a key sequence.
        """
        self._requested = self._checked(families)

    def press_counts(self):
        return dict(self._press_count)

    def modifiers_held(self):
        """Modifiers physically held right now, as the hook sees them.

        Use this, not GetAsyncKeyState alone: a hidden Win key looks "up" to Windows.
        Ctrl, Shift and Alt are never hidden, so one that Windows says is up is left out
        (the hook missed its release, e.g. on the Ctrl+Alt+Del screen), and a paste
        never waits for a key nobody is holding. One swallowed during a long recording is left
        out the same way: Windows never saw it, so a paste needn't wait for it.
        """
        return {vk for vk in set(self._held) if vk not in VISIBLE_MODIFIERS or _windows_thinks_held(vk)}

    def release_stuck_modifiers(self, counts_before=None):
        """Release modifiers that Windows thinks are held but the user has let go of."""
        if counts_before is None:
            counts_before = self.press_counts()
        stuck = [vk for vk in MODIFIER_NAMES if vk not in self._held and _windows_thinks_held(vk)]
        dummy_sent = False
        for vk in stuck:
            # Last check right before sending: never release a key the user is holding.
            if vk in self._held or self._press_count[vk] != counts_before[vk]:
                continue
            injected_at = self._injected_at.get(vk)
            if injected_at is not None and time.perf_counter() - injected_at < INJECTED_GRACE_SECONDS:
                continue
            if not dummy_sent:
                send_dummy_key()  # so releasing a stuck Win can't open Start
                dummy_sent = True
            send_keys([(vk, KEYEVENTF_KEYUP)])
            print(f"unstuck: released {MODIFIER_NAMES[vk]} (Windows thought it was still held)")

    def stop(self):
        """Ask the message loop to exit. Safe to call from any thread."""
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # ---- lifecycle (main thread) ----

    def install(self):
        """Install the hook. Must be called on the thread that will run run_loop()."""
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.SetConsoleCtrlHandler(self._console_proc, True)
        # The release check: a thread timer (no window), so WM_TIMER arrives in run_loop on this thread.
        self._timer_id = user32.SetTimer(None, 0, RELEASE_CHECK_MS, None) or 0

    def run_loop(self):
        """Run the Windows message loop the hook needs. Blocks until stop()."""
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_TIMER and not msg.hWnd and self._timer_id and msg.wParam == self._timer_id:
                self._on_release_timer()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def uninstall(self):
        """Remove the hook. Safe to call more than once."""
        if self._timer_id:
            user32.KillTimer(None, self._timer_id)
            self._timer_id = 0
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            kernel32.SetConsoleCtrlHandler(self._console_proc, False)
