"""Checks that the History panel scrolls with the mouse wheel without ever taking focus.

Opens the real History panel above where the pill sits, filled with 20 fake
entries (a temporary folder; your real History isn't touched). Nothing else
runs: no hotkey, no mic, no Groq. Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\wheel_test.py

1. Open Notepad, click into it and type something.
2. Move the mouse over the panel (don't click) and scroll the wheel up and down.
3. Type again: the letters must still land in Notepad.
Every wheel event prints a line, with a warning if the panel ever became the
active window. Close with the panel's X or Ctrl+C here.
"""

import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.history import HistoryStore  # noqa: E402
from app.ui import win32  # noqa: E402

SAMPLES = [
    "Remind me to renew the domain for Acme Creations before Friday.",
    "Can you check whether the Blender render farm finished overnight?",
    "Okay, here's the plan for the launch video. We open on the logo reveal, then cut to the product "
    "shots, and end with the call to action and the website link on screen for at least three seconds.",
    "Thanks, that works for me.",
    "Let's move the call to Thursday afternoon if that still works for everyone on the team.",
]


def main():
    app = QApplication(sys.argv)
    from app.ui.history_panel import HistoryPanel  # after QApplication (fonts)

    folder = Path(tempfile.mkdtemp())
    now = time.time()
    clock = [now - 20 * 600]
    store = HistoryStore(folder / "history.db", folder / "failed", clock=lambda: clock[0])
    for i in range(20):
        clock[0] = now - (20 - i) * 600  # one every 10 minutes
        if i % 7 == 3:
            store.add("failed", error="No answer from Groq", wav_bytes=b"x")
        else:
            store.add("pasted" if i % 2 else "copied", raw="x", cleaned=SAMPLES[i % len(SAMPLES)], cleanup="ok")
    clock[0] = now

    class TestPanel(HistoryPanel):
        def wheelEvent(self, event):
            before = self._scroll.end
            super().wheelEvent(event)
            focus = "FOCUS STOLEN!" if win32.foreground_window() == self._hwnd else "focus ok"
            print(f"[wheel] notch {event.angleDelta().y():+d}, scroll {before:.0f} -> {self._scroll.end:.0f}"
                  f" of {self._max_scroll():.0f} ({focus})")

    panel = TestPanel(store, None, on_retry=lambda entry_id: print(f"[retry] entry {entry_id} (test only)"),
                      debug=True, clock=lambda: clock[0])
    screen = QGuiApplication.primaryScreen().availableGeometry()
    panel.set_anchor(screen.center().x(), screen.bottom() - 12 - 34)  # where the hover pill's top would be
    panel.closed.connect(lambda: app.quit())
    panel.open_panel()
    print("History panel open. Click into Notepad, then hover the panel and scroll. Close it with its X.")

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C quits
    app.exec()
    store.close()


if __name__ == "__main__":
    main()
