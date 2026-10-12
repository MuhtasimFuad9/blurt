"""Turns a finished recording into pasted text, on its own thread.

Steps: silence check, transcribe, clean up, paste (or leave the text on the
clipboard: with smart paste and no text field focused, or when you switched to
another window while it was processing).
Every dictation that reaches Groq is logged to the usage tracker (counts only),
and, unless "history" is off, saved to History with its text (history.py).
Dictations and History retries wait in one queue and are handled one at a
time, in order, so neither the hotkey worker nor the GUI waits on the network.
"""

import io
import queue
import re
import threading
import time
import wave

import groq
import numpy as np

from app.api import RateLimited, describe_for_log
from app.cleanup import FILLER_WORDS, clean_up
from app.config import load_config
from app.dictionary import load_terms
from app.focus import find_text_field, foreground_root, window_exe
from app.paster import copy_only, paste
from app.log import describe_exception, hides_text, print_exception
from app.paths import DATA_DIR, LAST_DISCARDED
from app.recorder import BLOCK_FRAMES, SAMPLE_RATE
from app.transcribe import is_dictionary_only, is_prompt_echo, transcribe
from app.usage import VOICE_HISTORY

MIN_LOUD_SLICES = 5  # 5 x 20ms = 0.1s of sound needed to count as speech
FLOOR_SLICES = 5  # the background noise level is measured on the quietest 0.1s
MIN_VOICE_LEVELS = 10  # pasted dictations needed before the dictionary-only loudness adapts
DEFAULT_MIN_LOUDEST = 0.02  # used until then
PASTE_FAILED = "Couldn't paste (the clipboard was busy). Press Copy."
KEYS_HELD = "Couldn't paste (keys were still held). Press Copy."
CRASHED = "Something went wrong (see the console or the log file)"


def is_only_filler(text):
    """True for text like "So." or "Um, uh" (or only punctuation) that isn't worth pasting.

    Words are letters and digits in any script, so "42", Bengali or Russian text
    always counts as real words.
    """
    words = re.findall(r"\w+(?:'\w+)*", text.lower())
    return all(w in FILLER_WORDS for w in words)


def shown(text):
    """Dictation text for the console. When the output goes to a log file (no console, or the
    installed app), History's 7 days and Clear wouldn't cover it, so only the word count is shown."""
    if hides_text():
        return f"(hidden, {len(text.split())} words)"
    return text


def slice_levels(audio):
    """Loudness (RMS) of each 20ms slice."""
    usable = len(audio) - len(audio) % BLOCK_FRAMES
    if usable == 0:
        return np.zeros(0)
    slices = audio[:usable].astype(np.float32).reshape(-1, BLOCK_FRAMES) / 32768.0
    return np.sqrt(np.mean(slices**2, axis=1))


def audio_stats(audio, silence_level, floor_ratio):
    """Returns (loudest slice, background noise floor, milliseconds of speech).

    The floor is the average of the quietest 0.1s worth of slices (anywhere in the
    clip, the post-roll always has some). Not a percentile: a long clip that is
    mostly talking would put a percentile on the voice itself and discard it.
    A slice counts as speech when it reaches silence_level and is floor_ratio
    times louder than the floor, so steady noise (a fan, mic hiss) never counts,
    however loud it is.
    """
    levels = slice_levels(audio)
    if levels.size == 0:
        return 0.0, 0.0, 0.0
    floor = float(np.sort(levels)[:FLOOR_SLICES].mean())
    threshold = max(silence_level, floor_ratio * floor)
    speech_ms = np.count_nonzero(levels >= threshold) * BLOCK_FRAMES / SAMPLE_RATE * 1000
    return float(levels.max()), floor, float(speech_ms)


def dictionary_only_min_loudest(voice_levels, ratio, override):
    """How loud a dictionary-only dictation must be. Returns (level, where it came from).

    voice_levels: the loudest level of recent pasted real dictations (UsageTracker.voice_levels).
    A fixed number only fits one mic, so it follows ratio x the median of those,
    once there are enough. override (config.json) replaces all of that when not None.
    """
    if override is not None:
        return float(override), "fixed in config.json"
    levels = voice_levels[-VOICE_HISTORY:]
    if len(levels) < MIN_VOICE_LEVELS:
        return DEFAULT_MIN_LOUDEST, f"default until {MIN_VOICE_LEVELS} dictations, {len(levels)} so far"
    median = float(np.median(levels))
    return ratio * median, f"adaptive, {ratio:g} x median {median:.3f} of the last {len(levels)}"


