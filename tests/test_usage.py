"""Tests for usage tracking and the retry rules. No network, no real clock.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_usage -v
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import groq
import httpx

from app import api
from app.config import clear_debug_setting
from app.usage import (
    DAY,
    HOUR,
    METER_LABELS,
    CAP_MIN_WAIT,
    Bucket,
    CapNotices,
    LimitWarnings,
    UsageTracker,
    billed_seconds,
    cap_message,
    format_ago,
    format_amount,
    format_wait,
    limit_warning_message,
    meter_style,
    parse_duration,
    parse_rate_limit,
    with_fake_usage,
)

LIMITS = {
    "audio_seconds_per_day": 28800,
    "audio_seconds_per_hour": 7200,
    "transcribe_requests_per_day": 2000,
    "cleanup_requests_per_day": 1000,
    "cleanup_tokens_per_day": 200000,
    "min_billed_audio_seconds": 10,
}

# 2026-09-28 00:00 in a test time zone at UTC+6, as a Unix time
TEST_ZONE_MIDNIGHT = 1790532000.0


def day_start_in_test_zone(now):
    return now - (now + 6 * HOUR) % DAY


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def rate_limit_message(code, name, limit, used, requested, wait):
    return (
        f"Rate limit reached for model `m` in organization `org_x` service tier `on_demand` on {name} ({code}): "
        f"Limit {limit}, Used {used}, Requested {requested}. Please try again in {wait}. "
        "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
    )


class TrackerTest(unittest.TestCase):
    """Each test gets a fresh usage.db in a temporary folder."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "usage.db"
        self.clock = FakeClock(TEST_ZONE_MIDNIGHT + 10 * HOUR)
        self.trackers = []

    def tearDown(self):
        for tracker in self.trackers:
            tracker.close()
        self._dir.cleanup()

    def tracker(self):
        tracker = UsageTracker(self.db_path, LIMITS, clock=self.clock, day_start=day_start_in_test_zone)
        self.trackers.append(tracker)
        return tracker

    def used(self, tracker, meter):
        return tracker.snapshot()["meters"][meter]["used"]


class BucketTest(unittest.TestCase):
    def test_daily_bucket_half_full_is_empty_after_12_hours(self):
        bucket = Bucket(28800, DAY)
        bucket.add(14400, now=0)
        self.assertAlmostEqual(bucket.level(6 * HOUR), 7200)
        self.assertAlmostEqual(bucket.level(12 * HOUR), 0)
        self.assertEqual(bucket.level(24 * HOUR), 0)  # never below 0

    def test_hourly_bucket_half_full_is_empty_after_30_minutes(self):
        bucket = Bucket(7200, HOUR)
        bucket.add(3600, now=0)
        self.assertAlmostEqual(bucket.level(15 * 60), 1800)
        self.assertAlmostEqual(bucket.level(30 * 60), 0)

    def test_hour_mark_is_not_a_reset(self):
        # Used at 10:59:30, checked at 11:00:30: drained 60s worth, not wiped.
        bucket = Bucket(7200, HOUR)
        start = TEST_ZONE_MIDNIGHT + 11 * HOUR - 30
        bucket.add(1000, now=start)
        self.assertAlmostEqual(bucket.level(start + 60), 1000 - 60 * 2)

    def test_refilled_in(self):
        bucket = Bucket(2000, DAY)
        bucket.add(1, now=0)
        self.assertAlmostEqual(bucket.refilled_in(0), 43.2)  # matches the probe's reset-requests header

    def test_clock_going_backwards_drains_nothing(self):
        bucket = Bucket(100, HOUR)
        bucket.add(50, now=1000)
        self.assertEqual(bucket.level(900), 50)

    def test_real_429_replay(self):
        # Groq said: TPD Limit 200000, Used 198823, Requested 3126, try again in 14m1.968s.
        info = parse_rate_limit(rate_limit_message("TPD", "tokens per day", 200000, 198823, 3126, "14m1.968s"))
        bucket = Bucket(200000, DAY)
        bucket.snap(info["used"], now=0)
        wait = bucket.wait_for(info["requested"], now=0)
        self.assertAlmostEqual(wait, info["retry_in"], delta=1.0)
        self.assertAlmostEqual(info["retry_in"], 14 * 60 + 1.968)


