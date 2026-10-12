"""Tests for the mic label: the name rule, the cut to fit, and when it says "No microphone".

No real mic needed. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_micname -v
"""

import unittest
from unittest import mock

import numpy as np

from app import recorder as recorder_module
from app.micname import ELLIPSIS, NO_MIC, LabelHold, elide, full_name, readable_mic_name
from app.recorder import BLOCK_FRAMES, Recorder


class ReadableNameTests(unittest.TestCase):
    def check(self, raw, expected):
        self.assertEqual(readable_mic_name(raw), expected, raw)

    def test_real_names_from_this_laptop(self):
        self.check("Microphone (Realtek(R) Audio)", "Realtek Audio")
        self.check("Microphone (e2eSoft iVCam)", "e2eSoft iVCam")
        self.check("AI Noise-cancelling Input (ASUS Utility)", "AI Noise-cancelling Input")

    def test_bluetooth_keeps_hands_free(self):
        # "Hands-Free" is the tell that a headset is in low-quality call mode.
        self.check("Headset (WH-1000XM4 Hands-Free AG Audio)", "WH-1000XM4 Hands-Free")
        self.check("Headset Microphone (Jabra Evolve2 65 Hands-Free)", "Jabra Evolve2 65 Hands-Free")

    def test_duplicate_number_dropped(self):
        self.check("Microphone (2- USB Audio Device)", "USB Audio Device")

    def test_renamed_mic_keeps_your_name(self):
        self.check("Laptop mic (Realtek(R) Audio)", "Laptop mic")

    def test_generic_names_are_case_insensitive(self):
        self.check("Microphone Array (Intel Smart Sound)", "Intel Smart Sound")
        self.check("microphone (Blue Yeti)", "Blue Yeti")

    def test_nested_brackets(self):
        self.check("Microphone (Intelligo VAC (W))", "Intelligo VAC (W)")

    def test_mme_cut_names(self):
        # MME cuts names at 31 characters, sometimes in the middle of a bracket.
        self.check("AI Noise-cancelling Input (ASUS", "AI Noise-cancelling Input")
        self.check("Microphone (Realtek(R", "Microphone")
        self.check("Headset (Foo (Bar", "Headset")

    def test_trademark_signs(self):
        self.check("Microphone (Contoso® Mic™)", "Contoso Mic")
        self.check("Microphone (Contoso(TM) Mic)", "Contoso Mic")

    def test_empty_or_bare(self):
        self.check("Microphone ()", "Microphone")
        self.check("", "Microphone")
        self.check(None, "Microphone")
        self.check("   ", "Microphone")
        self.check("Blue Yeti", "Blue Yeti")
        self.check("(Blue Yeti)", "Blue Yeti")

    def test_extra_spaces(self):
        self.check("  Microphone   (Realtek   Audio)  ", "Realtek Audio")


class FullNameTests(unittest.TestCase):
    CANDIDATES = [
        "AI Noise-cancelling Input (ASUS",  # the MME name itself
        "Microphone (Realtek(R) Audio)",
        "AI Noise-cancelling Input (ASUS Utility)",
    ]

    def test_cut_name_is_completed(self):
        self.assertEqual(full_name("AI Noise-cancelling Input (ASUS", self.CANDIDATES), self.CANDIDATES[2])

    def test_short_name_is_left_alone(self):
        self.assertEqual(full_name("Microphone (Realtek(R) Audio)", self.CANDIDATES), "Microphone (Realtek(R) Audio)")

    def test_no_match_keeps_the_cut_name(self):
        cut = "X" * 31
        self.assertEqual(full_name(cut, self.CANDIDATES), cut)


class ElideTests(unittest.TestCase):
    # len() as the "font": every character is 1 unit wide.

    def test_fits(self):
        self.assertEqual(elide("Realtek Audio", 20, len), "Realtek Audio")

    def test_exact_fit(self):
        self.assertEqual(elide("Realtek Audio", 13, len), "Realtek Audio")

    def test_cut(self):
        self.assertEqual(elide("abcdefghij", 6, len), "abcde" + ELLIPSIS)

    def test_trailing_space_and_punctuation_trimmed(self):
        self.assertEqual(elide("Realtek Audio", 9, len), "Realtek" + ELLIPSIS)
        self.assertEqual(elide("Jabra (Evolve2 65)", 8, len), "Jabra" + ELLIPSIS)

    def test_result_always_fits(self):
        text = "My Extremely Long Custom Microphone Name From Sound Settings"
        for width in range(2, len(text)):
            self.assertLessEqual(len(elide(text, width, len)), width)

    def test_tiny_width(self):
        self.assertEqual(elide("Realtek Audio", 1, len), ELLIPSIS)
        self.assertEqual(elide("Realtek Audio", 0, len), ELLIPSIS)


