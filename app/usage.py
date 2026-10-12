"""Tracks how much of Groq's free tier is used, and remembers it across restarts.

How Groq counts (checked with tools/groq_probe.py and real 429 messages):
- Every limit is a bucket that refills steadily, not a counter that resets at
  midnight. One of 2,000 daily requests frees up again after 86,400 / 2,000 =
  43.2 seconds (the probe saw exactly "reset-requests: 43.2s").
- Audio is billed at least 10 seconds per request.
- Responses carry x-ratelimit-* headers for requests per day only. Audio and
  tokens per day are counted here, and corrected whenever a 429 says "Used N".

Everything is saved in usage.db (SQLite). The pipeline thread writes; the GUI
only calls snapshot(), which never touches the disk.
"""

import math
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime

from app.paths import APP_DISPLAY_NAME

DAY = 86400.0
HOUR = 3600.0
KEEP_DAYS = 30  # older log rows are deleted
PRUNE_EVERY = HOUR
VOICE_HISTORY = 20  # how many recent voice levels voice_levels() returns

# Meter name: (its limit in config.json "groq_limits", how long a full refill takes)
METERS = {
    "audio_day": ("audio_seconds_per_day", DAY),
    "audio_hour": ("audio_seconds_per_hour", HOUR),
    "transcriptions": ("transcribe_requests_per_day", DAY),
    "cleanups": ("cleanup_requests_per_day", DAY),
    "cleanup_tokens": ("cleanup_tokens_per_day", DAY),
}

# What the Usage panel and the "almost used up" notification call each meter.
METER_LABELS = {
    "audio_day": "Audio, daily limit",
    "audio_hour": "Audio, hourly limit",
    "transcriptions": "Transcriptions, daily limit",
    "cleanups": "Cleanup requests, daily limit",
    "cleanup_tokens": "Cleanup tokens, daily limit",
}

# The "almost used up" notification: shown when a meter reaches WARN_AT percent, and
# not again for that meter until it has drained below REARM_BELOW percent.
WARN_AT = 80
REARM_BELOW = 70
FAKE_WARNING_SHARE = 0.85  # debug_fake_limit_warning pretends the meter is this full
# The "Groq limit used up" notification: only for a 429 that asks to wait at least this long (seconds).
CAP_MIN_WAIT = 60