class ParsingTest(unittest.TestCase):
    def test_durations(self):
        self.assertAlmostEqual(parse_duration("2m59.56s"), 179.56)
        self.assertAlmostEqual(parse_duration("7.66s"), 7.66)
        self.assertAlmostEqual(parse_duration("14m1.968s"), 841.968)
        self.assertAlmostEqual(parse_duration("1h2m3s"), 3723)
        self.assertAlmostEqual(parse_duration("43.2s"), 43.2)
        self.assertAlmostEqual(parse_duration("250ms"), 0.25)
        self.assertIsNone(parse_duration(""))
        self.assertIsNone(parse_duration(None))
        self.assertIsNone(parse_duration("soon"))
        self.assertIsNone(parse_duration("5s later"))

    def test_rate_limit_messages(self):
        cases = [
            ("RPD", "requests per day", 2000, 2000, 1, "43.2s", 43.2),
            ("TPD", "tokens per day", 200000, 198823, 3126, "14m1.968s", 841.968),
            ("TPM", "tokens per minute", 8000, 7900, 400, "2.91s", 2.91),
            ("ASH", "audio seconds per hour", 7200, 7195, 12, "3.5s", 3.5),
            ("ASD", "audio seconds per day", 28800, 28790, 10, "1m12s", 72),
        ]
        for code, name, limit, used, requested, wait, seconds in cases:
            with self.subTest(code):
                info = parse_rate_limit(rate_limit_message(code, name, limit, used, requested, wait))
                self.assertEqual(info["type"], code)
                self.assertEqual(info["name"], name)
                self.assertEqual((info["limit"], info["used"], info["requested"]), (limit, used, requested))
                self.assertAlmostEqual(info["retry_in"], seconds)

    def test_unknown_message_gives_empty_fields(self):
        info = parse_rate_limit("Too many requests")
        self.assertIsNone(info["type"])
        self.assertIsNone(info["retry_in"])


class BilledSecondsTest(TrackerTest):
    def test_minimum(self):
        self.assertEqual(billed_seconds(3, 10), 10)
        self.assertAlmostEqual(billed_seconds(14.2, 10), 14.2)

    def test_short_clip_fills_both_audio_meters_with_10s(self):
        tracker = self.tracker()
        self.assertEqual(tracker.log_transcription(500, clip_seconds=3), 10)
        self.assertAlmostEqual(self.used(tracker, "audio_day"), 10)
        self.assertAlmostEqual(self.used(tracker, "audio_hour"), 10)
        self.assertAlmostEqual(self.used(tracker, "transcriptions"), 1)


class HeaderSnapTest(TrackerTest):
    def test_snaps_from_reset_requests(self):
        tracker = self.tracker()
        headers = {
            "x-ratelimit-limit-requests": "2000",
            "x-ratelimit-remaining-requests": "1995",
            "x-ratelimit-reset-requests": "3m36s",  # 216s x 2000 / 86400 = 5 used
        }
        tracker.log_transcription(500, 3, headers)
        self.assertAlmostEqual(self.used(tracker, "transcriptions"), 5)

    def test_falls_back_to_remaining(self):
        tracker = self.tracker()
        tracker.log_transcription(500, 3, {"x-ratelimit-limit-requests": "2000", "x-ratelimit-remaining-requests": "1990"})
        self.assertAlmostEqual(self.used(tracker, "transcriptions"), 10)

    def test_cleanup_headers_snap_the_cleanup_meter_only(self):
        tracker = self.tracker()
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
        tracker.log_cleanup(300, usage, {"x-ratelimit-limit-requests": "1000", "x-ratelimit-reset-requests": "4m19.2s"})
        self.assertAlmostEqual(self.used(tracker, "cleanups"), 3)
        self.assertAlmostEqual(self.used(tracker, "transcriptions"), 0)
        self.assertAlmostEqual(self.used(tracker, "cleanup_tokens"), 120)


