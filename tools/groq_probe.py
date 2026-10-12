"""One-time check of Groq's rate limit headers.

Answers three questions with as few requests as possible:
1. Which x-ratelimit headers do transcription, chat and models.list return?
2. Does models.list() (the warm-up) count against the Whisper request limit?
3. Do the daily limits refill steadily, or reset at a fixed time?

Cost: 2 transcriptions (billed 10s each), 1 tiny cleanup call, 3 models.list calls.
Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\groq_probe.py
"""

import io
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.keystore import make_client  # noqa: E402
WHISPER = "whisper-large-v3-turbo"
CHAT = "openai/gpt-oss-20b"


def one_second_clip():
    """1s of a quiet tone, as WAV bytes (16kHz mono int16, same as the app)."""
    t = np.arange(16000) / 16000
    audio = (np.sin(2 * np.pi * 220 * t) * 1000).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())
    return buffer.getvalue()


def show(title, raw):
    """Print the rate limit headers (never the API key or other request data)."""
    print(f"\n== {title} (HTTP {raw.http_response.status_code})")
    found = False
    for name, value in sorted(raw.headers.items()):
        if name.startswith("x-ratelimit") or name in ("retry-after", "x-groq-region"):
            print(f"  {name}: {value}")
            found = True
    if not found:
        print("  (no rate limit headers)")
    return raw.headers


def main():
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY is missing from .env")
        return
    client = make_client(api_key, timeout=15)
    clip = one_second_clip()

    def transcribe(title):
        raw = client.audio.transcriptions.with_raw_response.create(file=("probe.wav", clip), model=WHISPER, language="en")
        headers = show(title, raw)
        print(f"  text: {raw.parse().text.strip()!r}")
        return headers

    a = transcribe("transcription A")
    for i in range(3):
        show(f"models.list {i + 1}", client.models.with_raw_response.list())
    b = transcribe("transcription B (right after)")

    raw = client.chat.completions.with_raw_response.create(
        model=CHAT,
        messages=[{"role": "user", "content": "Say ok."}],
        reasoning_effort="low",
        temperature=0.2,
    )
    show("cleanup (chat)", raw)
    usage = raw.parse().usage
    print(f"  usage: {usage}")

    print("\n== verdict")
    try:
        ra = int(a["x-ratelimit-remaining-requests"])
        rb = int(b["x-ratelimit-remaining-requests"])
        print(f"  whisper remaining-requests: A={ra}, B={rb}, dropped by {ra - rb}")
        if ra - rb == 1:
            print("  -> models.list does NOT count against the Whisper request limit")
        else:
            print("  -> dropped by more (or less) than 1: models.list may count, or the bucket refilled")
    except (KeyError, TypeError, ValueError):
        print("  whisper remaining-requests header missing, can't compare")


if __name__ == "__main__":
    main()
