"""Measures cleanup: runs it on fixed raw transcripts and checks what comes back.

Uses the app's own cleanup code (the same prompt, model and safety nets), so a prompt change
can be measured before it's used, and a fix for one problem can't quietly break another.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py                   every case, once
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --group numbers   one group (or --case <id>)
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --runs 3          each case 3 times (the answers vary a bit)
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --prompt data\\draft.txt   try a draft prompt
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --show            also print the answers that failed
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --private         add your own cases (see below)
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py --budget 150000   a bigger eval budget, this run only
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py add-from-history 14         from the installed app's History
    .\\.venv\\Scripts\\python.exe tools\\cleanup_eval.py add-from-history 14 --dev   from this folder's History

The cases in tools/cleanup_evals.json are made up. Real dictations only go in
data/cleanup_evals_private.json (data/ is never in Git): add-from-history copies an entry's raw
transcript and your dictionary there, read-only on History. Private cases print only their id,
pass or fail and which check failed, never the text, unless you add --show.

Every answer split over lines gets a warning, passing or failing (public cases print the answer
with it; private ones only with --show). A warning never changes pass or fail; only a case with
"no_line_breaks": true fails on it. The summary counts them.

Costs: every case is one cleanup request (about 300 to 372 tokens, 450 in the estimates) on
your own Groq key, from the same 200K tokens a day as your dictation. Groq's limit refills steadily (about 8,300 tokens
an hour), and so does the eval budget: evals stop when the eval tokens Groq still counts
would pass EVAL_BUDGET (counted in data/cleanup_eval_usage.json), and a run that doesn't fit
says when it will. --budget N raises the limit for that one run only, never past MAX_BUDGET
(150K, so evals can never take all of Groq's 200K); the ledger counts those tokens as usual, so
the next run without it sees them. The app's Usage panel never sees eval tokens (usage.db isn't
touched), so it shows less than Groq counts while evals are recent. The key is never printed.
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import api, keystore, log, paths  # noqa: E402
from app.cleanup import ask_model, check_result  # noqa: E402
from app.config import DEFAULTS  # noqa: E402
from app.usage import UsageTracker  # noqa: E402

CASES_PATH = ROOT / "tools" / "cleanup_evals.json"
PRIVATE_PATH = ROOT / "data" / "cleanup_evals_private.json"
LEDGER_PATH = ROOT / "data" / "cleanup_eval_usage.json"
DEV_HISTORY = ROOT / "data" / "history.db"
INSTALLED_HISTORY = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / paths.APP_ID / "data" / "history.db"
INSTALLED_DICTIONARY = INSTALLED_HISTORY.parent.parent / "dictionary.txt"
DEV_DICTIONARY = ROOT / "dictionary.txt"

# The eval budget works like Groq's own limit: a bucket that drains at 200K tokens a day, not
# a total that resets at midnight. So evals can hold at most half of Groq's 200K at any
# moment, and the other half (more than twice a busy day of dictation) stays for the app.
EVAL_BUDGET = 100_000
MAX_BUDGET = 150_000  # --budget can raise the limit for one run, never past this: 50K always stays for dictation
REFILL_PER_SECOND = 200_000 / 86_400  # Groq's daily token limit for gpt-oss-20b, draining steadily
# Measured about 300 to 372 per request with the M13 prompt (cached tokens subtracted); 450 is kept
# as a cautious estimate, to refuse a run that wouldn't fit
TOKENS_PER_CASE = 450
TOKENS_PER_MINUTE = 7000  # Groq allows 8,000 per minute; stay under it
REQUESTS_PER_MINUTE = 25  # Groq allows 30
RATE_LIMIT_RETRIES = 2


# ---- the checks (pure, tested in tests/test_cleanup.py) ----

def load_cases(path):
    """Returns (default terms, cases) from a cases file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("terms", []), data.get("cases", [])


def has_word(item, text):
    """True if item appears in text as whole words (any case): "um" doesn't match "umbrella"."""
    return re.search(r"(?<!\w)" + re.escape(item) + r"(?!\w)", text, flags=re.IGNORECASE) is not None


