"""Looks at and retries History from the console (before the History panel exists, and for debugging).

Quit the dictation app first: this uses the same history.db and usage.db.
Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\history_tool.py list
    .\\.venv\\Scripts\\python.exe tools\\history_tool.py retry 12
    .\\.venv\\Scripts\\python.exe tools\\history_tool.py clear

retry sends the entry's saved audio to Groq again (costs one transcription and
one cleanup). The result goes to the clipboard, just like Retry in the panel.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from app.config import load_config  # noqa: E402
from app.history import HistoryStore, entry_text  # noqa: E402
from app.keystore import make_client  # noqa: E402
from app.paths import ENV_PATH  # noqa: E402
from app.paster import read_text  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.usage import UsageTracker  # noqa: E402
from main import API_TIMEOUT_SECONDS, USAGE_DB, claim_single_instance, usage_limits  # noqa: E402


def show(store):
    entries = store.entries()
    if not entries:
        print("History is empty.")
        return
    for e in reversed(entries):  # oldest first, so the newest ends up next to the prompt
        when = datetime.fromtimestamp(e["ts"]).strftime("%a %H:%M:%S")
        tags = []
        if e["cleanup"] in ("limited", "failed"):
            tags.append("not cleaned up")
        if e["retried_ts"]:
            tags.append("retried")
        if e["can_retry"]:
            tags.append("audio kept, can retry")
        text = entry_text(e) or f"({e['error'] or 'no text'})"
        if len(text) > 70:
            text = text[:69] + "…"
        extra = f" [{', '.join(tags)}]" if tags else ""
        print(f"{e['id']:>4}  {when}  {e['status']:<8} {text}{extra}")
        if e["error"] and entry_text(e):
            print(f"{'':>6}{'':<14}  {e['error']}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "retry", "clear"):
        print(__doc__)
        return
    if not claim_single_instance():
        print("The dictation app is running. Quit it first (Ctrl+C in its console).")
        return
    store = HistoryStore()
    try:
        command = sys.argv[1]
        if command == "list":
            show(store)
        elif command == "clear":
            store.clear()
            print("History cleared (text and saved audio erased).")
        else:
            if len(sys.argv) < 3 or not sys.argv[2].isdigit():
                print("Which entry? For example: history_tool.py retry 12")
                return
            entry_id = int(sys.argv[2])
            if store.audio_path(entry_id) is None:
                print(f"Entry {entry_id} has no saved audio to retry (see: history_tool.py list).")
                return
            load_dotenv(ENV_PATH)
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                print(f"GROQ_API_KEY is missing. Add it to {ENV_PATH}.")
                return
            config = load_config()
            tracker = UsageTracker(USAGE_DB, usage_limits(config))
            try:
                client = make_client(api_key, timeout=API_TIMEOUT_SECONDS)
                pipeline = Pipeline(client, None, config, tracker, history=store)
                pipeline._run_retry(entry_id, time.perf_counter())
            finally:
                tracker.close()
            clipboard = read_text()
            print(f"clipboard now holds: {clipboard!r}" if clipboard is not None else "clipboard now holds no text")
            print()
            show(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
