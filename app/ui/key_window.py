"""The API key window: paste your Groq key and pick a language. Opens on first run
and from "Change API key" in Settings.

Unlike the pill and the panels, this is a normal window that takes focus, with
a title bar and a taskbar button: typing and pasting need focus. That's safe
because dictation is paused while it's open (main.py), so a dictation can never
be pasted into it. Built from real Qt widgets (a real text box, so Ctrl+V,
selecting and screen readers all work), in the panels' look: the blue body with
light cards (Settings, M9).

States (set_status): "empty", "not_groq" (a soft warning, never a block),
"checking", "works", "rejected", "unreachable" (then Save becomes "Save anyway"),
"save_failed" (Credential Manager said no), "invalid" (too long, or characters a key
never has: refused here, never sent to Groq). The status line keeps room for two
lines, so the window never changes size.

KeyFlow (below) runs it: open, check the key on a background thread, save it,
switch the app to it, close, and give focus back.
"""

import threading

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from app import keystore
from app.config import ConfigError, load_config_or_defaults, set_setting
from app.keystore import KEY_PREFIX, MAX_KEY_LENGTH, clean_key, is_impossible_key, looks_like_groq_key
from app.paths import APP_DISPLAY_NAME
from app.ui.history_panel import NAVY, SLATE, _css
from app.ui.history_panel import PALETTE as HISTORY_PALETTE
from app.ui.icons import icon_image
from app.ui import win32
from app.ui.panel import AMBER
from app.ui.pill import BLUE
from app.ui.settings_panel import LANGUAGE_NAMES

GET_KEY_URL = "https://console.groq.com/keys"
WIDTH = 380
PAD = 20
# First run only (M17): Windows gives no error when two apps watch the same keys, so say it here.
CONFLICT_TITLE = "Close other dictation apps first"
CONFLICT_BODY = "Apps like WisprFlow use the same keys as Blurt. If both are open, Blurt may stop working."
# First run only (M17 item 14), one line in the Language card (a longer sentence wraps and the window grows).
DICTIONARY_TIP = "Misspelled names or rare words? Add them in Dictionary."
CARD_PAD = 14
CALLOUT_PAD = 12
CALLOUT_ICON = 18
CALLOUT_ICON_GAP = 10
FIELD_H = 34

C_BLUE = _css(BLUE)
C_NAVY = _css(NAVY)
C_SLATE = _css(SLATE)
C_CARD = _css(HISTORY_PALETTE["card"][0])
C_FAILED = _css(HISTORY_PALETTE["failed"][0])  # dark orange: "rejected"
C_LIMITED = _css(HISTORY_PALETTE["limited"][0])  # dark amber: warnings
C_BLUE_HOVER = _css(HISTORY_PALETTE["retry_hover"][0])
# The warning callout: History's warm card color with the usage bars' amber edge. Measured
# contrast on it: navy 14.7:1, slate 7.1:1, the dark amber icon 5.1:1.
C_WARM = _css(HISTORY_PALETTE["highlight"][0])
C_AMBER = _css(AMBER)
FONT = "'Segoe UI Variable Text', 'Segoe UI Variable', 'Segoe UI'"
# Qt finds only Regular and Bold in Segoe UI Variable, so weight 600 there is drawn as Bold, and
# at 13px its strokes come out uneven (measured, M17). Classic Segoe UI has a real Semibold face:
# even strokes, and still clearly heavier than the body. Its real Bold face (segoeuib.ttf) clumps
# the same way at 13 and 14px, so the title gets its extra weight from 14px Semibold instead.
# Only the callout title uses it.
CALLOUT_TITLE_FONT = "'Segoe UI'"

# (icon, icon color, text color, text) for each state; {prefix} is the key prefix.
STATUS = {
    "empty": (None, None, C_SLATE, "Starts with {prefix}. Saved encrypted in Windows, for your account only."),
    "not_groq": ("alert", C_LIMITED, C_LIMITED, "This doesn't look like a Groq key. They start with {prefix}."),
    "checking": ("hourglass", C_SLATE, C_SLATE, "Checking the key with Groq…"),
    "works": ("check", C_BLUE, C_NAVY, "Key works. Saved."),
    "rejected": ("alert", C_FAILED, C_FAILED,
                 "Groq rejected this key. Copy it again: it may be cut short, or deleted in the Groq console."),
    "unreachable": ("alert", C_LIMITED, C_LIMITED,
                    "Couldn't reach Groq, so the key wasn't checked. Check your internet, or save it anyway."),
    "save_failed": ("alert", C_FAILED, C_FAILED, "Couldn't save the key in Windows. Try Save again."),
    "invalid": ("alert", C_FAILED, C_FAILED,
                "This can't be an API key: it's too long, or has characters keys never have."),
}
CLOSE_DELAY_MS = 900  # "Key works. Saved." stays this long before the window closes
FOCUS_CHECK_MS = 150
SAVE_TEXT = {"checking": "Checking…", "works": "Saved", "unreachable": "Save anyway"}

