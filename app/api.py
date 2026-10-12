"""Sends one Groq request with our own retry rules, and logs failures to the usage tracker.

The Groq client is created with max_retries=0, because the SDK's own retry
quietly sleeps up to 60 seconds on a rate limit. Our rules instead:
- Timeout, no connection, or a server error (5xx): retry once.
- Rate limit (429): retry once only if Groq asks us to wait `rate_limit_wait`
  seconds or less (5s for transcription, 0 for cleanup). Otherwise raise
  RateLimited right away so the pipeline can decide what to do.
"""

import json
import re
import time

import groq

from app import log
from app.config import CONFIG_PATH, clear_debug_setting
from app.usage import parse_duration, parse_rate_limit

RETRY_DELAY = 0.3  # seconds before retrying a timeout or server error
MAX_ERROR_FIELD = 40  # Groq's error type and code in the log: at most this long...
NOT_A_CODE = re.compile(r"[^A-Za-z0-9_.-]")  # ...and only these characters, so no free text gets through

# What debug_fake_rate_limit pretends Groq said. Real 429 messages look like this.
FAKE_MESSAGES = {
    "transcription": (
        "Rate limit reached for model `whisper-large-v3-turbo` on requests per minute (RPM): "
        "Limit 20, Used 20, Requested 1. Please try again in 30s."
    ),
    "transcription_short": (
        "Rate limit reached for model `whisper-large-v3-turbo` on requests per minute (RPM): "
        "Limit 20, Used 20, Requested 1. Please try again in 2s."
    ),
    "cleanup": (
        "Rate limit reached for model `openai/gpt-oss-20b` on requests per minute (RPM): "
        "Limit 30, Used 30, Requested 1. Please try again in 20s."
    ),
    # A daily limit used up (the "Groq limit used up" notification). No "Used N" on purpose:
    # with it, the fake would also move the real meter in usage.db for hours.
    "transcription_cap": (
        "Rate limit reached for model `whisper-large-v3-turbo` on audio seconds per day (ASD): "
        "Limit 28800. Please try again in 41m0s."
    ),
    "cleanup_cap": (
        "Rate limit reached for model `openai/gpt-oss-20b` on tokens per day (TPD): "
        "Limit 200000. Please try again in 41m0s."
    ),
}


class RateLimited(Exception):
    """Groq said "too many requests". info is parse_rate_limit()'s dict."""

    def __init__(self, kind, info):
        self.kind = kind
        self.info = info
        name = info["name"] or "unknown limit"
        wait = info["retry_in"]
        super().__init__(f"{kind} rate limited ({name})" + (f", try again in {wait:.0f}s" if wait else ""))


def request(tracker, kind, send, rate_limit_wait=0.0):
    """Calls send() (a Groq with_raw_response call). Returns (raw response, latency in ms).

    Raises RateLimited on a 429 we won't wait for, or the SDK's error for anything else.
    """
    retried = False
    while True:
        t0 = time.perf_counter()
        try:
            fake = _take_fake_rate_limit(kind)
            if fake:
                info = parse_rate_limit(fake)
                tracker.log_failure(kind, 0.0, 429, rate_limit=info)
                wait = info["retry_in"]
            else:
                return send(), _ms_since(t0)
        except groq.RateLimitError as e:
            info = parse_rate_limit(e.message)
            if info["retry_in"] is None:
                info["retry_in"] = parse_duration(e.response.headers.get("retry-after")) or _number(
                    e.response.headers.get("retry-after")
                )
            tracker.log_failure(kind, _ms_since(t0), 429, rate_limit=info, headers=e.response.headers)
            wait = info["retry_in"]
        except groq.APIStatusError as e:
            took_ms = _ms_since(t0)
            tracker.log_failure(kind, took_ms, e.status_code, headers=e.response.headers)
            if e.status_code < 500 or retried:
                raise
            print(f"{kind}: Groq server error {e.status_code} after {took_ms / 1000:.2f}s, retrying once")
            retried = True
            time.sleep(RETRY_DELAY)
            continue
        except groq.APIConnectionError:  # includes timeouts
            took_ms = _ms_since(t0)
            tracker.log_failure(kind, took_ms)
            if retried:
                raise
            print(f"{kind}: no answer from Groq after {took_ms / 1000:.2f}s, retrying once")
            retried = True
            time.sleep(RETRY_DELAY)
            continue

        # Only rate limits get here.
        if not retried and wait is not None and wait <= rate_limit_wait:
            print(f"{kind}: rate limited ({info['name']}), waiting {wait:.1f}s and retrying once")
            retried = True
            time.sleep(wait)
            continue
        raise RateLimited(kind, info)


def describe_for_log(error):
    """One line about a failed request, for the console and the log file.

    Never Groq's message or body: the SDK's own text is "Error code: 400 - {whole body}",
    and a body can quote what was sent. A real one (a 401):
        {'error': {'message': 'Invalid API Key', 'type': 'invalid_request_error', 'code': 'invalid_api_key'}}
    So only the status, and the type and code when they're short plain words:
        "Groq error 401 (invalid_request_error, invalid_api_key)"
    """
    if isinstance(error, RateLimited):
        return str(error)  # ours: only the limit's name and the wait
    if isinstance(error, groq.APIStatusError):
        fields = _error_fields(error.body)
        return f"Groq error {error.status_code}" + (f" ({', '.join(fields)})" if fields else "")
    if isinstance(error, groq.APIConnectionError):  # includes APITimeoutError
        return type(error).__name__
    if isinstance(error, groq.APIError):  # e.g. APIResponseValidationError, which has a body too
        return f"Groq error ({type(error).__name__})"
    return log.describe_exception(error)


def _error_fields(body):
    """The type and code from Groq's {"error": {...}}, cleaned; [] when there are none."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return []
    fields = []
    for key in ("type", "code"):
        value = error.get(key)
        if isinstance(value, str):
            value = NOT_A_CODE.sub("", value)[:MAX_ERROR_FIELD]
            if value:
                fields.append(value)
    return fields


def _ms_since(t0):
    return (time.perf_counter() - t0) * 1000


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _take_fake_rate_limit(kind):
    """For testing: config.json "debug_fake_rate_limit" makes the next request of that kind
    act like Groq sent a 429, without sending anything. It fires once, then switches
    itself off by writing null back into config.json.

    Values: "transcription" (30s wait), "transcription_short" (2s wait, so the
    automatic retry runs and the real request goes through), "cleanup", and
    "transcription_cap" / "cleanup_cap" (a daily limit used up, 41m wait).
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        value = json.loads(text).get("debug_fake_rate_limit")
    except (OSError, ValueError):
        return None
    if value not in FAKE_MESSAGES or not value.startswith(kind):
        return None
    try:
        clear_debug_setting("debug_fake_rate_limit", CONFIG_PATH)
    except (OSError, ValueError) as e:
        print(f"couldn't set debug_fake_rate_limit back to null: {e}")
    print(f'*** FAKE RATE LIMIT fired for {kind} ("{value}"). debug_fake_rate_limit is now null again. ***')
    return FAKE_MESSAGES[value]
