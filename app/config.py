"""Loads config.json, and changes single settings in it (the Settings panel).

Missing settings fall back to the defaults below.

- A typo in config.json raises ConfigError, which says which line and what's
  probably wrong ("probably a missing comma"). At startup the app then runs on
  the defaults instead of crashing (load_config_or_defaults).
- set_setting() changes one value and leaves every other character of the file
  as it was. It re-reads the file right before writing (so an edit you just
  saved in Notepad is kept), refuses to touch a broken file (so your settings
  are never replaced by the defaults), and replaces the file all at once.
"""

import json
import os
import threading
import time
from app import shortcut
from app.paths import CONFIG_PATH

# Every value here must equal config.example.json (a test checks it): the example is what a
# first run copies, and these are what a broken config.json falls back to. Debug switches stay
# off in both, so a broken file never starts logging key names or added dictionary terms.
DEFAULTS = {
    # Hold these to dictate. One of the five in app/shortcut.py; anything else uses Ctrl+Win.
    "hotkey": shortcut.text(shortcut.DEFAULT),
    "keep_mic_open": True,
    "debug_hotkey": False,  # print what the hook decides about the Win key, and hook lag
    "debug_pill": False,  # print the pill's state changes and focus checks
    "language": "en",  # Whisper language code, or "auto" to let it detect
    "post_roll_ms": 250,  # keep recording this long after release, so the last word isn't cut off
    "silence_level": 0.01,  # loudness a 20ms slice needs to count as speech
    # A slice must also be this many times louder than the clip's background noise.
    # Lower it if real speech gets discarded (1 = ignore the background noise).
    "speech_floor_ratio": 4,
    # A transcript made only of dictionary words needs at least this much speech, else it's a guess.
    "dictionary_only_min_speech_ms": 120,
    # ...and its loudest slice must reach this share of your usual loudness (median of recent dictations).
    "dictionary_only_loudness_ratio": 0.3,
    "dictionary_only_min_loudest": None,  # a fixed number like 0.02 replaces the adaptive one; null = adaptive
    "clipboard_restore_ms": 300,  # wait this long after pasting before restoring
    # Smart paste. Read fresh on every dictation, so edits apply without a restart.
    "paste_mode": "smart",  # "smart": Ctrl+V only into text fields; "always": Ctrl+V every time
    "paste_always_apps": ["WindowsTerminal.exe"],  # exe names that always get Ctrl+V
    "paste_never_apps": ["blender.exe", "Adobe Premiere Pro.exe"],  # always clipboard only
    # Confirm each paste: Windows asks Blurt for the text only when the app really pastes, and
    # without that the copy icon shows. false = Ctrl+V counts as pasted once Windows took the keys.
    "paste_confirm": True,
    "debug_paste": False,  # print why each dictation was pasted or only copied
    # Keep the text of recent dictations in data/history.db (this laptop only). Read fresh on
    # every dictation. false = nothing new is saved; old entries stay until they expire or Clear.
    "history": True,
    # Testing only: "transcription", "transcription_short", "cleanup", "transcription_cap" or
    # "cleanup_cap" makes the next request of that kind act like Groq sent a rate limit (the
    # _cap ones: a daily limit used up). Fires once, then sets itself back to null.
    "debug_fake_rate_limit": None,
    # Testing only: a meter name ("audio_day", "audio_hour", "transcriptions", "cleanups",
    # "cleanup_tokens") makes the next check pretend that meter is at 85%, so the "almost used
    # up" notification shows. Nothing is added to usage.db. Fires once, then sets itself back to null.
    "debug_fake_limit_warning": None,
    # Tuning only: keep the audio of the latest discarded dictation (silence, echo, a guess) in
    # data/last_discarded.wav, so a wrong discard can be listened to. Off: no audio is kept.
    "debug_save_discarded": False,
    # Groq free tier limits, from console.groq.com/settings/limits. Read at startup.
    # Each one refills steadily (see usage.py), it doesn't reset at a fixed time.
    "groq_limits": {
        "audio_seconds_per_day": 28800,  # whisper-large-v3-turbo, 480 min
        "audio_seconds_per_hour": 7200,  # 120 min
        "transcribe_requests_per_day": 2000,
        "cleanup_requests_per_day": 1000,  # openai/gpt-oss-20b
        "cleanup_tokens_per_day": 200000,
        "min_billed_audio_seconds": 10,  # Groq bills shorter clips as 10s
    },
    # The pill's look and motion. Reloaded live when config.json is saved.
    "pill": {
        "scale": 1.0,  # multiplies every size (not the gap above the taskbar)
        "idle_opacity": 0.22,
        "gap_above_taskbar": 12,
        "menu_padding_left": 18,
        "menu_padding_right": 7,
        "menu_button_gap": 4,
        "shadow_opacity": 0.30,  # hover and recording
        "shadow_opacity_small": 0.26,  # processing, done, error
        "shadow_offset_y": 6,
        "shadow_blur": 12,  # how far the soft edge fades out around the pill
        "resize_ms": 300,
        "resize_easing": [0.2, 0.8, 0.2, 1.0],  # cubic-bezier, like CSS
        "color_ms": 200,
        "content_in_ms": 260,
        "content_in_scale": 0.85,
        "content_out_ms": 120,
        "hover_leave_delay_ms": 150,
        "dots_cycle_ms": 1000,
        "done_hold_ms": 1200,
        "error_hold_ms": 4000,
        "hide_in_fullscreen": True,
        # Recording bars (the live waveform)
        "wave_sensitivity": 1.0,  # multiplies the mic level; 2.0 = bars react to quieter speech
        "wave_min_height": 3.5,  # bar height in silence
        "wave_max_height": 14,  # tallest bar
        "wave_attack_ms": 50,  # how fast a bar rises
        "wave_release_ms": 220,  # how fast a bar falls back
        "wave_step_ms": 50,  # how often a new level enters the center (ripple speed)
        # Mic label: a small pill above the recording pill naming the mic in use
        "mic_label": True,  # false = never show it
        "mic_label_height": 28,
        "mic_label_gap": 8,  # between the label and the recording pill
        "mic_label_max_width": 200,  # longer names are cut with "…"
        "mic_label_padding_left": 10,
        "mic_label_padding_right": 12,
        "mic_label_icon_size": 14,
        "mic_label_icon_gap": 6,  # between the mic icon and the name
        "mic_label_rise": 6,  # how far it rises while fading in
        "mic_label_hold_ms": 1500,  # shown this long, then fades; 0 = the whole recording. "No microphone" always stays
    },
}

