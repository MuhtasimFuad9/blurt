"""Tests for Pause (the mic closes and stays closed) and a clean quit.

No real mic, keyboard hook or network: the mic stream, the hook thread and the
pipeline's work are faked. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_pause -v
"""

import contextlib
import io
import threading
import time
import unittest
from unittest import mock

import numpy as np

import main
from app.pipeline import Pipeline
from app.recorder import Recorder


class FakeStream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def fake_open_stream(recorder):
    recorder._stream = FakeStream()
    recorder._mic_name = "Fake mic"
    recorder._last_audio = time.monotonic()


class PauseReasonsTest(unittest.TestCase):
    def setUp(self):
        self.applied = []
        self.pause = main.PauseReasons(self.applied.append)

    def test_only_changes_are_applied(self):
        self.pause.user_toggle(True)
        self.pause.set("key_window", True)
        self.pause.set("key_window", False)
        self.assertTrue(self.pause.paused)  # your own pause stays
        self.pause.user_toggle(False)
        self.assertEqual(self.applied, [True, False])

    def test_resume_doesnt_lift_the_no_key_pause(self):
        self.pause.set("no_key", True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.pause.user_toggle(False)
        self.assertTrue(self.pause.paused)
        self.assertIn("no Groq API key", out.getvalue())
        self.pause.set("no_key", False)
        self.assertFalse(self.pause.paused)
        self.assertEqual(self.applied, [True, False])


class RecorderPauseTest(unittest.TestCase):
    def setUp(self):
        self.fingerprint = ["mic A"]
        patches = [
            mock.patch("app.recorder.device_fingerprint", lambda: tuple(self.fingerprint)),
            mock.patch.object(Recorder, "_open_stream", fake_open_stream),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_pause_closes_the_mic_and_resume_reopens_it(self):
        recorder = Recorder(keep_open=True)
        recorder.open()
        stream = recorder._stream
        recorder.set_paused(True)
        self.assertTrue(stream.closed)
        self.assertFalse(recorder.is_open())
        recorder.set_paused(False)
        self.assertTrue(recorder.is_open())

    def test_device_check_never_reopens_while_paused(self):
        recorder = Recorder(keep_open=True)
        recorder.open()
        recorder.set_paused(True)
        self.fingerprint[0] = "mic B"  # a new mic plugged in
        recorder.check_devices()
        self.assertFalse(recorder.is_open())
        recorder.set_paused(False)
        self.assertTrue(recorder.is_open())

    def test_pause_during_a_recording_keeps_the_mic_until_it_ends(self):
        recorder = Recorder(keep_open=True)
        recorder.open()
        recorder.start()
        recorder._callback(np.ones((320, 1), dtype=np.int16), 320, None, None)
        recorder.set_paused(True)
        self.assertTrue(recorder.is_open())
        audio = recorder.stop()
        self.assertEqual(len(audio), 320)  # the recording kept its audio
        self.assertFalse(recorder.is_open())

    def test_resume_with_the_mic_opening_on_demand_stays_closed(self):
        recorder = Recorder(keep_open=False)
        recorder.open()
        recorder.set_paused(True)
        recorder.set_paused(False)
        self.assertFalse(recorder.is_open())  # it opens on the next Ctrl+Win, as before


class WaitUntilIdleTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = Pipeline(client=None, hotkey=None, config={}, tracker=None)
        self.release = threading.Event()

    def slow_job(self, audio, submitted_at, target=None):
        self.release.wait(5)
        return "done"

    def test_waits_for_the_dictation_in_progress(self):
        self.pipeline._process = self.slow_job
        self.pipeline.start()
        self.assertTrue(self.pipeline.wait_until_idle(0.01))  # nothing queued yet
        self.pipeline.submit(np.zeros(10, dtype=np.int16))
        self.assertFalse(self.pipeline.wait_until_idle(0.05))  # still pasting
        self.release.set()
        self.assertTrue(self.pipeline.wait_until_idle(2))

    def test_a_crashing_job_still_counts_as_finished(self):
        def crash(audio, submitted_at):
            raise RuntimeError("boom")

        self.pipeline._process = crash
        self.pipeline._save_entry = mock.Mock()
        self.pipeline.start()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.pipeline.submit(np.zeros(10, dtype=np.int16))
            self.assertTrue(self.pipeline.wait_until_idle(2))


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.hotkey = mock.Mock(paused=False)
        self.hotkey.stop.side_effect = lambda: self.log.append("stop hook")
        self.hotkey.uninstall.side_effect = lambda: self.log.append("unhook here")
        self.hotkey.release_stuck_modifiers.side_effect = lambda: self.log.append("repair keys here")
        self.hook_thread = mock.Mock(ident=1)
        self.hook_thread.is_alive.return_value = False
        self.pipeline = mock.Mock()

        def wait(seconds):
            self.log.append(f"wait for pipeline, paused={self.hotkey.paused}")
            return True

        self.pipeline.wait_until_idle.side_effect = wait
        self.recorder = mock.Mock()
        self.recorder.close.side_effect = lambda: self.log.append("close mic")
        self.stores = [mock.Mock(), mock.Mock()]
        for i, store in enumerate(self.stores):
            store.close.side_effect = lambda i=i: self.log.append(f"close db {i}")

    def shutdown(self, before=None):
        main.shutdown(self.hotkey, self.hook_thread, self.pipeline, self.recorder, self.stores, before=before)

    def test_order(self):
        self.shutdown(before=lambda: self.log.append("hide windows"))
        self.assertEqual(
            self.log,
            ["hide windows", "wait for pipeline, paused=True", "stop hook", "close mic", "close db 0", "close db 1"],
        )

    def test_a_hanging_hook_thread_is_cleaned_up_here(self):
        self.hook_thread.is_alive.return_value = True
        self.shutdown()
        self.assertEqual(self.log[1:4], ["stop hook", "unhook here", "repair keys here"])

    def test_still_unhooks_when_an_earlier_step_fails(self):
        def broken():
            raise RuntimeError("hiding failed")

        with self.assertRaises(RuntimeError):
            self.shutdown(before=broken)
        self.assertTrue(self.hotkey.paused)
        self.assertEqual(self.log, ["stop hook", "close mic", "close db 0", "close db 1"])

    def test_one_close_failing_doesnt_skip_the_rest(self):
        self.recorder.close.side_effect = OSError("device gone")
        with contextlib.redirect_stderr(io.StringIO()):
            self.shutdown()
        self.assertEqual(self.log[-2:], ["close db 0", "close db 1"])

    def test_a_slow_dictation_is_given_up_on(self):
        self.pipeline.wait_until_idle.side_effect = None
        self.pipeline.wait_until_idle.return_value = False
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.shutdown()
        self.assertIn("still processing", out.getvalue())
        self.assertIn("stop hook", self.log)


if __name__ == "__main__":
    unittest.main()