def has_line_break(text):
    """True if the answer is split over lines. A dictation should paste as one paragraph unless
    the speaker made a list; every run prints a warning for it, passing or failing."""
    return "\n" in text or "\r" in text


def evaluate(case, answer):
    """Checks one answer. Returns (the text that would be pasted, [failed checks]).

    Each failed check is (label, detail): the label never contains dictation text, so
    private cases can print it; the detail may.
    """
    raw = case["raw"]
    failures = []
    if "—" in answer:
        failures.append(("em dash", "the answer used an em dash"))
    text, reason = check_result(raw, answer, case.get("terms", ()))
    if text is None:
        failures.append(("safety net", reason))
        text = answer.strip()
    for i, item in enumerate(case.get("has", []), 1):
        options = item if isinstance(item, list) else [item]
        if not any(option in text for option in options):
            failures.append((f"has #{i}", f"has {' or '.join(repr(o) for o in options)}"))
    for i, item in enumerate(case.get("lacks", []), 1):
        if has_word(item, text):
            failures.append((f"lacks #{i}", f"lacks {item!r}"))
    for i, item in enumerate(case.get("keeps", []), 1):
        if not has_word(item, text):
            failures.append((f"keeps #{i}", f"keeps {item!r}"))
    if "max_words" in case and len(text.split()) > case["max_words"]:
        failures.append(("max words", f"at most {case['max_words']} words, got {len(text.split())}"))
    if case.get("no_line_breaks") and has_line_break(text):
        failures.append(("line break", "the answer is split over lines"))
    return text, failures


def case_report(case, runs, passes, notes, show):
    """The lines printed for one case. notes: ("error", detail), ("fail", failures, text) or
    ("line break", text), one per run that had something to say. Private cases never print
    dictation text unless show is on."""
    private = case.get("private", False)
    mark = "PASS" if passes == runs else ("FAIL" if passes == 0 else "SOME")
    score = f" {passes}/{runs}" if runs > 1 else ""
    lines = [f"  {mark}{score}  {case['id']}"]
    for note in notes:
        if note[0] == "error":
            lines.append(f"        {note[1]}")
        elif note[0] == "line break":
            lines.append("        warning: line break in the answer")
            if show or not private:
                lines.append(f"        answer: {note[1]!r}")
        else:
            _, failures, text = note
            if private and not show:
                lines.append(f"        failed: {', '.join(label for label, _ in failures)}")
            else:
                lines.append(f"        failed: {'; '.join(detail for _, detail in failures)}")
            if show:
                lines.append(f"        answer: {text}")
    return lines


# ---- the token budget (a bucket that refills, like Groq's) ----

def eval_level(path=LEDGER_PATH, now=None):
    """The eval tokens Groq still counts: the saved level, drained since it was saved."""
    now = time.time() if now is None else now
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "level" in data:
            level, saved = float(data["level"]), float(data["time"])
        else:
            # The old format, {"2026-09-28": 73984}: a day's total, last written at the
            # file's time. Counting it all from then errs on the safe side.
            level, saved = float(sum(data.values())), path.stat().st_mtime
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 0.0
    return max(0.0, level - max(0.0, now - saved) * REFILL_PER_SECOND)


def add_tokens(tokens, path=LEDGER_PATH, now=None):
    """Adds to the level. Returns the new level."""
    now = time.time() if now is None else now
    level = eval_level(path, now) + tokens
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"level": round(level, 1), "time": now}), encoding="utf-8")
    return level


def fits_in(needed, level, budget=EVAL_BUDGET):
    """Seconds until a run needing this many tokens fits under the budget (0 = now)."""
    return max(0.0, (level + needed - budget) / REFILL_PER_SECOND)


def budget_for(requested):
    """The budget for this run: EVAL_BUDGET, or --budget's number (1 to MAX_BUDGET).
    Raises ValueError outside that range. The ledger counts the same either way."""
    if requested is None:
        return EVAL_BUDGET
    if not 1 <= requested <= MAX_BUDGET:
        raise ValueError(f"--budget must be between 1 and {MAX_BUDGET:,} (Groq's limit is 200,000 a day, "
                         f"and dictation needs the rest)")
    return requested