# Settings whose default is null, and the kind of value they take when set.
NULLABLE = {"dictionary_only_min_loudest": "number", "debug_fake_rate_limit": "text", "debug_fake_limit_warning": "text"}

REPLACE_RETRY_SECONDS = 0.2  # config.json busy (antivirus, Notepad saving): wait this long, try once more

# Python's JSON error messages, and what they usually mean in a hand-edited file.
HINTS = (
    ("Expecting ',' delimiter", "probably a missing comma"),
    ("Illegal trailing comma", "probably an extra comma before a } or ]"),
    ("Expecting property name", "probably an extra comma before a }, or a name without double quotes"),
    ("Expecting ':' delimiter", "probably a missing colon after a setting's name"),
    ("Unterminated string", "probably a missing closing quote"),
    ("Invalid control character", "probably a missing closing quote"),
    ("Extra data", "something comes after the last }"),
    ("Expecting value", "probably a missing value, or single quotes where double quotes belong"),
)

_write_lock = threading.Lock()  # the GUI (Settings) and the pipeline (debug switches) both write


class ConfigError(ValueError):
    """config.json can't be read or written. str() is a friendly message; line is None if it's not about a line."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


def _syntax_error(e):
    hint = next((text for start, text in HINTS if e.msg.startswith(start)), f"Python says: {e.msg}")
    where = f"line {e.lineno}"
    if e.msg.startswith("Expecting ',' delimiter") and e.lineno > 1:
        where += f" (or the end of line {e.lineno - 1})"  # a missing comma is noticed on the next line
    return ConfigError(f"config.json has a mistake on {where}: {hint}.", e.lineno)


def parse_config_text(text):
    """The settings in config.json's text, as a dict. Raises ConfigError with a friendly message."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _syntax_error(e) from None
    if not isinstance(data, dict):
        raise ConfigError("config.json should start with { and end with }.", 1)
    return data


