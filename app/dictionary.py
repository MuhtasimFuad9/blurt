"""Reads and changes dictionary.txt: one term per line, # for comments.

The file is only re-read when it changes, so edits (in Notepad or the Dictionary
panel) apply on the next dictation.

Order matters: Whisper's vocabulary hint lists the terms in file order, and the
echo guard (transcribe.is_prompt_echo) checks for that same order. Adding a term
appends it at the end and removing one deletes only its line, so the others keep
their order and every comment stays.
"""

import math
import os
import re
import threading
from pathlib import Path

from app.paths import DICTIONARY_PATH

MAX_TERM_LENGTH = 40
# Whisper only reads the last ~224 tokens of its prompt. We keep the hint under a
# lower budget, using an estimate that errs high, so it never gets near the limit.
WHISPER_TOKEN_LIMIT = 224
WHISPER_PROMPT_BUDGET = 200
CHARS_PER_TOKEN = 2.5  # real names split into short pieces, so this counts high on purpose
CLEANUP_CHARS_PER_TOKEN = 3.5  # gpt-oss's tokenizer packs more into a token
# Starts cleanup's dictionary line; the terms follow, comma separated. What to do with them is
# in prompts/cleanup.txt, so tools/cleanup_eval.py can try other wording with --prompt.
CLEANUP_DICTIONARY_INTRO = "\n\nDictionary: "

_lock = threading.Lock()  # the panel writes on the GUI thread while the pipeline reads
_cache = {}  # path: {"key": (mtime, size), "terms": [...]}


def _parse(text):
    return [s.strip() for s in text.splitlines() if s.strip() and not s.strip().startswith("#")]


def _read(path):
    # utf-8-sig drops the invisible byte-order mark some editors put at the start.
    # newline="" keeps the file's own line endings, so a write doesn't change them.
    with open(path, encoding="utf-8-sig", newline="") as f:
        return f.read()


def load_terms(path=None):
    """The terms, in file order. Re-reads the file only when it changed."""
    path = Path(path or DICTIONARY_PATH)
    with _lock:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _cache.get(path)
        if cached is None or cached["key"] != key:
            cached = {"key": key, "terms": _parse(_read(path))}
            _cache[path] = cached
        return list(cached["terms"])


def normalize(text):
    """What a term looks like once added: trimmed, spaces inside squeezed to one."""
    return re.sub(r"[  ]+", " ", (text or "").strip())


def check_new_term(text, terms):
    """Why text can't be added (a short reason for the panel), or None if it can."""
    term = normalize(text)
    if not term:
        return "Copy a word anywhere, then add it here"
    if "\n" in term or "\r" in term:
        return "More than one line"
    if len(term) > MAX_TERM_LENGTH:
        return f"Too long ({MAX_TERM_LENGTH} characters max)"
    if term.startswith("#"):
        return "Starts with #, which marks a comment"
    if "," in term:
        return "Has a comma (Whisper's hint is comma separated)"
    if "\t" in term or any(ord(c) < 32 for c in term):
        return "Has a tab or a hidden character"
    # The echo guard treats "claude" and "Claude" as the same term.
    if term.lower() in (t.lower() for t in terms):
        return "Already in the dictionary"
    return None


def _write(path, text):
    """Replaces the file all at once, so it's never half written."""
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(temp, path)
    stat = path.stat()
    _cache[path] = {"key": (stat.st_mtime_ns, stat.st_size), "terms": _parse(text)}


def add_term(text, path=None):
    """Adds a term at the end of the file. Returns None, or the reason it wasn't added."""
    path = Path(path or DICTIONARY_PATH)
    with _lock:
        # Read from disk right now, so an edit made in Notepad a moment ago is kept.
        current = _read(path) if path.exists() else ""
        reason = check_new_term(text, _parse(current))
        if reason:
            return reason
        newline = "\r\n" if "\r\n" in current else "\n"
        if current and not current.endswith(("\n", "\r")):
            current += newline
        _write(path, current + normalize(text) + newline)
    return None


def remove_term(term, path=None):
    """Removes a term's line (the first one that matches exactly). Returns True if it was there."""
    path = Path(path or DICTIONARY_PATH)
    with _lock:
        if not path.exists():
            return False
        lines = _read(path).splitlines(keepends=True)
        for i, line in enumerate(lines):
            if not line.strip().startswith("#") and line.strip() == term:
                del lines[i]
                _write(path, "".join(lines))
                return True
    return False


def estimate_tokens(text, chars_per_token=CHARS_PER_TOKEN):
    """About how many tokens text is. Errs high (no tokenizer package needed)."""
    return math.ceil(len(text) / chars_per_token)


def whisper_terms(terms, budget=WHISPER_PROMPT_BUDGET):
    """The terms that fit in Whisper's hint, in order. Drops the oldest (first) ones.

    Whisper itself would ignore the start of a prompt that's too long, so this keeps
    the same terms it would, and the newest ones you added.
    """
    kept = list(terms)
    while kept and estimate_tokens(", ".join(kept)) > budget:
        kept.pop(0)
    return kept


def whisper_prompt(terms):
    """Whisper's vocabulary hint, e.g. "Blender, Groq, Claude Code"."""
    return ", ".join(whisper_terms(terms))


def cleanup_dictionary_line(terms):
    """What cleanup's system prompt gets about the dictionary (all terms, not only Whisper's)."""
    if not terms:
        return ""
    return CLEANUP_DICTIONARY_INTRO + ", ".join(terms)


def cleanup_tokens(terms):
    """About how many tokens the dictionary adds to every cleanup request."""
    if not terms:
        return 0
    return estimate_tokens(cleanup_dictionary_line(terms), CLEANUP_CHARS_PER_TOKEN)
