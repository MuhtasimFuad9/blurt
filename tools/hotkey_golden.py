"""Golden key sequences for the Ctrl+Win hook (M14).

Records what the hook in app/hotkey.py does with thousands of key sequences: what
Windows ends up seeing (keys let through and keys injected) and what the worker is
told (start, stop, cancel, debug lines, stuck-key repairs). It was generated with the
Ctrl+Win-only hook from before M14, so tests/test_hotkey.py can prove the configurable
hook still does exactly the same with the default shortcut.

No real keyboard: fake key events go straight into the hook's decision code, injected
keys are recorded instead of sent, and the clock is fake, so every run is identical.

One exception, on purpose (M14 bug 3): the hook now forgets a Win whose release it missed
(Win+L, Win released on the lock screen) when Windows says it's up. That changes exactly
MISSED_WIN_CASES cases, all with such a release ("~LW" or "~RW" while the hook wasn't
hiding that Win). Those count as expected only if they match the file up to the step where
the rule first fired and pass the invariants after it, and only if they are exactly the
pinned set. Every other case must match exactly. The file itself is never rewritten.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\hotkey_golden.py check          (compare the hook with the file)
    .\\.venv\\Scripts\\python.exe tools\\hotkey_golden.py check --list   (also list the missed-Win cases)
    .\\.venv\\Scripts\\python.exe tools\\hotkey_golden.py generate       (write the file; only with the old hook)
    .\\.venv\\Scripts\\python.exe tools\\hotkey_golden.py save-missed-win   (save the missed-Win cases' output;
                                                                   refuses if the file exists)
"""

import contextlib
import hashlib
import io
import json
import queue
import random
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import hotkey as hotkey_module  # noqa: E402
from app.hotkey import (FAMILY_KEYS, KBDLLHOOKSTRUCT, KEYEVENTF_KEYUP, LLKHF_EXTENDED, WIN_KEYS,  # noqa: E402
                        Hotkey)

GOLDEN_PATH = ROOT / "tests" / "golden" / "hotkey_golden.json"  # not tests/data: .gitignore's data/ would hide it
# SHA-256 of the file as committed in 03dae73, line endings as LF (a CRLF checkout gives the same).
GOLDEN_SHA256 = "47ddb46b0b90a29c2b1c4bae471bce1d4bb7b13417eb69cf73c6fe222a5bd038"
# The cases the missed-Win rule changes (M14 bug 3): how many, and the SHA-256 of their text
# joined with "\n" in the file's order. Measured before the fix was written.
MISSED_WIN_CASES = 164
MISSED_WIN_SHA256 = "cbaff7a0835335b581e250daec8752403d33968859086a7fd979a9e1db63d2c0"
# The exact output of the fixed hook for those cases, saved once after the user's bug 3 retests
# passed (save-missed-win), and never rewritten either.
MISSED_WIN_PATH = GOLDEN_PATH.parent / "hotkey_golden_missed_win.json"
MISSED_WIN_FILE_SHA256 = "18271bb3801a023cc2ae1845cd92c867971c684ca26aaf2216630dda1fe48067"
FORMAT = 1
SEED = 14
RANDOM_CASES = 3000
STEP_SECONDS = 0.02  # the fake clock moves this much per step
STUCK_CHECK_DELAY = 0.05  # like main.py: the worker repairs stuck keys this long after the hook asks

# Short names for the keys used, with the scan code and extended flag a real keyboard sends.
KEYS = {
    "LC": (0xA2, 0x1D, False), "RC": (0xA3, 0x1D, True),
    "LW": (0x5B, 0x5B, True), "RW": (0x5C, 0x5C, True),
    "LS": (0xA0, 0x2A, False), "RS": (0xA1, 0x36, False),
    "LA": (0xA4, 0x38, False), "RA": (0xA5, 0x38, True),
    "D": (0x44, 0x20, False), "L": (0x4C, 0x26, False), "LEFT": (0x25, 0x4B, True),
}
NAMES = {vk: name for name, (vk, _, _) in KEYS.items()}
MODIFIER_NAMES = ["LC", "RC", "LW", "RW", "LS", "RS", "LA", "RA"]