class Pacer:
    """Waits between requests so Groq's per-minute limits are never hit."""

    def __init__(self):
        self._sent = deque()  # (time, tokens) of the last minute

    def wait(self):
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0][0] > 60:
                self._sent.popleft()
            used = sum(t for _, t in self._sent)
            if len(self._sent) < REQUESTS_PER_MINUTE and used + TOKENS_PER_CASE <= TOKENS_PER_MINUTE:
                return
            time.sleep(max(0.5, 60 - (now - self._sent[0][0])))

    def sent(self, tokens):
        self._sent.append((time.monotonic(), tokens))


# ---- running ----

def ask(client, tracker, pacer, case, terms, prompt_path):
    """One cleanup request, retried after a rate limit. Returns (answer, tokens, seconds)."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        pacer.wait()
        t0 = time.perf_counter()
        try:
            answer, tokens = ask_model(client, tracker, case["raw"], terms, prompt_path)
        except api.RateLimited as e:
            if attempt == RATE_LIMIT_RETRIES:
                raise
            wait = max(e.info.get("retry_in") or 10.0, 2.0)
            print(f"  (rate limited, waiting {wait:.0f}s)")
            time.sleep(wait + 1)
            continue
        pacer.sent(tokens)
        return answer, tokens, time.perf_counter() - t0


def run(args):
    terms, cases = load_cases(CASES_PATH)
    for case in cases:
        case.setdefault("terms", terms)
    if args.private:
        if PRIVATE_PATH.exists():
            _, private = load_cases(PRIVATE_PATH)
            for case in private:
                case["private"] = True
            cases += private
        else:
            print(f"no private cases yet ({PRIVATE_PATH.name}); use add-from-history")
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.group:
        cases = [c for c in cases if c.get("group") in args.group]
    if not cases:
        print("no cases match")
        return 1

    prompt_path = Path(args.prompt).resolve() if args.prompt else None
    budget = args.budget
    level = eval_level()
    estimate = len(cases) * args.runs * TOKENS_PER_CASE
    wait = fits_in(estimate, level, budget)
    if wait > 0:
        at = time.strftime("%H:%M", time.localtime(time.time() + wait))
        print(f"Stopped: Groq still counts about {level:,.0f} eval tokens, and this run needs about "
              f"{estimate:,}. The eval budget is {budget:,} (it refills like Groq's 200K a day, "
              f"which your dictation shares). This run fits from about {at}.")
        return 2

    key, _ = keystore.find_key()  # registers the key with the log filter; never printed
    if not key:
        print("No Groq API key (save one in the app, or GROQ_API_KEY in .env)")
        return 1
    client = keystore.make_client(key, timeout=15.0)
    tracker = UsageTracker(":memory:", DEFAULTS["groq_limits"])  # usage.db is never touched
    pacer = Pacer()

    shown_prompt = prompt_path or paths.PROMPT_PATH
    print(f"Prompt: {shown_prompt.relative_to(ROOT) if shown_prompt.is_relative_to(ROOT) else shown_prompt}")
    if budget != EVAL_BUDGET:
        print(f"Eval budget for this run: {budget:,} (normally {EVAL_BUDGET:,})")
    print(f"{len(cases)} cases x {args.runs} run{'s' if args.runs > 1 else ''}\n")

    spent, seconds, groups = 0, [], {}
    passed_total = runs_total = broken_answers = 0
    broken_cases = set()
    for case in cases:
        passes, notes = 0, []
        for _ in range(args.runs):
            if eval_level() + TOKENS_PER_CASE > budget:
                print(f"Stopped: the eval budget of {budget:,} tokens is used up for now (it refills "
                      f"about {REFILL_PER_SECOND * 3600:,.0f} an hour).")
                return 2
            try:
                answer, tokens, took = ask(client, tracker, pacer, case, case["terms"], prompt_path)
            except Exception as e:
                notes.append(("error", f"request failed: {type(e).__name__}"))
                continue
            spent += tokens
            add_tokens(tokens)
            seconds.append(took)
            text, failures = evaluate(case, answer)
            if failures:
                notes.append(("fail", failures, text))
            else:
                passes += 1
            if has_line_break(text):
                notes.append(("line break", text))
                broken_answers += 1
                broken_cases.add(case["id"])
        g = groups.setdefault(case.get("group", "other"), [0, 0])
        g[0] += passes
        g[1] += args.runs
        passed_total += passes
        runs_total += args.runs

        for line in case_report(case, args.runs, passes, notes, args.show):
            print(line)

    print("\nBy group:")
    for name, (p, n) in groups.items():
        print(f"  {name:22} {p}/{n}")
    print(f"\nLine breaks: {broken_answers} answer{'' if broken_answers == 1 else 's'} "
          f"in {len(broken_cases)} case{'' if len(broken_cases) == 1 else 's'}")
    pct = 100 * passed_total / runs_total if runs_total else 0
    median = f"{statistics.median(seconds):.2f}s" if seconds else "n/a"
    print(f"\nTotal {passed_total}/{runs_total} ({pct:.0f}%) | cleanup median {median} | "
          f"tokens this run {spent:,} | eval tokens Groq still counts {eval_level():,.0f} of {budget:,}")
    return 0


# ---- private cases from History ----

def read_history_raw(db_path, entry_id):
    """An entry's raw transcript, read-only. None if there's no such entry or it has no text."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"no History at {db_path}")
    db = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT raw FROM entries WHERE id = ?", (entry_id,)).fetchone()
    finally:
        db.close()
    return (row[0] or None) if row else None


