"""The Settings panel: the few settings worth a UI. Everything else stays in config.json.

Opens from the pill's Settings button. Built on Panel (panel.py): same no-focus
window, and History's light cards (dark text on near-white; white on the saturated
blue was hard to read here). Only clicks and drags, never typing: toggles,
two-choice button rows and one slider (no dropdowns either: a dropdown opens a
popup window that takes focus).

- Every change is written with config.set_setting, which changes only that one
  value in config.json and never writes over a broken file. Everything applies
  right away, so there's no "restart needed".
- While config.json has a mistake, a banner says where, the controls are dimmed
  and show no values, and clicks on them do nothing.
- The idle opacity slider has its own preview (the idle pill on a light and a dark
  strip), because while a panel is open the pill shows its hover look.
- Quit takes two clicks ("Quit?" for 3s), because a confirm dialog would take focus.
- The API key row (M11) says where the key comes from, and its button opens the key
  window (a normal window that may take focus; dictation pauses while it's open).
  The key isn't in config.json, so this row still works while the file is broken.
- Start with Windows (M12) lives in the registry (app/autostart.py), not config.json,
  so it works while the file is broken too. From source it's dimmed and does nothing.
- The Shortcut row (M14) is the one row that isn't applied at once: Change opens a
  list of the five allowed shortcuts, and only Save writes config.json (the worker
  then switches the hook between key presses, main.apply_new_hotkey). A hotkey in
  config.json that isn't allowed shows as Ctrl+Win (what the app really uses) with a
  dark amber hint.
- The footer shows the version.
- On a screen too small for the card (M16), the card stops at the screen's top and
  everything under the header scrolls with the wheel, the footer and Quit included
  (Panel.FITS_SCREEN). Every rect comes from _top(), which follows the scroll, so
  painting and clicks move together; a click outside the visible part does nothing.
"""

import subprocess

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPen

from app import shortcut
from app.config import CONFIG_PATH, ConfigError, config_problem, load_config, set_setting
from app.ui.history_panel import NAVY, SLATE, _css
from app.ui.history_panel import PALETTE as HISTORY_PALETTE
from app.ui.panel import AMBER, PAD, SECTION_GAP, WIDTH, Panel, _font
from app.ui.pill import BLUE, WHITE, _color, _now_ms
from app.version import VERSION

ROW_H = 26  # a row's top line: the label on the left, the control on the right
HINT_GAP = 1
CARD_PAD_X, CARD_PAD_Y = 12, 9
CARD_RADIUS = 10
CARD_GAP = 8  # between the two cards
ROW_SEP = 9  # above and below the thin line between two rows
TOGGLE_W, TOGGLE_H = 34, 20
KNOB = 16  # the toggle's knob
SEG_H = 26
SEG_INSET = 2
SEG_TEXT_PAD = 11
SLIDER_H = 20  # the slider's hit area; the track is thinner
TRACK_H = 4
SLIDER_KNOB = 14
SLIDER_KNOB_DRAG = 16
PREVIEW_W, PREVIEW_H = 50, 20
PREVIEW_GAP = 4
IDLE_W, IDLE_H = 40, 6  # the idle pill, at its real size
PREVIEW_LIGHT = (0xD4, 0xD9, 0xE3)  # darker than the card, so the strip shows
PREVIEW_DARK = (0x1F, 0x23, 0x30)
FOOTER_H = 24
QUIT_PAD = 9
HEADER_BUTTON = 28
HEADER_ICON = 14
BANNER_PAD = 12
BANNER_ICON = 15
BUTTON_H = 28
BUTTON_PAD_LEFT, BUTTON_PAD_RIGHT = 9, 12
BUTTON_ICON = 13
DIMMED = 0.35  # the controls while config.json is broken

OPACITY_MIN, OPACITY_MAX = 0.05, 1.0
QUIT_CONFIRM_MS = 3000
NOTE_MS = 4000  # how long "Not saved: ..." stays in the footer
LIVE_MS = 500

# History's colors, plus the controls. Each color is (rgb, alpha).
PALETTE = {
    "card": HISTORY_PALETTE["card"],
    "divider": (SLATE, 0.16),
    "label": (NAVY, 1.0),
    "hint": (SLATE, 1.0),
    "warning": HISTORY_PALETTE["limited"],  # dark amber: the broken-config icon (bright amber vanishes on white)
    "on_track": (BLUE, 1.0),
    "on_knob": (WHITE, 1.0),
    "off_track": (SLATE, 0.28),
    "off_knob": (WHITE, 1.0),
    "halo": (BLUE, 0.12),
    "seg_track": (NAVY, 0.07),
    "seg_on": (BLUE, 1.0),
    "seg_on_text": (WHITE, 1.0),
    "seg_text": (NAVY, 1.0),
    "seg_hover": (BLUE, 0.10),
    "slider_track": (SLATE, 0.25),
    "slider_fill": (BLUE, 1.0),
    "slider_knob": (BLUE, 1.0),
    "button": (BLUE, 1.0),
    "button_hover": HISTORY_PALETTE["retry_hover"],
    "button_text": (WHITE, 1.0),
    "rejected": HISTORY_PALETTE["failed"],  # dark orange: "Rejected by Groq"
    "radio_ring": (SLATE, 0.55),  # a shortcut not picked in the list
    "tag": HISTORY_PALETTE["tag"],  # "Default", like History's tags
    "tag_text": HISTORY_PALETTE["tag_text"],
    "link": (BLUE, 1.0),  # Cancel: blue text, no fill
}
KEY_BUTTON_H = 24
KEY_BUTTON_PAD = 12
# Where the key comes from (key_status() in main.py), as the row's hint.
KEY_HINTS = {
    "windows": "Saved in Windows",
    "env": "From the .env file",
    None: "None yet: dictation is paused",
    "rejected": "Rejected by Groq: change it",
}