# Which meter a 429 corrects, by call kind and Groq's limit code.
# Per-minute limits (RPM, TPM) have no meter.
SNAP_FROM_429 = {
    ("transcription", "RPD"): "transcriptions",
    ("transcription", "ASH"): "audio_hour",
    ("transcription", "ASD"): "audio_day",
    ("cleanup", "RPD"): "cleanups",
    ("cleanup", "TPD"): "cleanup_tokens",
}
# The meter each call kind's x-ratelimit-*-requests headers describe (requests per day).
HEADER_METER = {"transcription": "transcriptions", "cleanup": "cleanups"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,              -- "transcription" or "cleanup"
    status TEXT NOT NULL,            -- "ok", "rate_limited" or "error"
    http_status INTEGER,             -- empty when Groq never answered (timeout, no connection)
    latency_ms REAL,
    clip_seconds REAL,
    billed_seconds REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,       -- includes the reasoning tokens
    reasoning_tokens INTEGER,
    cached_tokens INTEGER,
    limit_type TEXT,                 -- on a 429: "RPM", "RPD", "TPM", "TPD", "ASH" or "ASD"
    retry_in REAL                    -- on a 429: seconds Groq asked us to wait
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);
CREATE TABLE IF NOT EXISTS dictations (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    result TEXT NOT NULL,            -- "done", "limited", "error" or "nothing"
    transcription TEXT NOT NULL,     -- "ok", "limited" or "failed"
    cleanup TEXT NOT NULL,           -- "ok", "limited", "failed" or "skipped"
    billed_seconds REAL,
    cleanup_tokens INTEGER,
    words INTEGER NOT NULL,          -- 0 when nothing was pasted
    latency_ms REAL,
    loudest REAL                     -- loudest 20ms slice, only for pasted real speech (see voice_levels)
);
CREATE INDEX IF NOT EXISTS dictations_ts ON dictations (ts);
CREATE TABLE IF NOT EXISTS buckets (
    name TEXT PRIMARY KEY,
    level REAL NOT NULL,
    updated REAL NOT NULL
);
"""

LIMIT_NAMES = {
    "RPM": "requests per minute",
    "RPD": "requests per day",
    "TPM": "tokens per minute",
    "TPD": "tokens per day",
    "ASH": "audio seconds per hour",
    "ASD": "audio seconds per day",
}


class Bucket:
    """One Groq limit: `level` is the amount used, and it drains steadily.

    It drains at limit / window per second, so a full bucket is empty again
    after one window (24h for daily limits, 1h for the hourly one).
    Every method takes `now` (seconds), so tests can use a fake clock.
    """

    def __init__(self, limit, window, level=0.0, updated=0.0):
        self.limit = limit
        self.window = window
        self._level = level
        self._updated = updated

    @property
    def rate(self):
        return self.limit / self.window

    def level(self, now):
        elapsed = max(0.0, now - self._updated)  # a clock that jumped back drains nothing
        return max(0.0, self._level - elapsed * self.rate)

    def add(self, amount, now):
        self._level = self.level(now) + amount
        self._updated = now

    def snap(self, used, now):
        """Replace our count with Groq's own number."""
        self._level = max(0.0, float(used))
        self._updated = now

    def refilled_in(self, now):
        """Seconds until the bucket is completely empty again."""
        return self.level(now) / self.rate

    def wait_for(self, amount, now):
        """Seconds until `amount` more fits under the limit."""
        return max(0.0, (self.level(now) + amount - self.limit) / self.rate)


def parse_duration(text):
    """Groq's durations ("43.2s", "14m1.968s", "1h2m3s", "250ms") in seconds, or None."""
    if not text:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", str(text))
    if not parts or "".join(n + u for n, u in parts) != str(text).strip():
        return None
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * scale[u] for n, u in parts)


def parse_rate_limit(message):
    """Reads a 429 message like "... on tokens per day (TPD): Limit 200000, Used 198823,
    Requested 3126. Please try again in 14m1.968s."

    Returns a dict (type, name, limit, used, requested, retry_in); missing parts are None.
    """
    message = str(message or "")
    info = {"type": None, "name": None, "limit": None, "used": None, "requested": None, "retry_in": None}
    match = re.search(r"\(([A-Z]{3})\)", message)
    if match:
        info["type"] = match.group(1)
        info["name"] = LIMIT_NAMES.get(match.group(1))
    for key in ("limit", "used", "requested"):
        match = re.search(rf"\b{key.capitalize()} (\d+)", message)
        if match:
            info[key] = int(match.group(1))
    match = re.search(r"try again in ([\d.hms]+?)\.?(?:\s|$|\")", message)
    if match:
        info["retry_in"] = parse_duration(match.group(1))
    return info


def billed_seconds(clip_seconds, minimum):
    """Groq bills every transcription as at least `minimum` seconds (10 on the free tier)."""
    return max(float(clip_seconds), float(minimum))


def meter_style(used, limit):
    """(percent, level, note) for a usage bar.

    The percent is rounded down, and that one number decides both the color
    level and the text, so they can never disagree. Levels: "normal" (under 60),
    "close" (60 to 79), "out" (80 and up).
    """
    if limit <= 0:
        return 0, "normal", "0%"
    percent = min(100, max(0, math.floor(round(used / limit * 100, 6))))
    if percent >= 80:
        return percent, "out", f"{percent}%, almost out"
    if percent >= 60:
        return percent, "close", f"{percent}%, getting close"
    return percent, "normal", f"{percent}%"