def _read(path):
    """(text, had_bom) of the file. Notepad can save with a byte order mark; it's kept on write."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


def _defaults():
    return {key: dict(value) if isinstance(value, dict) else value for key, value in DEFAULTS.items()}


def _kind(value):
    """What kind of value this is, in words: "true/false", "a number", "text", "a list", "a section", "null"."""
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list" if all(isinstance(v, str) for v in value) else "a list with non-text in it"
    if isinstance(value, dict):
        return "a section"
    return "null"


def _expected_kind(key):
    default = DEFAULTS[key]
    if default is None:
        return {"number": "a number", "text": "text"}[NULLABLE[key]]
    return _kind(default)


def _fix_kinds(config):
    """Puts the default back for a top-level setting of the wrong kind (like "post_roll_ms": "250"),
    so it can't crash the app. Returns a short message for each one."""
    problems = []
    for key, default in DEFAULTS.items():
        value = config[key]
        if _kind(value) == _expected_kind(key) or (default is None and value is None):
            continue
        config[key] = dict(default) if isinstance(default, dict) else default
        using = "the defaults" if isinstance(default, dict) else json.dumps(default)
        problems.append(f"{key} should be {_expected_kind(key)}, not {_kind(value)}; using {using}.")
    return problems


def _fix_values(config):
    """Puts Ctrl+Win back for a hotkey that isn't allowed (like "Ctrl+Shift"), and writes an
    allowed one in its canonical form ("ctrl + win" -> "Ctrl+Win"), so nothing after this
    has to check it. Runs after _fix_kinds, so the hotkey is text by now."""
    problem = shortcut.problem(config["hotkey"])
    if problem:
        config["hotkey"] = DEFAULTS["hotkey"]
        return [problem]
    config["hotkey"] = shortcut.text(shortcut.parse(config["hotkey"]))
    return []


def _merged(data, problems=None):
    config = _defaults()
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    found = _fix_kinds(config)
    found += _fix_values(config)
    if problems is not None:
        problems.extend(found)
    return config


