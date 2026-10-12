"""Tests for History: the store (history.py) and what the pipeline saves and retries.

Fake clock, fake audio, fake Groq replies, temporary folders; the real
data/history.db is never touched. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_history -v
"""

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import groq
import httpx

from app import pipeline as pipeline_module
from app.api import RateLimited
from app.history import DAY, KEEP_DAYS, KEEP_ENTRIES, HistoryStore, entry_text, format_entry_time
from app.pipeline import Pipeline, to_wav_bytes
from app.usage import parse_rate_limit
from tests.test_hallucination import CONFIG, TERMS, FakeTracker, spoken, steady_noise

SECRET = "my secret dictation about Acme Creations"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.db_path = self.folder / "history.db"
        self.audio_dir = self.folder / "failed"
        self.clock = Clock()

    def open(self):
        store = HistoryStore(self.db_path, self.audio_dir, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def wavs(self):
        return sorted(p.name for p in self.audio_dir.glob("*.wav")) if self.audio_dir.exists() else []

    def test_entries_are_newest_first_and_survive_a_restart(self):
        store = self.open()
        store.add("pasted", raw="one", cleaned="One.", cleanup="ok")
        self.clock.now += 5
        store.add("copied", raw="two", cleaned=None, cleanup="limited")
        store.close()
        entries = self.open().entries()
        self.assertEqual([entry_text(e) for e in entries], ["two", "One."])
        self.assertEqual(entries[0]["status"], "copied")

    def test_failed_entry_keeps_its_audio(self):
        store = self.open()
        entry_id = store.add("failed", error="No answer from Groq", wav_bytes=b"RIFF fake")
        self.assertEqual(self.wavs(), [f"{entry_id}.wav"])
        self.assertEqual(store.audio_path(entry_id).read_bytes(), b"RIFF fake")
        self.assertTrue(store.entries()[0]["can_retry"])

    def test_keeps_only_the_newest_200(self):
        store = self.open()
        first = store.add("failed", wav_bytes=b"x")
        for i in range(KEEP_ENTRIES):
            store.add("pasted", raw=str(i))
        entries = store.entries()
        self.assertEqual(len(entries), KEEP_ENTRIES)
        self.assertNotIn(first, [e["id"] for e in entries])
        self.assertEqual(self.wavs(), [])  # the dropped entry's audio went with it

    def test_drops_entries_older_than_7_days(self):
        store = self.open()
        store.add("limited", wav_bytes=b"x")
        self.clock.now += KEEP_DAYS * DAY + 1
        store.add("pasted", raw="new")
        self.assertEqual([e["raw"] for e in store.entries()], ["new"])
        self.assertEqual(self.wavs(), [])

    def test_old_entries_are_dropped_at_startup_too(self):
        store = self.open()
        store.add("failed", wav_bytes=b"x")
        store.close()
        self.clock.now += KEEP_DAYS * DAY + 1
        self.assertEqual(self.open().entries(), [])
        self.assertEqual(self.wavs(), [])

    def test_orphan_audio_is_deleted_at_startup(self):
        store = self.open()
        entry_id = store.add("failed", wav_bytes=b"x")
        store.close()
        (self.audio_dir / "999.wav").write_bytes(b"left by a crash")
        self.open()
        self.assertEqual(self.wavs(), [f"{entry_id}.wav"])

    def test_missing_audio_file_means_no_retry(self):
        store = self.open()
        entry_id = store.add("failed", wav_bytes=b"x")
        (self.audio_dir / f"{entry_id}.wav").unlink()
        self.assertIsNone(store.audio_path(entry_id))

    def test_update_with_drop_audio(self):
        store = self.open()
        entry_id = store.add("failed", wav_bytes=b"x")
        store.update(entry_id, drop_audio=True, status="copied", raw="hi", cleaned="Hi.")
        entry = store.entries()[0]
        self.assertEqual((entry["status"], entry_text(entry), entry["can_retry"]), ("copied", "Hi.", False))
        self.assertEqual(self.wavs(), [])
        store.close()
        self.assertEqual(self.open().entries()[0]["status"], "copied")

    def test_retrying_is_memory_only(self):
        store = self.open()
        entry_id = store.add("failed", wav_bytes=b"x")
        store.set_retrying(entry_id, True)
        self.assertTrue(store.entries()[0]["retrying"])
        store.close()  # the app closes mid retry
        entry = self.open().entries()[0]
        self.assertFalse(entry["retrying"])
        self.assertTrue(entry["can_retry"])

    def test_version_goes_up_on_every_change(self):
        store = self.open()
        versions = [store.version]
        entry_id = store.add("failed", wav_bytes=b"x")
        versions.append(store.version)
        store.set_retrying(entry_id, True)
        versions.append(store.version)
        store.update(entry_id, error="again")
        versions.append(store.version)
        self.assertEqual(versions, sorted(set(versions)))

    def test_clear_really_erases_the_text(self):
        store = self.open()
        for i in range(30):
            store.add("pasted", raw=SECRET, cleaned=f"{SECRET} {i}")
        store.add("failed", wav_bytes=b"x")
        store.clear()
        self.assertEqual(store.entries(), [])
        self.assertEqual(self.wavs(), [])
        store.close()
        # The text must not be readable anywhere in the database files.
        for path in self.folder.glob("history.db*"):
            self.assertNotIn(b"secret dictation", path.read_bytes(), path.name)
        self.assertEqual(self.open().entries(), [])

    def test_clear_also_deletes_the_loose_clips(self):
        store = self.open()
        loose = [self.folder / "last_discarded.wav", self.folder / "last_recording.wav"]
        for path in loose:
            path.write_bytes(b"audio")
        store.clear()
        self.assertEqual([p.name for p in loose if p.exists()], [])

    def test_an_unwritable_folder_does_not_crash(self):
        blocker = self.folder / "not a folder"
        blocker.write_bytes(b"")  # a file where the data folder should be: mkdir fails
        with contextlib.redirect_stdout(io.StringIO()):
            store = HistoryStore(blocker / "history.db", blocker / "failed", clock=self.clock)
            entry_id = store.add("pasted", raw="hello")
        self.addCleanup(store.close)
        self.assertEqual(entry_text(store.entry(entry_id)), "hello")  # kept in memory

    def test_secure_delete_is_on(self):
        store = self.open()
        (value,) = store._db.execute("PRAGMA secure_delete").fetchone()
        self.assertEqual(value, 1)


class TimeFormatTests(unittest.TestCase):
    def at(self, day, hour, minute):
        return datetime(2026, 9, day, hour, minute).timestamp()

    def test_today(self):
        now = self.at(28, 18, 0)
        self.assertEqual(format_entry_time(self.at(28, 14, 41), now), "2:41 PM")
        self.assertEqual(format_entry_time(self.at(28, 0, 5), now), "12:05 AM")
        self.assertEqual(format_entry_time(self.at(28, 12, 0), now), "12:00 PM")

    def test_yesterday_and_older(self):
        now = self.at(28, 0, 30)  # just after midnight: 11 PM is already yesterday
        self.assertEqual(format_entry_time(self.at(27, 23, 0), now), "Yesterday 11:00 PM")
        self.assertEqual(format_entry_time(self.at(22, 9, 3), now), "Tue 9:03 AM")


def connection_error():
    return groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))


