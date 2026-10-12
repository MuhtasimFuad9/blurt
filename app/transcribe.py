"""Speech to text with Groq Whisper (same call as test_record.py)."""

import re
import threading

import groq

from app import api
from app.dictionary import whisper_terms

MODEL = "whisper-large-v3-turbo"
RATE_LIMIT_WAIT = 5.0  # on a 429, wait and retry once if Groq asks for this many seconds or less

_warm_lock = threading.Lock()


def warm_up(client, tracker=None):
    """Gets the connection to Groq ready, so the next real request is fast.

    The first request pays for loading the SDK's code and opening a secure
    connection (about 1.3s extra). Idle connections close after 5s, so this runs
    at startup and on every Ctrl+Win press, while you're still speaking.
    models.list() doesn't count against any limit (checked with tools/groq_probe.py).
    Runs on its own short-lived thread. Failures don't matter here: the real
    request reports any problem. It can tell the Usage panel about two problems,
    though: no connection at all, or a rejected API key.
    """
    if not _warm_lock.acquire(blocking=False):
        return  # one is already on its way (e.g. the network is slow)
    problem = None
    try:
        client.audio.transcriptions  # loads the SDK code the real calls need
        client.chat.completions
        client.models.list()  # a small request that opens the connection
    except (groq.AuthenticationError, groq.PermissionDeniedError):
        problem = "key_rejected"  # 401 or 403
    except groq.APIStatusError:
        pass  # Groq answered, just not happily; the real request will tell
    except groq.APIConnectionError:
        problem = "unreachable"
    except Exception:
        pass
    finally:
        _warm_lock.release()
    if tracker is not None:
        tracker.log_warm_up(problem)


ECHO_MAX_EXTRA_WORDS = 2  # an in-order echo may have this many other words left over...
ECHO_MAX_EXTRA_LENGTH = 2  # ...each at most this many characters ("I", "a", "to")


def _split_terms(text, terms):
    """Returns (the dictionary terms found, in order, each as its position in terms; the other words)."""
    if not terms:
        return [], re.findall(r"\w+", text)
    # Longest terms first, so "Claude Code" matches before a shorter overlapping term.
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b"
    position = {t.lower(): i for i, t in enumerate(terms)}
    found = [position[m.group().lower()] for m in re.finditer(pattern, text, flags=re.IGNORECASE)]
    return found, re.findall(r"\w+", re.sub(pattern, " ", text, flags=re.IGNORECASE))


def is_prompt_echo(text, terms):
    """True if Whisper just repeated the vocabulary hint back.

    Whisper does this on noise with no speech in it. Two ways to catch it:
    - Two or more terms and nothing else (a pure tone came back as
      "Blender, Groq, n8n, Claude Code, WisprFlow"). A single term like "Blender"
      still works.
    - Three or more terms in dictionary.txt order, with at most 2 short extra
      words (a silent hold came back as "I, Groq, n8n, Claude, WisprFlow").
      A real sentence like "I use Groq, n8n and Claude" has longer words left over.
    """
    found, extra = _split_terms(text, terms)
    if len(found) >= 2 and not extra:
        return True
    in_order = all(a < b for a, b in zip(found, found[1:]))
    few_short_extras = len(extra) <= ECHO_MAX_EXTRA_WORDS and all(len(w) <= ECHO_MAX_EXTRA_LENGTH for w in extra)
    return len(found) >= 3 and in_order and few_short_extras


def is_dictionary_only(text, terms):
    """True if text is made of dictionary terms and nothing else (punctuation ignored).

    On near-silent audio Whisper tends to guess words from its vocabulary hint (a
    silent hold once came back as a dictionary term spelled out letter by letter,
    which cleanup turned into the term itself). The pipeline only discards these
    when the audio also had very little speech in it.
    """
    found, extra = _split_terms(text, terms)
    return len(found) >= 1 and not extra


def transcribe(client, tracker, wav_bytes, clip_seconds, terms, language):
    """Returns (raw transcript, billed audio seconds).

    Raises api.RateLimited on a rate limit, or the SDK's error on anything else.
    language is a code like "en", or "auto" to let Whisper detect it.
    """
    options = {}
    if terms:
        # Vocabulary hint, e.g. "Blender, Groq". Kept under Whisper's prompt limit
        # by leaving out the oldest terms; cleanup and the guards still use all of them.
        hint = whisper_terms(terms)
        if len(hint) < len(terms):
            print(f"dictionary: the {len(terms) - len(hint)} oldest terms don't fit in Whisper's hint")
        options["prompt"] = ", ".join(hint)
    if language and language != "auto":
        options["language"] = language

    def send():
        return client.audio.transcriptions.with_raw_response.create(
            file=("recording.wav", wav_bytes),
            model=MODEL,
            **options,
        )

    raw, latency_ms = api.request(tracker, "transcription", send, rate_limit_wait=RATE_LIMIT_WAIT)
    result = raw.parse()
    billed = tracker.log_transcription(latency_ms, clip_seconds, raw.headers)
    return result.text.strip(), billed
