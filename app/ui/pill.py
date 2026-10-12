"""The pill: a small always-on-top indicator just above the taskbar.

States: idle, hover, recording, processing, done, copied
(the text was left on the clipboard, not pasted: a copy icon), error, limited (a rate limit: an hourglass instead of "!"), and paused (the shortcut was
pressed while paused: a pause icon for a moment). While paused, the idle pill
is an outline instead of a filled bar.

While recording, a second small pill above it names the mic in use (the mic
label). It's painted in this same window, so it moves in step with the pill.

How it's built:
- One fixed-size, see-through window. The pill is painted inside it, so the
  window itself never resizes (resizing a window 60 times a second stutters).
- Every animated value moves from wherever it is right now to its new target,
  so interrupting an animation (hover out halfway) never jumps.
- It never becomes the active window (see win32.py), so pastes still land in
  your document.
- Other threads never touch it directly. They emit UiBridge signals, and Qt
  runs the pill's handlers here, on the GUI thread.
- The "pill" section of config.json is reloaded live when you save it.
"""

import math
import time

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from app import shortcut
from app.config import CONFIG_PATH, load_config, load_config_or_defaults
from app.micname import NO_MIC, NO_MIC_TEXT, LabelHold, elide
from app.ui import win32
from app.ui.icons import icon_image

BLUE = (0x13, 0x43, 0xD0)
# The idle pill while paused is only an outline, in a lighter blue: about 3.7:1 or
# better on white, light grey, dark and black (the pill blue is only 2.3:1 on dark).
PAUSED_OUTLINE = (0x3F, 0x68, 0xE6)
PAUSED_OUTLINE_W = 1.0

# All sizes below are "design pixels". The "scale" setting multiplies them all.

# Pill size (width, height) per state, from the mockup.
# The hover width is worked out from the menu's contents (see _menu_layout).
SIZES = {
    "idle": (40, 6),
    "hover": (0, 34),
    "recording": (76, 26),
    "processing": (52, 26),
    "done": (52, 26),
    "copied": (52, 26),  # left on the clipboard instead of pasted: a copy icon instead of the checkmark
    "error": (52, 26),
    "limited": (52, 26),  # rate limit: an hourglass instead of "!"
    "paused": (52, 26),  # the shortcut pressed while paused: a pause icon, then back to idle
}
CONTENT = {
    "idle": None,
    "hover": "menu",
    "recording": "bars",
    "processing": "dots",
    "done": "check",
    "copied": "copied",
    "error": "error",
    "limited": "hourglass",
    "paused": "paused",
}
ALERT_STATES = ("error", "limited")  # clickable, and hover waits until they're gone
QUIET = object()  # the "!" is because the silence check heard no speech (see job_finished)
QUIET_TEXT = "Didn't hear you"
BIG_SHADOW_STATES = {"hover", "recording"}  # the rest use shadow_opacity_small

# The shadow is faked with stacked soft layers about 1px apart (much faster than
# a real blur). Its offset, blur and opacity come from config.json.
MAX_SHADOW_LAYERS = 64

CANVAS_MARGIN = 4  # spare room around the pill and shadow inside the window

# Invisible hover area around the tiny idle pill
IDLE_HIT_W, IDLE_HIT_H = 120, 30
HIT_MARGIN = 4

# Hover menu layout, from the mockup. Paddings and the button gap come from config.json.
DIVIDER_GAP = 2  # on each side of the divider, on top of the margins
LABEL_TEXT = shortcut.label(shortcut.DEFAULT)  # "Ctrl + Win"; the menu shows the shortcut in use
LABEL_MARGIN = 6
DIVIDER_H = 14
DIVIDER_MARGIN = 3
BUTTON = 28
MENU_ICON = 15
BUTTONS = [
    ("usage", "Usage"),
    ("history", "History"),
    ("dictionary", "Dictionary"),
    ("settings", "Settings"),
    ("pause", "Pause"),
]
BUTTON_TITLES = dict(BUTTONS)
# While paused, the Pause button turns into Resume (a play icon).
PAUSED_BUTTON = ("play", "Resume")

# Recording bars, driven by the mic. Heights, sensitivity and smoothing come from config.json.
BAR_COUNT = 12
BAR_W = 2
BAR_GAP = 2
# Mic loudness in decibels: WAVE_FLOOR_DB or quieter = shortest bar, WAVE_CEIL_DB or louder = tallest.
# Decibels match how loud things sound to us, so quiet speech still moves the bars.
WAVE_FLOOR_DB = -50.0
WAVE_CEIL_DB = -12.0
MAX_FRAME_GAP_MS = 100  # after a stall, don't let the smoothing jump in one go

# Processing dots
DOT_COUNT = 3
DOT_SIZE = 4
DOT_GAP = 4
DOT_STAGGER = 0.15  # each dot starts this fraction of a cycle after the one before
DOT_LIFT = 2
DOT_MIN_OPACITY = 0.35

STATUS_ICON = 14  # checkmark
ERROR_STROKE = 2.0  # the "!"

FRAME_MS = 16  # about 60fps, only while something moves
HOVER_POLL_MS = 33
FULLSCREEN_POLL_MS = 500
CONFIG_POLL_MS = 1000

CSS_EASE = [0.25, 0.1, 0.25, 1.0]  # CSS "ease", used for color and fades

WHITE = (255, 255, 255)


def _now_ms():
    return time.perf_counter() * 1000


def bezier_curve(points):
    """A QEasingCurve from cubic-bezier numbers, exactly like CSS."""
    x1, y1, x2, y2 = (float(v) for v in points)
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return curve


def pill_font():
    """The pill's text font: 11.5px medium Segoe UI Variable. The cancel notice uses it too."""
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"])
    font.setPointSizeF(11.5 * 0.75)  # 11.5px (1pt = 1.333px at 100%)
    font.setWeight(QFont.Weight.Medium)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # smoother while scaling
    return font