class CleanupTokensTest(TrackerTest):
    def test_token_columns_and_cached_tokens(self):
        tracker = self.tracker()
        usage = SimpleNamespace(
            prompt_tokens=74,
            completion_tokens=22,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
            prompt_tokens_details=SimpleNamespace(cached_tokens=50),
        )
        self.assertEqual(tracker.log_cleanup(300, usage), 46)  # 74 + 22 - 50 cached
        row = tracker._db.execute(
            "SELECT prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens FROM requests"
        ).fetchone()
        self.assertEqual(row, (74, 22, 12, 50))

    def test_missing_details_are_stored_empty(self):
        tracker = self.tracker()
        tracker.log_cleanup(300, SimpleNamespace(prompt_tokens=74, completion_tokens=22, prompt_tokens_details=None))
        row = tracker._db.execute("SELECT reasoning_tokens, cached_tokens FROM requests").fetchone()
        self.assertEqual(row, (None, None))


class RateLimitTrackingTest(TrackerTest):
    def test_429_snaps_meter_and_shows_banner_countdown(self):
        tracker = self.tracker()
        info = parse_rate_limit(rate_limit_message("TPD", "tokens per day", 200000, 198823, 3126, "14m1.968s"))
        tracker.log_failure("cleanup", 200, 429, rate_limit=info)
        snap = tracker.snapshot()
        self.assertAlmostEqual(snap["meters"]["cleanup_tokens"]["used"], 198823)
        self.assertEqual(snap["rate_limit"]["name"], "tokens per day")
        self.assertAlmostEqual(snap["rate_limit"]["retry_in"], 841.968, places=3)
        self.assertEqual(snap["today"]["rate_limits"], 1)
        self.assertEqual(snap["connection"], "connected")  # Groq answered, even if with a 429
        self.clock.advance(842)
        self.assertIsNone(tracker.snapshot()["rate_limit"])

    def test_per_minute_429_changes_no_meter(self):
        tracker = self.tracker()
        info = parse_rate_limit(rate_limit_message("RPM", "requests per minute", 20, 20, 1, "3s"))
        tracker.log_failure("transcription", 200, 429, rate_limit=info)
        self.assertEqual(self.used(tracker, "transcriptions"), 0)


class ConnectionStatusTest(TrackerTest):
    def status(self, tracker):
        return tracker.snapshot()["connection"]

    def test_only_real_requests_say_connected(self):
        tracker = self.tracker()
        tracker.log_warm_up(None)
        self.assertIsNone(self.status(tracker))  # a fine warm-up alone isn't "connected"
        tracker.log_transcription(500, 3)
        self.assertEqual(self.status(tracker), "connected")

    def test_no_answer_means_unreachable(self):
        tracker = self.tracker()
        tracker.log_transcription(500, 3)
        tracker.log_failure("transcription", 8000)
        self.assertEqual(self.status(tracker), "unreachable")
        tracker.log_warm_up(None)
        self.assertEqual(self.status(tracker), "unreachable")  # still the last real request's answer

    def test_warm_up_problems_show_until_something_newer(self):
        tracker = self.tracker()
        tracker.log_transcription(500, 3)
        tracker.log_warm_up("unreachable")
        self.assertEqual(self.status(tracker), "unreachable")
        tracker.log_warm_up(None)  # connection is back
        self.assertEqual(self.status(tracker), "connected")
        tracker.log_warm_up("key_rejected")
        self.assertEqual(self.status(tracker), "key_rejected")
        tracker.log_transcription(500, 3)  # a real request that worked is newer
        self.assertEqual(self.status(tracker), "connected")

    def test_real_401_means_key_rejected(self):
        tracker = self.tracker()
        tracker.log_failure("cleanup", 100, 401)
        self.assertEqual(self.status(tracker), "key_rejected")

    def test_updated_ago_follows_real_requests_only(self):
        tracker = self.tracker()
        tracker.log_warm_up(None)
        self.assertIsNone(tracker.snapshot()["last_response_ago"])
        tracker.log_transcription(500, 3)
        self.clock.advance(30)
        self.assertAlmostEqual(tracker.snapshot()["last_response_ago"], 30)


