"""Shows only the pill, so you can tune its look and motion.

No keyboard hook, no API calls. The mic is opened so the recording bars follow
your real voice (if there's no mic, they follow a fake wobble instead).
Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\pill_preview.py

It cycles through the states on its own. Type a letter and press Enter to
drive it by hand instead (the automatic cycle stops).

Edit the "pill" section of config.json and save: changes apply right away.
"""

import math
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.api import FAKE_MESSAGES  # noqa: E402
from app.config import CONFIG_PATH, DEFAULTS  # noqa: E402
from app.history import HistoryStore  # noqa: E402
from app.micname import NO_MIC, readable_mic_name  # noqa: E402
from app.recorder import Recorder  # noqa: E402
from app.dictionary import DICTIONARY_PATH  # noqa: E402
from app.ui.dictionary_panel import DictionaryPanel  # noqa: E402
from app.ui.settings_panel import SettingsPanel  # noqa: E402
from app.ui.history_panel import HistoryPanel  # noqa: E402
from app.ui.notice import CancelNotice  # noqa: E402
from app.ui.panel import UsagePanel  # noqa: E402
from app.ui.pill import Pill, UiBridge  # noqa: E402
from app.usage import UsageTracker, parse_rate_limit  # noqa: E402

HELP = """Commands (type a letter, then Enter):
  r  start recording
  s  stop recording (sent for processing)
  t  stop recording as a quick tap (not sent)
  d  processing finished: done (checkmark)
  c  processing finished: copied, not pasted (copy icon)
  e  processing finished: error (!), with a fake History entry to open
  l  processing finished: rate limited (hourglass), with a fake History entry
  n  processing finished: nothing to paste (silence)
  x  stop recording with no mic (the "!" with "No microphone" above it)
  w  stop recording as cancelled by another key (the cancel notice in the idle pill's spot)
  m  mic label: next name (your real mic, short, very long, no mic); starts recording
  u  add fake usage, to see the Usage panel's colors (the hourly audio bar fills fastest)
  k  fake a rate limit (the Usage panel shows the banner for 30s)
  h  add fake History entries (pasted, copied, failed)
  a  automatic cycle on/off
  0  reset to idle
  q  quit
Hover the pill to see the menu, click the Usage, History or Dictionary icon for its panel.
Usage, History and Dictionary here are fake and temporary: the real usage.db, history.db
and dictionary.txt are never touched, and Retry is simulated (no Groq). Ctrl+C here also quits."""

# (wait in ms, then this command). The last step just waits, then it loops.
AUTO_STEPS = [
    (2500, "r"),
    (2000, "s"),
    (1200, "d"),
    (3000, "r"),
    (400, "t"),
    (2000, "r"),
    (1800, "s"),
    (1500, "e"),
    (5000, "r"),
    (1500, "s"),
    (1200, "l"),
    (5000, None),
]


class Commands(QObject):
    """Carries typed commands from the input thread to the GUI thread."""

    received = Signal(str)


# What "m" steps through. None = your real mic. Names go through the real name rule.
MIC_NAMES = [
    None,
    "Microphone (Realtek(R) Audio)",
    "My Extremely Long Custom Microphone Name From Sound Settings (USB Audio Device)",
    NO_MIC,
]


def fake_level():
    """Speech-like loudness that rises and falls, for when there's no mic."""
    t = time.perf_counter()
    syllables = 0.5 + 0.5 * math.sin(t * 9)
    phrases = max(0.0, math.sin(t * 1.3))
    return 0.002 + 0.08 * syllables * phrases