def rate_limited():
    message = ("Rate limit reached for model `whisper-large-v3-turbo` on requests per minute (RPM): "
               "Limit 20, Used 20, Requested 1. Please try again in 30s.")
    return RateLimited("transcription", parse_rate_limit(message))


class PipelineHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = self.folder = Path(tempfile.mkdtemp())
        self.audio_dir = folder / "failed"
        self.settings = {"history": True}
        for name, value in [
            ("DATA_DIR", folder),
            ("LAST_DISCARDED", folder / "last_discarded.wav"),
            ("load_terms", lambda: TERMS),
            ("load_config", lambda: self.settings),
        ]:
            mock.patch.object(pipeline_module, name, value).start()
        self.addCleanup(mock.patch.stopall)
        self.history = HistoryStore(folder / "history.db", self.audio_dir)
        self.addCleanup(self.history.close)
        self.results = []
        # A stand-in client: Groq itself is faked below (fake_groq).
        self.pipeline = Pipeline(client=object(), hotkey=None, config=CONFIG, tracker=FakeTracker(),
                                 on_result=lambda *args: self.results.append(args), history=self.history)
        self.deliver = mock.patch.object(self.pipeline, "_deliver", return_value="pasted").start()
        self.copy_only = mock.patch.object(pipeline_module, "copy_only", return_value=True).start()

    def fake_groq(self, raw=None, cleaned=None, cleanup="ok", error=None):
        """Makes the next transcriptions return raw (or raise error), and cleanup return cleaned."""
        transcribe = mock.patch.object(pipeline_module, "transcribe").start()
        if error is not None:
            transcribe.side_effect = error
        else:
            transcribe.return_value = (raw, 10.0)
        mock.patch.object(pipeline_module, "clean_up", return_value=(cleaned, cleanup, 300)).start()

    def dictate(self, audio):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.pipeline._process(audio, 0.0)

    def retry(self, entry_id):
        with contextlib.redirect_stdout(io.StringIO()):
            self.pipeline._run_retry(entry_id, 0.0)

    def only_entry(self):
        entries = self.history.entries()
        self.assertEqual(len(entries), 1)
        return entries[0]

    # ---- what each dictation leaves in History ----

    def test_pasted(self):
        self.fake_groq("hello there", "Hello there.")
        self.assertEqual(self.dictate(spoken(1500)), "done")
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry["raw"], entry["cleaned"]), ("pasted", "hello there", "Hello there."))
        self.assertFalse(entry["can_retry"])
        self.assertEqual(self.pipeline._entry_id, entry["id"])

    def test_left_on_the_clipboard(self):
        self.fake_groq("hello there", "Hello there.")
        self.deliver.return_value = "copied"
        self.dictate(spoken(1500))
        self.assertEqual(self.only_entry()["status"], "copied")

    def test_cleanup_rate_limited_keeps_the_raw_text(self):
        self.fake_groq("hello there", None, cleanup="limited")
        self.assertEqual(self.dictate(spoken(1500)), "limited")
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry["cleanup"], entry_text(entry)), ("pasted", "limited", "hello there"))
        self.assertFalse(entry["can_retry"])

    def test_transcription_rate_limited_keeps_the_audio(self):
        self.fake_groq(error=rate_limited())
        self.assertEqual(self.dictate(spoken(1500)), "limited")
        entry = self.only_entry()
        self.assertEqual(entry["status"], "limited")
        self.assertEqual(entry["error"], "Rate limit: requests per minute")
        self.assertTrue(entry["can_retry"])

    def test_transcription_failed_keeps_the_audio(self):
        self.fake_groq(error=connection_error())
        self.assertEqual(self.dictate(spoken(1500)), "error")
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry["error"]), ("failed", "No answer from Groq"))
        self.assertTrue(entry["can_retry"])

    def test_paste_failed_keeps_the_text_not_the_audio(self):
        self.fake_groq("hello there", "Hello there.")
        self.deliver.return_value = "error"
        self.assertEqual(self.dictate(spoken(1500)), "error")
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry_text(entry)), ("failed", "Hello there."))
        self.assertFalse(entry["can_retry"])

    def test_keys_still_held_says_so(self):
        self.fake_groq("hello there", "Hello there.")
        self.deliver.return_value = "held"  # paste() waited 10s for the keys to be released
        self.assertEqual(self.dictate(spoken(1500)), "error")
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry_text(entry)), ("failed", "Hello there."))
        self.assertEqual(entry["error"], "Couldn't paste (keys were still held). Press Copy.")
        self.assertFalse(entry["can_retry"])

    def test_crash_keeps_the_audio(self):
        self.fake_groq("hello there", "Hello there.")
        self.deliver.side_effect = RuntimeError("boom")
        jobs = [("dictation", spoken(1500), 0.0)]

        def get():
            if not jobs:
                raise StopIteration  # ends the pipeline's endless loop
            return jobs.pop(0)

        with mock.patch.object(self.pipeline._jobs, "get", get), contextlib.redirect_stdout(io.StringIO()),                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(StopIteration):
            self.pipeline._run()
        entry = self.only_entry()
        self.assertEqual(entry["status"], "failed")
        self.assertTrue(entry["can_retry"])
        self.assertEqual(self.results, [("error", entry["id"])])

    def test_discards_leave_nothing(self):
        cases = [
            (steady_noise(), "R-Claude", "R-Claude", "quiet"),  # silence check
            (spoken(1500), "", "", "nothing"),  # empty transcript
            (spoken(160, 0.016), "I, Groq, n8n, Claude, WisprFlow", "x", "nothing"),  # echo guard
            (spoken(100, 0.015), "Groq.", "Groq.", "nothing"),  # dictionary-only rule
            (spoken(1500), "um so", "Um, so.", "nothing"),  # only filler
        ]
        for audio, raw, cleaned, expected in cases:
            with self.subTest(raw=raw):
                self.fake_groq(raw, cleaned)
                self.assertEqual(self.dictate(audio), expected)
        self.assertEqual(self.history.entries(), [])

    def test_history_off_saves_nothing(self):
        self.settings["history"] = False
        self.fake_groq(error=connection_error())
        self.dictate(spoken(1500))
        self.fake_groq("hello", "Hello.")
        self.dictate(spoken(1500))
        self.assertEqual(self.history.entries(), [])
        self.assertEqual(list(self.folder.rglob("*.wav")), [])  # no audio anywhere

    def test_no_history_store_still_works(self):
        pipeline = Pipeline(client=None, hotkey=None, config=CONFIG, tracker=FakeTracker())
        mock.patch.object(pipeline, "_deliver", return_value="pasted").start()
        self.fake_groq("hello", "Hello.")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline._process(spoken(1500), 0.0), "done")

    # ---- pasting only into the window you dictated into ----

    def real_deliver(self, text, target, now, settings=None):
        """Runs the real _deliver with target as the window at release and now as the one in front."""
        self.settings.update(settings or {"paste_mode": "always"})
        pipeline = Pipeline(client=None, hotkey=None, config=dict(CONFIG, clipboard_restore_ms=0),
                            tracker=FakeTracker())
        self.paste = mock.patch.object(pipeline_module, "paste", return_value="pasted").start()
        mock.patch.object(pipeline_module, "foreground_root", return_value=now).start()
        mock.patch.object(pipeline_module, "window_exe", lambda hwnd: {1: "notepad.exe", 2: "Discord.exe"}[hwnd]).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = pipeline._deliver(text, target)
        return result, out.getvalue()

    def test_same_window_pastes(self):
        result, _ = self.real_deliver("Hello.", target=1, now=1)
        self.assertEqual(result, "pasted")
        self.paste.assert_called_once()
        self.copy_only.assert_not_called()

    def test_switched_window_copies_instead(self):
        for mode in ("always", "smart"):
            with self.subTest(mode=mode):
                self.copy_only.reset_mock()
                result, output = self.real_deliver("Hello.", target=1, now=2, settings={"paste_mode": mode})
                self.assertEqual(result, "copied")
                self.paste.assert_not_called()
                self.copy_only.assert_called_once_with("Hello.")
                self.assertIn("notepad.exe -> Discord.exe", output)

    def test_no_target_skips_the_check(self):
        result, _ = self.real_deliver("Hello.", target=None, now=2)
        self.assertEqual(result, "pasted")

    def test_copied_shows_on_the_pill_and_counts_as_done(self):
        self.fake_groq("hello there", "Hello there.")
        self.deliver.return_value = "copied"
        self.assertEqual(self.dictate(spoken(1500)), "copied")
        self.assertEqual(self.only_entry()["status"], "copied")

    def test_copied_with_cleanup_rate_limited_shows_the_hourglass(self):
        self.fake_groq("hello there", None, cleanup="limited")
        self.deliver.return_value = "copied"
        self.assertEqual(self.dictate(spoken(1500)), "limited")

    # ---- retry ----

    def failed_entry(self):
        self.fake_groq(error=connection_error())
        self.dictate(spoken(1500))
        return self.only_entry()["id"]

    def test_retry_that_works_copies_and_drops_the_audio(self):
        entry_id = self.failed_entry()
        self.fake_groq("hello there", "Hello there.")
        self.retry(entry_id)
        self.copy_only.assert_called_once_with("Hello there.")
        self.deliver.assert_not_called()  # never pasted
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry_text(entry), entry["error"]), ("copied", "Hello there.", None))
        self.assertIsNotNone(entry["retried_ts"])
        self.assertFalse(entry["can_retry"])
        self.assertFalse(entry["retrying"])
        self.assertEqual(list(self.audio_dir.glob("*.wav")), [])

    def test_retry_that_fails_again_keeps_the_audio(self):
        entry_id = self.failed_entry()
        self.fake_groq(error=rate_limited())
        self.retry(entry_id)
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry["error"]), ("limited", "Rate limit: requests per minute"))
        self.assertTrue(entry["can_retry"])
        self.assertFalse(entry["retrying"])

    def test_retry_that_hears_nothing_keeps_the_audio_and_retry(self):
        entry_id = self.failed_entry()
        self.fake_groq("", "")
        self.retry(entry_id)
        entry = self.only_entry()
        self.assertEqual((entry["status"], entry["error"]), ("failed", "Nothing heard on retry"))
        self.assertTrue(entry["can_retry"])
        self.copy_only.assert_not_called()

    def test_retry_sends_the_saved_audio(self):
        audio = spoken(1500)
        self.fake_groq(error=connection_error())
        self.dictate(audio)
        entry_id = self.only_entry()["id"]
        self.fake_groq("hello", "Hello.")
        self.retry(entry_id)
        sent = pipeline_module.transcribe.call_args.args[2]
        self.assertEqual(sent, to_wav_bytes(audio))

    def test_retry_is_queued_and_marked(self):
        entry_id = self.failed_entry()
        self.pipeline.retry(entry_id)
        self.assertTrue(self.only_entry()["retrying"])
        self.assertEqual(self.pipeline._jobs.get_nowait()[:2], ("retry", entry_id))

    def test_retry_without_an_api_key_does_nothing(self):
        entry_id = self.failed_entry()
        self.pipeline.set_client(None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.pipeline.retry(entry_id)
        self.assertFalse(self.only_entry()["retrying"])
        self.assertTrue(self.pipeline._jobs.empty())

    def test_retry_does_not_tell_the_pill(self):
        entry_id = self.failed_entry()
        self.fake_groq("hello", "Hello.")
        self.retry(entry_id)
        self.assertEqual(self.results, [])


RAW = "um so the render finished I think around like two a m and uh the colors look a bit off"
CLEANED = "The render finished around 2 AM, and the colors look a bit off."


class ShowOriginalTests(unittest.TestCase):
    """The History panel's Original button (M13). Nothing is shown on screen; the clipboard is faked."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.ui import history_panel
        from app.ui.history_panel import HistoryPanel

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = HistoryStore(Path(folder.name) / "history.db", Path(folder.name) / "failed", clock=Clock())
        self.addCleanup(self.store.close)
        self.changed = self.store.add("pasted", raw=RAW, cleaned=CLEANED, cleanup="ok")
        self.punctuation_only = self.store.add("pasted", raw="thanks that works for me",
                                               cleaned="Thanks, that works for me.", cleanup="ok")
        self.not_cleaned = self.store.add("pasted", raw="so the intro should ease out", cleanup="limited")
        self.copy_only = mock.patch.object(history_panel, "copy_only", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(history_panel, "load_config", return_value={"history": True}):
            self.panel = HistoryPanel(self.store, None, debug=False)
            self.addCleanup(self.panel.deleteLater)
            self.open()

    def open(self):
        from app.ui import history_panel

        with mock.patch.object(history_panel, "load_config", return_value={"history": True}):
            self.panel._open = True  # like open_panel(), without showing a window
            self.panel._on_open()
        self.addCleanup(self.panel._live_timer.stop)

    def item(self, entry_id):
        return next(i for i in self.panel._items if i["id"] == entry_id)

    def buttons(self, entry_id):
        rect = self.panel._entry_rect(self.item(entry_id), self.panel._list_rect())
        return self.panel._buttons(self.panel._by_id[entry_id], rect)

    def shown(self, entry_id):
        return " ".join(self.item(entry_id)["lines"])

    def test_only_when_cleanup_changed_the_words(self):
        from app.ui.history_panel import has_original

        self.assertTrue(has_original(self.store.entry(self.changed)))
        self.assertFalse(has_original(self.store.entry(self.punctuation_only)))  # only commas and capitals
        self.assertFalse(has_original(self.store.entry(self.not_cleaned)))  # already shows the raw text
        self.assertIn("original", self.buttons(self.changed))
        self.assertNotIn("original", self.buttons(self.punctuation_only))
        self.assertNotIn("original", self.buttons(self.not_cleaned))

    def test_no_button_on_failed_entries(self):
        failed = self.store.add("failed", error="No answer from Groq", wav_bytes=b"x")
        self.panel._refresh()
        self.assertEqual(set(self.buttons(failed)), {"retry"})

    def test_a_click_on_the_button_hits_it(self):
        button = self.buttons(self.changed)["original"]
        self.assertEqual(self.panel._content_target(button.center()), ("original", self.changed))

    def test_switches_to_the_original_and_back(self):
        self.assertEqual(self.shown(self.changed), CLEANED)
        self.panel._content_release(("original", self.changed))
        self.assertEqual(self.shown(self.changed), RAW)
        self.panel._content_release(("original", self.changed))
        self.assertEqual(self.shown(self.changed), CLEANED)

    def test_copy_copies_what_is_shown(self):
        self.panel._content_release(("copy", self.changed))
        self.copy_only.assert_called_with(CLEANED)
        self.panel._content_release(("original", self.changed))
        self.panel._content_release(("copy", self.changed))
        self.copy_only.assert_called_with(RAW)

    def test_the_original_gets_more_lines(self):
        from app.ui.history_panel import ORIGINAL_LINES, TEXT_LINES

        long_raw = " ".join(["I want to build an installer so other people can use the app."] * 6)
        entry_id = self.store.add("pasted", raw=long_raw, cleaned="Anyone can download one file.", cleanup="ok")
        self.panel._refresh()
        self.panel._content_release(("original", entry_id))
        lines = self.item(entry_id)["lines"]
        self.assertGreater(len(lines), TEXT_LINES)
        self.assertLessEqual(len(lines), ORIGINAL_LINES)
        self.assertTrue(lines[-1].endswith("…"))

    def test_only_the_clicked_entry_switches(self):
        other = self.store.add("pasted", raw="uh send it tomorrow", cleaned="Send it tomorrow.", cleanup="ok")
        self.panel._refresh()
        self.panel._content_release(("original", self.changed))
        self.assertEqual(self.shown(other), "Send it tomorrow.")

    def test_stays_switched_when_a_new_dictation_arrives(self):
        self.panel._content_release(("original", self.changed))
        self.store.add("pasted", raw="hello", cleaned="Hello.", cleanup="ok")
        self.panel._refresh()
        self.assertEqual(self.shown(self.changed), RAW)

    def test_reopening_shows_the_cleaned_text_again(self):
        self.panel._content_release(("original", self.changed))
        self.panel._on_close()
        self.open()
        self.assertEqual(self.shown(self.changed), CLEANED)


if __name__ == "__main__":
    unittest.main()