class FormatTest(unittest.TestCase):
    def test_wait(self):
        self.assertEqual(format_wait(0.2), "1s")  # rounded up, never "0s" while still waiting
        self.assertEqual(format_wait(12), "12s")
        self.assertEqual(format_wait(842), "15m")
        self.assertEqual(format_wait(125), "2m 5s")
        self.assertEqual(format_wait(3599), "1h")
        self.assertEqual(format_wait(6 * HOUR + 20 * 60), "6h 20m")

    def test_ago(self):
        self.assertEqual(format_ago(3), "just now")
        self.assertEqual(format_ago(12), "12s ago")
        self.assertEqual(format_ago(125), "2 min ago")
        self.assertEqual(format_ago(7300), "2h ago")

    def test_amounts(self):
        self.assertEqual(format_amount("audio_day", 18 * 60, 28800), "18 of 480 min")
        self.assertEqual(format_amount("audio_hour", 30, 7200), "0.5 of 120 min")
        self.assertEqual(format_amount("transcriptions", 142.6, 2000), "143 of 2,000")
        self.assertEqual(format_amount("cleanup_tokens", 128000, 200000), "128K of 200K")
        self.assertEqual(format_amount("cleanup_tokens", 4700, 200000), "4.7K of 200K")
        self.assertEqual(format_amount("cleanup_tokens", 467, 200000), "467 of 200K")


class TodayTest(TrackerTest):
    def test_today_tiles_reset_at_local_midnight_but_meters_dont(self):
        self.clock.t = TEST_ZONE_MIDNIGHT + DAY - 60  # 23:59 local time in that zone
        tracker = self.tracker()
        tracker.log_transcription(500, 600)
        tracker.log_dictation("done", "ok", "ok", 600, 100, words=5, latency_ms=900)
        tracker.log_dictation("nothing", "ok", "skipped", 10, 0, words=0, latency_ms=400)  # not counted
        today = tracker.snapshot()["today"]
        self.assertEqual((today["dictations"], today["words"], today["avg_latency_ms"]), (1, 5, 900))

        self.clock.advance(120)  # 00:01 the next day
        snap = tracker.snapshot()
        self.assertEqual(snap["today"]["dictations"], 0)
        self.assertIsNone(snap["today"]["avg_latency_ms"])
        # Still there, only drained by 2 minutes' worth (1/3 s per s).
        self.assertAlmostEqual(snap["meters"]["audio_day"]["used"], 600 - 40)


class PersistenceTest(TrackerTest):
    def test_restart_keeps_numbers_and_drains_the_gap(self):
        tracker = self.tracker()
        tracker.log_transcription(500, clip_seconds=3600)
        tracker.log_dictation("done", "ok", "ok", 3600, 100, words=7, latency_ms=1000)
        tracker.close()

        self.clock.advance(2 * HOUR)
        tracker = self.tracker()
        snap = tracker.snapshot()
        self.assertAlmostEqual(snap["meters"]["audio_hour"]["used"], 0)  # 3600 drains in 30 min
        self.assertAlmostEqual(snap["meters"]["audio_day"]["used"], 3600 - 2 * HOUR / 3)  # 1/3 s per s
        self.assertEqual(snap["today"]["dictations"], 1)
        self.assertEqual(snap["today"]["words"], 7)

    def test_pruning_keeps_30_days(self):
        tracker = self.tracker()
        tracker.log_dictation("done", "ok", "ok", 10, 0, words=1, latency_ms=1)  # day 0
        self.clock.advance(2 * DAY)
        tracker.log_dictation("done", "ok", "ok", 10, 0, words=1, latency_ms=1)  # day 2
        self.clock.advance(29 * DAY)
        tracker.log_dictation("done", "ok", "ok", 10, 0, words=1, latency_ms=1)  # day 31: prunes day 0
        (count,) = tracker._db.execute("SELECT COUNT(*) FROM dictations").fetchone()
        self.assertEqual(count, 2)


class MeterStyleTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(meter_style(59, 100), (59, "normal", "59%"))
        self.assertEqual(meter_style(59.9, 100), (59, "normal", "59%"))
        self.assertEqual(meter_style(60, 100), (60, "close", "60%, getting close"))
        self.assertEqual(meter_style(79, 100), (79, "close", "79%, getting close"))
        self.assertEqual(meter_style(80, 100), (80, "out", "80%, almost out"))
        self.assertEqual(meter_style(100, 100), (100, "out", "100%, almost out"))

    def test_edges(self):
        self.assertEqual(meter_style(29, 100)[0], 29)  # 0.29 x 100 is 28.999... in floating point
        self.assertEqual(meter_style(0, 100), (0, "normal", "0%"))
        self.assertEqual(meter_style(250, 100)[0], 100)  # over the limit shows 100
        self.assertEqual(meter_style(1200, 2000)[1], "close")  # works on any limit, not just 100


class RetryRulesTest(unittest.TestCase):
    """api.request with fake Groq responses (nothing is sent)."""

    def setUp(self):
        self.tracker = mock.Mock()
        self.request = httpx.Request("POST", "https://api.groq.com/openai/v1/x")
        sleep = mock.patch("app.api.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        no_fake = mock.patch("app.api._take_fake_rate_limit", return_value=None)
        no_fake.start()
        self.addCleanup(no_fake.stop)

    def rate_limit_error(self, wait):
        message = rate_limit_message("RPM", "requests per minute", 20, 20, 1, wait)
        return groq.RateLimitError(message, response=httpx.Response(429, request=self.request), body=None)

    def server_error(self):
        return groq.InternalServerError("oops", response=httpx.Response(503, request=self.request), body=None)

    def sender(self, *outcomes):
        """A fake send(): raises or returns each outcome in turn."""
        outcomes = list(outcomes)

        def send():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return send

    def test_short_rate_limit_waits_and_retries_once(self):
        raw, _ = api.request(self.tracker, "transcription", self.sender(self.rate_limit_error("2s"), "ok"), 5.0)
        self.assertEqual(raw, "ok")
        self.sleep.assert_called_once_with(2.0)

    def test_long_rate_limit_raises_right_away(self):
        with self.assertRaises(api.RateLimited) as caught:
            api.request(self.tracker, "transcription", self.sender(self.rate_limit_error("30s")), 5.0)
        self.assertEqual(caught.exception.info["type"], "RPM")
        self.sleep.assert_not_called()

    def test_second_rate_limit_is_not_retried(self):
        send = self.sender(self.rate_limit_error("2s"), self.rate_limit_error("2s"))
        with self.assertRaises(api.RateLimited):
            api.request(self.tracker, "transcription", send, 5.0)

    def test_cleanup_never_waits_for_a_rate_limit(self):
        with self.assertRaises(api.RateLimited):
            api.request(self.tracker, "cleanup", self.sender(self.rate_limit_error("1s")))

    def test_server_error_retries_once(self):
        raw, _ = api.request(self.tracker, "cleanup", self.sender(self.server_error(), "ok"))
        self.assertEqual(raw, "ok")
        with self.assertRaises(groq.InternalServerError):
            api.request(self.tracker, "cleanup", self.sender(self.server_error(), self.server_error()))

    def test_timeout_retries_once(self):
        raw, _ = api.request(self.tracker, "cleanup", self.sender(groq.APITimeoutError(self.request), "ok"))
        self.assertEqual(raw, "ok")

    def test_a_retry_says_how_long_the_failed_try_took(self):
        # M19: tells "Groq answered nothing for 8 seconds" from "no internet, failed at once".
        for error, line in [
            (groq.APITimeoutError(self.request), "transcription: no answer from Groq after 8.01s, retrying once"),
            (self.server_error(), "transcription: Groq server error 503 after 8.01s, retrying once"),
        ]:
            with self.subTest(line=line):
                # The clock at: the first try's start, its failure, the retry's start, its answer.
                with mock.patch("app.api.time.perf_counter", side_effect=[100.0, 108.01, 200.0, 200.5]), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    raw, _ = api.request(self.tracker, "transcription", self.sender(error, "ok"))
                self.assertEqual(raw, "ok")
                self.assertEqual(out.getvalue(), line + "\n")
                self.assertAlmostEqual(self.tracker.log_failure.call_args.args[1], 8010)  # the time usage.db gets

    def test_bad_request_is_not_retried(self):
        error = groq.BadRequestError("bad", response=httpx.Response(400, request=self.request), body=None)
        with self.assertRaises(groq.BadRequestError):
            api.request(self.tracker, "cleanup", self.sender(error, "ok"))


class FakeRateLimitTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config = Path(self._dir.name) / "config.json"
        patch = mock.patch("app.api.CONFIG_PATH", self.config)
        patch.start()
        self.addCleanup(patch.stop)

    def test_fires_once_and_switches_itself_off(self):
        self.config.write_text('{\n  "language": "en",\n  "debug_fake_rate_limit": "cleanup",\n  "x": 1\n}\n')
        self.assertIsNone(api._take_fake_rate_limit("transcription"))  # wrong kind: stays armed
        self.assertIn("(RPM)", api._take_fake_rate_limit("cleanup"))
        self.assertEqual(
            self.config.read_text(), '{\n  "language": "en",\n  "debug_fake_rate_limit": null,\n  "x": 1\n}\n'
        )
        self.assertIsNone(api._take_fake_rate_limit("cleanup"))

    def test_fake_goes_through_the_same_rules(self):
        self.config.write_text('{"debug_fake_rate_limit": "transcription_short"}')
        tracker = mock.Mock()
        with mock.patch("app.api.time.sleep") as sleep:
            raw, _ = api.request(tracker, "transcription", lambda: "real", 5.0)
        self.assertEqual(raw, "real")  # waited 2s, then the real request went through
        sleep.assert_called_once_with(2.0)
        self.assertEqual(tracker.log_failure.call_args.kwargs["rate_limit"]["type"], "RPM")


def meters_at(**percents):
    """A snapshot()["meters"] dict with the given meters that full (the rest empty)."""
    limits = {"audio_day": 28800, "audio_hour": 7200, "transcriptions": 2000, "cleanups": 1000,
              "cleanup_tokens": 200000}
    return {
        name: {"used": limit * percents.get(name, 0) / 100, "limit": limit, "refilled_in": 3600.0}
        for name, limit in limits.items()
    }


class LimitWarningsTest(unittest.TestCase):
    def setUp(self):
        self.warnings = LimitWarnings()
        self.assertEqual(self.warnings.check(meters_at()), [])  # the startup check

    def test_fires_once_when_crossing_80(self):
        self.assertEqual(self.warnings.check(meters_at(cleanup_tokens=79)), [])
        self.assertEqual(self.warnings.check(meters_at(cleanup_tokens=80)), ["cleanup_tokens"])
        self.assertEqual(self.warnings.check(meters_at(cleanup_tokens=84)), [])

    def test_wobbling_around_80_never_fires_twice(self):
        self.warnings.check(meters_at(cleanups=81))
        for percent in (79, 80, 75, 85, 70, 82):
            self.assertEqual(self.warnings.check(meters_at(cleanups=percent)), [], percent)

    def test_rearms_below_70(self):
        self.warnings.check(meters_at(audio_hour=90))
        self.assertEqual(self.warnings.check(meters_at(audio_hour=69)), [])
        self.assertEqual(self.warnings.check(meters_at(audio_hour=80)), ["audio_hour"])

    def test_several_meters_at_once_in_panel_order(self):
        crossed = self.warnings.check(meters_at(cleanup_tokens=95, audio_day=88, transcriptions=10))
        self.assertEqual(crossed, ["audio_day", "cleanup_tokens"])

    def test_uses_the_panels_rounded_down_percent(self):
        # 79.99% shows as "79%" in the panel, so it isn't "almost out" yet.
        self.assertEqual(self.warnings.check(meters_at(cleanups=79.99)), [])
        self.assertEqual(self.warnings.check(meters_at(cleanups=80.0)), ["cleanups"])

    def test_already_high_at_startup_stays_quiet(self):
        warnings = LimitWarnings()
        self.assertEqual(warnings.check(meters_at(cleanup_tokens=90)), [])  # just started
        self.assertEqual(warnings.check(meters_at(cleanup_tokens=91)), [])
        warnings.check(meters_at(cleanup_tokens=60))  # drained below 70: armed again
        self.assertEqual(warnings.check(meters_at(cleanup_tokens=80)), ["cleanup_tokens"])

    def test_a_real_bucket_draining(self):
        bucket = Bucket(200000, DAY)
        bucket.add(162000, 0)  # 81%
        snap = lambda now: {"cleanup_tokens": {"used": bucket.level(now), "limit": 200000, "refilled_in": 0}}
        self.assertEqual(self.warnings.check(snap(0)), ["cleanup_tokens"])
        self.assertEqual(self.warnings.check(snap(2 * HOUR)), [])  # about 73%: still warned
        self.assertEqual(self.warnings.check(snap(3 * HOUR)), [])  # about 68.5%: armed again
        bucket.add(24000, 3 * HOUR)  # back up to about 80.5%
        self.assertEqual(self.warnings.check(snap(3 * HOUR)), ["cleanup_tokens"])


class LimitWarningMessageTest(unittest.TestCase):
    def test_one_meter(self):
        meters = meters_at(cleanup_tokens=82)
        meters["cleanup_tokens"]["refilled_in"] = 6 * HOUR + 20 * 60
        title, text = limit_warning_message(["cleanup_tokens"], meters)
        self.assertEqual(title, "Groq limit almost used up")
        self.assertEqual(text, "Cleanup tokens, daily limit: 82%. Fully refilled in 6h 20m.")

    def test_two_meters(self):
        title, text = limit_warning_message(["audio_day", "cleanups"], meters_at(audio_day=80, cleanups=91))
        self.assertEqual(title, "2 Groq limits almost used up")
        self.assertEqual(text.splitlines()[1], "Cleanup requests, daily limit: 91%. Fully refilled in 1h.")

    def test_fake_usage_leaves_the_real_numbers_alone(self):
        real = meters_at(cleanup_tokens=5)
        fake = with_fake_usage(real, "audio_hour")
        self.assertEqual(real["audio_hour"]["used"], 0)
        self.assertEqual(meter_style(fake["audio_hour"]["used"], fake["audio_hour"]["limit"])[0], 85)
        self.assertEqual(fake["audio_hour"]["refilled_in"], 0.85 * HOUR)
        warnings = LimitWarnings()
        warnings.check(real)
        self.assertEqual(warnings.check(fake), ["audio_hour"])  # goes through the real rules

    def test_panel_uses_the_same_labels(self):
        from app.ui import panel

        self.assertEqual(dict(panel.METERS), METER_LABELS)


class ClearDebugSettingTest(unittest.TestCase):
    def test_only_that_value_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{\n  "debug_fake_limit_warning": "cleanup_tokens",\n  "debug_x": "keep"\n}\n')
            clear_debug_setting("debug_fake_limit_warning", path)
            self.assertEqual(path.read_text(), '{\n  "debug_fake_limit_warning": null,\n  "debug_x": "keep"\n}\n')


class CapNoticesTest(TrackerTest):
    """The "Groq limit used up" notification: once per hit limit, only for waits of 60s or more."""

    def setUp(self):
        super().setUp()
        self.usage = self.tracker()
        self.notices = CapNotices()

    def hit(self, kind, code, name, wait):
        info = parse_rate_limit(rate_limit_message(code, name, 1, 1, 1, wait))
        self.usage.log_failure(kind, 200, 429, rate_limit=info)

    def check(self):
        return self.notices.check(self.usage.snapshot()["rate_limit"])

    def test_the_snapshot_keeps_what_groq_asked_for(self):
        self.hit("cleanup", "TPD", "tokens per day", "14m1.968s")
        self.clock.advance(100)
        rate_limit = self.usage.snapshot()["rate_limit"]
        self.assertAlmostEqual(rate_limit["wait"], 841.968, places=3)
        self.assertAlmostEqual(rate_limit["retry_in"], 741.968, places=3)

    def test_a_long_wait_is_reported_once_per_limit(self):
        self.hit("transcription", "ASD", "audio seconds per day", "41m")
        self.assertEqual(self.check()["type"], "ASD")
        self.clock.advance(2)
        self.assertIsNone(self.check())  # the next 2s poll: same limit, already said
        self.clock.advance(60)
        self.hit("transcription", "ASD", "audio seconds per day", "40m")  # a later dictation hits it again
        self.assertIsNone(self.check())
        self.clock.advance(41 * 60)  # it refilled...
        self.assertIsNone(self.check())
        self.hit("transcription", "ASD", "audio seconds per day", "5m")  # ...and was used up again: a new hit
        self.assertEqual(self.check()["type"], "ASD")

    def test_waits_under_a_minute_stay_quiet(self):
        self.assertEqual(CAP_MIN_WAIT, 60)
        for wait in ("2s", "30s", "59s"):
            with self.subTest(wait=wait):
                self.hit("transcription", "RPM", "requests per minute", wait)
                self.assertIsNone(self.check())
                self.clock.advance(61)
        self.hit("transcription", "RPM", "requests per minute", "60s")
        self.assertIsNotNone(self.check())

    def test_seen_a_moment_late_still_counts_groqs_wait(self):
        self.hit("cleanup", "TPD", "tokens per day", "61s")
        self.clock.advance(2)  # the tray polls every 2s: only 59s are left by then
        self.assertIsNotNone(self.check())

    def test_no_rate_limit_or_no_wait_says_nothing(self):
        self.assertIsNone(self.check())
        info = parse_rate_limit("Rate limit reached on tokens per day (TPD).")  # no "try again in"
        self.usage.log_failure("cleanup", 200, 429, rate_limit=info)
        self.assertIsNone(self.check())

    def test_transcription_and_cleanup_limits_are_separate(self):
        self.hit("cleanup", "TPD", "tokens per day", "41m")
        self.assertIsNotNone(self.check())
        self.hit("transcription", "ASD", "audio seconds per day", "41m")
        self.assertEqual(self.check()["kind"], "transcription")

    def test_the_fake_caps_move_no_meter(self):
        # debug_fake_rate_limit "transcription_cap" / "cleanup_cap" (manual testing) have no "Used N".
        for kind, code, meter in (("transcription", "ASD", "audio_day"), ("cleanup", "TPD", "cleanup_tokens")):
            with self.subTest(kind=kind):
                info = parse_rate_limit(api.FAKE_MESSAGES[f"{kind}_cap"])
                self.assertEqual((info["type"], info["retry_in"], info["used"]), (code, 2460.0, None))
                self.usage.log_failure(kind, 0.0, 429, rate_limit=info)
                self.assertEqual(self.used(self.usage, meter), 0)
                self.assertEqual(self.check()["wait"], 2460.0)


class CapMessageTest(unittest.TestCase):
    def rate_limit(self, kind, name, retry_in=2460.0):
        return {"kind": kind, "type": "X", "name": name, "until": 5000.0, "wait": 2460.0, "retry_in": retry_in}

    def test_transcription_with_history_on(self):
        title, text = cap_message(self.rate_limit("transcription", "audio seconds per day"), history_on=True)
        self.assertEqual(title, "Groq limit used up")
        self.assertEqual(text, "Groq's free limit is used up (audio seconds per day). Dictation works again in "
                               "about 41m. Until then your dictations wait in History for Retry.")

    def test_transcription_with_history_off(self):
        _, text = cap_message(self.rate_limit("transcription", "requests per day"), history_on=False)
        self.assertEqual(text, "Groq's free limit is used up (requests per day). Dictation works again in "
                               "about 41m. Until then your dictations can't be kept, because History is off.")

    def test_cleanup_says_the_words_are_pasted_without_cleanup(self):
        for history_on in (True, False):
            _, text = cap_message(self.rate_limit("cleanup", "tokens per day", 3 * HOUR), history_on)
            self.assertEqual(text, "Groq's free limit is used up (tokens per day). Until it refills "
                                   "(about 3h), Blurt pastes your words without cleanup.")

    def test_an_unknown_limit_and_no_em_dash(self):
        _, text = cap_message(self.rate_limit("transcription", None, 75))
        self.assertTrue(text.startswith("Groq's free limit is used up. Dictation works again in about 1m 15s."))
        for kind in ("transcription", "cleanup"):
            for history_on in (True, False):
                title, text = cap_message(self.rate_limit(kind, "tokens per day"), history_on)
                self.assertNotIn("\u2014", title + text)


if __name__ == "__main__":
    unittest.main()