LANGUAGES = [("en", "English"), ("auto", "Auto-detect")]

STYLE = f"""
QWidget#key_window {{ background: {C_BLUE}; }}
QLabel {{ font-family: {FONT}; }}
QLabel#title {{ color: white; font-size: 18px; font-weight: 600; }}
QLabel#subtitle {{ color: rgba(255, 255, 255, 0.82); font-size: 12.5px; }}
QFrame#card {{ background: {C_CARD}; border-radius: 10px; }}
QFrame#callout {{ background: {C_WARM}; border: 1px solid {C_AMBER}; border-radius: 10px; }}
QLabel#callout_title {{ color: {C_NAVY}; font-family: {CALLOUT_TITLE_FONT}; font-size: 14px; font-weight: 600; }}
QLabel#callout_body {{ color: {C_SLATE}; font-size: 12px; }}
QLabel#label {{ color: {C_NAVY}; font-size: 13px; font-weight: 500; }}
QLabel#hint {{ color: {C_SLATE}; font-size: 11.5px; }}
QLabel#status {{ font-size: 12px; }}
QLineEdit#key {{
    background: white; color: {C_NAVY}; font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12.5px;
    border: 1px solid rgba(68, 79, 107, 0.35); border-radius: 8px; padding: 0 5px 0 9px;
    selection-background-color: {C_BLUE}; selection-color: white;
}}
QLineEdit#key:focus {{ border: 2px solid {C_BLUE}; padding: 0 4px 0 8px; }}
QLineEdit#key[state="rejected"] {{ border: 2px solid {C_FAILED}; padding: 0 4px 0 8px; }}
QLineEdit#key:read-only {{ color: {C_SLATE}; }}
QPushButton {{ font-family: {FONT}; }}
QPushButton#paste {{
    background: {C_BLUE}; color: white; font-size: 12px; font-weight: 500;
    border: none; border-radius: 8px; padding: 0 12px 0 9px;
}}
QPushButton#paste:hover {{ background: {C_BLUE_HOVER}; }}
QPushButton#paste:disabled {{ background: rgba(19, 67, 208, 0.4); }}
QPushButton#link {{
    background: transparent; color: {C_BLUE}; font-size: 12px; font-weight: 500;
    border: none; border-radius: 6px; padding: 3px 6px;
}}
QPushButton#link:hover {{ background: rgba(19, 67, 208, 0.10); }}
QFrame#segments {{ background: rgba(16, 27, 63, 0.07); border-radius: 8px; }}
QPushButton#segment {{
    background: transparent; color: {C_NAVY}; font-size: 12px; font-weight: 500;
    border: none; border-radius: 6px; padding: 0 11px;
}}
QPushButton#segment:hover {{ background: rgba(19, 67, 208, 0.10); }}
QPushButton#segment:checked {{ background: {C_BLUE}; color: white; }}
QPushButton#save {{
    background: white; color: {C_BLUE}; font-size: 13px; font-weight: 600;
    border: none; border-radius: 17px; padding: 0 20px;
}}
QPushButton#save:hover {{ background: #e3e9fb; }}
QPushButton#save:disabled {{ background: rgba(255, 255, 255, 0.45); color: rgba(19, 67, 208, 0.75); }}
QPushButton#later {{
    background: transparent; color: white; font-size: 13px; font-weight: 500;
    border: none; border-radius: 17px; padding: 0 14px;
}}
QPushButton#later:hover {{ background: rgba(255, 255, 255, 0.14); }}
"""


def _icon(name, size, color):
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    return QIcon(QPixmap.fromImage(icon_image(name, size, ratio, color)))


def _label(text, name, wrap=False):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