def _color(rgb, alpha):
    color = QColor(*rgb)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def paint_soft_shadow(painter, rect, radius, strength, blur, offset_y):
    """A soft blue shadow under a rounded rect, faked with stacked layers about 1px apart.

    Much faster than a real blur on a see-through window. Used by the pill and the panels.
    """
    if strength <= 0.001:
        return
    # About one layer per pixel of soft edge, so no visible rings.
    layers = max(1, min(MAX_SHADOW_LAYERS, math.ceil(2 * blur)))
    # Stacked layers: where all of them overlap, the total opacity equals `strength`.
    layer_alpha = 1 - (1 - min(strength, 0.999)) ** (1 / layers)
    painter.setBrush(_color(BLUE, layer_alpha))
    base = rect.translated(0, offset_y)
    for i in range(layers):
        grow = blur - 2 * blur * (i + 0.5) / layers
        layer = base.adjusted(-grow, -grow, grow, grow)
        if layer.width() <= 0 or layer.height() <= 0:
            continue
        layer_radius = max(0.0, min(radius + grow, layer.width() / 2, layer.height() / 2))
        painter.drawRoundedRect(layer, layer_radius, layer_radius)


class Tween:
    """One animated number. to() always starts from the current value."""

    def __init__(self, value):
        self.value = self.start = self.end = value
        self.t0 = 0.0
        self.ms = 0
        self.curve = None
        self.active = False

    def to(self, end, ms, curve, now):
        if end == self.end:
            return
        self.start, self.end, self.t0, self.ms, self.curve = self.value, end, now, ms, curve
        self.active = ms > 0
        if not self.active:
            self.value = end

    def jump(self, value):
        self.value = self.start = self.end = value
        self.active = False

    def update(self, now):
        """Moves the value along. Returns True while still animating."""
        if not self.active:
            return False
        progress = (now - self.t0) / self.ms
        if progress >= 1:
            self.value = self.end
            self.active = False
            return False
        self.value = self.start + (self.end - self.start) * self.curve.valueForProgress(progress)
        return True


class UiBridge(QObject):
    """The safe way for other threads to update the pill.

    Create it on the GUI thread and attach() it. Any thread can then call
    bridge.job_finished.emit("done", 0) and so on; Qt queues the call and runs
    it on the GUI thread.
    """

    recording_started = Signal()
    recording_ended = Signal(bool)  # True if the audio was sent to the pipeline
    # "done", "copied", "limited", "error", "quiet" or "nothing", and its History entry (0 = none)
    job_finished = Signal(str, int)
    error = Signal()  # something failed outside the pipeline (e.g. no mic)
    paused_press = Signal()  # the shortcut was pressed while paused
    dictation_cancelled = Signal()  # another key in a recording's first second (main.py shows the notice)
    shortcut_changed = Signal(str)  # config.json has a new hotkey ("Alt+Win"); the worker sends it
    quit_requested = Signal()  # main.py connects this to the Qt app's quit
    second_copy = Signal()  # someone started the app again; main.py connects it to the tray

    def attach(self, pill):
        queued = Qt.ConnectionType.QueuedConnection
        self.recording_started.connect(pill.recording_started, queued)
        self.recording_ended.connect(pill.recording_ended, queued)
        self.job_finished.connect(pill.job_finished, queued)
        self.error.connect(pill.show_error, queued)
        self.paused_press.connect(pill.show_paused_hint, queued)
        self.shortcut_changed.connect(pill.set_shortcut, queued)


_NO_SWAP = object()


