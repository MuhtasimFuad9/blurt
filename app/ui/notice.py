"""The cancel notice: a small window in the idle pill's spot that says a dictation was
cancelled because another key was pressed in its first second (M17 item 14).

A separate window from the pill, built the same way (see pill.py and win32.py):
- It never becomes the active window and can't be clicked (clicks pass straight through),
  so the text cursor stays in your document.
- Everything is in Qt's logical pixels times the pill's "scale" setting, placed from the
  pill's own idle spot, with icons drawn at the screen's pixel ratio. So at 100%, 125% and
  150% display scaling it sits and scales exactly like the pill.
- It fades in, stays for error_hold_ms (like the "!"), fades out, and goes away at once
  when the pill starts recording or opens its menu.
It has no settings of its own: sizes come from the pill's mic label settings.
"""

import math
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from app.ui import win32
from app.ui.icons import icon_image
from app.ui.pill import BLUE, CANVAS_MARGIN, CSS_EASE, FRAME_MS, WHITE, Tween, bezier_curve, paint_soft_shadow, pill_font

TEXT = "Dictation cancelled: another key was pressed"
ICON = "alert"
HIDE_STATES = ("recording", "hover")  # the pill needs its spot back
FOCUS_CHECK_MS = 100


def _now_ms():
    return time.perf_counter() * 1000


class CancelNotice(QWidget):
    def __init__(self, pill, debug=False):
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

        self._pill = pill
        self.debug = debug
        self._font = pill_font()
        self._metrics = QFontMetricsF(self._font)
        self._fade_curve = bezier_curve(CSS_EASE)
        self._opacity = Tween(0.0)

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(FRAME_MS)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self._tick)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self.dismiss)
        self._apply_layout()

        self._hwnd = int(self.winId())  # creates the real Windows window
        win32.make_no_activate(self._hwnd)
        win32.set_click_through(self._hwnd, True)
        pill.state_changed.connect(self._pill_state)

    # ---- showing and hiding ----

    @Slot()
    def show_notice(self):
        """Show it (or show it again) for error_hold_ms. GUI thread, via UiBridge."""
        s = self._pill.settings
        self._apply_layout()  # the pill's settings may have changed since (scale, sizes)
        self._place()
        if not self.isVisible():
            self.show()
        # Re-apply after every show, in case Windows reset anything (like the pill's _show).
        win32.make_no_activate(self._hwnd)
        win32.set_click_through(self._hwnd, True)
        win32.keep_on_top(self._hwnd)
        self._opacity.to(1.0, s["content_in_ms"], self._fade_curve, _now_ms())
        self._frame_timer.start()
        self._hold_timer.start(s["error_hold_ms"])
        if self.debug:
            print("[notice] dictation cancelled")
        QTimer.singleShot(FOCUS_CHECK_MS, self._check_focus)

    @Slot()
    def dismiss(self):
        """Fade out, then hide."""
        self._hold_timer.stop()
        self._opacity.to(0.0, self._pill.settings["content_out_ms"], self._fade_curve, _now_ms())
        self._frame_timer.start()

    def hide_now(self):
        self._hold_timer.stop()
        self._frame_timer.stop()
        self._opacity.jump(0.0)
        self.hide()

    @Slot(str)
    def _pill_state(self, state):
        if state in HIDE_STATES and self.isVisible():
            self.hide_now()

    def _tick(self):
        moving = self._opacity.update(_now_ms())
        self.update()
        if not moving:
            self._frame_timer.stop()
            if self._opacity.value <= 0.001:
                self.hide()

    def _check_focus(self):
        """The hard rule: it must never become the active window."""
        if win32.foreground_window() == self._hwnd:
            print("FOCUS STOLEN: the cancel notice became the active window. Pastes would go to the wrong place!")

    def nativeEvent(self, event_type, message):
        # Belt and braces, like the pill: never "activate me" on a click.
        if win32.message_id(int(message)) == win32.WM_MOUSEACTIVATE:
            return True, win32.MA_NOACTIVATE
        return super().nativeEvent(event_type, message)

    # ---- size and place ----

    def _apply_layout(self):
        """The window fits the notice plus its shadow, times "scale" (like Pill._apply_layout)."""
        s = self._pill.settings
        self._text_w = self._metrics.horizontalAdvance(TEXT)
        self._notice_w = math.ceil(
            s["mic_label_padding_left"] + s["mic_label_icon_size"] + s["mic_label_icon_gap"]
            + self._text_w + s["mic_label_padding_right"]
        )
        self._notice_h = s["mic_label_height"]
        blur, offset = s["shadow_blur"], s["shadow_offset_y"]
        side = blur + CANVAS_MARGIN
        top = max(0, blur - offset) + CANVAS_MARGIN
        bottom = max(0, blur + offset) + CANVAS_MARGIN
        scale = s["scale"]
        window_w = math.ceil((self._notice_w + 2 * side) * scale)
        window_w += window_w % 2  # even, so the center lands on a whole pixel
        window_h = math.ceil((top + self._notice_h + bottom) * scale)
        self._canvas_w = window_w / scale  # in design pixels
        self._bottom = top + self._notice_h  # y of the notice's bottom edge, in design pixels
        self.setFixedSize(window_w, window_h)

    def _place(self):
        """Centered on the idle pill, its bottom on the idle pill's bottom, inside the screen."""
        center_x, bottom = self._pill.idle_spot()
        x = round(center_x - self.width() / 2)
        y = bottom - round(self._bottom * self._pill.settings["scale"])
        area = QGuiApplication.primaryScreen().availableGeometry()
        x = max(area.x(), min(x, area.x() + area.width() - self.width()))
        self.move(x, y)

    def _notice_rect(self):
        """The blue notice inside the window, in design pixels."""
        return QRectF((self._canvas_w - self._notice_w) / 2, self._bottom - self._notice_h,
                      self._notice_w, self._notice_h)

    def notice_rect_on_screen(self):
        """The blue notice in screen pixels (for tests and the preview tool)."""
        rect, scale = self._notice_rect(), self._pill.settings["scale"]
        return QRectF(self.x() + rect.x() * scale, self.y() + rect.y() * scale,
                      rect.width() * scale, rect.height() * scale)

    # ---- painting ----

    def paintEvent(self, event):
        opacity = self._opacity.value
        if opacity <= 0.001:
            return
        s = self._pill.settings
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(Qt.PenStyle.NoPen)
        scale = s["scale"]
        painter.scale(scale, scale)  # everything below is in design pixels
        rect = self._notice_rect()
        radius = rect.height() / 2
        paint_soft_shadow(painter, rect, radius, s["shadow_opacity"] * opacity, s["shadow_blur"], s["shadow_offset_y"])
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setOpacity(opacity)
        painter.fillPath(path, QColor(*BLUE))
        painter.setClipPath(path)
        icon_size = s["mic_label_icon_size"]
        x = rect.left() + s["mic_label_padding_left"]
        image = icon_image(ICON, icon_size, self.devicePixelRatioF() * scale)  # sharp at any scaling
        center_y = rect.center().y()
        painter.drawImage(QRectF(x, center_y - icon_size / 2, icon_size, icon_size), image)
        x += icon_size + s["mic_label_icon_gap"]
        painter.setFont(self._font)
        painter.setPen(QColor(*WHITE))
        text_rect = QRectF(x, rect.top(), self._text_w + 2, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, TEXT)
        painter.end()

    def icon_pixel_size(self):
        """How many real pixels wide the icon is drawn (for the scaling test)."""
        s = self._pill.settings
        return icon_image(ICON, s["mic_label_icon_size"], self.devicePixelRatioF() * s["scale"]).width()