def format_wait(seconds):
    """ "12s", "14m 2s", "41m", "6h 20m": short, and rounded up so it never says 0 too early."""
    seconds = max(0, math.ceil(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 600:
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    total_minutes = math.ceil(seconds / 60)
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_ago(seconds):
    """ "just now", "12s ago", "5 min ago", "2h ago"."""
    seconds = max(0, int(seconds))
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600}h ago"


def _short_number(value):
    """467, 4.7K, 128K"""
    if value < 1000:
        return f"{round(value):,}"
    if value < 10000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{round(value / 1000):,}K"


def format_amount(meter, used, limit):
    """The "X of Y" text for a meter: "18 of 480 min", "143 of 2,000", "128K of 200K"."""
    if meter in ("audio_day", "audio_hour"):
        minutes = used / 60
        shown = f"{minutes:.1f}".rstrip("0").rstrip(".") if minutes < 10 else f"{round(minutes):,}"
        return f"{shown} of {round(limit / 60):,} min"
    if meter == "cleanup_tokens":
        return f"{_short_number(used)} of {_short_number(limit)}"
    return f"{round(used):,} of {round(limit):,}"


class LimitWarnings:
    """Decides when to show the "Groq limit almost used up" notification.

    Each meter is either armed or warned. An armed meter that reaches WARN_AT
    percent is reported once and becomes warned; it's armed again only after
    draining below REARM_BELOW. The buckets drain steadily, so usage wobbling
    around 80% can't notify twice (a daily meter takes 2.4h to go from 80 to 70).

    The percent is meter_style's (rounded down), so the notification and the
    panel's "almost out" always agree. Meters already at WARN_AT on the first
    check (the app just started) are warned quietly, so restarting doesn't notify.
    """

    def __init__(self, warn_at=WARN_AT, rearm_below=REARM_BELOW):
        self.warn_at = warn_at
        self.rearm_below = rearm_below
        self._warned = None  # meter names; None until the first check

    def check(self, meters):
        """meters is snapshot()["meters"]. Returns the meters that just reached warn_at, in panel order."""
        first = self._warned is None
        if first:
            self._warned = set()
        crossed = []
        for name in sorted(meters, key=_meter_order):
            meter = meters[name]
            percent = meter_style(meter["used"], meter["limit"])[0]
            if name in self._warned:
                if percent < self.rearm_below:
                    self._warned.discard(name)
            elif percent >= self.warn_at:
                self._warned.add(name)
                if not first:
                    crossed.append(name)
        return crossed


def _meter_order(name):
    order = list(METER_LABELS)
    return order.index(name) if name in order else len(order)


def limit_warning_message(names, meters):
    """(title, text) for the notification about the meters in names."""
    title = "Groq limit almost used up" if len(names) == 1 else f"{len(names)} Groq limits almost used up"
    lines = []
    for name in names:
        meter = meters[name]
        percent = meter_style(meter["used"], meter["limit"])[0]
        lines.append(
            f"{METER_LABELS.get(name, name)}: {percent}%. Fully refilled in {format_wait(meter['refilled_in'])}."
        )
    return title, "\n".join(lines)


class CapNotices:
    """Decides when to show the "Groq limit used up" notification (a 429 that blocks for a while).

    Once per hit limit: a limit (call kind plus Groq's limit code) is reported the
    first time it's seen, and not again while it's still in force, however often
    later dictations hit it again. Only when Groq asks to wait at least min_wait
    seconds, so per-minute blips (wait 20s and it works) stay quiet.
    """

    def __init__(self, min_wait=CAP_MIN_WAIT):
        self.min_wait = min_wait
        self._until = {}  # (kind, type) -> when the limit we last reported ends

    def check(self, rate_limit):
        """rate_limit is snapshot()["rate_limit"]. Returns it when it should be reported, else None."""
        if not rate_limit or rate_limit.get("until") is None:
            return None
        key = (rate_limit["kind"], rate_limit["type"])
        now = rate_limit["until"] - rate_limit["retry_in"]
        if key in self._until and now < self._until[key]:
            self._until[key] = max(self._until[key], rate_limit["until"])  # the same hit, maybe longer
            return None
        wait = rate_limit.get("wait")
        if wait is None or wait < self.min_wait:
            return None
        self._until[key] = rate_limit["until"]
        return rate_limit


def cap_message(rate_limit, history_on=True):
    """(title, text) for the notification about a limit that's used up."""
    name = rate_limit.get("name")
    first = f"Groq's free limit is used up ({name})." if name else "Groq's free limit is used up."
    wait = format_wait(rate_limit["retry_in"])
    if rate_limit["kind"] == "cleanup":
        rest = f"Until it refills (about {wait}), {APP_DISPLAY_NAME} pastes your words without cleanup."
    elif history_on:
        rest = f"Dictation works again in about {wait}. Until then your dictations wait in History for Retry."
    else:
        rest = (f"Dictation works again in about {wait}. Until then your dictations can't be kept, "
                "because History is off.")
    return "Groq limit used up", f"{first} {rest}"


def with_fake_usage(meters, name, share=FAKE_WARNING_SHARE):
    """A copy of meters where `name` is `share` full (for debug_fake_limit_warning). The tracker is untouched."""
    meters = {key: dict(value) for key, value in meters.items()}
    meter = meters[name]
    meter["used"] = meter["limit"] * share
    meter["refilled_in"] = share * METERS[name][1]  # a full bucket takes one window to drain
    return meters


def local_day_start(now):
    """The moment your local calendar day began (for the "today" numbers)."""
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class UsageTracker:
    """Thread safe. Any thread may log; snapshot() is cheap enough for the GUI thread."""

    def __init__(self, db_path, limits, clock=time.time, day_start=local_day_start):
        self._clock = clock
        self._day_start = day_start
        self._lock = threading.Lock()
        self.min_billed = float(limits["min_billed_audio_seconds"])
        self._buckets = {name: Bucket(float(limits[key]), window) for name, (key, window) in METERS.items()}
        self._rate_limit = None  # the last 429: kind, type, name, until
        # Connection status for the panel: "connected", "unreachable" or "key_rejected".
        # It comes from the last real request; a warm-up can only report a problem.
        self._real_status = None  # None until the first real request
        self._warm_problem = None
        self._last_response = None  # when a real request last got an answer
        self._today = None
        self._voice_levels = deque(maxlen=VOICE_HISTORY)  # oldest first
        self._last_prune = 0.0
        self._db = self._open(db_path)
        with self._lock:
            self._load(self._clock())

    # ---- logging (called from the pipeline and warm-up threads) ----

    def log_transcription(self, latency_ms, clip_seconds, headers=None):
        """A successful transcription. Returns the billed seconds."""
        with self._lock:
            now = self._clock()
            billed = billed_seconds(clip_seconds, self.min_billed)
            self._buckets["transcriptions"].add(1, now)
            self._buckets["audio_day"].add(billed, now)
            self._buckets["audio_hour"].add(billed, now)
            self._snap_from_headers("transcription", headers, now)
            self._heard_from_groq(now)
            self._write_request(
                now, "transcription", "ok", 200, latency_ms, clip_seconds=clip_seconds, billed_seconds=billed
            )
            return billed

    def log_cleanup(self, latency_ms, usage=None, headers=None):
        """A successful cleanup. Returns the tokens that count toward the daily limit."""
        prompt = getattr(usage, "prompt_tokens", None) or 0
        completion = getattr(usage, "completion_tokens", None) or 0
        reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        # Cached tokens don't count toward Groq's limits.
        tokens = max(0, prompt + completion - (cached or 0))
        with self._lock:
            now = self._clock()
            self._buckets["cleanups"].add(1, now)
            self._buckets["cleanup_tokens"].add(tokens, now)
            self._snap_from_headers("cleanup", headers, now)
            self._heard_from_groq(now)
            self._write_request(
                now, "cleanup", "ok", 200, latency_ms,
                prompt_tokens=prompt, completion_tokens=completion,
                reasoning_tokens=reasoning, cached_tokens=cached,
            )
            return tokens

    def log_failure(self, kind, latency_ms, http_status=None, rate_limit=None, headers=None):
        """A failed request. Nothing is added to the meters (Groq doesn't charge for rejections).

        rate_limit is parse_rate_limit()'s dict for a 429. When it says "Used N",
        the matching meter is corrected to Groq's number.
        """
        with self._lock:
            now = self._clock()
            status = "error"
            limit_type = retry_in = None
            if rate_limit is not None:
                status = "rate_limited"
                limit_type, retry_in = rate_limit["type"], rate_limit["retry_in"]
                meter = SNAP_FROM_429.get((kind, limit_type))
                if meter and rate_limit["used"] is not None:
                    self._buckets[meter].snap(rate_limit["used"], now)
                self._rate_limit = {
                    "kind": kind,
                    "type": limit_type,
                    "name": rate_limit["name"],
                    "until": now + retry_in if retry_in is not None else None,
                    "wait": retry_in,  # what Groq asked for (snapshot's retry_in is what's left of it)
                }
                self._roll_day(now)
                self._today["rate_limits"] += 1
            self._snap_from_headers(kind, headers, now)
            if http_status is None:
                self._real_status = "unreachable"
                self._warm_problem = None
            else:
                self._heard_from_groq(now, http_status)
            self._write_request(now, kind, status, http_status, latency_ms, limit_type=limit_type, retry_in=retry_in)

    def log_warm_up(self, problem):
        """The warm-up's result: None (fine), "unreachable" or "key_rejected".

        Memory only (it runs on every key press). A fine warm-up doesn't count
        as "connected"; it only clears a problem an earlier warm-up reported.
        """
        with self._lock:
            self._warm_problem = problem

    def forget_connection(self):
        """A new API key was saved: what the old key got from Groq no longer applies."""
        with self._lock:
            self._real_status = None
            self._warm_problem = None

    def log_dictation(self, result, transcription, cleanup, billed, tokens, words, latency_ms, loudest=None):
        """One row per dictation that reached Groq. Counts only, never the text.

        loudest is given only for pasted real speech (not dictionary-only text),
        so voice_levels() learns how loud this mic and voice really are.
        """
        with self._lock:
            now = self._clock()
            self._roll_day(now)
            if words > 0:  # something was pasted (or copied)
                self._today["dictations"] += 1
                self._today["words"] += words
                self._today["latency_ms"] += latency_ms
            if loudest is not None:
                self._voice_levels.append(loudest)
            self._write(
                now,
                "INSERT INTO dictations (ts, result, transcription, cleanup, billed_seconds, cleanup_tokens,"
                " words, latency_ms, loudest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, result, transcription, cleanup, billed, tokens, words, latency_ms, loudest),
            )

    def voice_levels(self):
        """The loudest level of the last VOICE_HISTORY pasted real dictations, oldest first. Memory only."""
        with self._lock:
            return list(self._voice_levels)

    # ---- reading (the Usage panel) ----

    def snapshot(self):
        """Everything the Usage panel shows, as of now. Never touches the disk."""
        with self._lock:
            now = self._clock()
            self._roll_day(now)
            meters = {
                name: {"used": b.level(now), "limit": b.limit, "refilled_in": b.refilled_in(now)}
                for name, b in self._buckets.items()
            }
            today = self._today
            count = today["dictations"]
            rate_limit = None
            if self._rate_limit and self._rate_limit["until"] is not None and self._rate_limit["until"] > now:
                rate_limit = dict(self._rate_limit, retry_in=self._rate_limit["until"] - now)
            return {
                "meters": meters,
                "today": {
                    "dictations": count,
                    "words": today["words"],
                    "avg_latency_ms": today["latency_ms"] / count if count else None,
                    "rate_limits": today["rate_limits"],
                },
                "rate_limit": rate_limit,
                "connection": self._warm_problem or self._real_status,
                "last_response_ago": None if self._last_response is None else now - self._last_response,
            }

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---- inside (call with the lock held) ----

    def _heard_from_groq(self, now, http_status=200):
        """A real request got an answer. 401/403 means the API key doesn't work."""
        self._real_status = "key_rejected" if http_status in (401, 403) else "connected"
        self._warm_problem = None  # this is newer than any warm-up result
        self._last_response = now

    def _snap_from_headers(self, kind, headers, now):
        """Correct the requests-per-day meter from Groq's x-ratelimit-*-requests headers.

        reset-requests is how long until the bucket is full again, so
        used = reset x limit / 24h. That's more precise than "remaining", which
        is a whole number (and already includes the request it came with).
        """
        meter = HEADER_METER.get(kind)
        if not headers or not meter:
            return
        limit = _number(headers.get("x-ratelimit-limit-requests"))
        reset = parse_duration(headers.get("x-ratelimit-reset-requests"))
        remaining = _number(headers.get("x-ratelimit-remaining-requests"))
        if not limit:
            return
        if reset is not None:
            used = reset * limit / DAY
        elif remaining is not None:
            used = limit - remaining
        else:
            return
        self._buckets[meter].snap(used, now)

    def _roll_day(self, now):
        start = self._day_start(now)
        if self._today is None or self._today["start"] != start:
            self._today = {"start": start, "dictations": 0, "words": 0, "latency_ms": 0.0, "rate_limits": 0}

    # ---- the database ----

    def _open(self, path):
        try:
            db = sqlite3.connect(str(path), check_same_thread=False)
            # WAL: a crash mid-write can lose that one write, never corrupt the file.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            # usage.db files made before the loudest column existed get it added (old rows stay empty).
            if "loudest" not in [row[1] for row in db.execute("PRAGMA table_info(dictations)")]:
                db.execute("ALTER TABLE dictations ADD COLUMN loudest REAL")
            return db
        except sqlite3.Error as e:
            print(f"usage.db couldn't be opened, usage is only counted until the app closes: {e}")
            return None

    def _load(self, now):
        """At startup: restore the meters (they drained while the app was closed) and today's totals."""
        self._roll_day(now)
        if self._db is None:
            return
        try:
            for name, level, updated in self._db.execute("SELECT name, level, updated FROM buckets"):
                if name in self._buckets:
                    self._buckets[name].snap(level, updated)
            start = self._today["start"]
            count, words, latency = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(words), 0), COALESCE(SUM(latency_ms), 0)"
                " FROM dictations WHERE ts >= ? AND words > 0",
                (start,),
            ).fetchone()
            (limits,) = self._db.execute(
                "SELECT COUNT(*) FROM requests WHERE ts >= ? AND status = 'rate_limited'", (start,)
            ).fetchone()
            self._today.update(dictations=count, words=words, latency_ms=latency, rate_limits=limits)
            rows = self._db.execute(
                "SELECT loudest FROM dictations WHERE loudest IS NOT NULL ORDER BY id DESC LIMIT ?", (VOICE_HISTORY,)
            ).fetchall()
            self._voice_levels.extend(level for (level,) in reversed(rows))
            self._prune(now)
        except sqlite3.Error as e:
            print(f"usage.db couldn't be read: {e}")

    def _write_request(self, now, kind, status, http_status, latency_ms, **fields):
        columns = ["ts", "kind", "status", "http_status", "latency_ms", *fields]
        values = [now, kind, status, http_status, latency_ms, *fields.values()]
        marks = ", ".join("?" * len(columns))
        self._write(now, f"INSERT INTO requests ({', '.join(columns)}) VALUES ({marks})", values)

    def _write(self, now, sql, values):
        """One transaction: the log row plus every meter's current level."""
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(sql, values)
                self._db.executemany(
                    "INSERT OR REPLACE INTO buckets (name, level, updated) VALUES (?, ?, ?)",
                    [(name, b.level(now), now) for name, b in self._buckets.items()],
                )
            if now - self._last_prune >= PRUNE_EVERY:
                self._prune(now)
        except sqlite3.Error as e:
            print(f"usage.db write failed (usage still counted in memory): {e}")

    def _prune(self, now):
        cutoff = now - KEEP_DAYS * DAY
        with self._db:
            self._db.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM dictations WHERE ts < ?", (cutoff,))
        self._last_prune = now
