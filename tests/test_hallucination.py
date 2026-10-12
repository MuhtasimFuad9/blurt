"""Tests for the guards against Whisper guessing words on near-silence.

The silence check, the echo guard, the dictionary-only rule and its adaptive
loudness threshold. Fake audio and fake Groq replies, no network. Run from the
project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_hallucination -v
"""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from app import pipeline as pipeline_module
from app.pipeline import Pipeline, audio_stats, dictionary_only_min_loudest, is_only_filler
from app.recorder import SAMPLE_RATE
from app.transcribe import is_dictionary_only, is_prompt_echo
from app.usage import UsageTracker

TERMS = ["Acme Creations", "Blender", "Groq", "n8n", "Claude Code", "Claude", "WisprFlow"]
CONFIG = {
    "silence_level": 0.01,
    "speech_floor_ratio": 4,
    "dictionary_only_min_speech_ms": 120,
    "dictionary_only_loudness_ratio": 0.3,
    "dictionary_only_min_loudest": None,
    "language": "en",
}
LIMITS = {
    "audio_seconds_per_day": 28800,
    "audio_seconds_per_hour": 7200,
    "transcribe_requests_per_day": 2000,
    "cleanup_requests_per_day": 1000,
    "cleanup_tokens_per_day": 200000,
    "min_billed_audio_seconds": 10,
}
PRE_ROLL_MS = 200
POST_ROLL_MS = 250


def samples(ms):
    return int(SAMPLE_RATE * ms / 1000)


def room(ms, rms=0.003, seed=0):
    """Quiet background noise."""
    return np.random.default_rng(seed).normal(0, rms * 32768, samples(ms))


def tone(ms, rms=0.05):
    """A steady 200Hz sound, standing in for a voice. 20ms slices hold whole cycles, so every slice has this RMS."""
    t = np.arange(samples(ms)) / SAMPLE_RATE
    return np.sin(2 * np.pi * 200 * t) * rms * np.sqrt(2) * 32768


def clip(*parts):
    return np.clip(np.concatenate(parts), -32768, 32767).astype(np.int16)


def spoken(speech_ms, loudest=0.05, room_rms=0.003):
    """Pre-roll, speech_ms of voice at this loudness, post-roll, like a real dictation."""
    return clip(room(PRE_ROLL_MS, room_rms), tone(speech_ms, loudest), room(POST_ROLL_MS, room_rms, seed=1))


def steady_noise(ms=2400, rms=0.012):
    """A fan or mic hiss, loud enough to pass the old silence check everywhere."""
    return clip(room(ms, rms=rms))


def two_short_sounds():
    """100ms of sound in two bits (e.g. key clicks): passes the silence check, too little for dictionary words."""
    return clip(room(PRE_ROLL_MS), tone(40), room(1000, seed=1), tone(60), room(POST_ROLL_MS, seed=2))


class AudioStatsTests(unittest.TestCase):
    def test_digital_silence_has_no_speech(self):
        loudest, floor, speech_ms = audio_stats(np.zeros(samples(2000), dtype=np.int16), 0.01, 4)
        self.assertEqual((loudest, speech_ms), (0.0, 0.0))

    def test_steady_noise_has_no_speech(self):
        loudest, floor, speech_ms = audio_stats(steady_noise(), 0.01, 4)
        self.assertGreater(loudest, 0.01)  # the old check would have let this through
        self.assertEqual(speech_ms, 0.0)

    def test_short_word_counts(self):
        self.assertEqual(audio_stats(spoken(700), 0.01, 4)[2], 700)

    def test_long_speech_counts(self):
        # Over 90% of this clip is voice. A percentile-based floor landed on the voice and counted 0s.
        self.assertEqual(audio_stats(spoken(5000), 0.01, 4)[2], 5000)
        self.assertEqual(audio_stats(spoken(20000), 0.01, 4)[2], 20000)

    def test_soft_voice_over_a_fan_counts(self):
        audio = clip(room(PRE_ROLL_MS, rms=0.008), tone(1500, rms=0.04) + room(1500, rms=0.008, seed=1), room(POST_ROLL_MS, rms=0.008, seed=2))
        self.assertGreaterEqual(audio_stats(audio, 0.01, 4)[2], 1400)

    def test_floor_ratio_1_ignores_the_background(self):
        # The setting really is the knob: at 1, steady noise above silence_level counts again.
        self.assertGreater(audio_stats(steady_noise(), 0.01, 1)[2], 2000)


