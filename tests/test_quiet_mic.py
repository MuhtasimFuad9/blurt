"""Tests for a quiet mic (M17): the silence check's "quiet" result, and the notification
after 2 of them in a row (main.py's QuietMic).

No real mic, network or notifications. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_quiet_mic -v
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

import main
from app import pipeline as pipeline_module
from app.history import HistoryStore
from app.pipeline import Pipeline
from app.ui.pill import UiBridge
from tests.test_hallucination import CONFIG, TERMS, FakeTracker, spoken, steady_noise

app = QApplication.instance() or QApplication([])


class QuietMicTest(unittest.TestCase):
    def setUp(self):
        self.notify = mock.Mock()
        self.quiet = main.QuietMic(self.notify)

    def results(self, *results):
        for result in results:
            self.quiet.job_finished(result, 0)

    def test_two_in_a_row(self):
        self.assertEqual(main.QUIET_IN_A_ROW, 2)
        self.results("quiet")
        self.notify.assert_not_called()
        self.results("quiet")
        self.notify.assert_called_once_with()

    def test_once_per_run(self):
        self.results("quiet", "quiet", "quiet", "quiet")
        self.results("done", "quiet", "quiet")  # reset, and 2 in a row again
        self.notify.assert_called_once_with()

    def test_anything_that_heard_something_starts_the_count_again(self):
        for heard in ("done", "copied", "limited", "error", "nothing"):
            with self.subTest(heard=heard):
                quiet = main.QuietMic(self.notify)
                for result in ("quiet", heard, "quiet"):
                    quiet.job_finished(result, 0)
        self.notify.assert_not_called()

    def test_through_the_bridge_signal(self):
        # The same signal the pill listens to: "quiet" and its History entry (always 0).
        bridge = UiBridge()
        bridge.job_finished.connect(self.quiet.job_finished)
        bridge.job_finished.emit("quiet", 0)
        bridge.job_finished.emit("quiet", 0)
        self.notify.assert_called_once_with()


class QuietResultTest(unittest.TestCase):
    """A dictation the silence check drops: "quiet", nothing sent, nothing saved."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        settings = {"history": True}
        for name, value in [("DATA_DIR", self.folder), ("LAST_DISCARDED", self.folder / "last_discarded.wav"),
                            ("load_terms", lambda: TERMS), ("load_config", lambda: settings)]:
            patcher = mock.patch.object(pipeline_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.history = HistoryStore(self.folder / "history.db", self.folder / "failed")
        self.addCleanup(self.history.close)
        self.tracker = FakeTracker()
        self.results = []
        self.pipeline = Pipeline(client=object(), hotkey=None, config=CONFIG, tracker=self.tracker,
                                 on_result=lambda *args: self.results.append(args), history=self.history)
        self.transcribe = mock.patch.object(pipeline_module, "transcribe", return_value=("hello", 10.0)).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(pipeline_module, "clean_up", return_value=("Hello.", "ok", 300)).start()
        mock.patch.object(self.pipeline, "_deliver", return_value="pasted").start()

    def dictate(self, audio):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.pipeline._run_job("dictation", audio, 0.0, None)
        return out.getvalue()

    def test_silence_gives_quiet_and_saves_nothing(self):
        out = self.dictate(steady_noise())
        self.assertEqual(self.results, [("quiet", 0)])  # to the pill and QuietMic, no History entry
        self.assertIn("discarded by silence check", out)
        self.transcribe.assert_not_called()  # never sent to Groq
        self.assertEqual(self.tracker.logged, [])  # no usage.db row
        self.assertEqual(self.history.entries(), [])
        self.assertEqual(list(self.folder.rglob("*.wav")), [])  # debug_save_discarded is off

    def test_speech_is_not_quiet(self):
        self.dictate(spoken(1500))
        self.assertEqual(self.results[0][0], "done")


if __name__ == "__main__":
    unittest.main()