LANGUAGES = [("en", "English"), ("auto", "Auto-detect")]
# A language code set in config.json by hand shows as a third choice, by name if we know it.
LANGUAGE_NAMES = {
    "bn": "Bengali", "hi": "Hindi", "ur": "Urdu", "ar": "Arabic", "es": "Spanish", "fr": "French",
    "de": "German", "pt": "Portuguese", "it": "Italian", "ru": "Russian", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "tr": "Turkish", "id": "Indonesian", "nl": "Dutch",
}
PASTE_MODES = [("smart", "Smart"), ("always", "Always")]
PASTE_HINTS = {"smart": "Only into text fields, else copied", "always": "Ctrl+V every time"}
AUTOSTART_HINT = "Opens when you sign in"
AUTOSTART_SOURCE_HINT = "Only in the installed app"

# The Shortcut row's list (open after Change)
OPTION_H = 28  # one shortcut in the list
RADIO = 14
RADIO_GAP = 9  # between the circle and the name
LIST_GAP = 6  # between the hint and the list
NOTE_GAP = 4
ACTIONS_GAP = 8  # between the note and Cancel / Save
ACTION_GAP = 6  # between Cancel and Save
TAG_H, TAG_PAD, TAG_GAP = 16, 6, 7  # the "Default" tag, like History's tags
SHORTCUT_HINT = "Hold {} to dictate"
SHORTCUT_BROKEN_HINT = "Hold to dictate"
SHORTCUT_PROBLEM_HINT = "Not allowed in config.json: using {}"
SHORTCUT_NOTE = "Other combinations clash with typing or Windows shortcuts, so they aren't offered."
SHORTCUT_DEFAULT_TAG = "Default"
SHORTCUT_CURRENT = "in use"
SHORTCUT_SUBTITLE = "Pick one, then Save"  # the header while the list is open


