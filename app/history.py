"""History: the text of recent dictations, kept on this laptop only.

- Saved in data/history.db (SQLite), separate from usage.db, which only ever
  holds counts. data/ is in .gitignore, and nothing here is sent anywhere.
- Kept: the newest KEEP_ENTRIES entries, and nothing older than KEEP_DAYS.
- Failed and rate limited dictations keep their audio in data/failed/<id>.wav,
  so Retry can send it again. The file goes when a retry works, when the entry
  expires, or on Clear.
- clear() really erases: secure_delete overwrites deleted rows with zeros, and
  the journal (WAL) is emptied and the file compacted afterwards.
- The pipeline thread writes; the GUI only calls entries() and version, which
  never touch the disk.
"""

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from app.paths import FAILED_DIR, HISTORY_DB, LAST_DISCARDED, OLD_LAST_RECORDING

# Clips the pipeline keeps next to failed/ (in data/). Clear deletes them too.
LOOSE_CLIPS = (LAST_DISCARDED.name, OLD_LAST_RECORDING.name)

KEEP_ENTRIES = 200
KEEP_DAYS = 7
DAY = 86400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    status TEXT NOT NULL,            -- "pasted", "copied", "failed" or "limited"
    raw TEXT,                        -- Whisper's transcript (empty if transcription failed)
    cleaned TEXT,                    -- after cleanup; empty if cleanup was skipped
    cleanup TEXT,                    -- "ok", "limited" or "failed"
    error TEXT,                      -- a short reason, for failed and rate limited entries
    audio TEXT,                      -- file name in data/failed/, only while Retry is possible
    latency_ms REAL,
    retried_ts REAL                  -- when a retry last ran
);
"""
COLUMNS = ["id", "ts", "status", "raw", "cleaned", "cleanup", "error", "audio", "latency_ms", "retried_ts"]


def format_entry_time(ts, now):
    """ "2:41 PM" today, "Yesterday 2:41 PM", or "Mon 2:41 PM" (entries are at most 7 days old)."""
    when = datetime.fromtimestamp(ts)
    days = (datetime.fromtimestamp(now).date() - when.date()).days
    clock = f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"
    if days <= 0:
        return clock
    if days == 1:
        return f"Yesterday {clock}"
    return f"{when.strftime('%a')} {clock}"


def entry_text(entry):
    """What the entry shows and Copy copies: the cleaned text, or the raw transcript if cleanup was skipped."""
    return entry["cleaned"] or entry["raw"] or ""


class HistoryStore:
    """Thread safe. Every method that looks at the time takes it from `clock`, so tests can fake it."""

    def __init__(self, db_path=HISTORY_DB, audio_dir=FAILED_DIR, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._audio_dir = Path(audio_dir)
        self._entries = []  # newest first, the same rows as the database
        self._retrying = set()  # entry ids with a retry on its way (memory only)
        self._version = 0
        self._db = self._open(db_path)
        with self._lock:
            self._load()

    # ---- writing (the pipeline thread) ----

    def add(self, status, raw=None, cleaned=None, cleanup=None, error=None, latency_ms=None, wav_bytes=None):
        """Saves one dictation. wav_bytes (failed and rate limited ones) is kept for Retry. Returns the id."""
        with self._lock:
            now = self._clock()
            entry = dict.fromkeys(COLUMNS)
            entry.update(ts=now, status=status, raw=raw, cleaned=cleaned, cleanup=cleanup, error=error,
                         latency_ms=latency_ms)
            entry["id"] = self._insert(entry)
            if wav_bytes is not None:
                # Row first, then the file, then the row points at it. A crash in between
                # leaves a file nothing points at, which the next start deletes.
                name = f"{entry['id']}.wav"
                try:
                    self._audio_dir.mkdir(parents=True, exist_ok=True)
                    (self._audio_dir / name).write_bytes(wav_bytes)
                    entry["audio"] = name
                    self._execute("UPDATE entries SET audio = ? WHERE id = ?", (name, entry["id"]))
                except OSError as e:
                    print(f"history: couldn't keep the audio for Retry: {e}")
            self._entries.insert(0, entry)
            self._prune(now)
            self._version += 1
            return entry["id"]

    def update(self, entry_id, drop_audio=False, **fields):
        """Changes an entry (after a retry). drop_audio deletes its saved audio (Retry isn't needed anymore)."""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return  # expired or cleared in the meantime
            if drop_audio:
                self._delete_audio(entry)
                fields["audio"] = None
            entry.update(fields)
            names = [key for key in fields if key in COLUMNS and key != "id"]
            if names:
                sets = ", ".join(f"{name} = ?" for name in names)
                self._execute(f"UPDATE entries SET {sets} WHERE id = ?", [fields[n] for n in names] + [entry_id])
            self._version += 1

    def set_retrying(self, entry_id, retrying):
        """Memory only: after a restart, an entry that was being retried is a normal failed entry again."""
        with self._lock:
            if retrying:
                self._retrying.add(entry_id)
            else:
                self._retrying.discard(entry_id)
            self._version += 1

    def clear(self):
        """Deletes every entry and every saved clip, and makes sure the text can't be read from the file."""
        with self._lock:
            for entry in self._entries:
                self._delete_audio(entry)
            self._entries.clear()
            self._retrying.clear()
            self._version += 1
            if self._db is None:
                return
            try:
                with self._db:
                    self._db.execute("DELETE FROM entries")
                # secure_delete already zeroed the rows; now empty the journal and shrink the file.
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db.execute("VACUUM")
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as e:
                print(f"history.db couldn't be cleared completely: {e}")
        # Clips left over from a crash, and the loose clips in data/ (a discarded dictation, and
        # last_recording.wav from older versions), so no audio of you is left behind.
        for path in [*self._audio_dir.glob("*.wav"), *(self._audio_dir.parent / name for name in LOOSE_CLIPS)]:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                print(f"history: couldn't delete {path.name}: {e}")

    # ---- reading ----

    @property
    def version(self):
        """Goes up on every change, so the panel knows when to repaint."""
        with self._lock:
            return self._version

    def entries(self):
        """Copies of all entries, newest first, plus "retrying" and "can_retry". Memory only."""
        with self._lock:
            return [
                dict(e, retrying=e["id"] in self._retrying, can_retry=e["audio"] is not None)
                for e in self._entries
            ]

    def entry(self, entry_id):
        with self._lock:
            entry = self._find(entry_id)
            return dict(entry) if entry else None

    def audio_path(self, entry_id):
        """The saved clip for Retry, or None (no clip, or its file is gone)."""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None or entry["audio"] is None:
                return None
            path = self._audio_dir / entry["audio"]
            return path if path.exists() else None

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---- inside (call with the lock held) ----

    def _find(self, entry_id):
        return next((e for e in self._entries if e["id"] == entry_id), None)

    def _delete_audio(self, entry):
        if entry["audio"]:
            (self._audio_dir / entry["audio"]).unlink(missing_ok=True)
            entry["audio"] = None

    def _prune(self, now):
        """Drops entries past KEEP_ENTRIES or older than KEEP_DAYS, with their audio."""
        cutoff = now - KEEP_DAYS * DAY
        keep, drop = [], []
        for entry in self._entries:  # newest first
            (keep if len(keep) < KEEP_ENTRIES and entry["ts"] >= cutoff else drop).append(entry)
        if not drop:
            return
        for entry in drop:
            self._delete_audio(entry)
            self._retrying.discard(entry["id"])
        self._entries = keep
        if self._db is not None:
            try:
                with self._db:
                    self._db.executemany("DELETE FROM entries WHERE id = ?", [(e["id"],) for e in drop])
            except sqlite3.Error as e:
                print(f"history.db couldn't delete old entries: {e}")

    def _sweep_audio(self):
        """Deletes clips no entry points at (left by a crash, or by entries deleted elsewhere)."""
        if not self._audio_dir.exists():
            return
        wanted = {e["audio"] for e in self._entries if e["audio"]}
        for path in self._audio_dir.glob("*.wav"):
            if path.name not in wanted:
                path.unlink(missing_ok=True)

    # ---- the database ----

    def _open(self, path):
        try:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(path), check_same_thread=False)
            # Deleted rows are overwritten with zeros, not just marked free.
            db.execute("PRAGMA secure_delete=ON")
            # WAL: a crash mid-write can lose that one write, never corrupt the file.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            return db
        except (sqlite3.Error, OSError) as e:  # OSError: a folder that can't be written
            print(f"history.db couldn't be opened, History only lasts until the app closes: {e}")
            return None

    def _load(self):
        if self._db is not None:
            try:
                rows = self._db.execute(f"SELECT {', '.join(COLUMNS)} FROM entries ORDER BY id DESC").fetchall()
                self._entries = [dict(zip(COLUMNS, row)) for row in rows]
            except sqlite3.Error as e:
                print(f"history.db couldn't be read: {e}")
        self._prune(self._clock())
        self._sweep_audio()

    def _insert(self, entry):
        if self._db is None:
            # No database: ids still have to be unique for this run.
            return max((e["id"] for e in self._entries), default=0) + 1
        names = [c for c in COLUMNS if c != "id"]
        try:
            with self._db:
                cursor = self._db.execute(
                    f"INSERT INTO entries ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                    [entry[n] for n in names],
                )
            return cursor.lastrowid
        except sqlite3.Error as e:
            print(f"history.db write failed (kept until the app closes): {e}")
            return max((e["id"] for e in self._entries), default=0) + 1

    def _execute(self, sql, values):
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(sql, values)
        except sqlite3.Error as e:
            print(f"history.db write failed (kept until the app closes): {e}")