class KeyWindow(QWidget):
    """save_requested(key, language) is sent when Save is clicked (main.py checks and saves
    the key, then calls set_status). closed_without_saving when it's closed any other way."""

    save_requested = Signal(str, str)
    closing = Signal()  # sent on any close, while the window is still the active one

    def __init__(self, first_run=True, language="en", window_icon=None):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowTitleHint
                         | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMinimizeButtonHint
                         | Qt.WindowType.CustomizeWindowHint)
        self.first_run = first_run
        self.status = "empty"
        self._shown_key = False
        self.setObjectName("key_window")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.setWindowTitle(APP_DISPLAY_NAME if first_run else f"{APP_DISPLAY_NAME} API key")
        if window_icon is not None:
            self.setWindowIcon(window_icon)

        root = QVBoxLayout(self)
        root.setContentsMargins(PAD, PAD - 2, PAD, PAD)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        # Header
        title = _label(f"Welcome to {APP_DISPLAY_NAME}" if first_run else "Change your Groq API key", "title")
        subtitle = _label(
            f"{APP_DISPLAY_NAME} uses Groq, a fast AI service, to turn what you say into text. "
            "It needs your own Groq API key, which is free."
            if first_run else f"Paste a new key to replace the one {APP_DISPLAY_NAME} uses now.",
            "subtitle", wrap=True)
        subtitle.setFixedWidth(WIDTH - 2 * PAD)
        root.addWidget(title)
        root.addSpacing(4)
        root.addWidget(subtitle)
        root.addSpacing(16)

        # Card 1: the key
        card = self._card()
        layout = card.layout()
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(_label("Groq API key", "label"))
        top.addStretch(1)
        self.link = QPushButton("Get a free key")
        self.link.setObjectName("link")
        self.link.setIcon(_icon("external", 13, C_BLUE))
        self.link.setIconSize(QSize(13, 13))
        self.link.setLayoutDirection(Qt.LayoutDirection.RightToLeft)  # the icon after the text
        self.link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.link.setToolTip(GET_KEY_URL)
        self.link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GET_KEY_URL)))
        top.addWidget(self.link)
        layout.addLayout(top)
        layout.addSpacing(8)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.field = QLineEdit()
        self.field.setObjectName("key")
        self.field.setFixedHeight(FIELD_H)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setMaxLength(MAX_KEY_LENGTH + 1)  # a pasted document is cut, and still shows as too long
        self.field.setPlaceholderText(f"{KEY_PREFIX}…")
        self.field.setClearButtonEnabled(False)
        self.field.setTextMargins(0, 0, 8, 0)  # the text stops short of the eye icon
        self.field.textEdited.connect(self._edited)
        self.field.returnPressed.connect(self._save_clicked)
        self._eye = QAction(self)
        self._eye.triggered.connect(lambda: self.show_key(not self._shown_key))
        self.field.addAction(self._eye, QLineEdit.ActionPosition.TrailingPosition)
        row.addWidget(self.field, 1)
        self.paste_button = QPushButton("Paste key")
        self.paste_button.setObjectName("paste")
        self.paste_button.setFixedHeight(FIELD_H)
        self.paste_button.setIcon(_icon("paste", 14, "#ffffff"))
        self.paste_button.setIconSize(QSize(14, 14))
        self.paste_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_button.clicked.connect(self.paste_key)
        row.addWidget(self.paste_button)
        layout.addLayout(row)
        layout.addSpacing(8)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.status_icon = QLabel()
        self.status_icon.setFixedSize(16, 16)
        status_row.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.status_text = _label("", "status", wrap=True)
        self.status_text.setMinimumHeight(34)  # two lines, so the window never changes size
        self.status_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status_text, 1)
        layout.addLayout(status_row)
        root.addWidget(card)
        root.addSpacing(8)

        # Card 2: the language
        card = self._card()
        layout = card.layout()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(_label("Language", "label"))
        row.addStretch(1)
        segments = QFrame()
        segments.setObjectName("segments")
        seg_layout = QHBoxLayout(segments)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(0)
        self.language_group = QButtonGroup(self)
        self.language_buttons = {}
        choices = list(LANGUAGES)
        if language not in dict(choices):  # a code set in config.json by hand (like "bn") stays a choice
            choices.append((language, LANGUAGE_NAMES.get(str(language).lower(), str(language))))
        for code, name in choices:
            button = QPushButton(name)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setFixedHeight(22)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.language_group.addButton(button)
            seg_layout.addWidget(button)
            self.language_buttons[code] = button
        (self.language_buttons.get(language) or self.language_buttons["en"]).setChecked(True)
        row.addWidget(segments)
        layout.addLayout(row)
        layout.addSpacing(2)
        layout.addWidget(_label("Auto-detect can guess wrong on short clips.", "hint"))
        self.dictionary_tip = None
        if first_run:
            layout.addSpacing(2)
            self.dictionary_tip = _label(DICTIONARY_TIP, "hint", wrap=True)
            layout.addWidget(self.dictionary_tip)
        root.addWidget(card)

        # First run only: the warning callout, right above the buttons (see CONFLICT_TITLE).
        self.conflict_note = None
        if first_run:
            root.addSpacing(8)
            self.conflict_note = self._callout(CONFLICT_TITLE, CONFLICT_BODY)
            root.addWidget(self.conflict_note)
        root.addSpacing(16)

        # Footer
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addStretch(1)
        self.later_button = QPushButton("Not now" if first_run else "Cancel")
        self.later_button.setObjectName("later")
        self.later_button.setFixedHeight(34)
        self.later_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_button.clicked.connect(self.close)
        footer.addWidget(self.later_button)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("save")
        self.save_button.setFixedHeight(34)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save_clicked)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

        self._update_eye()
        self.set_status("empty")

    def _card(self):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(WIDTH - 2 * PAD)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(CARD_PAD, CARD_PAD - 3, CARD_PAD, CARD_PAD - 4)
        layout.setSpacing(0)
        return card

    def _callout(self, title, body):
        """A warm warning card: the alert icon on the left, a bold title and a short body."""
        callout = QFrame()
        callout.setObjectName("callout")
        callout.setFixedWidth(WIDTH - 2 * PAD)
        row = QHBoxLayout(callout)
        row.setContentsMargins(CALLOUT_PAD, CALLOUT_PAD - 2, CALLOUT_PAD, CALLOUT_PAD - 1)
        row.setSpacing(CALLOUT_ICON_GAP)
        icon = QLabel()
        icon.setObjectName("callout_icon")
        icon.setFixedSize(CALLOUT_ICON, CALLOUT_ICON)
        icon.setPixmap(_icon("alert", CALLOUT_ICON, C_LIMITED).pixmap(CALLOUT_ICON, CALLOUT_ICON))
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        # Fixed widths, so the wrapped body's height is measured for its real width.
        text_width = WIDTH - 2 * PAD - 2 * CALLOUT_PAD - 2 - CALLOUT_ICON - CALLOUT_ICON_GAP  # 2: the 1px border
        callout.title = _label(title, "callout_title", wrap=True)
        callout.body = _label(body, "callout_body", wrap=True)
        for label in (callout.title, callout.body):
            label.setFixedWidth(text_width)
            text.addWidget(label)
        row.addLayout(text)
        return callout

    # ---- what you do in it ----

    def key(self):
        return clean_key(self.field.text())

    def language(self):
        for code, button in self.language_buttons.items():
            if button.isChecked():
                return code
        return "en"

    def paste_key(self):
        text = clean_key(QGuiApplication.clipboard().text())
        if text:
            self.field.setText(text)
            self._edited()
        self.field.setFocus()

    def show_key(self, shown):
        self._shown_key = shown
        self.field.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self._update_eye()

    def _update_eye(self):
        self._eye.setIcon(_icon("eye_off" if self._shown_key else "eye", 16, C_SLATE))
        self._eye.setToolTip("Hide the key" if self._shown_key else "Show the key")

    def _edited(self, *_):
        """Typing or pasting clears an old answer: it was about a different key."""
        if self.status in ("checking", "works"):
            return
        key = self.key()
        if key and is_impossible_key(key):
            self.set_status("invalid")
        else:
            self.set_status("not_groq" if key and not looks_like_groq_key(key) else "empty")

    def _save_clicked(self):
        if self.save_button.isEnabled():
            self.save_requested.emit(self.key(), self.language())

    # ---- what main.py tells it ----

    def set_status(self, status):
        self.status = status
        icon, icon_color, text_color, text = STATUS[status]
        self.status_icon.setPixmap(_icon(icon, 16, icon_color).pixmap(16, 16) if icon else QPixmap())
        self.status_icon.setVisible(bool(icon))  # no icon: the text starts at the edge
        self.status_text.setText(text.format(prefix=KEY_PREFIX))
        self.status_text.setStyleSheet(f"color: {text_color};")
        self.field.setProperty("state", status)
        self.field.style().unpolish(self.field)
        self.field.style().polish(self.field)
        busy = status in ("checking", "works")
        self.field.setReadOnly(busy)
        self.paste_button.setEnabled(not busy)
        self.save_button.setText(SAVE_TEXT.get(status, "Save"))
        self.save_button.setEnabled(not busy and status != "invalid" and bool(self.key()))

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)