def add_from_history(args):
    from app.dictionary import load_terms

    db_path, dictionary = (DEV_HISTORY, DEV_DICTIONARY) if args.dev else (INSTALLED_HISTORY, INSTALLED_DICTIONARY)
    where = "this folder's History" if args.dev else "the installed app's History"
    try:
        raw = read_history_raw(db_path, args.entry_id)
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"couldn't read {where}: {e}")
        return 1
    if raw is None:
        print(f"{where} has no entry {args.entry_id} with a transcript")
        return 1

    cases = []
    if PRIVATE_PATH.exists():
        cases = json.loads(PRIVATE_PATH.read_text(encoding="utf-8")).get("cases", [])
    case_id = f"{'dev' if args.dev else 'installed'}-{args.entry_id}"
    if any(c["id"] == case_id for c in cases):
        print(f"{case_id} is already in {PRIVATE_PATH.name}")
        return 1
    cases.append({"id": case_id, "group": "private", "raw": raw, "terms": load_terms(dictionary),
                  "has": [], "lacks": [], "keeps": []})
    PRIVATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    about = "Real dictations, copied from History by tools/cleanup_eval.py add-from-history. Never commit this file."
    PRIVATE_PATH.write_text(json.dumps({"about": about, "cases": cases}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"added {case_id} ({len(raw.split())} words) to {PRIVATE_PATH.relative_to(ROOT)}.")
    print("Add its checks (has, lacks, keeps) in that file, then run with --private.")
    return 0


def parse_args(argv=None):
    """The command line. args.budget is always set: EVAL_BUDGET unless --budget was given."""
    parser = argparse.ArgumentParser(description="Measure cleanup on fixed transcripts.")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("add-from-history", help="copy a History entry into the private cases")
    add.add_argument("entry_id", type=int)
    add.add_argument("--dev", action="store_true", help="this folder's History, not the installed app's")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--case", action="append", help="only this case (can repeat)")
    parser.add_argument("--group", action="append", help="only this group (can repeat)")
    parser.add_argument("--prompt", help="a draft prompt file instead of prompts/cleanup.txt")
    parser.add_argument("--show", action="store_true", help="print the answers that failed")
    parser.add_argument("--private", action="store_true", help=f"also run {PRIVATE_PATH.name}")
    parser.add_argument("--budget", type=int,
                        help=f"raise the eval budget for this run only (at most {MAX_BUDGET:,}; normally {EVAL_BUDGET:,})")
    args = parser.parse_args(argv)
    if args.command == "add-from-history":
        return args
    if args.runs < 1:
        parser.error("--runs must be 1 or more")
    try:
        args.budget = budget_for(args.budget)
    except ValueError as e:
        parser.error(str(e))
    return args


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Bengali in a Windows console
    except (AttributeError, ValueError):
        pass
    log.start()  # the key filter on everything printed

    args = parse_args()
    if args.command == "add-from-history":
        return add_from_history(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