class Pill(QWidget):
    state_changed = Signal(str)  # the state just picked ("idle", "recording", "hover", ...)

    def __init__(self, level_source=None, debug=False, panels=None, mic_source=None, hotkey=None):
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)

        # A function returning the latest mic loudness (RMS). None = resting bars.
        self.level_source = level_source
        # The hover menu names the hold-to-talk shortcut. hotkey: config.json's text, like "Alt+Win".
        self._menu_label = LABEL_TEXT if hotkey is None else shortcut.label(shortcut.parse(hotkey))
        # A function returning the mic's name, NO_MIC, or None (not known yet). None = no mic label.
        self.mic_source = mic_source
        self.debug = debug
        # The panels, by menu button name: {"usage": UsagePanel, "history": HistoryPanel,
        # "dictionary": DictionaryPanel}.
        # One is open at a time; while it's open the pill stays in its hover look.
        self._panels = dict(panels or {})
        self._screen_top = None  # the top of the screen area the pill is placed in (_place)
        self._mouse_was_down = False  # for noticing clicks outside the open panel
        for panel in self._panels.values():
            panel.closed.connect(self._refresh)

        self._settings = load_config_or_defaults()[0]["pill"]  # a broken config.json: the defaults
        self._config_mtime = self._read_config_mtime()
        self._resize_curve = bezier_curve(self._settings["resize_easing"])
        self._fade_curve = bezier_curve(CSS_EASE)

        self._font = pill_font()
        self._metrics = QFontMetricsF(self._font)
        self._label_cache = {}  # mic name -> (name cut to fit, label width)
        self._apply_layout()

        # The facts the state is picked from
        self._recording = False
        self._pending = 0  # dictations still in the pipeline
        self._result = None  # "done", "copied", "error", "limited" or "paused", shown for a moment
        self._error_note = None  # why the "!" shows, said by the label above it: NO_MIC or QUIET
        self._result_entry = 0  # the History entry of the shown result (0 = none)
        self._hovered = False
        self._outside_since = None
        self._fullscreen = False
        self._paused = False
        # Called with True/False when the Pause button is clicked (main.py's set_paused). None = just print.
        self.on_pause = None
        self._state = "idle"

        # What's on screen right now
        width, height = self._sizes["idle"]
        self._w = Tween(width)
        self._h = Tween(height)
        self._fill = Tween(self._settings["idle_opacity"])
        self._outline = Tween(0.0)  # the paused idle pill's outline
        self._shadow = Tween(0.0)
        self._content = None
        self._swap_to = _NO_SWAP  # content waiting for the old one to fade out
        self._content_opacity = Tween(1.0)
        self._content_scale = Tween(1.0)
        # The waveform: newest loudness enters the middle and ripples out to both edges.
        self._bar_levels = []  # each bar's height on screen right now (design px)
        self._wave_history = []  # loudness 0..1, from the center (index 0) to the edge
        self._wave_peak = 0.0  # loudest level since the last ripple step
        self._wave_last_tick = None
        self._wave_last_step = 0.0
        self._reset_wave(0.0)
        # The mic label: (text, icon) on screen, and the one waiting for the old text to fade out
        self._label = None
        self._label_next = None
        self._label_hold = LabelHold()  # a name shows for mic_label_hold_ms, then fades
        self._label_opacity = Tween(0.0)
        self._label_text_opacity = Tween(1.0)
        self._label_w = Tween(0)
        self._hover_button = None
        self._pressed = None

        self._frame_timer = self._timer(FRAME_MS, self._tick)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._result_timer = self._timer(0, self._clear_result, single_shot=True)
        self._hover_timer = self._timer(HOVER_POLL_MS, self._poll_hover)
        self._fullscreen_timer = self._timer(FULLSCREEN_POLL_MS, self._poll_fullscreen)
        self._config_timer = self._timer(CONFIG_POLL_MS, self._poll_config)

        self._hwnd = int(self.winId())  # creates the real Windows window
        win32.make_no_activate(self._hwnd)
        win32.set_click_through(self._hwnd, True)

        app = QGuiApplication.instance()
        app.primaryScreenChanged.connect(self._watch_screen)
        self._watch_screen(QGuiApplication.primaryScreen())

    def _timer(self, interval, handler, single_shot=False):
        timer = QTimer(self)
        timer.setInterval(interval)
        timer.setSingleShot(single_shot)
        timer.timeout.connect(handler)
        return timer

    def start(self):
        """Show the pill and start watching the mouse, fullscreen apps and config.json."""
        self._place()
        self._show()
        self._hover_timer.start()
        self._fullscreen_timer.start()
        self._config_timer.start()

    # ---- events from the app (always on the GUI thread, via UiBridge) ----

    @Slot()
    def recording_started(self):
        # The panel covers the bottom of the screen, where chat boxes usually are.
        self._close_panel()
        self._recording = True
        self._result = None  # a new dictation replaces the last checkmark or error
        self._error_note = None
        self._result_timer.stop()
        self._label_hold.reset()  # every dictation shows the mic name again
        self._reset_wave(_now_ms())  # every dictation starts with calm bars
        self._refresh()

    @Slot(bool)
    def recording_ended(self, submitted):
        self._recording = False
        if submitted:
            self._pending += 1
        self._refresh()

    @Slot(str, int)
    def job_finished(self, result, entry_id=0):
        self._pending = max(0, self._pending - 1)
        self._result_entry = entry_id  # clicking the "!" or hourglass opens History here
        if result in ("done", "copied", "error", "limited"):
            self._show_result(result)
        elif result == "quiet":
            # The silence check heard no speech: the "!" with "Didn't hear you" above it.
            self._show_result("error", note=QUIET)
        else:
            self._refresh()

    @Slot()
    def show_error(self):
        # Only sent when there was no mic, so the mic label says "No microphone" above the "!".
        self._result_entry = 0  # nothing was recorded, so there's no History entry
        self._show_result("error", note=NO_MIC)

    @Slot()
    def show_paused_hint(self):
        """The shortcut was pressed while paused: show a pause icon for a moment, so it's clear why nothing records."""
        self._close_panel()  # like a dictation starting: the panel covers where chat boxes usually are
        self._result_entry = 0
        self._show_result("paused")

    @property
    def paused(self):
        return self._paused

    def set_paused(self, paused):
        """Only changes the look (idle outline, Resume button); main.py's set_paused does the rest."""
        if paused == self._paused:
            return
        self._paused = paused
        self._animate_to_state(_now_ms())  # the idle pill fades between filled and outline
        self.update()  # the menu's Pause/Resume icon

    @Slot(str)
    def set_shortcut(self, hotkey):
        """A new hold-to-talk shortcut ("Alt+Shift+Win"): the menu's label changes, and a longer
        one makes the hover pill (and the window) wider."""
        label = shortcut.label(shortcut.parse(hotkey))
        if label == self._menu_label:
            return
        self._menu_label = label
        self._apply_layout()
        self._place()
        self._animate_to_state(_now_ms())  # an open menu grows to its new width
        self.update()

    def open_panel(self, name):
        """Open a panel from outside the pill (the tray menu)."""
        return self._open_panel(name)

    def reset(self):
        """Back to idle, forgetting everything in progress (used by the preview tool)."""
        self._recording = False
        self._pending = 0
        self._result = None
        self._error_note = None
        self._result_timer.stop()
        self._label_hold.reset()
        self._refresh()

    def restart_mic_label(self):
        """Show the mic label again for a full hold (used by the preview tool's "m")."""
        self._label_hold.reset()
        self._update_label(_now_ms())
        self._frame_timer.start()

    def _show_result(self, result, note=None):
        self._result = result
        self._error_note = note
        # The hourglass holds like "!"; the pause hint and the copy icon like the checkmark.
        hold = "error_hold_ms" if result in ALERT_STATES else "done_hold_ms"
        self._result_timer.start(self._settings[hold])
        self._refresh()

    def _clear_result(self):
        self._result = None
        self._result_entry = 0
        self._error_note = None
        self._refresh()

    # ---- picking the state ----

    def _pick_state(self):
        if self._recording:
            return "recording"
        if self._pending:
            return "processing"
        if self._result in ALERT_STATES:
            return self._result
        if self._result == "paused":
            return "paused"
        if self._hovered or self._panel_open():
            return "hover"
        if self._result in ("done", "copied"):
            return self._result
        return "idle"

    def _refresh(self):
        state = self._pick_state()
        self._update_visibility(state)
        if state == self._state:
            return
        previous, self._state = self._state, state
        now = _now_ms()
        self._animate_to_state(now)
        self._switch_content(CONTENT[state], now)
        self._update_label(now)

        win32.set_click_through(self._hwnd, not self._clickable())
        if previous == "idle":
            win32.keep_on_top(self._hwnd)  # in case another app pushed itself above us
        if state != "hover":
            self._hover_button = None
        if self.debug:
            print(f"[pill] {state}")
        self._frame_timer.start()
        self.state_changed.emit(state)

    def _animate_to_state(self, now):
        """Start the size, color and shadow animations toward the current state's look."""
        s = self._settings
        width, height = self._sizes[self._state]
        self._w.to(width, s["resize_ms"], self._resize_curve, now)
        self._h.to(height, s["resize_ms"], self._resize_curve, now)
        paused_idle = self._state == "idle" and self._paused
        if paused_idle:
            fill = 0.0  # only the outline
        else:
            fill = s["idle_opacity"] if self._state == "idle" else 1.0
        self._fill.to(fill, s["color_ms"], self._fade_curve, now)
        self._outline.to(1.0 if paused_idle else 0.0, s["color_ms"], self._fade_curve, now)
        if self._state == "idle":
            shadow = 0.0
        elif self._state in BIG_SHADOW_STATES:
            shadow = s["shadow_opacity"]
        else:
            shadow = s["shadow_opacity_small"]
        self._shadow.to(shadow, s["color_ms"], self._fade_curve, now)
        self._frame_timer.start()

    # ---- the panels (Usage, History, Dictionary, Settings) ----

    def _open_name(self):
        """The name of the open panel, or None."""
        return next((name for name, panel in self._panels.items() if panel.is_open), None)

    def _panel_open(self):
        return self._open_name() is not None

    def _open_panel(self, name, entry_id=0):
        """Opens one panel (closing any other). History can open at an entry."""
        panel = self._panels.get(name)
        if panel is None:
            return False
        for other_name, other in self._panels.items():
            if other_name != name:
                other.close_panel()
        if entry_id and hasattr(panel, "show_entry"):
            panel.show_entry(entry_id)
        else:
            panel.open_panel()
        self._mouse_was_down = True  # wait for the button to go up before watching for outside clicks
        win32.keep_on_top(self._hwnd)  # the pill stays above the panel's shadow, so its buttons still work
        self._refresh()
        return True

    def _toggle_panel(self, name):
        if self._open_name() == name:
            self._close_panel()
        else:
            self._open_panel(name)

    def _close_panel(self):
        for panel in self._panels.values():
            panel.close_panel()  # does nothing if it's closed; its "closed" signal refreshes the pill

    def _panel_anchor(self):
        """Tell the panels where the hover pill's top center is, and the top of the pill's own
        screen (so Settings and Usage fit under it), in screen pixels."""
        scale = self._settings["scale"]
        top = self.y() + (self._pill_bottom - self._sizes["hover"][1]) * scale
        for panel in self._panels.values():
            panel.set_anchor(self.x() + self.width() / 2, top, self._screen_top)

    def _watch_outside_clicks(self, pos):
        """Close the panel when a mouse button goes down anywhere except the panel or the pill.

        The panel never has focus, so this is the only way to notice.
        """
        down = win32.mouse_button_down()
        if down and not self._mouse_was_down:
            on_panel = any(panel.contains_global(QCursor.pos()) for panel in self._panels.values())
            if not self._hit_rect().contains(pos) and not on_panel:
                self._close_panel()
        self._mouse_was_down = down

    def _clickable(self):
        return self._state == "hover" or self._state in ALERT_STATES

    # ---- showing and hiding ----

    def _update_visibility(self, state):
        # Fullscreen apps hide only the quiet states; dictation feedback still shows.
        hide = self._settings["hide_in_fullscreen"] and self._fullscreen and state in ("idle", "hover")
        if hide and self.isVisible():
            self.hide()
        elif not hide and not self.isVisible():
            self._show()

    def _show(self):
        self.show()
        # Re-apply after every show, in case Windows reset anything.
        win32.make_no_activate(self._hwnd)
        win32.set_click_through(self._hwnd, not self._clickable())
        win32.keep_on_top(self._hwnd)

    def _watch_screen(self, screen):
        screen.availableGeometryChanged.connect(self._place)
        self._place()

    def _place(self):
        """Bottom center of the primary screen, gap_above_taskbar px above the taskbar."""
        area = QGuiApplication.primaryScreen().availableGeometry()  # screen minus taskbar
        self._screen_top = area.y()  # the panels never ask for a screen themselves: they use the pill's
        pill_bottom = area.y() + area.height() - self._settings["gap_above_taskbar"]
        self.move(
            area.x() + (area.width() - self.width()) // 2,
            pill_bottom - round(self._pill_bottom * self._settings["scale"]),
        )
        self._panel_anchor()

    def idle_spot(self):
        """Where the idle pill sits, in screen pixels: (center x, bottom y). The bottom is
        gap_above_taskbar above the taskbar, exactly as _place puts it."""
        bottom = self.y() + round(self._pill_bottom * self._settings["scale"])
        return self.x() + self.width() / 2, bottom

    @property
    def settings(self):
        """The "pill" settings in use right now (reloaded live from config.json)."""
        return self._settings

    def _apply_layout(self):
        """Work out the menu, the hover width and the window size from the settings.

        The window is sized to fit the widest pill plus its shadow, times "scale".
        """
        s = self._settings
        self._menu, menu_width = self._menu_layout()
        self._sizes = dict(SIZES)
        self._sizes["hover"] = (menu_width, SIZES["hover"][1])

        blur, offset = s["shadow_blur"], s["shadow_offset_y"]
        widest = max(width for width, _ in self._sizes.values())
        tallest = max(height for _, height in self._sizes.values())
        if s["mic_label"]:
            # Room for the mic label above the recording pill. Its see-through parts let clicks through.
            widest = max(widest, s["mic_label_max_width"])
            tallest = max(tallest, self._sizes["recording"][1] + s["mic_label_gap"] + s["mic_label_height"])
        self._label_cache.clear()  # the sizes may have changed
        side = blur + CANVAS_MARGIN
        top = max(0, blur - offset) + CANVAS_MARGIN
        bottom = max(0, blur + offset) + CANVAS_MARGIN

        scale = s["scale"]
        window_w = math.ceil((widest + 2 * side) * scale)
        window_w += window_w % 2  # even, so the pill's center lands on a whole pixel
        window_h = math.ceil((top + tallest + bottom) * scale)
        self._canvas_w = window_w / scale  # in design pixels
        self._pill_bottom = top + tallest  # y of the pill's bottom edge, in design pixels
        self.setFixedSize(window_w, window_h)

    # ---- content crossfade ----

    def _switch_content(self, kind, now):
        s = self._settings
        if kind == self._content:
            # Came back before the old content finished fading out: fade it back in.
            self._swap_to = _NO_SWAP
            self._content_opacity.to(1.0, s["content_in_ms"], self._fade_curve, now)
            return
        if self._content is None or self._content_opacity.value <= 0.01 or s["content_out_ms"] <= 0:
            self._swap_to = _NO_SWAP
            self._show_content(kind, now)
        else:
            self._swap_to = kind
            self._content_opacity.to(0.0, s["content_out_ms"], self._fade_curve, now)

    def _show_content(self, kind, now):
        s = self._settings
        self._content = kind
        self._content_opacity.jump(0.0)
        self._content_opacity.to(1.0, s["content_in_ms"], self._resize_curve, now)
        self._content_scale.jump(s["content_in_scale"])
        self._content_scale.to(1.0, s["content_in_ms"], self._resize_curve, now)

    # ---- the frame loop (runs only while something moves) ----

    def _tick(self):
        now = _now_ms()
        moving = False
        for tween in (
            self._w, self._h, self._fill, self._outline, self._shadow, self._content_opacity, self._content_scale
        ):
            moving = tween.update(now) or moving
        if self._swap_to is not _NO_SWAP and not self._content_opacity.active:
            self._show_content(self._swap_to, now)
            self._swap_to = _NO_SWAP
            moving = True
        if self._state == "processing":
            moving = True  # the dots pulse
        if self._state == "recording" and self.level_source is not None:
            self._update_wave(now)
            moving = True
        if self._state == "recording" and self.mic_source is not None:
            self._update_label(now)  # the mic can die mid-recording, or finish opening
            moving = True
        for tween in (self._label_opacity, self._label_text_opacity, self._label_w):
            moving = tween.update(now) or moving
        if self._label_next is not None and not self._label_text_opacity.active:
            # The old name has faded out: swap in the new one.
            self._label, self._label_next = self._label_next, None
            self._label_text_opacity.to(1.0, self._settings["content_in_ms"], self._fade_curve, now)
            moving = True
        self.update()
        if not moving:
            self._frame_timer.stop()

    # ---- the live waveform ----

    def _reset_wave(self, now):
        self._bar_levels = [self._settings["wave_min_height"]] * BAR_COUNT
        self._wave_history = [0.0] * (BAR_COUNT // 2)
        self._wave_peak = 0.0
        self._wave_last_tick = None
        self._wave_last_step = now

    def _loudness(self, level):
        """Mic RMS to 0..1, on a decibel scale."""
        value = level * self._settings["wave_sensitivity"]
        if value <= 0:
            return 0.0
        db = 20 * math.log10(value)
        return max(0.0, min(1.0, (db - WAVE_FLOOR_DB) / (WAVE_CEIL_DB - WAVE_FLOOR_DB)))

    def _update_wave(self, now):
        s = self._settings
        dt = FRAME_MS if self._wave_last_tick is None else min(now - self._wave_last_tick, MAX_FRAME_GAP_MS)
        self._wave_last_tick = now

        # Keep the loudest moment between steps, so short syllables aren't missed.
        self._wave_peak = max(self._wave_peak, self._loudness(self.level_source()))
        step = s["wave_step_ms"]
        if now - self._wave_last_step >= step:
            # Everything moves one bar outward; the newest loudness enters the center.
            self._wave_history = [self._wave_peak] + self._wave_history[:-1]
            self._wave_peak = 0.0
            self._wave_last_step += step
            if now - self._wave_last_step >= step:
                self._wave_last_step = now  # fell behind (a stall): restart the rhythm

        low, high = s["wave_min_height"], s["wave_max_height"]
        half = BAR_COUNT // 2
        for i, height in enumerate(self._bar_levels):
            distance = half - 1 - i if i < half else i - half  # 0 for the two middle bars
            target = low + (high - low) * self._wave_history[distance]
            # Fast up, slower down. The same feel at any frame rate.
            ms = s["wave_attack_ms"] if target > height else s["wave_release_ms"]
            follow = 1.0 if ms <= 0 else 1 - math.exp(-dt / ms)
            self._bar_levels[i] = height + (target - height) * follow

    # ---- the mic label (a small pill above the recording pill) ----

    def _label_wanted(self):
        """What the mic label should show right now: (text, icon), or None to hide it."""
        if self.mic_source is None or not self._settings["mic_label"]:
            return None
        if self._state == "recording":
            value = self.mic_source()
        elif self._state == "error" and self._error_note is QUIET:
            return (QUIET_TEXT, "mic")  # stays as long as the "!" does (error_hold_ms)
        elif self._state == "error" and self._error_note is NO_MIC:
            value = NO_MIC
        else:
            return None
        # None while the mic is still opening: stay hidden instead of flashing "No microphone".
        # A name hides again after mic_label_hold_ms; "No microphone" stays.
        value = self._label_hold.visible(value, self._settings["mic_label_hold_ms"], _now_ms())
        if value is None:
            return None
        if value is NO_MIC:
            return (NO_MIC_TEXT, "mic_off")
        return (value, "mic")

    @staticmethod
    def _label_text_room(s):
        """How wide the name may be, inside mic_label_max_width."""
        return (
            s["mic_label_max_width"] - s["mic_label_padding_left"] - s["mic_label_icon_size"]
            - s["mic_label_icon_gap"] - s["mic_label_padding_right"]
        )

    def _label_display(self, label):
        """(name cut to fit, label width) for a (text, icon) pair. Cached, since cutting measures a lot."""
        text = label[0]
        if text not in self._label_cache:
            s = self._settings
            shown = elide(text, self._label_text_room(s), self._metrics.horizontalAdvance)
            width = (
                s["mic_label_padding_left"] + s["mic_label_icon_size"] + s["mic_label_icon_gap"]
                + self._metrics.horizontalAdvance(shown) + s["mic_label_padding_right"]
            )
            self._label_cache[text] = (shown, math.ceil(width))
        return self._label_cache[text]

    def _update_label(self, now):
        """Fade the label in or out, or crossfade to a new name. Cheap enough to call every frame."""
        s = self._settings
        wanted = self._label_wanted()
        if wanted is None:
            self._label_next = None
            self._label_opacity.to(0.0, s["content_out_ms"], self._fade_curve, now)
            return
        hidden = self._label is None or (self._label_opacity.end == 0.0 and self._label_opacity.value <= 0.01)
        if hidden:
            # Coming in fresh: no crossfade, just the new name.
            self._label, self._label_next = wanted, None
            self._label_text_opacity.jump(1.0)
            self._label_w.jump(self._label_display(wanted)[1])
            if self.debug:
                print(f"[pill] mic label: {wanted[0]}")
        elif wanted == self._label:
            if self._label_next is not None:
                # Changed back before the swap happened: undo it.
                self._label_next = None
                self._label_text_opacity.to(1.0, s["content_in_ms"], self._fade_curve, now)
                self._label_w.to(self._label_display(wanted)[1], s["resize_ms"], self._resize_curve, now)
        elif wanted != self._label_next:
            # A new name while showing: the text fades out, then _tick swaps it in. The width follows.
            self._label_next = wanted
            self._label_text_opacity.to(0.0, s["content_out_ms"], self._fade_curve, now)
            self._label_w.to(self._label_display(wanted)[1], s["resize_ms"], self._resize_curve, now)
            if self.debug:
                print(f"[pill] mic label: {wanted[0]}")
        self._label_opacity.to(1.0, s["content_in_ms"], self._resize_curve, now)

    def _label_visible(self):
        return self._label is not None and self._label_opacity.value > 0.001

    def _label_rect(self):
        """Centered above the recording pill; it rises into place as it fades in."""
        s = self._settings
        width, height = self._label_w.value, s["mic_label_height"]
        rise = (1 - self._label_opacity.value) * s["mic_label_rise"]
        bottom = self._pill_bottom - self._sizes["recording"][1] - s["mic_label_gap"] + rise
        return QRectF((self._canvas_w - width) / 2, bottom - height, width, height)

    # ---- hover and clicks ----

    def _hit_rect(self):
        """The area (in design pixels) where the mouse counts as "on the pill"."""
        if self._state == "idle":
            center_y = self._pill_bottom - self._sizes["idle"][1] / 2
            return QRectF(
                (self._canvas_w - IDLE_HIT_W) / 2, center_y - IDLE_HIT_H / 2, IDLE_HIT_W, IDLE_HIT_H
            )
        width, height = self._sizes[self._state]
        return QRectF(
            (self._canvas_w - width) / 2 - HIT_MARGIN,
            self._pill_bottom - height - HIT_MARGIN,
            width + 2 * HIT_MARGIN,
            height + 2 * HIT_MARGIN,
        )

    def _to_design(self, pos):
        """Window pixels to design pixels (undo the "scale" setting)."""
        scale = self._settings["scale"]
        return QPointF(pos.x() / scale, pos.y() / scale)

    def _poll_hover(self):
        # We check the cursor position ourselves, because the idle pill lets the
        # mouse pass straight through it and so gets no mouse events.
        pos = self._to_design(self.mapFromGlobal(QCursor.pos()))
        can_hover = (
            self.isVisible() and not self._recording and not self._pending and self._result not in ALERT_STATES
        )
        inside = can_hover and self._hit_rect().contains(pos)

        if inside:
            self._outside_since = None
            if not self._hovered:
                self._hovered = True
                self._result = None  # hovering the checkmark opens the menu instead
                self._refresh()
        elif self._hovered:
            now = _now_ms()
            if self._outside_since is None:
                self._outside_since = now
            if not can_hover or now - self._outside_since >= self._settings["hover_leave_delay_ms"]:
                self._hovered = False
                self._outside_since = None
                self._refresh()
        self._set_hover_button(pos)
        if self._panel_open():
            self._watch_outside_clicks(pos)

    def _button_at(self, pos):
        if self._state != "hover" or self._content != "menu":
            return None
        local = pos - self._pill_rect().center()
        for name, rect in self._menu["buttons"]:
            if rect.contains(local):
                return name
        return None

    def _set_hover_button(self, pos):
        button = self._button_at(pos)
        if button != self._hover_button:
            self._hover_button = button
            self.update()

    def _target_at(self, pos):
        if self._state in ALERT_STATES:
            # The label above the "!" ("No microphone", "Didn't hear you") clicks like the "!",
            # instead of being a dead spot.
            on_label = self._label_visible() and self._label_rect().contains(pos)
            if self._pill_rect().contains(pos) or on_label:
                return self._state
        return self._button_at(pos)

    def mouseMoveEvent(self, event):
        self._set_hover_button(self._to_design(event.position()))

    def mousePressEvent(self, event):
        self._pressed = self._target_at(self._to_design(event.position()))

    def mouseReleaseEvent(self, event):
        target = self._target_at(self._to_design(event.position()))
        pressed, self._pressed = self._pressed, None
        if target is None or target != pressed:
            return
        if target in ALERT_STATES:
            entry_id = self._result_entry
            self._result_timer.stop()
            self._clear_result()
            # The "!" and the hourglass open History at that dictation (Retry and Copy are there).
            # A "!" with no entry (no mic, didn't hear you) just goes away; the label already said why.
            opened = bool(entry_id) and self._open_panel("history", entry_id)
            if not opened and target == "limited":
                self._open_panel("usage")  # no History: at least show which limit was hit
        elif target in self._panels:
            self._toggle_panel(target)
        elif target == "pause" and self.on_pause is not None:
            self.on_pause(not self._paused)
        else:
            print(f"[pill] {BUTTON_TITLES[target]} clicked (placeholder)")
        QTimer.singleShot(100, self._check_focus)

    def _check_focus(self):
        """The hard rule: the pill must never become the active window."""
        if win32.foreground_window() == self._hwnd:
            print("FOCUS STOLEN: the pill became the active window. Pastes would go to the wrong place!")
        elif self.debug:
            print("[pill] focus check ok (the pill is not the active window)")

    def nativeEvent(self, event_type, message):
        # Belt and braces: when clicked, tell Windows "don't activate me".
        if win32.message_id(int(message)) == win32.WM_MOUSEACTIVATE:
            return True, win32.MA_NOACTIVATE
        return super().nativeEvent(event_type, message)

    # ---- live settings from config.json ----

    def _read_config_mtime(self):
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return None

    def _poll_config(self):
        mtime = self._read_config_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            settings = load_config()["pill"]
            resize_curve = bezier_curve(settings["resize_easing"])
            if not settings["scale"] > 0:
                raise ValueError("scale must be above 0")
            if not settings["wave_sensitivity"] > 0:
                raise ValueError("wave_sensitivity must be above 0")
            if not 0 <= settings["wave_min_height"] <= settings["wave_max_height"]:
                raise ValueError("wave_min_height must be between 0 and wave_max_height")
            for key in ("wave_attack_ms", "wave_release_ms"):
                if not settings[key] >= 0:
                    raise ValueError(f"{key} can't be negative")
            if not settings["wave_step_ms"] > 0:
                raise ValueError("wave_step_ms must be above 0")
            for key in (
                "mic_label_gap", "mic_label_padding_left", "mic_label_padding_right",
                "mic_label_icon_size", "mic_label_icon_gap", "mic_label_rise", "mic_label_hold_ms",
            ):
                if not settings[key] >= 0:
                    raise ValueError(f"{key} can't be negative")
            if not settings["mic_label_height"] > 0:
                raise ValueError("mic_label_height must be above 0")
            if not self._label_text_room(settings) > 0:
                raise ValueError("mic_label_max_width leaves no room for the name (paddings and icon take it all)")
        except (OSError, ValueError, TypeError, KeyError) as e:
            print(f"[pill] config.json has a mistake, keeping the old settings: {e}")
            return
        self._settings = settings
        self._resize_curve = resize_curve
        for panel in self._panels.values():
            panel.set_settings(settings)
        self._apply_layout()
        self._place()
        now = _now_ms()
        self._animate_to_state(now)  # e.g. a wider menu grows to its new width
        showing = self._label_next or self._label
        if showing is not None:
            self._label_w.jump(self._label_display(showing)[1])  # new paddings or max width
        self._update_label(now)  # e.g. "mic_label" turned off
        self.update()
        print("[pill] settings reloaded from config.json")
        self._refresh()

    def _poll_fullscreen(self):
        fullscreen = win32.fullscreen_app_active(self._hwnd)
        if fullscreen != self._fullscreen:
            self._fullscreen = fullscreen
            if self.debug:
                print(f"[pill] fullscreen app {'focused' if fullscreen else 'gone'}")
            if fullscreen and self._settings["hide_in_fullscreen"]:
                self._close_panel()
            self._refresh()

    # ---- painting ----

    def _pill_rect(self):
        width, height = self._w.value, self._h.value
        return QRectF((self._canvas_w - width) / 2, self._pill_bottom - height, width, height)

    def _menu_layout(self):
        """Where the label, divider and buttons go, relative to the pill's center.

        Returns (layout, total width). The hover pill is exactly that wide.
        """
        s = self._settings
        label_w = QFontMetricsF(self._font).horizontalAdvance(self._menu_label)
        count = len(BUTTONS)
        gap = s["menu_button_gap"]
        total = math.ceil(
            s["menu_padding_left"] + label_w + LABEL_MARGIN + DIVIDER_GAP + 1 + DIVIDER_MARGIN + DIVIDER_GAP
            + count * BUTTON + (count - 1) * gap + s["menu_padding_right"]
        )
        height = SIZES["hover"][1]
        x = -total / 2 + s["menu_padding_left"]
        label = QRectF(x, -height / 2, label_w + 1, height)
        x += label_w + LABEL_MARGIN + DIVIDER_GAP
        divider = QRectF(x, -DIVIDER_H / 2, 1, DIVIDER_H)
        x += 1 + DIVIDER_MARGIN + DIVIDER_GAP
        buttons = []
        for name, _ in BUTTONS:
            buttons.append((name, QRectF(x, -BUTTON / 2, BUTTON, BUTTON)))
            x += BUTTON + gap
        return {"label": label, "divider": divider, "buttons": buttons}, total

    def paintEvent(self, event):
        rect = self._pill_rect()
        pill_visible = rect.width() >= 0.5 and rect.height() >= 0.5
        label = self._label_rect() if self._label_visible() else None
        if not pill_visible and label is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(Qt.PenStyle.NoPen)
        scale = self._settings["scale"]
        painter.scale(scale, scale)  # everything below is in design pixels

        # Both shadows first, so the label's shadow never darkens the pill.
        if pill_visible:
            self._paint_shadow(painter, rect)
        if label is not None:
            s = self._settings
            strength = s["shadow_opacity"] * self._label_opacity.value
            paint_soft_shadow(painter, label, label.height() / 2, strength, s["shadow_blur"], s["shadow_offset_y"])

        if pill_visible:
            painter.save()
            radius = min(rect.width(), rect.height()) / 2
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.fillPath(path, _color(BLUE, self._fill.value))
            if self._outline.value > 0.001:
                self._paint_outline(painter, rect)

            if self._content is not None and self._content_opacity.value > 0.001:
                painter.setClipPath(path)  # content never spills outside the pill
                painter.translate(rect.center())
                content_scale = self._content_scale.value
                painter.scale(content_scale, content_scale)
                painter.setOpacity(self._content_opacity.value)
                getattr(self, f"_paint_{self._content}")(painter)
            painter.restore()

        if label is not None:
            self._paint_mic_label(painter, label)
        painter.end()

    def _paint_mic_label(self, painter, rect):
        s = self._settings
        opacity = self._label_opacity.value
        radius = rect.height() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setOpacity(opacity)
        painter.fillPath(path, QColor(*BLUE))

        painter.setClipPath(path)  # while the width animates, the name never spills out
        painter.setOpacity(opacity * self._label_text_opacity.value)
        icon = self._label[1]
        shown = self._label_display(self._label)[0]
        icon_size = s["mic_label_icon_size"]
        x = rect.left() + s["mic_label_padding_left"]
        self._paint_icon(painter, icon, icon_size, QPointF(x + icon_size / 2, rect.center().y()))
        x += icon_size + s["mic_label_icon_gap"]
        painter.setFont(self._font)
        painter.setPen(QColor(*WHITE))
        text_rect = QRectF(x, rect.top(), self._metrics.horizontalAdvance(shown) + 2, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)

    def _paint_outline(self, painter, rect):
        """The paused idle pill: a thin outline just inside the pill's edge."""
        inset = PAUSED_OUTLINE_W / 2
        inner = rect.adjusted(inset, inset, -inset, -inset)
        radius = min(inner.width(), inner.height()) / 2
        painter.save()
        painter.setPen(QPen(_color(PAUSED_OUTLINE, self._outline.value), PAUSED_OUTLINE_W))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(inner, radius, radius)
        painter.restore()

    def _paint_shadow(self, painter, rect):
        s = self._settings
        radius = min(rect.width(), rect.height()) / 2  # fully round
        paint_soft_shadow(painter, rect, radius, self._shadow.value, s["shadow_blur"], s["shadow_offset_y"])

    def _paint_icon(self, painter, name, size, center):
        # Drawn at the final on-screen pixel size, so "scale" keeps icons sharp.
        image = icon_image(name, size, self.devicePixelRatioF() * self._settings["scale"])
        painter.drawImage(QRectF(center.x() - size / 2, center.y() - size / 2, size, size), image)

    def _paint_menu(self, painter):
        menu = self._menu
        painter.setFont(self._font)
        painter.setPen(QColor(*WHITE))
        painter.drawText(menu["label"], Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._menu_label)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillRect(menu["divider"], _color(WHITE, 0.32))
        for name, rect in menu["buttons"]:
            if name == self._open_name():
                painter.setBrush(_color(WHITE, 0.22))  # "this one is open", from the mockup
                painter.drawEllipse(rect)
            elif name == self._hover_button:
                painter.setBrush(_color(WHITE, 0.18))
                painter.drawEllipse(rect)
            icon = PAUSED_BUTTON[0] if name == "pause" and self._paused else name
            self._paint_icon(painter, icon, MENU_ICON, rect.center())

    def _paint_bars(self, painter):
        painter.setBrush(QColor(*WHITE))
        total = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
        x = -total / 2
        for height in self._bar_levels:
            painter.drawRoundedRect(QRectF(x, -height / 2, BAR_W, height), BAR_W / 2, BAR_W / 2)
            x += BAR_W + BAR_GAP

    def _paint_dots(self, painter):
        cycle = self._settings["dots_cycle_ms"] / 1000
        t = time.perf_counter()
        total = DOT_COUNT * DOT_SIZE + (DOT_COUNT - 1) * DOT_GAP
        x = -total / 2
        for i in range(DOT_COUNT):
            phase = (t / cycle - i * DOT_STAGGER) % 1.0
            wave = 0.5 - 0.5 * math.cos(2 * math.pi * phase)  # 0 -> 1 -> 0, smooth
            painter.setBrush(_color(WHITE, DOT_MIN_OPACITY + (1 - DOT_MIN_OPACITY) * wave))
            painter.drawEllipse(QRectF(x, -DOT_SIZE / 2 - DOT_LIFT * wave, DOT_SIZE, DOT_SIZE))
            x += DOT_SIZE + DOT_GAP

    def _paint_check(self, painter):
        self._paint_icon(painter, "check", STATUS_ICON, QPointF(0, 0))

    def _paint_copied(self, painter):
        self._paint_icon(painter, "copy", STATUS_ICON, QPointF(0, 0))

    def _paint_paused(self, painter):
        self._paint_icon(painter, "pause", STATUS_ICON, QPointF(0, 0))

    def _paint_hourglass(self, painter):
        self._paint_icon(painter, "hourglass", STATUS_ICON, QPointF(0, 0))

    def _paint_error(self, painter):
        # Drawn directly: an SVG dot this small comes out blurry.
        pen = QPen(QColor(*WHITE), ERROR_STROKE)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, -4.5), QPointF(0, 1.5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*WHITE))
        painter.drawEllipse(QPointF(0, 4.8), ERROR_STROKE * 0.6, ERROR_STROKE * 0.6)