class DictionaryOnlyTests(unittest.TestCase):
    def test_only_terms(self):
        for text in ["Acme Creations", "Acme Creations.", "groq", "Blender, Groq!"]:
            self.assertTrue(is_dictionary_only(text, TERMS), text)

    def test_other_words(self):
        for text in ["I use Blender", "Ritual", "Acme C-L-A-Y-O-N", ""]:
            self.assertFalse(is_dictionary_only(text, TERMS), text)

    def test_no_dictionary(self):
        self.assertFalse(is_dictionary_only("Blender", []))


class EchoGuardTests(unittest.TestCase):
    def test_echoes(self):
        for text in [
            "I, Groq, n8n, Claude, WisprFlow",  # real silent hold from the checklist
            "Blender, Groq, n8n, Claude Code, WisprFlow",  # the pure tone from Milestone 2
            "Blender, Groq",  # 2+ terms and nothing else (the old rule)
            "Groq, n8n, a Claude",
        ]:
            self.assertTrue(is_prompt_echo(text, TERMS), text)

    def test_real_sentences(self):
        for text in [
            "I use Groq, n8n and Claude every day",
            "I use Groq, n8n, Claude",  # "use" is too long to be a stray word
            "WisprFlow, Groq and Blender",  # not in dictionary order
            "Blender",  # a single term is fine
            "Groq and n8n",
        ]:
            self.assertFalse(is_prompt_echo(text, TERMS), text)


class AdaptiveThresholdTests(unittest.TestCase):
    def test_default_until_10_dictations(self):
        self.assertEqual(dictionary_only_min_loudest([], 0.3, None)[0], 0.02)
        level, source = dictionary_only_min_loudest([0.1] * 9, 0.3, None)
        self.assertEqual(level, 0.02)
        self.assertIn("9 so far", source)

    def test_median_math(self):
        levels = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.5]  # median 0.11
        level, source = dictionary_only_min_loudest(levels, 0.3, None)
        self.assertAlmostEqual(level, 0.033)
        self.assertIn("adaptive", source)

    def test_only_the_last_20_count(self):
        levels = [1.0] * 30 + [0.1] * 20
        self.assertAlmostEqual(dictionary_only_min_loudest(levels, 0.3, None)[0], 0.03)

    def test_override(self):
        level, source = dictionary_only_min_loudest([0.1] * 20, 0.3, 0.05)
        self.assertEqual(level, 0.05)
        self.assertIn("fixed", source)


class VoiceLevelStorageTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def open(self):
        tracker = UsageTracker(self.db_path, LIMITS)
        self.addCleanup(tracker.close)
        return tracker

    def test_levels_survive_a_restart(self):
        tracker = self.open()
        tracker.log_dictation("done", "ok", "ok", 10, 300, 5, 900, loudest=0.07)
        tracker.log_dictation("nothing", "ok", "skipped", 10, 0, 0, 400)  # no level
        tracker.log_dictation("done", "ok", "ok", 10, 300, 3, 900, loudest=0.09)
        tracker.close()
        self.assertEqual(self.open().voice_levels(), [0.07, 0.09])

    def test_keeps_the_last_20(self):
        tracker = self.open()
        for i in range(25):
            tracker.log_dictation("done", "ok", "ok", 10, 300, 5, 900, loudest=i / 100)
        self.assertEqual(tracker.voice_levels(), [i / 100 for i in range(5, 25)])
        tracker.close()
        self.assertEqual(self.open().voice_levels(), [i / 100 for i in range(5, 25)])

    def test_old_usage_db_gets_the_new_column(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE dictations (id INTEGER PRIMARY KEY, ts REAL NOT NULL, result TEXT NOT NULL,"
                " transcription TEXT NOT NULL, cleanup TEXT NOT NULL, billed_seconds REAL, cleanup_tokens INTEGER,"
                " words INTEGER NOT NULL, latency_ms REAL)"
            )
        db.close()
        tracker = self.open()
        tracker.log_dictation("done", "ok", "ok", 10, 300, 5, 900, loudest=0.08)
        tracker.close()
        self.assertEqual(self.open().voice_levels(), [0.08])


class FakeTracker:
    def __init__(self, voice_levels=()):
        self.levels = list(voice_levels)
        self.logged = []  # the loudest level passed with each dictation

    def log_dictation(self, *args):
        self.logged.append(args[7] if len(args) > 7 else None)

    def voice_levels(self):
        return list(self.levels)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.recording = folder / "last_recording.wav"
        self.discarded = folder / "last_discarded.wav"
        self.settings = {"debug_save_discarded": True}  # read fresh, like config.json
        for name, value in [("DATA_DIR", folder), ("LAST_DISCARDED", self.discarded),
                            ("load_terms", lambda: TERMS), ("load_config", lambda: self.settings)]:
            patcher = mock.patch.object(pipeline_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = FakeTracker()
        self.pipeline = Pipeline(client=None, hotkey=None, config=CONFIG, tracker=self.tracker)
        self.deliver = mock.patch.object(self.pipeline, "_deliver", return_value="done").start()
        self.addCleanup(mock.patch.stopall)

    def run_dictation(self, audio, raw, cleaned):
        """Runs one dictation with fake Groq replies. Returns (result, transcribe mock, clean_up mock)."""
        with mock.patch.object(pipeline_module, "transcribe", return_value=(raw, 10.0)) as transcribe, \
                mock.patch.object(pipeline_module, "clean_up", return_value=(cleaned, "ok", 300)) as clean_up, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = self.pipeline._process(audio, 0.0)
        self.output = out.getvalue()
        return result, transcribe, clean_up

    # ---- the five checklist cases, with their real console numbers ----

    def test_real_silent_hold_steady_noise(self):
        result, transcribe, _ = self.run_dictation(steady_noise(), "R-Claude", "R-Claude")
        self.assertEqual(result, "quiet")
        transcribe.assert_not_called()
        self.assertIn("silence check", self.output)
        self.assertTrue(self.discarded.exists())
        self.assertFalse(self.recording.exists())  # last_recording.wav is never written anymore

    def test_real_silent_hold_that_pasted(self):
        # loudest 0.016, speech 0.16s, Whisper: "I, Groq, n8n, Claude, WisprFlow"
        result, _, clean_up = self.run_dictation(spoken(160, 0.016), "I, Groq, n8n, Claude, WisprFlow", "")
        self.assertEqual(result, "nothing")
        clean_up.assert_not_called()
        self.assertIn("echo guard", self.output)
        self.assertTrue(self.discarded.exists())

    def test_real_acme_creations(self):
        result, _, _ = self.run_dictation(spoken(1480, 0.118), "Acme creations", "Acme Creations")
        self.assertEqual(result, "done")
        self.deliver.assert_called_once_with("Acme Creations", None)

    def test_real_groq_normal(self):
        result, _, _ = self.run_dictation(spoken(420, 0.080), "Groq.", "Groq.")
        self.assertEqual(result, "done")

    def test_real_groq_muttered(self):
        # loudest 0.015, speech 0.10s: the accepted loss, it sounds just like noise
        result, _, _ = self.run_dictation(spoken(100, 0.015), "Groq.", "Groq.")
        self.assertEqual(result, "nothing")
        self.deliver.assert_not_called()
        self.assertIn("dictionary-only rule", self.output)
        self.assertIn("default until 10 dictations, 0 so far", self.output)

    def test_real_soft_voice_sentence(self):
        # loudest 0.026, speech 1.80s, floor 0.0005
        audio = spoken(1800, 0.026, room_rms=0.0005)
        result, _, _ = self.run_dictation(audio, "this is me speaking softly", "This is me speaking softly.")
        self.assertEqual(result, "done")

    # ---- the dictionary-only rule ----

    def test_quiet_term_is_discarded_even_with_enough_speech(self):
        result, _, _ = self.run_dictation(spoken(300, 0.015), "Groq.", "Groq.")
        self.assertEqual(result, "nothing")

    def test_adaptive_threshold_is_used_once_learned(self):
        self.tracker.levels = [0.08] * 20  # needs 0.3 x 0.08 = 0.024
        result, _, _ = self.run_dictation(spoken(420, 0.022), "Groq.", "Groq.")
        self.assertEqual(result, "nothing")  # 0.022 would have passed the 0.02 default
        self.assertIn("adaptive", self.output)
        result, _, _ = self.run_dictation(spoken(420, 0.030), "Groq.", "Groq.")
        self.assertEqual(result, "done")

    def test_bug_replay_guess_turned_into_a_term_by_cleanup(self):
        result, _, _ = self.run_dictation(two_short_sounds(), "Acme C-L-A-Y-O-N", "Acme Creations")
        self.assertEqual(result, "nothing")
        self.deliver.assert_not_called()

    def test_other_words_are_not_touched_by_the_rule(self):
        result, _, _ = self.run_dictation(two_short_sounds(), "Ritual", "Ritual.")
        self.assertEqual(result, "done")

    def test_real_sentence_with_terms_still_pastes(self):
        text = "I use Groq, n8n and Claude every day."
        result, _, _ = self.run_dictation(spoken(2000, 0.08), text, text)
        self.assertEqual(result, "done")

    # ---- what gets stored for the adaptive threshold ----

    def test_pasted_sentence_stores_its_loudness(self):
        self.run_dictation(spoken(1800, 0.026, room_rms=0.0005), "hello there", "Hello there.")
        self.assertAlmostEqual(self.tracker.logged[-1], 0.026, places=3)

    def test_pasted_dictionary_only_text_stores_nothing(self):
        self.run_dictation(spoken(420, 0.08), "Groq.", "Groq.")
        self.assertIsNone(self.tracker.logged[-1])

    def test_discarded_dictation_stores_nothing(self):
        self.run_dictation(spoken(100, 0.015), "Groq.", "Groq.")
        self.assertIsNone(self.tracker.logged[-1])

    def test_failed_paste_stores_nothing(self):
        self.deliver.return_value = "error"
        self.run_dictation(spoken(1800, 0.05), "hello there", "Hello there.")
        self.assertIsNone(self.tracker.logged[-1])

    # ---- no audio of you left on disk ----

    def test_discarded_audio_is_not_kept_by_default(self):
        self.settings = {}  # debug_save_discarded not set: off
        result, _, _ = self.run_dictation(steady_noise(), "R-Claude", "R-Claude")
        self.assertEqual(result, "quiet")
        self.assertFalse(self.discarded.exists())

    def test_a_pasted_dictation_writes_no_audio(self):
        self.run_dictation(spoken(1480, 0.118), "Acme creations", "Acme Creations")
        self.assertEqual(list(self.discarded.parent.iterdir()), [])


class FillerTests(unittest.TestCase):
    def test_filler_is_dropped(self):
        for text in ["So.", "Um, uh", "um so", "", "...", "?"]:
            with self.subTest(text=text):
                self.assertTrue(is_only_filler(text))

    def test_real_words_in_any_language_are_kept(self):
        for text in ["Hello", "42", "3:30", "আমি ভাত খাই", "Привет, как дела?", "你好", "So, 42.", "Don't."]:
            with self.subTest(text=text):
                self.assertFalse(is_only_filler(text))


if __name__ == "__main__":
    unittest.main()