# Steps: "+K" press, "-K" release, "~K" a release only Windows saw (the secure screen),
# "!K" a release only the hook saw (a lost event: Windows still thinks it's held),
# "P" pause, "R" resume.
HAND_CASES = [
    "+LC +LW -LW -LC", "+LC +LW -LC -LW", "+LW +LC -LC -LW", "+LW +LC -LW -LC",
    "+RC +RW -RW -RC", "+LC +RW -RW -LC", "+RW +RC -RW -RC",
    "+LC +LW +LW +LW -LW -LC", "+LC +LC +LW -LW -LC", "+LW +LW +LC +LC -LC -LW",
    "+LC +LW +D -D -LW -LC", "+LC +LW +LEFT -LEFT -LW -LC", "+LW +LC +D -D -LC -LW",
    "+LC +LW +L -L -LW -LC", "+LC +LW +D +D -D -LW -LC", "+LC +LW +D -D +D -D -LW -LC",
    "+LC +LW -LC +LC -LC -LW", "+LC +LW -LC +LC -LW -LC", "+LW +LC -LC +LC -LC -LW",
    "+LC +LW +RC -RC -LW -LC", "+LC +LW +RW -RW -LW -LC", "+LC +LW +RW -LW -RW -LC",
    "+LS +LC +LW -LW -LC -LS", "+LC +LS +LW -LW -LS -LC", "+LA +LC +LW -LW -LC -LA",
    "+LC +LW +LS -LS -LW -LC", "+LC +LW +LA -LA -LW -LC", "+LW +LS +LC -LC -LS -LW",
    "+LW -LW", "+LC -LC", "+LW +D -D -LW", "+LW +LW +LW -LW",
    "+LC ~LC +LW -LW", "+LC ~LC +LC +LW -LW -LC", "+LC +LW ~LC -LW",
    "+LS ~LS +LC +LW -LW -LC", "+LA ~LA +LW +LC -LC -LW",
    "+LC +LW !LW -LC", "+LC +LW !LC -LW", "+LW !LW +LC -LC", "+LC !LC +LW -LW",
    "+LC +LW -LW +LW -LW -LC", "+LC +LW -LW -LC +LW +LC -LC -LW",
    "P +LC +LW -LW -LC R", "P +LW +LC -LC -LW R", "P +LC +LW +D -D -LW -LC R",
    "P +LC +LW +LW +LW -LW -LC R", "P +LC +LW R -LW -LC +LC +LW -LW -LC",
    "+LC +LW P -LW -LC R", "+LC +LW P +D -D -LW -LC R", "P +LW +LW +LC -LC -LW R",
]


