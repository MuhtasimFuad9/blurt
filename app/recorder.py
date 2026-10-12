"""Records the microphone into memory (16kHz, mono, int16).

Two modes, set by "keep_mic_open" in config.json:
- On: the mic stays open and always keeps the last 0.2s, so the first word is never cut off.
- Off: the mic opens on Ctrl+Win and closes on release. Better for Bluetooth headphones,
  which drop into low-quality call mode while a mic is open.
"""

import ctypes
import threading
import time
from collections import deque
from ctypes import wintypes

import numpy as np
import sounddevice as sd

from app.micname import NO_MIC, full_name, readable_mic_name

SAMPLE_RATE = 16000
BLOCK_FRAMES = 320  # 20ms per chunk
PREROLL_SECONDS = 0.2
STALL_SECONDS = 1.0  # no audio for this long means the mic is gone

# Windows' built-in audio functions, used to notice when the mics change.
winmm = ctypes.WinDLL("winmm")
WAVE_MAPPER = 0xFFFFFFFF
DRVM_MAPPER_PREFERRED_GET = 0x2015
MMSYSERR_NOERROR = 0


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


winmm.waveInGetNumDevs.argtypes = []
winmm.waveInGetNumDevs.restype = wintypes.UINT
winmm.waveInMessage.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t]
winmm.waveInMessage.restype = wintypes.UINT
winmm.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT]
winmm.waveInGetDevCapsW.restype = wintypes.UINT


def device_fingerprint():
    """(number of mics, name of the default mic). Changes when the mic setup changes."""
    count = winmm.waveInGetNumDevs()
    device_id = wintypes.DWORD()
    flags = wintypes.DWORD()
    result = winmm.waveInMessage(
        WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET, ctypes.addressof(device_id), ctypes.addressof(flags)
    )
    if result != MMSYSERR_NOERROR:
        return (count, None)
    caps = WAVEINCAPSW()
    if winmm.waveInGetDevCapsW(device_id.value, ctypes.byref(caps), ctypes.sizeof(caps)) != MMSYSERR_NOERROR:
        return (count, None)
    return (count, caps.szPname)


def stream_mic_name(stream):
    """The readable name of the mic a stream records from (see micname.py)."""
    try:
        short = sd.query_devices(stream.device)["name"]  # an MME name, maybe cut at 31 characters
        return readable_mic_name(full_name(short, [device["name"] for device in sd.query_devices()]))
    except Exception:
        return "Microphone"


def rms(audio):
    """Loudness from 0.0 (silence) to about 1.0 (max volume)."""
    if audio.size == 0:
        return 0.0
    samples = audio.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples**2)))


class Recorder:
    def __init__(self, keep_open):
        self.keep_open = keep_open
        self.level = 0.0  # latest loudness (RMS), read by the pill's waveform
        self._stream = None
        self._lock = threading.Lock()  # shared between the audio thread and the worker
        self._recording = False
        self._chunks = []
        self._preroll = deque(maxlen=int(PREROLL_SECONDS * SAMPLE_RATE / BLOCK_FRAMES))
        self._last_audio = time.monotonic()  # when the last chunk arrived, loud or silent
        self._mic_name = None  # readable name of the open stream's mic
        self._fingerprint = device_fingerprint()
        self._needs_refresh = False
        self._open_failed = False
        self._closed = False
        self._paused = False  # paused: the mic stays closed (Windows' mic-in-use icon goes away)

    def _callback(self, indata, frames, time_info, status):
        chunk = indata.copy()
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)
            else:
                self._preroll.append(chunk)
        self.level = rms(chunk)
        self._last_audio = time.monotonic()

    def _open_stream(self):
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_FRAMES,
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._mic_name = stream_mic_name(stream)
        self._last_audio = time.monotonic()
        self._stream = stream

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass  # the device may already be gone
        with self._lock:
            self._preroll.clear()
        self.level = 0.0  # a closed mic is silent, not stuck at its last level

    def _try_open(self):
        """Open the mic. Returns False (and reports once) if it can't."""
        if self._closed:
            return False
        try:
            if self._needs_refresh:
                # sounddevice remembers the device list from startup; reload it.
                sd._terminate()
                sd._initialize()
                self._needs_refresh = False
            self._open_stream()
        except Exception as e:
            self._stream = None
            self._needs_refresh = True
            if not self._open_failed:
                print(f"mic error: {e}")
                self._open_failed = True
            return False
        self._open_failed = False
        return True

    def open(self):
        """Called once at startup."""
        if self.keep_open:
            self._try_open()

    def is_open(self):
        return self._stream is not None

    def stalled(self):
        """True if the open stream stopped delivering audio (no chunks at all for STALL_SECONDS).

        Silence doesn't count: a quiet mic still delivers a chunk every 20ms.
        """
        return self._stream is not None and time.monotonic() - self._last_audio > STALL_SECONDS

    @property
    def mic_label(self):
        """What the pill's mic label shows: the mic's readable name, NO_MIC, or None.

        None means "not known yet" (with keep_mic_open off, the mic is still opening), so
        the label stays hidden instead of flashing "No microphone". Safe from any thread.
        """
        if self._open_failed:
            return NO_MIC
        if self._stream is None:
            return None
        if self.stalled():
            return NO_MIC
        return self._mic_name

    def start(self):
        """Start collecting audio. Returns False if no mic could be opened."""
        with self._lock:
            self._chunks = list(self._preroll)  # the 0.2s before the key press
            self._preroll.clear()
            self._recording = True
        if self._stream is None and not self._try_open():
            with self._lock:
                self._recording = False
            return False
        return True

    def stop(self):
        """Stop collecting and return the audio as one numpy array."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not self.keep_open or self._paused:
            self._close_stream()
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks).flatten()

    def check_devices(self):
        """Called every couple of seconds. Reopens the mic if it changed or died."""
        if self._recording or self._closed or self._paused:
            return
        fingerprint = device_fingerprint()
        changed = fingerprint != self._fingerprint
        if changed:
            self._fingerprint = fingerprint
            self._needs_refresh = True

        if not self.keep_open:
            if changed:
                print(f'mic changed: next recording will use "{fingerprint[1]}"')
            return

        stalled = self.stalled()
        if changed or stalled or self._stream is None:
            self._close_stream()
            if self._try_open():
                print(f'mic changed: reopened on "{self._fingerprint[1]}"')

    def set_paused(self, paused):
        """Pausing closes the mic; resuming reopens it (with keep_open). Called by the worker.

        A recording that's still going when you pause keeps its mic; stop() closes it.
        """
        self._paused = paused
        if paused:
            if not self._recording:
                self._close_stream()
        elif self.keep_open and self._stream is None:
            self._try_open()

    def set_keep_open(self, keep_open):
        """The "keep_mic_open" setting changed. Off closes the mic, on reopens it (unless paused).
        Called by the worker. A recording that's still going keeps its mic; stop() decides."""
        if keep_open == self.keep_open:
            return
        self.keep_open = keep_open
        if not keep_open:
            if not self._recording:
                self._close_stream()
        elif not self._paused and self._stream is None:
            self._try_open()

    def close(self):
        self._closed = True
        self._close_stream()