class KeyFlow(QObject):
    """Opens the key window and does what it asks. GUI thread only.

    - pause: main.PauseReasons. "key_window" is held while the window is open, so a
      dictation can never be pasted into it.
    - on_new_key(key): the key was checked and saved; main.py switches the app to it.
    - focus_home(): the window to give focus back to (the tray remembers it).
    check, save, set_language and current_language can be swapped out for tests.
    """

    _checked = Signal(int, str, str, str)  # attempt, result, key, language (from the check thread)

    def __init__(self, pause, on_new_key, focus_home, key_source=None, window_icon=None,
                 check=None, save=None, set_language=None, current_language=None):
        super().__init__()
        self.pause = pause
        self.on_new_key = on_new_key
        self.focus_home = focus_home
        self.key_source = key_source  # "windows", "env" or None
        self.window_icon = window_icon
        self._check = check or keystore.check_key
        self._save = save or keystore.save_key
        self._set_language = set_language or (lambda code: set_setting("language", code))
        self._current_language = current_language or (lambda: load_config_or_defaults()[0]["language"])
        self.window = None
        self._language_at_open = None
        self._attempt = 0  # a check that comes back after a newer one (or after closing) is ignored
        self._checked.connect(self._on_checked, Qt.ConnectionType.QueuedConnection)

    def is_open(self):
        return self.window is not None

    def open(self, first_run=None):
        """Open it (or bring it to the front). first_run defaults to "there's no key yet"."""
        if self.window is not None:
            self._bring_to_front()
            return
        first_run = self.key_source is None if first_run is None else first_run
        self._language_at_open = self._current_language()
        self.window = KeyWindow(first_run=first_run, language=self._language_at_open, window_icon=self.window_icon)
        self.window.save_requested.connect(self._save_requested)
        self.window.closing.connect(self._closing)
        self.pause.set("key_window", True)
        print("[key] window opened: dictation is paused while it's open")
        self.window.show()
        self._bring_to_front()

    def _bring_to_front(self):
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        win32.set_foreground(int(self.window.winId()))

    def close(self):
        if self.window is not None:
            self.window.close()

    def _save_requested(self, key, language):
        window = self.window
        if window is None or not key:
            return
        if is_impossible_key(key):
            window.set_status("invalid")  # never sent to Groq, never saved
            return
        if window.status == "unreachable":
            self._finish(key, language)  # "Save anyway": the key didn't change since (editing clears the state)
            return
        window.set_status("checking")
        self._attempt += 1
        attempt = self._attempt

        def run():
            self._checked.emit(attempt, self._check(key), key, language)

        threading.Thread(target=run, daemon=True).start()

    def _on_checked(self, attempt, result, key, language):
        if attempt != self._attempt or self.window is None:
            return  # closed, or a newer check is on its way
        print(f"[key] check: {result}")
        if result == "works":
            self._finish(key, language)
        else:
            self.window.set_status(result)

    def _finish(self, key, language):
        try:
            self._save(key)
        except keystore.KeyStoreError as e:
            print(f"[key] {e}")
            self.window.set_status("save_failed")
            return
        if language != self._language_at_open:
            try:
                self._set_language(language)
                print(f"[key] language: {language}")
            except ConfigError as e:
                print(f"[key] language not saved: {e}")
        self.key_source = "windows"
        print("[key] saved in Windows (Credential Manager)")
        self.on_new_key(key)
        self.window.set_status("works")
        window = self.window
        QTimer.singleShot(CLOSE_DELAY_MS, lambda: window.close() if self.window is window else None)

    def _closing(self):
        window, self.window = self.window, None
        if window is None:
            return
        self._attempt += 1  # a check still running is ignored
        # Still the active window: hand focus back to where you were, while Windows lets us.
        if win32.foreground_window() == int(window.winId()):
            win32.set_foreground(self.focus_home())
        if window.status != "works":
            print("[key] closed without saving" + (": dictation stays paused until there's a key"
                                                   if self.key_source is None else ""))
        self.pause.set("key_window", False)
        window.deleteLater()
        QTimer.singleShot(FOCUS_CHECK_MS, self._check_focus)

    def _check_focus(self):
        hwnd = win32.foreground_window()
        if hwnd and win32.is_own_window(hwnd):
            print("FOCUS STOLEN: this app is still the active window after the key window closed. "
                  "Pastes would go nowhere!")
