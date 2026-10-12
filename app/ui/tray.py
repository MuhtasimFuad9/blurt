"""The tray icon (next to the clock): Pause/Resume, the API key, the panels, Quit,
and the notifications ("Groq limit almost used up", "Groq limit used up", a quiet
mic, a missing or rejected key, "already running").

Focus: Windows only closes a tray menu properly (Escape, a click elsewhere)
when its app is the active window, so the menu is made active while it's open.
When it closes, focus goes back to the window you were working in, so your
typing and the next paste land there. Clicking the taskbar had already taken
focus from that window, so this also puts back what the click took.

The notification is a normal Windows notification: Windows shows it itself, so
it never takes focus.
"""

import json
import time

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app import shortcut
from app.config import CONFIG_PATH, clear_debug_setting, load_config_or_defaults
from app.focus import window_exe
from app.paths import APP_DISPLAY_NAME
from app.ui import win32
from app.ui.pill import BLUE
from app.usage import CapNotices, LimitWarnings, cap_message, limit_warning_message, with_fake_usage

# The icon (design A2): a blue rounded square with a white waveform; paused, grey with pause bars.
# Drawn at every size Windows may ask for (16px is 100% scaling, 20px 125%, 24px 150%, ...).
ICON_SIZES = (16, 20, 24, 32, 40, 48)
PAUSED_GREY = QColor("#6b7385")
WHITE = QColor("#ffffff")
WAVE = [6, 10, 6]  # bar heights on a 16 x 16 grid
PAUSE = [9, 9]

FOREGROUND_POLL_MS = 500  # how often we note the window you're working in
WARNING_POLL_MS = 2000  # how often the usage meters are checked for the 80% notification
NOTIFICATION_MS = 10000
FOCUS_BACK_DELAY_MS = 50  # after the menu closes, let Windows settle before checking focus
REOPEN_GUARD_S = 0.25  # clicking the icon while the menu is open just closes it
MISSING_KEY_REPEAT_S = 30  # "Add your Groq API key" at most this often (it comes from shortcut presses)
SOUND_SETTINGS_URL = "ms-settings:sound"  # Windows Settings > System > Sound (input volume is there)
QUIET_TITLE = f"{APP_DISPLAY_NAME} didn't hear you"
QUIET_TEXT = ("If you were speaking, your mic may be too quiet: raise its input volume in Windows "
              "Sound settings. Click here to open them.")

PANELS = (("usage", "Usage"), ("history", "History"), ("dictionary", "Dictionary"), ("settings", "Settings"))


def _paint_icon(painter, size, paused):
    unit = size / 16  # the design is on a 16 x 16 grid; every edge lands on a whole pixel
    square = QPainterPath()
    radius = round(4 * unit)
    square.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.fillPath(square, PAUSED_GREY if paused else QColor(*BLUE))

    heights, gap = (PAUSE, 3) if paused else (WAVE, 2)
    bar_w = max(1, round(2 * unit))
    gap = max(1, round(gap * unit))
    x = round((size - (len(heights) * bar_w + (len(heights) - 1) * gap)) / 2)
    for height in heights:
        bar_h = max(2, round(height * unit))
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(x, round((size - bar_h) / 2), bar_w, bar_h), bar_w / 2, bar_w / 2)
        painter.fillPath(bar, WHITE)
        x += bar_w + gap


def tray_icon(paused):
    """The tray icon as a QIcon holding every size, so Windows picks a sharp one."""
    icon = QIcon()
    for size in ICON_SIZES:
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _paint_icon(painter, size, paused)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon


class Tray(QObject):
    """The tray icon and its menu. Lives on the GUI thread.

    on_pause(bool) is main.py's set_paused, which calls set_paused() here, so the
    tray and the pill's button always agree. on_quit quits the app in order.
    """

    def __init__(self, tracker, pill, on_pause, on_quit, on_key=None, debug=False, hotkey=None):
        super().__init__()
        self.tracker = tracker
        self.pill = pill
        self.on_pause = on_pause
        self.on_quit = on_quit
        self.on_key = on_key  # opens the API key window
        self.debug = debug
        self._paused = False
        self._hotkey = hotkey or shortcut.text(shortcut.DEFAULT)  # "Ctrl+Win", as in config.json
        self._key_missing = False
        self._key_rejected = False  # the last check saw Groq reject the key (notified once per change)
        self._missing_notified_at = None
        self._icons = {False: tray_icon(False), True: tray_icon(True)}
        self._warnings = LimitWarnings()
        self._caps = CapNotices()
        self._config_mtime = None  # debug_fake_limit_warning is read when config.json changes
        self._last_window = None  # the last active window that isn't ours or the taskbar
        self._menu_hidden_at = 0.0

        self._menu = QMenu()
        self._pause_action = self._menu.addAction("Pause dictation", self._toggle_pause)
        self._key_action = self._menu.addAction("Change API key", self._open_key_window)
        self._menu.addSeparator()
        for name, title in PANELS:
            self._menu.addAction(title, lambda name=name: self.pill.open_panel(name))
        self._menu.addSeparator()
        self._menu.addAction("Quit", self.on_quit)
        self._menu.aboutToHide.connect(self._menu_closed)

        self._icon = QSystemTrayIcon(self._icons[False])
        self._icon.activated.connect(self._activated)
        # What a click on the latest notification opens: a panel name, "key" or "sound" (Windows Sound settings)
        self._message_panel = None
        self._icon.messageClicked.connect(self._message_clicked)
        self._update()

        self._foreground_timer = QTimer(self)
        self._foreground_timer.setInterval(FOREGROUND_POLL_MS)
        self._foreground_timer.timeout.connect(self._remember_window)
        self._warning_timer = QTimer(self)
        self._warning_timer.setInterval(WARNING_POLL_MS)
        self._warning_timer.timeout.connect(self._check_limits)

    def show_config_problem(self, problem):
        """At startup: config.json has a mistake, so the app runs on the default settings.
        A Windows notification, since there may be no console to read. Clicking it opens
        Settings, which shows the mistake and has Open in Notepad."""
        print(f"[tray] notification: running on default settings. {problem}")
        self._message_panel = "settings"
        self._icon.showMessage("Running on default settings", problem, self._icons[self._paused], NOTIFICATION_MS)

    def show_wrong_kinds(self, problems):
        """At startup: some settings in config.json have the wrong kind of value (like "250" in
        quotes), so those use their defaults. Clicking the notification opens Settings."""
        print(f"[tray] notification: some settings use their defaults. {' '.join(problems)}")
        self._message_panel = "settings"
        title = "A setting uses its default" if len(problems) == 1 else f"{len(problems)} settings use their defaults"
        self._icon.showMessage(title, "config.json: " + "\n".join(problems), self._icons[self._paused],
                               NOTIFICATION_MS)

    def show_already_running(self):
        """Someone started the app again while it's running (the second copy just exits)."""
        print(f"[tray] notification: {APP_DISPLAY_NAME} is already running")
        self._message_panel = None
        self._icon.showMessage(f"{APP_DISPLAY_NAME} is already running", f"Hold {self._hotkey} to dictate. "
                               "Its icon is next to the clock.", self._icons[self._paused], NOTIFICATION_MS)

    def show_missing_key(self):
        """The shortcut was pressed with no API key yet. Clicking the notification opens the key window."""
        now = time.monotonic()
        if self._missing_notified_at is not None and now - self._missing_notified_at < MISSING_KEY_REPEAT_S:
            return
        self._missing_notified_at = now
        print("[tray] notification: add your Groq API key")
        self._message_panel = "key"
        self._icon.showMessage("Add your Groq API key", f"{APP_DISPLAY_NAME} needs a free Groq API key before "
                               "you can dictate. Click here to add it.", self._icons[self._paused], NOTIFICATION_MS)

    def show_quiet_mic(self):
        """Dictations in a row were too quiet to hear (main.py decides when). Clicking the
        notification opens Windows Sound settings; Blurt never changes the mic volume itself."""
        print("[tray] notification: didn't hear you, the mic may be too quiet")
        self._message_panel = "sound"
        self._icon.showMessage(QUIET_TITLE, QUIET_TEXT, self._icons[self._paused], NOTIFICATION_MS)

    def _check_key(self, snapshot):
        """Notifies once when Groq starts rejecting the key (a warm-up or a real request got 401/403)."""
        rejected = snapshot.get("connection") == "key_rejected" and not self._key_missing
        if rejected and not self._key_rejected:
            print("[tray] notification: Groq rejected the API key")
            self._message_panel = "key"
            self._icon.showMessage("Groq rejected your API key", "Dictation won't work until the key is "
                                   "fixed. Click here to change it.", self._icons[self._paused], NOTIFICATION_MS)
        self._key_rejected = rejected

    def set_key_missing(self, missing):
        self._key_missing = missing
        self._update()

    def _open_key_window(self):
        if self.on_key is not None:
            self.on_key()

    def _message_clicked(self):
        if self._message_panel == "key":
            self._open_key_window()
        elif self._message_panel == "sound":
            QDesktopServices.openUrl(QUrl(SOUND_SETTINGS_URL))
        elif self._message_panel:
            self.pill.open_panel(self._message_panel)

    @property
    def last_window(self):
        """The last active window that isn't ours or the taskbar: where focus goes back to."""
        return self._last_window

    def start(self):
        self._icon.show()
        self._remember_window()
        # The first check only notes meters already at 80%+, so starting the app never notifies.
        self._warnings.check(self.tracker.snapshot()["meters"])
        self._foreground_timer.start()
        self._warning_timer.start()

    def hide(self):
        """Remove the icon right away (on quit), instead of when Qt shuts down."""
        self._foreground_timer.stop()
        self._warning_timer.stop()
        self._menu.hide()
        self._icon.hide()

    def set_paused(self, paused):
        self._paused = paused
        self._update()

    def set_shortcut(self, hotkey):
        """A new hold-to-talk shortcut ("Alt+Win"), for the tooltip and the notifications."""
        self._hotkey = hotkey
        self._update()

    def _update(self):
        self._icon.setIcon(self._icons[self._paused])
        if self._key_missing:
            tooltip = f"{APP_DISPLAY_NAME}: add your Groq API key"
        elif self._paused:
            tooltip = f"{APP_DISPLAY_NAME}: paused"
        else:
            tooltip = f"{APP_DISPLAY_NAME} (hold {self._hotkey})"
        self._icon.setToolTip(tooltip)
        self._key_action.setText("Add API key" if self._key_missing else "Change API key")
        self._key_action.setVisible(self.on_key is not None)
        self._pause_action.setText("Resume dictation" if self._paused else "Pause dictation")

    def _toggle_pause(self):
        self.on_pause(not self._paused)

    # ---- the menu, and giving focus back ----

    def _activated(self, reason):
        # Left click and right click both open the menu. A double click does nothing more.
        if reason not in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.Context):
            return
        if self._menu.isVisible() or time.monotonic() - self._menu_hidden_at < REOPEN_GUARD_S:
            return
        self._menu.popup(QCursor.pos())
        # Windows lets us do this right after a click on our icon. Without it, Escape and
        # clicks elsewhere wouldn't close the menu.
        win32.set_foreground(int(self._menu.winId()))

    def _menu_closed(self):
        self._menu_hidden_at = time.monotonic()
        QTimer.singleShot(FOCUS_BACK_DELAY_MS, self._give_focus_back)

    def _remember_window(self):
        hwnd = win32.foreground_window()
        if hwnd and not win32.is_own_window(hwnd) and not win32.is_taskbar(hwnd):
            self._last_window = hwnd

    def _give_focus_back(self):
        hwnd = win32.foreground_window()
        if hwnd and not win32.is_own_window(hwnd) and not win32.is_taskbar(hwnd):
            # You clicked another window to close the menu: that one stays active.
            if self.debug:
                print(f"[tray] focus is on {window_exe(hwnd) or 'another window'}, left alone")
            return
        if win32.set_foreground(self._last_window):
            if self.debug:
                print(f"[tray] focus returned to {window_exe(self._last_window) or 'your window'}")
        hwnd = win32.foreground_window()
        if hwnd and win32.is_own_window(hwnd):
            print("FOCUS STOLEN: the tray menu left this app as the active window. Pastes would go nowhere!")

    # ---- the 80% notification, and the one when a limit is used up ----

    def _check_limits(self):
        snapshot = self.tracker.snapshot()
        self._check_key(snapshot)
        self._check_cap(snapshot.get("rate_limit"))
        meters = snapshot["meters"]
        fake = self._take_fake_warning(meters)
        if fake:
            meters = with_fake_usage(meters, fake)
        crossed = self._warnings.check(meters)
        if crossed:
            title, text = limit_warning_message(crossed, meters)
            print(f"[tray] notification: {title}. {' '.join(text.splitlines())}")
            self._message_panel = "usage"
            self._icon.showMessage(title, text, self._icons[False], NOTIFICATION_MS)

    def _check_cap(self, rate_limit):
        """Groq said a limit is used up and asked to wait a minute or more: say so once per hit
        limit (CapNotices). Clicking the notification opens Usage, which counts down the wait."""
        rate_limit = self._caps.check(rate_limit)
        if rate_limit is None:
            return
        history_on = bool(load_config_or_defaults()[0].get("history", True))  # read fresh, like the pipeline
        title, text = cap_message(rate_limit, history_on)
        print(f"[tray] notification: {title}. {text}")
        self._message_panel = "usage"
        self._icon.showMessage(title, text, self._icons[self._paused], NOTIFICATION_MS)

    def _take_fake_warning(self, meters):
        """For testing: config.json "debug_fake_limit_warning" set to a meter name makes this
        check pretend that meter is 85% full. Fires once, then sets itself back to null."""
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return None
        if mtime == self._config_mtime:
            return None
        self._config_mtime = mtime
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("debug_fake_limit_warning")
        except (OSError, ValueError):
            return None  # a broken config.json: the pill already says so
        if value is None:
            return None
        try:
            clear_debug_setting("debug_fake_limit_warning")
        except (OSError, ValueError) as e:
            print(f"[tray] couldn't set debug_fake_limit_warning back to null: {e}")
        if value not in meters:
            print(f'[tray] debug_fake_limit_warning: "{value}" isn\'t a meter. Use one of: {", ".join(meters)}')
            return None
        print(f'*** FAKE LIMIT WARNING for {value} (85%). debug_fake_limit_warning is now null again. ***')
        return value