class LabelHoldTests(unittest.TestCase):
    """The label shows a name for mic_label_hold_ms, then hides. Times in ms, fake clock."""

    HOLD = 1500

    def setUp(self):
        self.hold = LabelHold()

    def at(self, value, now, hold=HOLD):
        return self.hold.visible(value, hold, now)

    def test_name_shows_then_hides(self):
        self.assertEqual(self.at("Realtek Audio", 0), "Realtek Audio")
        self.assertEqual(self.at("Realtek Audio", 1499), "Realtek Audio")
        self.assertIsNone(self.at("Realtek Audio", 1500))
        self.assertIsNone(self.at("Realtek Audio", 9000))  # stays hidden for the rest of the recording

    def test_clock_starts_when_the_name_first_appears(self):
        # keep_mic_open off: the mic is still opening for the first 300ms.
        self.assertIsNone(self.at(None, 0))
        self.assertIsNone(self.at(None, 300))
        self.assertEqual(self.at("Realtek Audio", 300), "Realtek Audio")
        self.assertEqual(self.at("Realtek Audio", 1700), "Realtek Audio")  # 1400ms after it appeared
        self.assertIsNone(self.at("Realtek Audio", 1800))

    def test_zero_means_the_whole_recording(self):
        self.assertEqual(self.at("Realtek Audio", 0, hold=0), "Realtek Audio")
        self.assertEqual(self.at("Realtek Audio", 60000, hold=0), "Realtek Audio")

    def test_no_mic_never_hides(self):
        self.assertIs(self.at(NO_MIC, 0), NO_MIC)
        self.assertIs(self.at(NO_MIC, 60000), NO_MIC)

    def test_mic_dies_after_the_label_faded(self):
        self.at("Realtek Audio", 0)
        self.assertIsNone(self.at("Realtek Audio", 2000))
        self.assertIs(self.at(NO_MIC, 2500), NO_MIC)  # shows right away
        self.assertIs(self.at(NO_MIC, 9000), NO_MIC)  # and stays

    def test_new_name_gets_a_full_hold(self):
        self.at("Realtek Audio", 0)
        self.assertEqual(self.at("e2eSoft iVCam", 1000), "e2eSoft iVCam")
        self.assertEqual(self.at("e2eSoft iVCam", 2400), "e2eSoft iVCam")
        self.assertIsNone(self.at("e2eSoft iVCam", 2500))

    def test_reset_shows_the_same_name_again(self):
        # A new recording, or "m" in the preview tool.
        self.at("Realtek Audio", 0)
        self.assertIsNone(self.at("Realtek Audio", 2000))
        self.hold.reset()
        self.assertEqual(self.at("Realtek Audio", 2000), "Realtek Audio")
        self.assertIsNone(self.at("Realtek Audio", 3500))


class MicLabelTests(unittest.TestCase):
    """Recorder.mic_label: "No microphone" only when the stream stops delivering, never for silence."""

    def setUp(self):
        self.now = 1000.0
        patcher = mock.patch.object(recorder_module.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = Recorder(keep_open=True)  # never opened: no real mic is touched

    def fake_open(self, name="Realtek Audio"):
        self.recorder._stream = object()
        self.recorder._mic_name = name
        self.recorder._last_audio = self.now

    def deliver(self, seconds, loudness=0):
        """The audio callback, every 20ms for `seconds`, with samples of this loudness."""
        chunk = np.full((BLOCK_FRAMES, 1), loudness, dtype=np.int16)
        for _ in range(round(seconds / 0.02)):
            self.now += 0.02
            self.recorder._callback(chunk, BLOCK_FRAMES, None, None)

    def test_silence_keeps_the_name(self):
        self.fake_open()
        self.recorder.start()
        self.deliver(3.0, loudness=0)  # holding Ctrl+Win for 3s without speaking
        self.assertEqual(self.recorder.level, 0.0)
        self.assertEqual(self.recorder.mic_label, "Realtek Audio")

    def test_stopped_stream_says_no_mic(self):
        self.fake_open()
        self.recorder.start()
        self.deliver(0.5, loudness=1000)
        self.now += 1.5  # no chunks at all: the mic was unplugged
        self.assertIs(self.recorder.mic_label, NO_MIC)
        self.deliver(0.1)  # chunks again
        self.assertEqual(self.recorder.mic_label, "Realtek Audio")

    def test_not_open_yet_is_hidden_not_no_mic(self):
        # keep_mic_open off: before the stream opens, the label stays hidden.
        self.assertIsNone(self.recorder.mic_label)

    def test_failed_open_says_no_mic(self):
        self.recorder._open_failed = True
        self.assertIs(self.recorder.mic_label, NO_MIC)


if __name__ == "__main__":
    unittest.main()