def encode_windows(entry):
    """What Windows saw, as a short token: "LC+" (let through), "iE8-" (injected), with flags and scan if any."""
    kind, vk, down, flags, scan = entry
    name = NAMES.get(vk, f"{vk:02X}")
    token = ("i" if kind == "injected" else "") + name + ("+" if down else "-")
    if flags & ~KEYEVENTF_KEYUP:
        token += f"/f{flags & ~KEYEVENTF_KEYUP}"
    if scan:
        token += f"/s{scan}"
    return token


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def perf_counter(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Replay:
    """Runs one sequence through a fresh hook and records everything it does."""

    def __init__(self, make_hotkey=None):
        self.clock = FakeTime()
        self.events = queue.Queue()
        self.hotkey = (make_hotkey or (lambda events: Hotkey(events, debug=True)))(self.events)
        self.windows = []  # (kind, vk, down, flags, scan)
        self.worker = []  # what the worker was told, in order
        self.pending_check = None  # (when, press counts) like the worker's stuck_check
        # Not part of the recorded output, only for checking it:
        self.starts = []  # (position in worker, key families the hook held) for each start or paused_press
        self.missed_visible_win = None  # step number of the first "~" Win release while that Win wasn't hidden
        self.missed_hidden_win = False  # a "~" Win release while the hook was hiding that Win (a known limit)
        self.win_forgotten_at = None  # (step number, len(windows), len(worker)) before the missed-Win rule first fired
        self.steps = 0

    def windows_thinks_held(self, vk):
        for kind, key, down, _, _ in reversed(self.windows):
            if key == vk:
                return down
        return False

    def injected(self, keys):
        for key in keys:
            vk, flags = key[0], key[1]
            scan = key[2] if len(key) > 2 else 0
            down = not flags & KEYEVENTF_KEYUP
            self.windows.append(("injected", vk, down, flags, scan))
            self.hotkey._track_injected(vk, down)  # injected keys come back through the hook, flagged

    def drain(self):
        while not self.events.empty():
            kind, _, detail = self.events.get()
            if kind == "check_stuck":
                self.pending_check = (self.clock.now + STUCK_CHECK_DELAY, self.hotkey.press_counts())
            self.worker.append(kind if detail is None else f"{kind}:{detail}")

    def run_pending_check(self):
        if self.pending_check is None:
            return
        when, counts = self.pending_check
        self.pending_check = None
        self.clock.now = max(self.clock.now, when)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.hotkey.release_stuck_modifiers(counts)
        self.worker.extend(f"print:{line}" for line in out.getvalue().splitlines())

    def key(self, name, down, windows_sees=True, hook_sees=True):
        vk, scan, extended = KEYS[name]
        swallowed = False
        if hook_sees:
            kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=LLKHF_EXTENDED if extended else 0,
                                 time=0, dwExtraInfo=0)
            swallowed = self.hotkey._handle(kb, down)
        if windows_sees and not swallowed:
            self.windows.append(("key", vk, down, 0, 0))

    def step(self, token):
        self.run_pending_check()  # the worker got to it before the next key (50ms is short)
        self.clock.now += STEP_SECONDS
        op, name = token[0], token[1:]
        hook = self.hotkey
        vk = KEYS[name][0] if name in KEYS else None
        if op == "~" and vk in WIN_KEYS and vk in hook._held:
            if vk in hook._win_hidden:
                self.missed_hidden_win = True
            elif self.missed_visible_win is None:
                self.missed_visible_win = self.steps
        before = (self.steps, len(self.windows), len(self.worker))
        wins_before = hook._held & WIN_KEYS
        if op == "+":
            self.key(name, True)
        elif op == "-":
            self.key(name, False)
        elif op == "~":
            self.key(name, False, hook_sees=False)
        elif op == "!":
            self.key(name, False, windows_sees=False)
        elif op == "P":
            self.hotkey.paused = True
        elif op == "R":
            self.hotkey.paused = False
        # A Win leaves _held only on its own release (the hook saw it: "-" or "!"), or when the
        # missed-Win rule forgets it. (A new press of a Win it thought was held keeps it held.)
        released = {vk} if op in "-!" else set()
        if (wins_before - hook._held - released) and self.win_forgotten_at is None:
            self.win_forgotten_at = before
        worker_before = len(self.worker)
        self.drain()
        for position, kind in enumerate(self.worker[worker_before:], worker_before):
            if kind in ("start", "paused_press"):
                self.starts.append((position, {family for family, keys in FAMILY_KEYS.items() if hook._held & keys}))
        self.steps += 1

    def run(self, case):
        patches = [
            mock.patch.object(hotkey_module, "send_keys", self.injected),
            mock.patch.object(hotkey_module, "_windows_thinks_held", self.windows_thinks_held),
            mock.patch.object(hotkey_module, "time", self.clock),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            for token in case.split():
                self.step(token)
            self.run_pending_check()
            self.drain()
            held = sorted(NAMES.get(vk, f"{vk:02X}") for vk in self.hotkey.modifiers_held())
        return {
            "windows": " ".join(encode_windows(entry) for entry in self.windows),
            "worker": self.worker,
            "held": " ".join(held),
            "recording": self.hotkey.recording,
        }


def random_case(rng):
    """A random but realistic sequence: mostly modifiers, some other keys, repeats,
    rare missed and lost releases, pause switched now and then. Ends with every key released."""
    held = []  # names the "user" is holding, in press order
    last = None
    paused = False
    steps = []
    for _ in range(rng.randint(4, 22)):
        roll = rng.random()
        if roll < 0.45 or not held:
            name = rng.choice(MODIFIER_NAMES + MODIFIER_NAMES[:4] + ["D", "L", "LEFT"])
            if name in held:
                continue
            held.append(name)
            last = name
            steps.append("+" + name)
        elif roll < 0.8:
            name = rng.choice(held)
            held.remove(name)
            steps.append("-" + name)
        elif roll < 0.88 and last in held:
            steps.append("+" + last)  # auto-repeat
        elif roll < 0.92:
            name = rng.choice(held)
            held.remove(name)
            steps.append("~" + name)
        elif roll < 0.95:
            name = rng.choice(held)
            held.remove(name)
            steps.append("!" + name)
        else:
            paused = not paused
            steps.append("P" if paused else "R")
    for name in held:
        steps.append("-" + name)
    return " ".join(steps)


def all_cases():
    rng = random.Random(SEED)
    return HAND_CASES + [random_case(rng) for _ in range(RANDOM_CASES)]


def record(make_hotkey=None):
    return [[case, Replay(make_hotkey).run(case)] for case in all_cases()]


def load():
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if data["format"] != FORMAT:
        raise ValueError(f"unknown golden format {data['format']}")
    return data["cases"]


def file_sha256(path):
    """SHA-256 of a file with its line endings as LF, so a CRLF checkout gives the same."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def golden_sha256():
    return file_sha256(GOLDEN_PATH)


def load_missed_win():
    """The saved output of the missed-Win cases: [[case, output], ...] in the golden file's order."""
    data = json.loads(MISSED_WIN_PATH.read_text(encoding="utf-8"))
    if data["format"] != FORMAT:
        raise ValueError(f"unknown format {data['format']}")
    return data["cases"]


def cases_sha256(cases):
    return hashlib.sha256("\n".join(cases).encode("utf-8")).hexdigest()


def windows_view(text):
    """A recorded "windows" string as [(key name, down), ...], injected or not."""
    view = []
    for token in text.split():
        token = token.split("/")[0].removeprefix("i")
        view.append((token[:-1], token[-1] == "+"))
    return view


def view_problems(view):
    """(releases Windows got without a press, per key; keys Windows thinks are held at the end)"""
    unpressed = {}
    state = {}
    for name, down in view:
        if not down and not state.get(name):
            unpressed[name] = unpressed.get(name, 0) + 1
        state[name] = down
    return unpressed, {name for name, down in state.items() if down}


def invariant_problems(replay, expected, got, shortcut):
    """What's wrong with a changed case's new output.

    Always required, from the step where the missed-Win rule fired: a start only with exactly
    the shortcut held, never two starts in a row, and no recording left at the end.

    Required as "no worse than the golden output": keys Windows thinks are held at the end, keys
    the hook holds at the end, and releases Windows got without a press. These can't be required
    outright: a release the hook saw but Windows didn't ("!D") leaves D held in the fake, a hidden
    Win whose release was missed is a known limit, and a missed Win release at the very end has no
    later press for the rule to run on. The golden output has those problems too."""
    _, _, from_worker = replay.win_forgotten_at
    problems = []
    for position, families in replay.starts:
        if position >= from_worker and families != set(shortcut):
            problems.append(f"started with {sorted(families)} held")
    kinds = [kind for kind in replay.worker[from_worker:] if kind in ("start", "stop", "cancel")]
    if any(first == second == "start" for first, second in zip(kinds, kinds[1:])):
        problems.append("two starts in a row")
    if got["recording"]:
        problems.append("still recording at the end")
    old_unpressed, old_held = view_problems(windows_view(expected["windows"]))
    new_unpressed, new_held = view_problems(windows_view(got["windows"]))
    for name, count in new_unpressed.items():
        if count > old_unpressed.get(name, 0):
            problems.append(f"Windows got more releases of {name} without a press than before")
    if new_held - old_held:
        problems.append(f"Windows thinks {sorted(new_held - old_held)} held at the end, and didn't before")
    if set(got["held"].split()) - set(expected["held"].split()):
        problems.append(f"the hook holds {got['held']} at the end, and held {expected['held'] or 'nothing'} before")
    return problems


def missed_win_problem(replay, expected, got, shortcut):
    """Why a changed case is NOT the allowed missed-Win exception, or None if it is."""
    if replay.win_forgotten_at is None:
        return "differs, and the missed-Win rule never fired"
    step, from_windows, from_worker = replay.win_forgotten_at
    if replay.missed_visible_win is None or replay.missed_visible_win >= step:
        return "the missed-Win rule fired without a missed release of a Win that wasn't hidden"
    if (got["windows"].split()[:from_windows] != expected["windows"].split()[:from_windows]
            or got["worker"][:from_worker] != expected["worker"][:from_worker]):
        return "differs before the missed-Win rule fired"
    problems = invariant_problems(replay, expected, got, shortcut)
    if problems:
        return "after the missed-Win rule fired: " + "; ".join(problems)
    return None


class Comparison:
    """Every golden case, sorted into: matches exactly, the allowed missed-Win exception, or a real difference."""

    def __init__(self, make_hotkey=None):
        self.matched = 0
        self.missed_win = []  # (case, expected, got)
        self.differences = []  # (case, expected, got, why)
        for case, expected in load():
            replay = Replay(make_hotkey)
            got = replay.run(case)
            if got == expected:
                self.matched += 1
                continue
            why = missed_win_problem(replay, expected, got, replay.hotkey.shortcut)
            if why is None:
                self.missed_win.append((case, expected, got))
            else:
                self.differences.append((case, expected, got, why))

    def missed_win_sha256(self):
        return cases_sha256([case for case, _, _ in self.missed_win])

    def problems(self):
        """Everything that makes the check fail, as short lines (empty when it passes)."""
        problems = []
        if golden_sha256() != GOLDEN_SHA256:
            problems.append(f"{GOLDEN_PATH.name} was changed (its SHA-256 isn't the one from commit 03dae73)")
        if self.differences:
            problems.append(f"{len(self.differences)} cases differ")
        if len(self.missed_win) != MISSED_WIN_CASES:
            problems.append(f"{len(self.missed_win)} missed-Win cases, expected exactly {MISSED_WIN_CASES}")
        elif self.missed_win_sha256() != MISSED_WIN_SHA256:
            problems.append(f"the {MISSED_WIN_CASES} missed-Win cases aren't the pinned ones")
        elif not MISSED_WIN_PATH.exists():
            problems.append(f"{MISSED_WIN_PATH.name} is missing")
        elif file_sha256(MISSED_WIN_PATH) != MISSED_WIN_FILE_SHA256:
            problems.append(f"{MISSED_WIN_PATH.name} was changed (its SHA-256 isn't the pinned one)")
        elif [[case, got] for case, _, got in self.missed_win] != load_missed_win():
            problems.append(f"the missed-Win cases' output doesn't match {MISSED_WIN_PATH.name}")
        return problems


def differences(make_hotkey=None, limit=10):
    """[(case, expected, got, why), ...]: cases that neither match the golden file nor are the
    allowed missed-Win exception. (The exception's count and set are checked by Comparison.problems.)"""
    return Comparison(make_hotkey).differences[:limit]


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "generate":
        cases = record()
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"format": FORMAT, "cases": cases}, separators=(",", ":"))
        GOLDEN_PATH.write_bytes(text.encode("utf-8") + b"\n")
        print(f"wrote {len(cases)} cases to {GOLDEN_PATH} ({GOLDEN_PATH.stat().st_size:,} bytes)")
    elif command == "save-missed-win":
        if MISSED_WIN_PATH.exists():
            print(f"{MISSED_WIN_PATH} already exists; it's never rewritten.")
            return 1
        result = Comparison()
        if result.differences or len(result.missed_win) != MISSED_WIN_CASES \
                or result.missed_win_sha256() != MISSED_WIN_SHA256:
            print("the hook doesn't give exactly the pinned missed-Win cases; nothing saved")
            return 1
        cases = [[case, got] for case, _, got in result.missed_win]
        text = json.dumps({"format": FORMAT, "cases": cases}, separators=(",", ":"))
        with open(MISSED_WIN_PATH, "xb") as file:  # "x": fails if the file appeared meanwhile
            file.write(text.encode("utf-8") + b"\n")
        print(f"wrote {len(cases)} cases to {MISSED_WIN_PATH}, SHA-256 {file_sha256(MISSED_WIN_PATH)}")
    elif command == "check":
        result = Comparison()
        total = result.matched + len(result.missed_win) + len(result.differences)
        if "--list" in sys.argv[2:]:
            for case, _, _ in result.missed_win:
                print(case)
        for case, expected, got, why in result.differences[:10]:
            print(f"case: {case}\n  why:      {why}\n  expected: {expected}\n  got:      {got}")
        print(f"{total} cases: {result.matched} match, {len(result.missed_win)} differ only after "
              f"the missed-Win rule fired (M14 bug 3), {len(result.differences)} other differences")
        problems = result.problems()
        if problems:
            print("MISMATCH: " + "; ".join(problems))
            return 1
        print("OK")
        return 0
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