class Preview:
    def __init__(self, level_source, recorder):
        self.recorder = recorder
        self.mic_step = 0  # index into MIC_NAMES
        # A tracker that lives only in memory, so the real usage.db is never touched.
        self.tracker = UsageTracker(":memory:", DEFAULTS["groq_limits"])
        # History in a temporary folder, for the same reason.
        folder = Path(tempfile.mkdtemp())
        self.history = HistoryStore(folder / "history.db", folder / "failed")
        # The Dictionary panel edits a copy, so the real dictionary.txt is never touched.
        dictionary = folder / "dictionary.txt"
        dictionary.write_bytes(DICTIONARY_PATH.read_bytes() if DICTIONARY_PATH.exists() else b"")
        print(f"Dictionary panel: editing a copy at {dictionary}")
        # The Settings panel edits a copy too, so the real config.json is never touched.
        config = folder / "config.json"
        config.write_bytes(CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else b"{}\n")
        print(f"Settings panel: editing a copy at {config}")
        self.samples = 0
        panels = {
            "usage": UsagePanel(self.tracker, debug=True),
            "history": HistoryPanel(self.history, self.tracker, on_retry=self.fake_retry, debug=True),
            "dictionary": DictionaryPanel(dictionary, debug=True),
            "settings": SettingsPanel(config, on_quit=QApplication.quit, debug=True),
        }
        self.pill = Pill(level_source=level_source, debug=True, panels=panels, mic_source=self.mic_label)
        self.bridge = UiBridge()
        self.bridge.attach(self.pill)
        self.notice = CancelNotice(self.pill, debug=True)
        self.bridge.dictation_cancelled.connect(self.notice.show_notice)
        self.commands = Commands()
        self.commands.received.connect(self.on_command)
        self.auto_timer = QTimer()
        self.auto_timer.setSingleShot(True)
        self.auto_timer.timeout.connect(self.auto_step)
        self.step = 0

    def send(self, command):
        """Drive the pill exactly like the real app will: through the bridge's signals."""
        if command == "r":
            self.bridge.recording_started.emit()
        elif command == "s":
            self.bridge.recording_ended.emit(True)
        elif command == "t":
            self.bridge.recording_ended.emit(False)
        elif command in ("d", "c", "n"):
            self.bridge.job_finished.emit({"d": "done", "c": "copied", "n": "nothing"}[command], 0)
        elif command == "e":
            entry_id = self.history.add("failed", error="No answer from Groq", wav_bytes=b"fake")
            self.bridge.job_finished.emit("error", entry_id)
        elif command == "l":
            entry_id = self.history.add("limited", error="Rate limit: requests per minute", wav_bytes=b"fake")
            self.bridge.job_finished.emit("limited", entry_id)
        elif command == "x":
            # What main.py sends when there was no mic.
            self.bridge.recording_ended.emit(False)
            self.bridge.error.emit()
        elif command == "w":
            # What main.py sends when another key cancelled a recording held at least 0.3s.
            self.bridge.recording_ended.emit(False)
            self.bridge.dictation_cancelled.emit()

    def mic_label(self):
        """The mic label's source: your real mic, or the name picked with "m"."""
        name = MIC_NAMES[self.mic_step]
        if name is None:
            return self.recorder.mic_label
        if name is NO_MIC:
            return NO_MIC
        return readable_mic_name(name)

    def next_mic_name(self):
        self.mic_step = (self.mic_step + 1) % len(MIC_NAMES)
        name = MIC_NAMES[self.mic_step]
        if name is None:
            print("mic label: your real mic")
        elif name is NO_MIC:
            print("mic label: no mic")
        else:
            print(f'mic label: "{name}"')
        if self.pill._recording:
            self.pill.restart_mic_label()  # a full hold again, even if the label already faded
        else:
            self.send("r")  # so the label is on screen (a new recording starts its hold)

    def add_fake_usage(self):
        """240 short dictations: 12% of the request and token bars, 33% of the hourly audio bar."""
        for _ in range(240):
            self.tracker.log_transcription(0, clip_seconds=3)  # billed as 10s each
        for _ in range(120):
            self.tracker.log_cleanup(0, SimpleNamespace(prompt_tokens=150, completion_tokens=50))
        self.tracker.log_dictation("done", "ok", "ok", 10, 200, words=38, latency_ms=1180)
        print("added fake usage (press u again for more; the hourly audio bar fills fastest)")

    def add_fake_history(self):
        texts = [
            "Remind me to renew the domain for Acme Creations before Friday.",
            "Okay, here's the plan for the launch video. We open on the logo reveal, then cut to the product "
            "shots, and end with the call to action and the website link on screen for at least three seconds.",
            "Thanks, that works for me.",
        ]
        self.samples += 1
        self.history.add("pasted", raw="x", cleaned=texts[self.samples % 3], cleanup="ok")
        self.history.add("copied", raw=texts[(self.samples + 1) % 3].lower(), cleaned=None, cleanup="limited")
        self.history.add("failed", error="No answer from Groq", wav_bytes=b"fake")
        print("added 3 fake History entries (open History from the pill's menu)")

    def fake_retry(self, entry_id):
        """Pretends to resend the audio: "Retrying…" for 1.5s, then it works."""
        self.history.set_retrying(entry_id, True)
        print(f"retry: pretending to resend entry {entry_id} (no Groq in the preview)")

        def done():
            self.history.update(entry_id, drop_audio=True, status="copied", raw="retried text",
                                cleaned="This came back from a pretend retry.", cleanup="ok", retried_ts=time.time())
            self.history.set_retrying(entry_id, False)

        QTimer.singleShot(1500, done)

    def start_auto(self):
        self.pill.reset()
        self.step = 0
        self.auto_timer.start(AUTO_STEPS[0][0])
        print("automatic cycle on")

    def stop_auto(self):
        if self.auto_timer.isActive():
            self.auto_timer.stop()
            print("automatic cycle off (type a to turn it back on)")

    def auto_step(self):
        _, command = AUTO_STEPS[self.step]
        if command:
            self.send(command)
        self.step = (self.step + 1) % len(AUTO_STEPS)
        self.auto_timer.start(AUTO_STEPS[self.step][0])

    def on_command(self, command):
        if command == "q":
            QApplication.quit()
        elif command == "a":
            if self.auto_timer.isActive():
                self.stop_auto()
            else:
                self.start_auto()
        elif command == "0":
            self.stop_auto()
            self.pill.reset()
        elif command in ("r", "s", "t", "d", "c", "e", "l", "n", "x", "w"):
            self.stop_auto()
            self.send(command)
        elif command == "m":
            self.stop_auto()
            self.next_mic_name()
        elif command == "u":
            self.add_fake_usage()
        elif command == "h":
            self.add_fake_history()
        elif command == "k":
            message = FAKE_MESSAGES["transcription"]  # "requests per minute", try again in 30s
            self.tracker.log_failure("transcription", 0, 429, rate_limit=parse_rate_limit(message))
            print("fake rate limit logged (open the Usage panel to see the banner)")
        else:
            print(HELP)


def read_input(commands):
    """Runs on its own thread, so waiting for typing never blocks the pill."""
    for line in sys.stdin:
        commands.received.emit(line.strip().lower())
    commands.received.emit("q")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C quits immediately
    app = QApplication(sys.argv)
    recorder = Recorder(keep_open=True)
    recorder.open()
    if recorder.is_open():
        print("Mic open: the recording bars follow your voice.")
        level_source = lambda: recorder.level  # noqa: E731
    else:
        print("No mic: the recording bars follow a fake wobble instead.")
        level_source = fake_level
    try:
        preview = Preview(level_source, recorder)
        preview.pill.start()
        print(HELP)
        preview.start_auto()
        threading.Thread(target=read_input, args=(preview.commands,), daemon=True).start()
        app.exec()
    finally:
        recorder.close()
    print("Bye.")


if __name__ == "__main__":
    main()