def describe_error(error):
    """A short reason for History, from a failed transcription's error."""
    if isinstance(error, (groq.AuthenticationError, groq.PermissionDeniedError)):
        return "Groq key rejected"
    if isinstance(error, groq.APITimeoutError):
        return "Groq took too long to answer"
    if isinstance(error, groq.APIConnectionError):
        return "No answer from Groq"
    if isinstance(error, groq.APIStatusError):
        return f"Groq error {error.status_code}"
    return CRASHED


class Heard:
    """What transcription and cleanup made of one clip (see Pipeline._hear).

    problem is None, "limited" or "failed" (transcription didn't work), with a
    short reason in error. text is None when there's nothing worth pasting
    (empty transcript, a guess, only filler).
    """

    def __init__(self, problem=None, error=None, raw=None, cleaned=None, cleanup="skipped", billed=None, tokens=0):
        self.problem = problem
        self.error = error
        self.raw = raw
        self.cleaned = cleaned
        self.cleanup = cleanup
        self.billed = billed
        self.tokens = tokens
        self.text = None


def to_wav_bytes(audio):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buffer.getvalue()


def read_wav(path):
    """A WAV file written by to_wav_bytes, back as int16 samples."""
    with wave.open(str(path), "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)


class Pipeline:
    def __init__(self, client, hotkey, config, tracker, on_result=None, history=None):
        self.client = client  # None until there's an API key (dictation is paused until then)
        self.hotkey = hotkey
        self.config = config
        self.tracker = tracker  # usage tracking (usage.py)
        self.history = history  # History (history.py), or None
        # Called after every dictation (not retries) with the result for the pill ("done",
        # "copied", "limited", "error", "quiet" or "nothing") and the History entry it made (0 = none).
        self.on_result = on_result
        self._jobs = queue.Queue()
        self._entry_id = None  # the History entry the current dictation made
        self._paste_error = None  # History's message for a failed paste, when paste() knew the reason
        self._busy = 0  # jobs queued or running
        self._idle = threading.Condition()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def set_client(self, client):
        """A new API key was saved: the next job uses the new client. Any thread."""
        self.client = client

    def submit(self, audio, target=None):
        """Called by the worker. Returns immediately.

        target is the window you dictated into (focus.foreground_root() at release).
        The text is only pasted if that window is still in front; None skips the check.
        """
        self._add(("dictation", audio, time.perf_counter(), target))

    def retry(self, entry_id):
        """Called from History (GUI thread). Returns immediately; the entry shows "Retrying…" meanwhile."""
        if self.history is None:
            return
        if self.client is None:
            print("retry: no Groq API key yet, so it can't be sent")
            return
        self.history.set_retrying(entry_id, True)
        self._add(("retry", entry_id, time.perf_counter(), None))

    def wait_until_idle(self, seconds):
        """Wait until every queued job is finished (pasted, clipboard restored). False if time ran out."""
        with self._idle:
            return self._idle.wait_for(lambda: self._busy == 0, seconds)

    def _add(self, job):
        with self._idle:
            self._busy += 1
        self._jobs.put(job)

    def _run(self):
        while True:
            job = self._jobs.get()
            try:
                self._run_job(*job)
            finally:
                with self._idle:
                    self._busy -= 1
                    self._idle.notify_all()

    def _run_job(self, kind, item, submitted_at, target=None):
        if kind == "retry":
            self._run_retry(item, submitted_at)
        else:
            self._entry_id = None
            try:
                result = self._process(item, submitted_at, target)
            except Exception as e:
                # One bad dictation must never stop the next one.
                print_exception(e)
                print("dictation failed (see error above)")
                result = "error"
                if self._entry_id is None:
                    self._save_entry("failed", error=CRASHED, wav_bytes=to_wav_bytes(item))
            if self.on_result:
                self.on_result(result, self._entry_id or 0)

    def _run_retry(self, entry_id, submitted_at):
        try:
            self._retry(entry_id, submitted_at)
        except Exception as e:
            print_exception(e)
            print(f"retry of History entry {entry_id} failed (see error above)")
            self.history.update(entry_id, error=CRASHED, retried_ts=time.time())
        finally:
            # Only after the entry is updated, so the panel never shows the old state in between.
            self.history.set_retrying(entry_id, False)

    def _record(self, submitted_at, result, transcription, heard=None, words=0, voice=None):
        """Logs the dictation's counts to usage.db. Returns result."""
        heard = heard or Heard()
        latency_ms = (time.perf_counter() - submitted_at) * 1000
        self.tracker.log_dictation(
            result, transcription, heard.cleanup, heard.billed, heard.tokens, words, latency_ms, voice
        )
        return result

    def _process(self, audio, submitted_at, target=None):
        """Returns "done" (pasted), "copied" (left on the clipboard, not pasted), "limited"
        (rate limit, see below), "error" (failed), "quiet" (the silence check heard no
        speech, so nothing was sent) or "nothing" (nothing worth pasting).

        "limited" means a rate limit got in the way: either nothing could be
        transcribed, or the raw transcript was pasted without cleanup.
        target: the window you dictated into (see submit).
        """
        loudest, floor, speech_ms = audio_stats(audio, self.config["silence_level"], self.config["speech_floor_ratio"])
        print(f"audio {len(audio) / SAMPLE_RATE:.2f}s | loudest {loudest:.3f} | floor {floor:.4f} | speech {speech_ms / 1000:.2f}s")
        min_speech_ms = MIN_LOUD_SLICES * BLOCK_FRAMES / SAMPLE_RATE * 1000
        if speech_ms < min_speech_ms:
            print(f"discarded by silence check (speech {speech_ms / 1000:.2f}s, needs {min_speech_ms / 1000:.2f}s)")
            self._save_discarded(audio)
            return "quiet"  # the pill says "Didn't hear you"; main.py notices a mic that's too quiet

        # Kept in memory only. If transcription fails, History saves it for Retry (unless History is off).
        wav_bytes = to_wav_bytes(audio)

        heard = self._hear(audio, wav_bytes, loudest, speech_ms)
        if heard.problem:
            self._save_entry(heard.problem, error=heard.error, started=submitted_at, wav_bytes=wav_bytes)
            if heard.problem == "limited":
                return self._record(submitted_at, "limited", "limited")
            return self._record(submitted_at, "error", "failed")
        if heard.text is None:
            return self._record(submitted_at, "nothing", "ok", heard)

        self._paste_error = None
        t0 = time.perf_counter()
        delivered = self._deliver(heard.text, target)
        # Numbers only. With the transcribe/cleanup line above, it tells a slow Groq from a slow paste.
        done_at = time.perf_counter()
        print(f"paste {done_at - t0:.2f}s | whole dictation {done_at - submitted_at:.2f}s since the recording ended")
        failed = delivered in ("error", "held")
        result = "error" if failed else "done"
        if result == "done" and heard.cleanup == "limited":
            result = "limited"  # pasted, but show the hourglass so you know it wasn't cleaned up
        words = len(heard.text.split()) if result != "error" else 0
        # Teach the dictionary-only rule how loud real speech is on this mic. Dictionary-only
        # text is left out: a quiet one could be a guess that got through.
        voice = loudest if words > 0 and not is_dictionary_only(heard.text, load_terms()) else None
        status = "failed" if failed else "copied" if delivered == "copied" else "pasted"
        self._save_entry(
            status, raw=heard.raw, cleaned=heard.cleaned, cleanup=heard.cleanup, started=submitted_at,
            error=self._paste_error or {"error": PASTE_FAILED, "held": KEYS_HELD}.get(delivered),
        )
        self._record(submitted_at, result, "ok", heard, words, voice)  # usage.db counts a copy as "done"
        if result == "done" and delivered == "copied":
            return "copied"  # the pill shows a copy icon instead of the checkmark
        return result

    def _retry(self, entry_id, submitted_at):
        """Sends a History entry's saved audio again.

        The result goes to the clipboard, not pasted: by now the cursor may be anywhere.
        If it fails again, or hears nothing (Whisper may have had a bad moment),
        the audio and the Retry button stay.
        """
        path = self.history.audio_path(entry_id)
        if path is None:
            print(f"retry: History entry {entry_id} has no saved audio anymore")
            self.history.update(entry_id, drop_audio=True)
            return
        audio = read_wav(path)
        loudest, _, speech_ms = audio_stats(audio, self.config["silence_level"], self.config["speech_floor_ratio"])
        print(f"retrying History entry {entry_id}: audio {len(audio) / SAMPLE_RATE:.2f}s | loudest {loudest:.3f}")
        heard = self._hear(audio, path.read_bytes(), loudest, speech_ms)
        now = time.time()
        if heard.problem:
            print("retry didn't work, the audio is still kept for another try")
            self.history.update(entry_id, status=heard.problem, error=heard.error, retried_ts=now)
            limited = heard.problem == "limited"
            self._record(submitted_at, "limited" if limited else "error", "limited" if limited else "failed")
            return
        if heard.text is None:
            print("retry heard nothing, the audio is still kept for another try")
            self.history.update(entry_id, error="Nothing heard on retry", retried_ts=now)
            self._record(submitted_at, "nothing", "ok", heard)
            return

        copied = copy_only(heard.text)
        if copied:
            print("retry worked: copied to clipboard. Press Ctrl+V to paste it.")
        # The text is in History now, so the audio isn't needed anymore.
        self.history.update(
            entry_id, drop_audio=True, status="copied" if copied else "failed", raw=heard.raw,
            cleaned=heard.cleaned, cleanup=heard.cleanup, retried_ts=now,
            latency_ms=(time.perf_counter() - submitted_at) * 1000,
            error=None if copied else "Couldn't copy (the clipboard was busy). Press Copy.",
        )
        self._record(submitted_at, "done" if copied else "error", "ok", heard, len(heard.text.split()) if copied else 0)

    def _hear(self, audio, wav_bytes, loudest, speech_ms):
        """Transcribe, check for guesses, clean up, check again. Returns a Heard."""
        terms = load_terms()
        clip_seconds = len(audio) / SAMPLE_RATE
        t0 = time.perf_counter()
        try:
            raw, billed = transcribe(self.client, self.tracker, wav_bytes, clip_seconds, terms, self._fresh("language"))
        except RateLimited as e:
            print(f"transcription failed: {describe_for_log(e)} (after {time.perf_counter() - t0:.2f}s)")
            return Heard("limited", f"Rate limit: {e.info['name'] or 'unknown limit'}")
        except Exception as e:
            print(f"transcription failed: {describe_for_log(e)} (after {time.perf_counter() - t0:.2f}s)")
            return Heard("failed", describe_error(e))
        t1 = time.perf_counter()
        if not raw:
            print("nothing heard (empty transcript)")
            return Heard(billed=billed)
        if self._is_hallucination(raw, terms, loudest, speech_ms, audio):  # before cleanup, saves a request
            return Heard(billed=billed)

        cleaned, cleanup_status, tokens = clean_up(self.client, self.tracker, raw, terms)
        t2 = time.perf_counter()
        print(f"RAW:     {shown(raw)}")
        if cleaned is None:
            reason = "rate limited" if cleanup_status == "limited" else "cleanup failed"
            print(f"CLEANED: (skipped, {reason}), pasting the raw transcript")
            text = raw
        else:
            print(f"CLEANED: {shown(cleaned)}")
            text = cleaned
        print(f"transcribe {t1 - t0:.2f}s | cleanup {t2 - t1:.2f}s | total {t2 - t0:.2f}s")
        heard = Heard(raw=raw, cleaned=cleaned, cleanup=cleanup_status, billed=billed, tokens=tokens)
        if is_only_filler(text):
            print("nothing pasted (only filler words)")
            return heard
        if self._is_hallucination(text, terms, loudest, speech_ms, audio):  # cleanup can turn a guess into terms
            return heard
        heard.text = text
        return heard

    def _is_hallucination(self, text, terms, loudest, speech_ms, audio):
        """True (and prints why) if text looks like Whisper guessing from its vocabulary hint.

        Two rules:
        - Echo guard: the dictionary read back (see is_prompt_echo). Any loudness.
        - Dictionary-only rule: only dictionary words, but too little speech or too
          quiet for this mic. Real speech of the same words is louder, so it still
          gets pasted. A muttered lone term sounds just like noise and is discarded.
        """
        if is_prompt_echo(text, terms):
            print(f"discarded by echo guard (Whisper repeated the dictionary): {shown(text)!r}")
            self._save_discarded(audio)
            return True
        if not is_dictionary_only(text, terms):
            return False
        needed_ms = self.config["dictionary_only_min_speech_ms"]
        needed_loudest, source = dictionary_only_min_loudest(
            self.tracker.voice_levels(),
            self.config["dictionary_only_loudness_ratio"],
            self.config["dictionary_only_min_loudest"],
        )
        if speech_ms >= needed_ms and loudest >= needed_loudest:
            return False
        print(
            f"discarded by dictionary-only rule: loudest {loudest:.3f}, needs {needed_loudest:.3f} ({source});"
            f" speech {speech_ms / 1000:.2f}s, needs {needed_ms / 1000:.2f}s: {shown(text)!r}"
        )
        self._save_discarded(audio)
        return True

    def _save_discarded(self, audio):
        """With debug_save_discarded on, keeps the audio of the latest discarded dictation, so a
        wrong discard can be listened to. Off (the default), no audio of you is written anywhere."""
        if not self._fresh("debug_save_discarded"):
            return
        try:
            DATA_DIR.mkdir(exist_ok=True)
            LAST_DISCARDED.write_bytes(to_wav_bytes(audio))
            print(f"  (audio saved to {LAST_DISCARDED})")
        except OSError as e:
            print(f"  (could not save the discarded audio: {e})")

    def _fresh(self, key):
        """A setting read fresh from config.json, so a change (Settings, Notepad) applies to the
        next dictation. A broken config.json falls back to the settings from startup."""
        try:
            return load_config()[key]
        except (OSError, ValueError, KeyError):
            return self.config.get(key)

    def _history_on(self):
        """The "history" setting, read fresh so switching it off applies to the next dictation."""
        if self.history is None:
            return False
        value = self._fresh("history")
        return True if value is None else bool(value)

    def _save_entry(self, status, started=None, wav_bytes=None, **fields):
        """Adds the current dictation to History (unless it's off). Failed ones keep their audio for Retry."""
        if not self._history_on():
            return
        if status not in ("failed", "limited") or fields.get("raw"):
            wav_bytes = None  # there's text: Copy is enough, no Retry needed
        latency_ms = (time.perf_counter() - started) * 1000 if started is not None else None
        try:
            self._entry_id = self.history.add(status, latency_ms=latency_ms, wav_bytes=wav_bytes, **fields)
        except Exception as e:
            print(f"history: couldn't save this dictation: {describe_exception(e)}")
            return
        kept = ", audio kept for Retry" if self.history.audio_path(self._entry_id) else ""
        print(f"history: saved entry {self._entry_id} ({status}{kept})")

    def _deliver(self, text, target=None):
        """Paste into a text field, or leave the text on the clipboard (see focus.py).

        target is the window you dictated into. If another window is in front by now
        (you switched while it was processing), nothing is pasted: the text could land
        in someone else's chat. It's left on the clipboard instead, in both paste modes.
        Windows are compared by their top-level window, so an app that swaps its inner
        windows doesn't count as a switch.

        The window is checked twice: here, and again at the last moment, after paste()
        has waited for the keys to be released, right before Ctrl+V (ready below). In
        smart mode the text field check runs only there, at the last moment.

        With "paste_confirm" on, "pasted" means the app asked for the text (see paster.paste).
        When paste() knows why it failed, History's message for it is left in _paste_error.

        Returns "pasted", "copied", "held" (keys still held, see paste) or "error".
        """
        info = {}  # paste() fills in "reason" (for "copied") and "error" (History's message)
        if target is not None:
            now = foreground_root()
            if now != target:
                print(f"switched windows while processing ({window_exe(target) or 'unknown app'} -> "
                      f"{window_exe(now) or 'unknown app'}), so it wasn't pasted")
                if not copy_only(text):
                    return "error"
                print("copied to clipboard, not pasted (you switched windows). Press Ctrl+V to paste it.")
                return "copied"

        try:
            settings = load_config()  # read fresh, so paste settings apply without a restart
        except (OSError, ValueError) as e:
            print(f"config.json has a mistake, using the settings from startup: {e}")
            settings = self.config
        mode = settings["paste_mode"]
        if mode not in ("smart", "always"):
            print(f'paste_mode should be "smart" or "always", not {mode!r}. Using "smart".')
            mode = "smart"

        why_not = None  # set by ready() when it says no

        def ready():
            """paste()'s last check, right before Ctrl+V. False leaves the text on the clipboard."""
            nonlocal why_not
            if target is not None:
                now = foreground_root()
                if now != target:
                    print(f"switched windows just before pasting ({window_exe(target) or 'unknown app'} -> "
                          f"{window_exe(now) or 'unknown app'}), so it wasn't pasted")
                    why_not = "you switched windows"
                    return False
            if mode == "smart":
                t0 = time.perf_counter()
                is_field, reason = find_text_field(settings)
                if settings["debug_paste"]:
                    print(f"[paste] {reason} ({(time.perf_counter() - t0) * 1000:.0f}ms)")
                if not is_field:
                    why_not = "no text field focused"
                    return False
            return True

        # Every config from load_config has "paste_confirm" (DEFAULTS: true); a settings dict
        # without it gets the unconfirmed paste of Checkpoint B.
        confirm = bool(settings.get("paste_confirm", False))
        delivered = paste(text, self.hotkey, restore_ms=self.config["clipboard_restore_ms"], ready=ready,
                          target=target, confirm=confirm, info=info)
        self._paste_error = info.get("error")
        if delivered == "copied":
            reason = why_not or info.get("reason") or "Windows refused the key presses"
            print(f"copied to clipboard, not pasted ({reason}). Press Ctrl+V to paste it.")
        return delivered