class SettingsPanel(Panel):
    NAME = "settings"
    TITLE = "Settings"
    FITS_SCREEN = True

    def __init__(self, path=None, on_quit=None, debug=False, key_status=None, on_change_key=None, autostart=None):
        self._path_arg = path
        self.on_quit = on_quit  # main.py: the same ordered quit as the tray's Quit
        # key_status() -> "windows", "env", None or "rejected"; on_change_key() opens the key window.
        # Without them (tests, tools) there's no API key row.
        self.key_status = key_status
        self.on_change_key = on_change_key
        # autostart: app/autostart.py (available(), enabled(), set_enabled(on)). Without it
        # (tests, tools) there's no Start with Windows row.
        self.autostart = autostart
        self._autostart_on = False  # read from the registry on open and every poll
        super().__init__(debug)

    def _extra_fonts(self):
        return {
            "row": _font(13, QFont.Weight.Medium),
            "hint": _font(11.5),
            "segment": _font(12, QFont.Weight.Medium),
            "value": _font(12, QFont.Weight.Medium),
            "button": _font(11.5, QFont.Weight.Medium),
            "problem_title": _font(12.5, QFont.Weight.DemiBold),
            "problem": _font(12),
            "tag": _font(10),
        }

    def _init_content(self):
        self.path = self._path_arg or CONFIG_PATH
        self._hover = None  # what the mouse is over: a control name, (name, value), "notepad", "banner_notepad", "quit"
        self._drag = None  # the slider's value while it's being dragged, else None
        self._quit_armed_until = 0.0
        self._note = None  # a save that failed ("Not saved: ..."), shown in the footer for a moment
        self._note_until = 0.0
        self._stamp = None  # config.json's (mtime, size) when last read
        self._shortcut_open = False  # the Shortcut row's list, open after Change
        self._shortcut_pick = None  # the shortcut picked in the list ("Alt+Win"), not saved yet
        self._hotkey_problem = False  # config.json's hotkey isn't allowed (the app uses Ctrl+Win)
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_MS)
        self._live_timer.timeout.connect(self._poll)
        self.reload()

    def _subtitle(self):
        # The shortcut is the one setting that waits for Save (panel.py paints this every frame).
        return SHORTCUT_SUBTITLE if self._shortcut_open else "Changes apply right away"

    # ---- data ----

    def _read_stamp(self):
        try:
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def reload(self):
        """Re-read config.json (it may have been changed in Notepad, broken or fixed)."""
        self._read_autostart()
        self._stamp = self._read_stamp()
        problems = []
        try:
            self._config, self._problem = load_config(self.path, problems), None
        except (OSError, ValueError):
            self._config, self._problem = None, config_problem(self.path) or "config.json couldn't be read."
            self._shortcut_open, self._shortcut_pick = False, None  # locked: the list closes
        # load_config put Ctrl+Win back for a hotkey that isn't allowed (or isn't text); its
        # message starts with the setting's name, like every _fix_kinds / _fix_values message.
        self._hotkey_problem = any(problem.startswith("hotkey ") for problem in problems)
        self._fit_scroll()  # a banner came or went: the content changed height
        self.update()

    def _on_open(self):
        self._hover = None
        self._quit_armed_until = 0.0
        self._note = None
        self._scroll.jump(0.0)  # always open at the top
        self.reload()
        self._live_timer.start()

    def _on_close(self):
        self._live_timer.stop()
        self._drag = None
        self._hover = None
        self._shortcut_open, self._shortcut_pick = False, None  # an unsaved pick is dropped

    def _poll(self):
        """While open: pick up a Notepad edit (or the file breaking or getting fixed), and let
        "Quit?" and a failed-save note expire. Never while the slider is held: it would jump."""
        now = _now_ms()
        if self._quit_armed_until and now > self._quit_armed_until:
            self._quit_armed_until = 0.0
            self.update()
        if self._note and now > self._note_until:
            self._note = None
            self.update()
        self._read_autostart()
        if self._drag is None and self._read_stamp() != self._stamp:
            self.reload()
            if self._mouse is not None:
                self._content_hover(self._mouse)  # rows may have moved (a banner came or went)

    def _read_autostart(self):
        """Start with Windows, from the registry (also changed by Task Manager's Startup tab)."""
        if self.autostart is None or not self.autostart.available():
            self._autostart_on = False
            return
        try:
            on = self.autostart.enabled()
        except OSError as e:
            print(f"[panel] settings: couldn't read Start with Windows ({e})")
            on = False
        if on != self._autostart_on:
            self._autostart_on = on
            self.update()

    def _value(self, key, section=None):
        """The value shown for a setting, or None while config.json is broken."""
        if self._config is None:
            return None
        return self._config[section][key] if section else self._config[key]

    def _languages(self):
        choices = list(LANGUAGES)
        language = self._value("language")
        if isinstance(language, str) and language not in dict(choices):
            choices.append((language, LANGUAGE_NAMES.get(language.lower(), language)))
        return choices

    def _hotkey(self):
        """The shortcut the app uses now, as config.json's text ("Ctrl+Win"), or None while it's broken."""
        return self._value("hotkey")

    def _shortcut_hint(self):
        hotkey = self._hotkey()
        if hotkey is None:
            return SHORTCUT_BROKEN_HINT  # no value while config.json is broken, like every other row
        name = shortcut.label(shortcut.parse(hotkey))
        return SHORTCUT_PROBLEM_HINT.format(name) if self._hotkey_problem else SHORTCUT_HINT.format(name)

    def _can_save_shortcut(self):
        """Save is on for a different pick, or for any pick while config.json's value isn't
        allowed (so picking Ctrl+Win writes a clean value)."""
        pick = self._shortcut_pick
        return pick is not None and (pick != self._hotkey() or self._hotkey_problem)

    def _opacity(self):
        if self._drag is not None:
            return self._drag
        value = self._value("idle_opacity", "pill")
        if not isinstance(value, (int, float)):
            return None
        return max(OPACITY_MIN, min(OPACITY_MAX, float(value)))

    # ---- layout ----
    # Two cards (dictation, then the pill), with a thin line between rows. A row is
    # a top line (label left, control right), then a short hint under it if it has one.
    # While the Shortcut list is open, it's the only card (so the panel gets shorter,
    # not taller: it fits small screens).

    def _groups(self):
        """[(name, kind, label, hint or None), ...] per card."""
        if self._shortcut_open:
            return [[("hotkey", "shortcut", "Shortcut", self._shortcut_hint())]]
        paste = self._value("paste_mode")
        key_row, autostart_row = [], []
        if self.key_status is not None:
            key_row = [("api_key", "button", "Groq API key", KEY_HINTS.get(self.key_status(), KEY_HINTS[None]))]
        if self.autostart is not None:
            hint = AUTOSTART_HINT if self.autostart.available() else AUTOSTART_SOURCE_HINT
            autostart_row = [("start_with_windows", "toggle", "Start with Windows", hint)]
        return [
            key_row + [
                ("hotkey", "shortcut", "Shortcut", self._shortcut_hint()),
                ("language", "segments", "Language", "Auto-detect can misread short clips"),
                ("paste_mode", "segments", "Paste", PASTE_HINTS.get(paste, "Smart or always")),
                ("keep_mic_open", "toggle", "Keep mic ready", "Off for Bluetooth headphones"),
                ("history", "toggle", "Save history", None),
            ] + autostart_row,
            [
                ("mic_label", "toggle", "Show mic name", None),
                ("idle_opacity", "slider", "Idle pill", None),
            ],
        ]

    def _choices(self, name):
        return self._languages() if name == "language" else PASTE_MODES

    def _segments_width(self, name):
        return sum(self._segment_widths(self._choices(name))) + 2 * SEG_INSET

    def _segments_below(self, name, label):
        """True when the buttons don't fit next to the label (e.g. a third language): they go under it."""
        row_w = WIDTH - 2 * PAD - 2 * CARD_PAD_X
        label_w = QFontMetricsF(self._fonts["row"]).horizontalAdvance(label)
        return label_w + 12 + self._segments_width(name) > row_w

    def _row_height(self, name, kind, label, hint):
        height = ROW_H
        if kind == "segments" and self._segments_below(name, label):
            height += SEG_H + 4
        if hint:
            height += HINT_GAP + self._line["hint"]
        if kind == "slider":
            height += 4 + PREVIEW_H  # the slider and its preview, under the top line
        if kind == "shortcut" and self._shortcut_open:
            height += self._shortcut_list_height()
        return height

    def _row_width(self):
        return WIDTH - 2 * PAD - 2 * CARD_PAD_X

    def _note_height(self):
        rect = QFontMetricsF(self._fonts["hint"]).boundingRect(
            QRectF(0, 0, self._row_width(), 1000), Qt.TextFlag.TextWordWrap, SHORTCUT_NOTE)
        return rect.height()

    def _shortcut_list_height(self):
        """The open list under the Shortcut row's hint: the five, the note, Cancel and Save."""
        return (LIST_GAP + len(shortcut.ALLOWED) * OPTION_H + NOTE_GAP + self._note_height()
                + ACTIONS_GAP + KEY_BUTTON_H)

    def _cards_height(self):
        """The cards and the footer under them. The same math as _layout."""
        groups = self._groups()
        height = (len(groups) - 1) * CARD_GAP + SECTION_GAP + FOOTER_H
        for group in groups:
            height += 2 * CARD_PAD_Y + (len(group) - 1) * (2 * ROW_SEP + 1)
            height += sum(self._row_height(name, kind, label, hint) for name, kind, label, hint in group)
        return height

    def _banner_block(self):
        """The broken-config banner's height plus the gap under it (0 when config.json is fine)."""
        if self._problem is None:
            return 0
        return self._problem_height() + CARD_GAP

    def _problem_lines(self):
        """The banner's text: where the mistake is, and what it means for this panel."""
        detail = (self._problem or "").removeprefix("config.json has a mistake on ")
        return detail[:1].upper() + detail[1:], "Settings are locked until it's fixed."

    def _problem_text_width(self):
        return WIDTH - 2 * PAD - 2 * BANNER_PAD - BANNER_ICON - 8

    def _wrapped_height(self, text, font):
        rect = QFontMetricsF(self._fonts[font]).boundingRect(
            QRectF(0, 0, self._problem_text_width(), 1000), Qt.TextFlag.TextWordWrap, text)
        return rect.height()

    def _problem_height(self):
        detail, _ = self._problem_lines()
        return (BANNER_PAD + self._line["problem_title"] + 2 + self._wrapped_height(detail, "problem")
                + self._line["hint"] + 10 + BUTTON_H + BANNER_PAD)

    def _content_height(self):
        return self._banner_block() + self._cards_height()

    def _max_content_height(self):
        # The tallest case: a broken config.json with a long message (which closes the
        # Shortcut list; the list open is shorter than the closed panel).
        saved = self._problem, self._shortcut_open
        self._problem = "config.json has a mistake on line 188 (or the end of line 187): probably an extra comma " \
                        "before a }, or a name without double quotes."
        self._shortcut_open = False
        height = self._content_height()
        self._problem, self._shortcut_open = saved
        return height

    def _top(self):
        """Where the content starts: under the header, moved up by the scroll."""
        card = self._card_rect()
        return card.top() + PAD + self._header_height() + SECTION_GAP - self._scroll.value

    def _layout(self):
        """{"rows": {name: (kind, row rect, control rect)}, "cards": [rect], "dividers": [rect],
        "footer": (rect, quit rect)}."""
        panel = self._card_rect()
        x, width = panel.left() + PAD, WIDTH - 2 * PAD
        y = self._top() + self._banner_block()
        rows, cards, dividers = {}, [], []
        for group in self._groups():
            card_top = y
            y += CARD_PAD_Y
            for i, (name, kind, label, hint) in enumerate(group):
                row = QRectF(x + CARD_PAD_X, y, width - 2 * CARD_PAD_X, self._row_height(name, kind, label, hint))
                rows[name] = (kind, row, self._control_rect(name, kind, label, row, hint))
                y = row.bottom()
                if i < len(group) - 1:
                    dividers.append(QRectF(row.left(), y + ROW_SEP, row.width(), 1))
                    y += 2 * ROW_SEP + 1
            y += CARD_PAD_Y
            cards.append(QRectF(x, card_top, width, y - card_top))
            y += CARD_GAP
        footer = QRectF(x, y - CARD_GAP + SECTION_GAP, width, FOOTER_H)
        return {"rows": rows, "cards": cards, "dividers": dividers, "footer": (footer, self._quit_rect(footer))}

    def _key_button_text(self):
        return "Add" if self.key_status is not None and self.key_status() is None else "Change"

    def _segment_widths(self, choices):
        metrics = QFontMetricsF(self._fonts["segment"])
        return [metrics.horizontalAdvance(label) + 2 * SEG_TEXT_PAD for _, label in choices]

    def _control_rect(self, name, kind, label, row, hint):
        right = row.right()
        center_y = row.top() + ROW_H / 2
        if kind == "toggle":
            return QRectF(right - TOGGLE_W, center_y - TOGGLE_H / 2, TOGGLE_W, TOGGLE_H)
        if kind == "segments":
            width = self._segments_width(name)
            if self._segments_below(name, label):
                return QRectF(row.left(), row.top() + ROW_H + 2, width, SEG_H)
            return QRectF(right - width, center_y - SEG_H / 2, width, SEG_H)
        if kind in ("button", "shortcut"):
            text = self._key_button_text() if kind == "button" else "Change"
            width = QFontMetricsF(self._fonts["button"]).horizontalAdvance(text) + 2 * KEY_BUTTON_PAD
            return QRectF(right - width, center_y - KEY_BUTTON_H / 2, width, KEY_BUTTON_H)
        if kind == "slider":
            top = row.top() + ROW_H + (HINT_GAP + self._line["hint"] if hint else 0) + 4
            preview_w = 2 * PREVIEW_W + PREVIEW_GAP
            return QRectF(row.left(), top + PREVIEW_H / 2 - SLIDER_H / 2, row.width() - preview_w - 14, SLIDER_H)
        return None

    def _segment_rects(self, name, control):
        choices = self._choices(name)
        x = control.left() + SEG_INSET
        rects = []
        for (value, label), width in zip(choices, self._segment_widths(choices)):
            rects.append((value, label, QRectF(x, control.top() + SEG_INSET, width, SEG_H - 2 * SEG_INSET)))
            x += width
        return rects

    def _shortcut_rects(self, row):
        """The open list's parts: {"options": [(text, label, rect)], "note": rect, "cancel": rect, "save": rect}."""
        y = row.top() + ROW_H + HINT_GAP + self._line["hint"] + LIST_GAP
        options = []
        for families in shortcut.ALLOWED:
            options.append((shortcut.text(families), shortcut.label(families),
                            QRectF(row.left() - 6, y, row.width() + 12, OPTION_H)))
            y += OPTION_H
        y += NOTE_GAP
        note = QRectF(row.left(), y, row.width(), self._note_height())
        y = note.bottom() + ACTIONS_GAP
        metrics = QFontMetricsF(self._fonts["button"])
        save_w = metrics.horizontalAdvance("Save") + 2 * KEY_BUTTON_PAD
        cancel_w = metrics.horizontalAdvance("Cancel") + 2 * KEY_BUTTON_PAD
        save = QRectF(row.right() - save_w, y, save_w, KEY_BUTTON_H)
        cancel = QRectF(save.left() - ACTION_GAP - cancel_w, y, cancel_w, KEY_BUTTON_H)
        return {"options": options, "note": note, "cancel": cancel, "save": save}

    def _quit_rect(self, footer):
        text = "Quit?" if self._quit_armed_until else "Quit"
        width = QFontMetricsF(self._fonts["button"]).horizontalAdvance(text) + 2 * QUIT_PAD
        return QRectF(footer.right() - width, footer.top(), width, FOOTER_H)

    def _notepad_rect(self):
        close = self._close_rect()
        return QRectF(close.left() - 2 - HEADER_BUTTON, close.top(), HEADER_BUTTON, HEADER_BUTTON)

    def _banner_rect(self):
        panel = self._card_rect()
        return QRectF(panel.left() + PAD, self._top(), WIDTH - 2 * PAD, self._problem_height())

    def _banner_button_rect(self):
        banner = self._banner_rect()
        label = "Open in Notepad"
        width = BUTTON_PAD_LEFT + BUTTON_ICON + 5 + QFontMetricsF(self._fonts["button"]).horizontalAdvance(label) \
            + BUTTON_PAD_RIGHT
        left = banner.left() + BANNER_PAD + BANNER_ICON + 8
        return QRectF(left, banner.bottom() - BANNER_PAD - BUTTON_H, width, BUTTON_H)

    # ---- mouse ----

    def _content_target(self, pos):
        if self._notepad_rect().contains(pos):
            return "notepad"
        if self._max_scroll() > 0:
            view = self._view_rect()
            if not view.top() <= pos.y() <= view.bottom():
                return None  # scrolled out of sight (under the header, or below the card)
        if self._problem is not None and self._banner_button_rect().contains(pos):
            return "banner_notepad"
        layout = self._layout()
        if layout["footer"][1].contains(pos):
            return "quit"
        key_row = layout["rows"].get("api_key")
        if key_row is not None and key_row[2].adjusted(-4, -4, 4, 4).contains(pos):
            return "api_key"  # the key isn't in config.json: works even while it's broken
        autostart_row = layout["rows"].get("start_with_windows")
        if (autostart_row is not None and self.autostart.available()
                and autostart_row[2].adjusted(-6, -6, 6, 6).contains(pos)):
            return "start_with_windows"  # in the registry, not config.json: works while it's broken
        if self._config is None:
            return None  # locked: a click can never save over a broken config.json
        shortcut_row = layout["rows"].get("hotkey")
        if shortcut_row is not None:
            target = self._shortcut_target(pos, shortcut_row)
            if target is not None:
                return target
        for name, (kind, _, control) in layout["rows"].items():
            if name in ("api_key", "start_with_windows", "hotkey"):
                continue  # handled above (from source, Start with Windows does nothing)
            if kind == "toggle" and control.adjusted(-6, -6, 6, 6).contains(pos):
                return name
            if kind == "segments" and control.contains(pos):
                for value, _, rect in self._segment_rects(name, control):
                    if rect.contains(pos):
                        return (name, value)
            if kind == "slider" and control.adjusted(-SLIDER_KNOB_DRAG / 2, 0, SLIDER_KNOB_DRAG / 2, 0).contains(pos):
                return name
        return None

    def _shortcut_target(self, pos, shortcut_row):
        """Closed: "hotkey" (the Change button). Open: ("hotkey_option", "Alt+Win"), "hotkey_cancel",
        or "hotkey_save" (only while Save is on)."""
        _, row, control = shortcut_row
        if not self._shortcut_open:
            return "hotkey" if control.adjusted(-4, -4, 4, 4).contains(pos) else None
        parts = self._shortcut_rects(row)
        if parts["cancel"].adjusted(-4, -4, 4, 4).contains(pos):
            return "hotkey_cancel"
        if parts["save"].adjusted(-4, -4, 4, 4).contains(pos):
            return "hotkey_save" if self._can_save_shortcut() else None
        for text, _, rect in parts["options"]:
            if rect.contains(pos):
                return ("hotkey_option", text)
        return None

    def _open_shortcut_list(self, is_open):
        """Change opens the list (the one in use picked); Cancel and Save close it."""
        self._shortcut_open = is_open
        self._shortcut_pick = self._hotkey() if is_open else None
        self._hover = None
        self._fit_scroll()  # the card changed height
        if self._mouse is not None:
            self._content_hover(self._mouse)  # the card changed size under the mouse
        self.update()

    def _save_shortcut(self):
        """Write the picked shortcut in config.json's canonical form. The worker notices the
        file within 1s and switches the hook between key presses (main.apply_new_hotkey), the
        same way as a Notepad edit or tools/config_tool.py. A failed save keeps the list open."""
        if not self._can_save_shortcut():
            return
        text = shortcut.text(shortcut.parse(self._shortcut_pick))  # always one of the five
        if self._save("hotkey", text):
            self._open_shortcut_list(False)

    def _content_hover(self, pos):
        if self._drag is not None:
            target = "idle_opacity"
        else:
            target = None if pos is None else self._content_target(pos)
        if target != self._hover:
            self._hover = target
            self.update()

    def _slider_value(self, pos):
        control = self._layout()["rows"]["idle_opacity"][2]
        t = max(0.0, min(1.0, (pos.x() - control.left()) / control.width()))
        return round(OPACITY_MIN + t * (OPACITY_MAX - OPACITY_MIN), 2)

    def mousePressEvent(self, event):
        pos = self._to_design(event.position())
        if self._open and self._content_target(pos) == "idle_opacity":
            # The slider follows the mouse from the press, even outside the panel: Qt keeps
            # sending the moves here until the button goes up, which doesn't need focus.
            self._pressed = None
            self._drag = self._slider_value(pos)
            self._hover = "idle_opacity"
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            value = self._slider_value(self._to_design(event.position()))
            if value != self._drag:
                self._drag = value
                self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            value, self._drag = self._drag, None
            self._save("idle_opacity", value, section="pill")  # written once, on release
            self._content_hover(self._to_design(event.position()))
            QTimer.singleShot(100, self._check_focus)
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._drag is not None:
            event.accept()  # scrolling while the slider is held would move another row under the mouse
            return
        super().wheelEvent(event)

    def _content_release(self, target):
        if target in ("notepad", "banner_notepad"):
            self._open_in_notepad()
        elif target == "api_key":
            self.close_panel()  # the key window takes focus; the panel shouldn't sit over it
            if self.on_change_key is not None:
                self.on_change_key()
        elif target == "quit":
            if self._quit_armed_until:
                self._quit_armed_until = 0.0
                if self.debug:
                    print("[panel] settings: Quit")
                if self.on_quit is not None:
                    self.on_quit()
            else:
                self._quit_armed_until = _now_ms() + QUIT_CONFIRM_MS
                self.update()
        elif target == "hotkey":
            self._open_shortcut_list(True)
        elif target == "hotkey_cancel":
            self._open_shortcut_list(False)
        elif target == "hotkey_save":
            self._save_shortcut()
        elif isinstance(target, tuple) and target[0] == "hotkey_option":
            self._shortcut_pick = target[1]
            self.update()
        elif isinstance(target, tuple):
            name, value = target
            if value != self._value(name):
                self._save(name, value)
        elif target == "start_with_windows":
            self._toggle_autostart()
        elif target in ("keep_mic_open", "history"):
            self._save(target, not bool(self._value(target)))
        elif target == "mic_label":
            self._save(target, not bool(self._value(target, "pill")), section="pill")

    def _save(self, key, value, section=None):
        """Write one setting (set_setting refuses a broken file), then show what the file says now.
        True if it was saved."""
        name = f"{section}.{key}" if section else key
        saved = False
        try:
            set_setting(key, value, section=section, path=self.path)
            saved = True
        except ConfigError as e:
            print(f"[panel] settings: {name} not saved. {e}")
            self._note = "Not saved: " + ("config.json is busy, try again" if "busy" in str(e) else "see the console")
            self._note_until = _now_ms() + NOTE_MS
        else:
            if self.debug:
                print(f"[panel] settings: {name} = {value!r}")
        self.reload()
        return saved

    def _toggle_autostart(self):
        if not self.autostart.available():
            return  # from source or a test run: never touch the startup list
        on = not self._autostart_on
        try:
            self.autostart.set_enabled(on)
        except OSError as e:
            print(f"[panel] settings: Start with Windows not changed ({e})")
            self._note = "Not saved: couldn't change Start with Windows"
            self._note_until = _now_ms() + NOTE_MS
        else:
            if self.debug:
                print(f"[panel] settings: start with Windows = {on}")
        self._read_autostart()
        self.update()

    def _open_in_notepad(self):
        self.close_panel()  # Notepad takes focus, which is fine: you chose to go there
        try:
            subprocess.Popen(["notepad.exe", str(self.path)])
        except OSError as e:
            print(f"[panel] settings: couldn't open Notepad ({e})")

    # ---- painting ----

    def _fill(self, painter, key, rect, radius=None):
        rgb, alpha = PALETTE[key]
        painter.setBrush(_color(rgb, alpha))
        radius = rect.height() / 2 if radius is None else radius
        painter.drawRoundedRect(rect, radius, radius)

    def _say(self, painter, rect, text, font, key, align=Qt.AlignmentFlag.AlignLeft):
        rgb, alpha = PALETTE[key]
        self._text(painter, rect, text, font, rgb, alpha, align)

    def _paint_header_extra(self, painter, x, y, width):
        button = self._notepad_rect()
        if self._hover == "notepad":
            painter.setBrush(_color(WHITE, 0.18))
            painter.drawEllipse(button)
        self._icon(painter, "file", HEADER_ICON, button.center())

    def _paint_content(self, painter, x, y, width):
        self._paint_fitted(painter, lambda: self._paint_rows(painter))  # _top() already follows the scroll

    def _paint_rows(self, painter):
        """Everything under the header: the banner, the cards and the footer."""
        if self._problem is not None:
            self._paint_problem(painter)
        layout = self._layout()
        for card in layout["cards"]:
            self._fill(painter, "card", card, CARD_RADIUS)
        rgb, alpha = PALETTE["divider"]
        for divider in layout["dividers"]:
            painter.fillRect(divider, _color(rgb, alpha))
        broken = self._config is None
        for group in self._groups():
            for name, _, label, hint in group:
                kind, row, control = layout["rows"][name]
                self._say(painter, QRectF(row.left(), row.top(), row.width(), ROW_H), label, "row", "label")
                if hint:
                    if kind == "shortcut":  # right under the label: the open list comes after it
                        hint_top = row.top() + ROW_H + HINT_GAP
                        color = "warning" if self._hotkey_problem and not broken else "hint"
                    else:
                        hint_top = row.bottom() - self._line["hint"]
                        color = "rejected" if name == "api_key" and self.key_status() == "rejected" else "hint"
                    self._say(painter, QRectF(row.left(), hint_top, row.width(), self._line["hint"]), hint, "hint", color)
                painter.save()
                if name == "start_with_windows":
                    if not self.autostart.available():  # from source: shown, but it does nothing
                        painter.setOpacity(painter.opacity() * DIMMED)
                    self._paint_toggle(painter, control, self._autostart_on, self._hover == name)
                    painter.restore()
                    continue
                if broken and kind != "button":
                    painter.setOpacity(painter.opacity() * DIMMED)
                if kind == "button":
                    self._fill(painter, "button_hover" if self._hover == "api_key" else "button", control)
                    self._say(painter, control, self._key_button_text(), "button", "button_text",
                              Qt.AlignmentFlag.AlignHCenter)
                elif kind == "shortcut":
                    self._paint_shortcut(painter, row, control)
                elif kind == "toggle":
                    section = "pill" if name == "mic_label" else None
                    value = self._value(name, section)
                    self._paint_toggle(painter, control, None if value is None else bool(value), self._hover == name)
                elif kind == "segments":
                    self._paint_segments(painter, name, control)
                else:
                    self._paint_slider(painter, row, control)
                painter.restore()
        self._paint_footer(painter, *layout["footer"])

    def _paint_shortcut(self, painter, row, control):
        """Closed: the Change button. Open: the five as a list, the note, Cancel and Save."""
        center = Qt.AlignmentFlag.AlignHCenter
        if not self._shortcut_open:
            self._fill(painter, "button_hover" if self._hover == "hotkey" else "button", control)
            self._say(painter, control, "Change", "button", "button_text", center)
            return
        parts = self._shortcut_rects(row)
        current = self._hotkey()
        name_metrics = QFontMetricsF(self._fonts["segment"])
        tag_metrics = QFontMetricsF(self._fonts["tag"])
        for text, label, rect in parts["options"]:
            if self._hover == ("hotkey_option", text):
                self._fill(painter, "seg_hover", rect, 8)
            self._paint_radio(painter, QPointF(row.left() + RADIO / 2, rect.center().y()),
                              text == self._shortcut_pick)
            x = row.left() + RADIO + RADIO_GAP
            name_w = name_metrics.horizontalAdvance(label)
            self._say(painter, QRectF(x, rect.top(), name_w + 2, OPTION_H), label, "segment", "seg_text")
            if text == shortcut.text(shortcut.DEFAULT):
                tag_w = tag_metrics.horizontalAdvance(SHORTCUT_DEFAULT_TAG) + 2 * TAG_PAD
                tag = QRectF(x + name_w + TAG_GAP, rect.center().y() - TAG_H / 2, tag_w, TAG_H)
                self._fill(painter, "tag", tag)
                self._say(painter, tag, SHORTCUT_DEFAULT_TAG, "tag", "tag_text", center)
            if text == current:
                self._say(painter, QRectF(row.left(), rect.top(), row.width(), OPTION_H), SHORTCUT_CURRENT,
                          "hint", "hint", Qt.AlignmentFlag.AlignRight)
        painter.setFont(self._fonts["hint"])
        painter.setPen(_color(*PALETTE["hint"]))
        painter.drawText(parts["note"], Qt.TextFlag.TextWordWrap, SHORTCUT_NOTE)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._hover == "hotkey_cancel":
            self._fill(painter, "seg_hover", parts["cancel"])
        self._say(painter, parts["cancel"], "Cancel", "button", "link", center)
        painter.save()
        can_save = self._can_save_shortcut()
        if not can_save:
            painter.setOpacity(painter.opacity() * DIMMED)
        self._fill(painter, "button_hover" if can_save and self._hover == "hotkey_save" else "button", parts["save"])
        self._say(painter, parts["save"], "Save", "button", "button_text", center)
        painter.restore()

    def _paint_radio(self, painter, center, on):
        if on:
            self._fill(painter, "on_track", QRectF(center.x() - RADIO / 2, center.y() - RADIO / 2, RADIO, RADIO))
            dot = 6
            self._fill(painter, "on_knob", QRectF(center.x() - dot / 2, center.y() - dot / 2, dot, dot))
            return
        rgb, alpha = PALETTE["radio_ring"]
        painter.setPen(QPen(_color(rgb, alpha), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = RADIO / 2 - 0.75
        painter.drawEllipse(center, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)

    def _paint_toggle(self, painter, rect, on, hover):
        inset = (TOGGLE_H - KNOB) / 2
        knob_y = rect.center().y() - KNOB / 2
        if hover and on is not None:
            self._fill(painter, "halo", rect.adjusted(-4, -4, 4, 4))  # hovering shows on either side
        if on is True:
            self._fill(painter, "on_track", rect)
            self._fill(painter, "on_knob", QRectF(rect.right() - inset - KNOB, knob_y, KNOB, KNOB))
            return
        # Off (or unknown): a quiet track, the knob on the left (none when unknown).
        self._fill(painter, "off_track", rect)
        if on is False:
            self._fill(painter, "off_knob", QRectF(rect.left() + inset, knob_y, KNOB, KNOB))

    def _paint_segments(self, painter, name, control):
        current = self._value(name)
        self._fill(painter, "seg_track", control)
        for value, label, rect in self._segment_rects(name, control):
            if value == current:
                self._fill(painter, "seg_on", rect)
                self._say(painter, rect, label, "segment", "seg_on_text", Qt.AlignmentFlag.AlignHCenter)
                continue
            if self._hover == (name, value):
                self._fill(painter, "seg_hover", rect)
            self._say(painter, rect, label, "segment", "seg_text", Qt.AlignmentFlag.AlignHCenter)

    def _paint_slider(self, painter, row, control):
        opacity = self._opacity()
        if opacity is not None:
            self._say(painter, QRectF(row.left(), row.top(), row.width(), ROW_H), f"{round(opacity * 100)}%", "value",
                      "label", Qt.AlignmentFlag.AlignRight)
        track = QRectF(control.left(), control.center().y() - TRACK_H / 2, control.width(), TRACK_H)
        self._fill(painter, "slider_track", track)
        if opacity is not None:
            t = (opacity - OPACITY_MIN) / (OPACITY_MAX - OPACITY_MIN)
            knob_x = track.left() + t * track.width()
            self._fill(painter, "slider_fill", QRectF(track.left(), track.top(), knob_x - track.left(), TRACK_H))
            size = SLIDER_KNOB_DRAG if self._drag is not None else SLIDER_KNOB
            if self._drag is not None or self._hover == "idle_opacity":
                halo = size + 10
                self._fill(painter, "halo", QRectF(knob_x - halo / 2, track.center().y() - halo / 2, halo, halo))
            self._fill(painter, "slider_knob", QRectF(knob_x - size / 2, track.center().y() - size / 2, size, size))
        # The preview: the idle pill at its real size, on a light and a dark strip.
        x = row.right() - (2 * PREVIEW_W + PREVIEW_GAP)
        y = control.center().y() - PREVIEW_H / 2
        for background in (PREVIEW_LIGHT, PREVIEW_DARK):
            strip = QRectF(x, y, PREVIEW_W, PREVIEW_H)
            painter.setBrush(QColor(*background))
            painter.drawRoundedRect(strip, 6, 6)
            if opacity is not None:
                painter.setBrush(_color(BLUE, opacity))
                pill = QRectF(strip.center().x() - IDLE_W / 2, strip.center().y() - IDLE_H / 2, IDLE_W, IDLE_H)
                painter.drawRoundedRect(pill, IDLE_H / 2, IDLE_H / 2)
            x += PREVIEW_W + PREVIEW_GAP

    def _paint_problem(self, painter):
        """The broken-config banner: a light card like the rows, with a dark amber "!" and dark text."""
        banner = self._banner_rect()
        self._fill(painter, "card", banner, CARD_RADIUS)
        line = self._line
        icon_x = banner.left() + BANNER_PAD
        text_x = icon_x + BANNER_ICON + 8
        text_w = self._problem_text_width()
        y = banner.top() + BANNER_PAD
        self._icon(painter, "alert", BANNER_ICON, QPointF(icon_x + BANNER_ICON / 2, y + line["problem_title"] / 2),
                   _css(PALETTE["warning"][0]))
        self._say(painter, QRectF(text_x, y, text_w, line["problem_title"]), "config.json has a mistake",
                  "problem_title", "label")
        y += line["problem_title"] + 2
        detail, locked = self._problem_lines()
        detail_h = self._wrapped_height(detail, "problem")
        painter.setFont(self._fonts["problem"])
        painter.setPen(_color(*PALETTE["label"]))
        painter.drawText(QRectF(text_x, y, text_w, detail_h), Qt.TextFlag.TextWordWrap, detail)
        painter.setPen(Qt.PenStyle.NoPen)
        y += detail_h
        self._say(painter, QRectF(text_x, y, text_w, line["hint"]), locked, "hint", "hint")
        button = self._banner_button_rect()
        self._fill(painter, "button_hover" if self._hover == "banner_notepad" else "button", button)
        self._icon(painter, "file", BUTTON_ICON,
                   QPointF(button.left() + BUTTON_PAD_LEFT + BUTTON_ICON / 2, button.center().y()))
        label_x = button.left() + BUTTON_PAD_LEFT + BUTTON_ICON + 5
        self._say(painter, QRectF(label_x, button.top(), button.right() - label_x, BUTTON_H), "Open in Notepad",
                  "button", "button_text")

    def _paint_footer(self, painter, footer, quit_rect):
        armed = bool(self._quit_armed_until)
        left = QRectF(footer.left(), footer.top(), footer.width() - quit_rect.width() - 8, FOOTER_H)
        if self._note:
            self._text(painter, left, self._note, "button", AMBER)  # amber on the blue: 4.8:1
        else:
            self._text(painter, left, f"Blurt {VERSION} · More in config.json", "button")
        if armed or self._hover == "quit":
            painter.setBrush(_color(WHITE, 0.18))
            painter.drawRoundedRect(quit_rect, FOOTER_H / 2, FOOTER_H / 2)
        self._text(painter, quit_rect, "Quit?" if armed else "Quit", "button", align=Qt.AlignmentFlag.AlignHCenter)