def load_config(path=None, problems=None):
    """Defaults, overridden by config.json. Sections like "pill" are merged key by key.

    A setting of the wrong kind gets its default; pass a list as problems to get a message for each.
    Raises ConfigError (a ValueError) when the file has a mistake, OSError if it can't be read.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        return _merged({})
    return _merged(parse_config_text(_read(path)[0]), problems)


def wrong_kinds(path=None):
    """Messages for settings of the wrong kind in config.json (empty if none, or if the file is broken)."""
    problems = []
    try:
        load_config(path, problems)
    except (OSError, ValueError):
        return []
    return problems


def config_problem(path=None):
    """The friendly message if config.json is broken or unreadable right now, else None."""
    try:
        load_config(path)
    except ConfigError as e:
        return str(e)
    except (OSError, UnicodeDecodeError) as e:
        return f"config.json couldn't be read ({e})."
    return None


def load_config_or_defaults(path=None):
    """(config, problem) for startup: a broken or unreadable config.json gives the defaults
    plus a friendly message, instead of crashing."""
    problem = config_problem(path)
    return (_merged({}) if problem else load_config(path)), problem


class ConfigWatcher:
    """Notices when config.json is saved. changed() returns the new settings once per save
    (None if nothing changed or the file is broken; the old settings then stay in use)."""

    def __init__(self, path=None):
        self.path = path
        self._stamp = self._read_stamp()
        self.problems = []  # messages for settings that got their default in the last changed() (see _merged)

    def _read_stamp(self):
        try:
            stat = (self.path or CONFIG_PATH).stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def changed(self):
        stamp = self._read_stamp()
        if stamp == self._stamp:
            return None
        self._stamp = stamp
        problems = []
        try:
            config = load_config(self.path, problems)
        except (OSError, ValueError):
            return None
        self.problems = problems
        return config


# ---- changing one setting ----

_WS = " \t\r\n"


def _skip_ws(text, pos):
    while pos < len(text) and text[pos] in _WS:
        pos += 1
    return pos


def _members(text, start):
    """The members of the JSON object whose { is at text[start] (the text is known to be valid JSON).

    Returns (members, end): members is a list of (name, name_start, value_start, value_end),
    end is the position of the closing }.
    """
    decoder = json.JSONDecoder()
    members = []
    pos = _skip_ws(text, start + 1)
    if text[pos] == "}":
        return members, pos
    while True:
        name, after_name = json.decoder.scanstring(text, pos + 1)
        value_start = _skip_ws(text, _skip_ws(text, after_name) + 1)  # past the ":"
        _, value_end = decoder.raw_decode(text, value_start)
        members.append((name, pos, value_start, value_end))
        pos = _skip_ws(text, value_end)
        if text[pos] == "}":
            return members, pos
        pos = _skip_ws(text, pos + 1)  # past the ","


def _line_indent(text, pos):
    """The spaces or tabs at the start of the line that pos is on."""
    line_start = text.rfind("\n", 0, pos) + 1
    end = line_start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[line_start:end]


def _set_in_object(text, start, key, value_json):
    """text with key set to value_json in the object at text[start]. Only that value's characters change.

    A missing key is added after the last member: on its own line (same indent) if the
    members are on their own lines, else on the same line.
    """
    members, end = _members(text, start)
    found = [m for m in members if m[0] == key]
    if found:
        _, _, value_start, value_end = found[-1]  # JSON uses the last one of a duplicated name
        return text[:value_start] + value_json + text[value_end:]
    entry = f"{json.dumps(key, ensure_ascii=False)}: {value_json}"
    newline = "\r\n" if "\r\n" in text else "\n"
    if members:
        last_name_start, last_end = members[-1][1], members[-1][3]
        if "\n" in text[start:members[0][1]]:
            entry = f",{newline}{_line_indent(text, last_name_start)}{entry}"
        else:
            entry = f", {entry}"
        return text[:last_end] + entry + text[last_end:]
    outer = _line_indent(text, start)
    return text[:start + 1] + f"{newline}{outer}  {entry}{newline}{outer}" + text[end:]


def _with_setting(text, key, value, section):
    value_json = json.dumps(value, ensure_ascii=False)
    top = _skip_ws(text, 0)
    if section is None:
        return _set_in_object(text, top, key, value_json)
    members, _ = _members(text, top)
    found = [m for m in members if m[0] == section]
    if found and text[found[-1][2]] == "{":
        return _set_in_object(text, found[-1][2], key, value_json)
    # No such section yet (or it isn't an object): write the section with just this setting.
    return _set_in_object(text, top, section, json.dumps({key: value}, ensure_ascii=False))


def _expected(data, key, value, section):
    expected = json.loads(json.dumps(data))  # a deep copy
    if section is None:
        expected[key] = value
    elif isinstance(expected.get(section), dict):
        expected[section][key] = value
    else:
        expected[section] = {key: value}
    return expected


def _replace(path, data):
    """Write a temp file, then swap it in all at once. A busy file gets one retry, then ConfigError."""
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(data)
        try:
            os.replace(temp, path)
        except PermissionError:
            time.sleep(REPLACE_RETRY_SECONDS)  # antivirus or Notepad saving at this very moment
            os.replace(temp, path)
    except PermissionError:
        raise ConfigError("config.json is busy (another program has it open), so the change wasn't saved. "
                          "Try again in a moment.") from None
    except OSError as e:
        raise ConfigError(f"config.json couldn't be saved ({e}).") from None
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def set_setting(key, value, section=None, path=None):
    """Changes one setting in config.json, e.g. set_setting("history", False) or
    set_setting("idle_opacity", 0.3, section="pill").

    Everything else in the file stays exactly as it was. The file is read right before
    writing, and if it's saved by someone else while we work (Notepad), the change is
    redone on their version. Raises ConfigError, and leaves the file untouched, if
    config.json has a mistake, can't be read, or stays busy.
    """
    path = path or CONFIG_PATH
    with _write_lock:
        for _ in range(2):
            if not path.exists():
                new = {section: {key: value}} if section else {key: value}
                _replace(path, (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                return
            try:
                before = path.stat()
                text, bom = _read(path)
            except (OSError, UnicodeDecodeError) as e:
                raise ConfigError(f"config.json couldn't be read ({e}), so the change wasn't saved.") from None
            data = parse_config_text(text)  # a broken file raises here: never write over it
            new_text = _with_setting(text, key, value, section)
            try:
                ok = parse_config_text(new_text) == _expected(data, key, value, section)
            except ConfigError:
                ok = False
            if not ok:
                raise ConfigError(f"Couldn't change {key} in config.json safely, so it wasn't changed.")
            try:
                now = path.stat()
            except OSError:
                now = None
            if now is None or (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
                continue  # saved by someone else while we worked: redo it on their version
            _replace(path, (b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
            return
        raise ConfigError("config.json kept changing, so the change wasn't saved. Try again in a moment.")


def clear_debug_setting(key, path=None):
    """Sets a one-shot testing setting (like "debug_fake_rate_limit") back to null in config.json.

    Only when it's armed (a string). Uses set_setting, so the rest of the file keeps its
    layout. Raises ConfigError if config.json is broken or busy.
    """
    path = path or CONFIG_PATH
    try:
        data = parse_config_text(_read(path)[0])
    except (OSError, UnicodeDecodeError):
        return
    if isinstance(data.get(key), str):
        set_setting(key, None, path=path)


