"""Turns Windows mic names into short labels for the pill, and cuts long ones to fit.

Pure text functions (no Qt, no sounddevice), so tests/test_micname.py can check them.
"""

import re

# Recorder.mic_label returns this when there's no working mic (the pill shows "No microphone").
NO_MIC = object()
NO_MIC_TEXT = "No microphone"

ELLIPSIS = "…"  # one character, tighter than "..."

# Windows' old MME audio API (what sounddevice records through) cuts names at 31 characters.
MME_NAME_LIMIT = 31

# Windows' own names for the kind of input. When the name is one of these, the part in
# brackets (the actual device) says more. A name you gave the mic in Sound settings wins.
GENERIC_ENDPOINTS = {
    "microphone",
    "microphone array",
    "headset",
    "headset microphone",
    "external microphone",
    "internal microphone",
    "line in",
    "input",
}

_MARKS = re.compile(r"\((?:R|TM)\)|[®™]", re.IGNORECASE)  # (R), (TM), ®, ™
_ENDPOINT_AND_DEVICE = re.compile(r"(.*?)\s*\((.*)\)")
_DUPLICATE_NUMBER = re.compile(r"^\d+-\s*")  # "2- USB Audio Device": Windows numbers duplicates
_AG_AUDIO = re.compile(r"\s+AG Audio$", re.IGNORECASE)  # Bluetooth: "Hands-Free AG Audio"


def readable_mic_name(raw):
    """Turns "Microphone (Realtek(R) Audio)" into "Realtek Audio". Never returns an empty string."""
    text = " ".join(_MARKS.sub("", raw or "").split())
    # A name cut short by MME can end in an unclosed "(...": drop that part.
    while text.count("(") > text.count(")"):
        text = text[: text.rfind("(")].rstrip()

    match = _ENDPOINT_AND_DEVICE.fullmatch(text)
    if not match:
        return text or "Microphone"
    endpoint = match.group(1).strip()
    device = _AG_AUDIO.sub("", _DUPLICATE_NUMBER.sub("", match.group(2).strip())).strip()
    if device and (not endpoint or endpoint.lower() in GENERIC_ENDPOINTS):
        return device
    return endpoint or "Microphone"


def full_name(short, candidates):
    """The full name of a device whose MME name was cut at 31 characters.

    `candidates` are names from the other Windows audio APIs, which aren't cut.
    """
    if len(short) < MME_NAME_LIMIT:
        return short  # short names are already complete
    for name in candidates:
        if len(name) > len(short) and name.startswith(short):
            return name
    return short


class LabelHold:
    """Shows each mic name for a moment, then hides it. "No microphone" never hides early.

    The clock starts when a name first appears (not when recording starts), and again
    whenever the name changes, so a new name always shows. Times are in ms; the pill
    passes its own clock, tests pass a fake one.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Forget the last name: the next one gets a full hold (a new recording)."""
        self._value = None
        self._since = 0.0

    def visible(self, value, hold_ms, now):
        """`value` is a name, NO_MIC, or None (not known yet). Returns it, or None to hide."""
        if value is None:
            return None
        if value != self._value:
            self._value, self._since = value, now
        if value is not NO_MIC and hold_ms > 0 and now - self._since >= hold_ms:
            return None
        return value


def elide(text, max_width, measure):
    """`text`, or as much of it as fits in `max_width` followed by "…".

    `measure` returns a string's width (the pill passes the font's; tests pass len).
    """
    if measure(text) <= max_width:
        return text
    for end in range(len(text) - 1, 0, -1):
        cut = text[:end].rstrip(" -_,.;:(/")
        if cut and measure(cut + ELLIPSIS) <= max_width:
            return cut + ELLIPSIS
    return ELLIPSIS
