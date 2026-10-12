"""The panels above the pill: the shared Panel base class, and the Usage panel.

- Each panel is a separate see-through window just above the pill, painted by
  hand like the pill (real Qt buttons try to take focus). It never becomes the
  active window.
- The pill decides when a panel opens and closes (see pill.py). Clicking the X
  or the panel's own shadow closes it from here.
- Sizes are "design pixels"; the pill's "scale" setting multiplies them too.
- A panel's card is anchored at its bottom edge (just above the pill), so a
  card that changes height grows or shrinks upward.

Panel does everything the panels share: the no-focus window, the open/close
motion, placement, the header with its close button, the focus check, and a
scrolling list (mouse wheel, glide, faded edges, scroll bar) for panels that have one.
A subclass fills in the content (see the "for subclasses" methods).

A panel with FITS_SCREEN (Settings, Usage; M16) never reaches above the top of the
screen: when its card doesn't fit, it gets shorter and everything under the header
scrolls (the same wheel, glide, faded edges and scroll bar as a list). Where it fits,
nothing changes, down to the pixel.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from app.config import load_config_or_defaults
from app.ui import win32
from app.ui.icons import icon_image
from app.ui.pill import BLUE, CSS_EASE, FRAME_MS, WHITE, Tween, _color, _now_ms, bezier_curve, paint_soft_shadow
from app.usage import METER_LABELS, format_ago, format_amount, format_wait, meter_style

AMBER = (0xFF, 0xC2, 0x4B)
ORANGE = (0xFF, 0x8A, 0x4C)
LEVEL_COLORS = {"normal": WHITE, "close": AMBER, "out": ORANGE}
SOFT = 0.82  # secondary text: white at 82%

# Layout shared by every panel, from the Usage mockup (Usage.dc.html)
WIDTH = 300
PAD = 16
RADIUS = 14
SECTION_GAP = 14
CLOSE_BUTTON = 28
CLOSE_ICON = 13
BANNER_PAD = 7
BANNER_ICON = 13
GAP_ABOVE_PILL = 10  # between the panel and the top of the hover pill

# Shadow: the mockup's "0 12px 28px" at 30%. Our soft edge spreads a bit wider
# than CSS blur, so it's scaled down the same way as the pill's.
SHADOW_OPACITY = 0.30
SHADOW_BLUR = 20
SHADOW_OFFSET = 12
CANVAS_MARGIN = 4

# Open/close motion: fades in while rising a few px and growing from the bottom center.
RISE = 6
START_SCALE = 0.96

# Scrolling lists (History, Dictionary)
SCROLL_STEP = 60  # design pixels per wheel notch
SCROLL_MS = 200
FADE = 16  # the list fades out at an edge with more to scroll
SCROLLBAR_W = 3


def _font(px, weight=QFont.Weight.Normal):
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"])
    font.setPointSizeF(px * 0.75)  # px to pt (1pt = 1.333px at 100%)
    font.setWeight(weight)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


class Panel(QWidget):
    closed = Signal()  # the panel started closing (the pill updates its look)

    NAME = "panel"  # for console messages
    TITLE = ""
    FITS_SCREEN = False  # True: shorter than the screen, the content scrolls (see the module docstring)

    def __init__(self, debug=False):
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

        self.debug = debug
        self._open = False
        self._anchor = (0.0, 0.0)  # (center x, top of the hover pill), screen pixels
        self._screen_top = None  # the top of the pill's screen (minus a top taskbar), screen pixels
        self._cap = None  # FITS_SCREEN: the tallest the card may be (design px), None = no limit
        self._shown = Tween(0.0)  # 0 = hidden, 1 = fully open
        self._hover_close = False
        self._pressed = None
        self._mouse = None  # last mouse position (design px), to re-check hover after scrolling
        self._scroll = Tween(0.0)  # how far a scrolling list is scrolled, design px
        self._total_h = 0.0  # a scrolling list's full height

        self._fonts = {
            "title": _font(14, QFont.Weight.DemiBold),
            "subtitle": _font(11),
            "label": _font(12, QFont.Weight.Medium),
            "amount": _font(12),
            "small": _font(10.5),
            "banner": _font(11, QFont.Weight.Medium),
            "number": _font(16, QFont.Weight.DemiBold),
            "tile": _font(11),
        }
        self._fonts.update(self._extra_fonts())
        self._line = {name: QFontMetricsF(font).height() for name, font in self._fonts.items()}

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(FRAME_MS)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self._tick)

        self._init_content()
        self.set_settings(load_config_or_defaults()[0]["pill"])
        self._hwnd = int(self.winId())  # creates the real Windows window
        win32.make_no_activate(self._hwnd)

    # ---- for subclasses ----

    def _extra_fonts(self):
        """More fonts, as {name: QFont}."""
        return {}

    def _init_content(self):
        """Set up the content's own state (runs before the first layout)."""

    def _subtitle(self):
        return ""

    def _content_height(self):
        """Height of everything below the header, right now."""
        return 0

    def _max_content_height(self):
        """The tallest the content can get (the window is made this big once)."""
        return self._content_height()

    def _paint_content(self, painter, x, y, width):
        """Paint everything below the header."""

    def _paint_header_extra(self, painter, x, y, width):
        """Anything else in the header row, left of the close button."""

    def _on_open(self):
        pass

    def _on_close(self):
        pass

    def _animate(self, now):
        """Move the content's own animations along. Return True while any is still moving."""
        return False

    def _content_target(self, pos):
        """What is under pos on the card (not the close button), or None."""
        return None

    def _content_release(self, target):
        """A click (press and release over the same target) on the content."""

    def _content_hover(self, pos):
        """The mouse moved over the panel (pos is None when it left)."""

    def _list_height(self):
        """How tall a scrolling list's visible part is (0 = the panel has no list).
        With FITS_SCREEN: how much of the content shows under the header (all of it when it fits)."""
        if not self.FITS_SCREEN:
            return 0
        content = self._content_height()
        cap = self._card_cap()
        return content if cap is None else min(content, cap - self._card_height(0))

    # ---- what the pill calls ----

    @property
    def is_open(self):
        return self._open

    def set_settings(self, settings):
        """The pill's settings: scale and motion. Called again when config.json changes."""
        self._settings = settings
        self._open_curve = bezier_curve(settings["resize_easing"])
        self._close_curve = bezier_curve(CSS_EASE)
        self._resize_window()

    def set_anchor(self, center_x, pill_top, screen_top=None):
        """Where the hover pill is: its center x and its top edge, in screen pixels. screen_top is
        the top of the screen's available area (None = unknown: the window can reach above it)."""
        self._anchor = (center_x, pill_top)
        self._screen_top = screen_top
        if self._compute_card_cap() != self._cap:
            self._resize_window()  # more or less room: a FITS_SCREEN panel changes height
        else:
            self._place()

    def open_panel(self):
        if self._open:
            return
        self._open = True
        self._on_open()
        self._place()
        if not self.isVisible():
            self.show()
        # Re-apply after every show, in case Windows reset anything.
        win32.make_no_activate(self._hwnd)
        win32.keep_on_top(self._hwnd)
        self._shown.to(1.0, self._settings["content_in_ms"], self._open_curve, _now_ms())
        self._start_frames()
        if self.debug:
            print(f"[panel] {self.NAME} open")

    def close_panel(self):
        if not self._open:
            return
        self._open = False
        self._on_close()
        self._hover_close = False
        self._shown.to(0.0, self._settings["content_out_ms"], self._close_curve, _now_ms())
        self._start_frames()
        if self.debug:
            print(f"[panel] {self.NAME} closed")
        self.closed.emit()

    def contains_global(self, point):
        """True if a screen point is on the panel's card (not its shadow)."""
        local = self.mapFromGlobal(point)
        scale = self._settings["scale"]
        return self._open and self._card_rect().contains(QPointF(local.x() / scale, local.y() / scale))

    # ---- size and position ----

    def _header_height(self):
        line = self._line
        return max(line["title"] + 2 + line["subtitle"], CLOSE_BUTTON - 6)

    def _card_height(self, content_height):
        return PAD + self._header_height() + SECTION_GAP + content_height + PAD

    def _banner_height(self):
        return BANNER_PAD + self._line["banner"] + 1 + self._line["small"] + BANNER_PAD

    def _margins(self):
        side = SHADOW_BLUR + CANVAS_MARGIN
        top = max(0, SHADOW_BLUR - SHADOW_OFFSET) + CANVAS_MARGIN
        bottom = SHADOW_BLUR + SHADOW_OFFSET + CANVAS_MARGIN
        return side, top, bottom

    def _resize_window(self):
        """Big enough for the tallest card plus its shadow. The card sits at the bottom."""
        side, top, bottom = self._margins()
        scale = self._settings["scale"]
        card_h = self._card_height(self._max_content_height())
        self._cap = self._compute_card_cap()
        if self._cap is not None:
            card_h = min(card_h, self._cap)
        self._canvas_h = top + card_h + bottom  # design pixels
        self.setFixedSize(round((WIDTH + 2 * side) * scale), round(self._canvas_h * scale))
        self._place()
        self._fit_scroll()

    def _card_cap(self):
        """The tallest the card may be (design px), or None: worked out when the window was sized."""
        return self._cap

    def _compute_card_cap(self):
        """With FITS_SCREEN: the tallest card that keeps the whole window on the screen, or None
        (not a FITS_SCREEN panel, no screen known, or the tallest card fits anyway)."""
        if not self.FITS_SCREEN or self._screen_top is None:
            return None
        _, top, _ = self._margins()
        scale = self._settings["scale"]
        room = (self._anchor[1] - GAP_ABOVE_PILL * scale - self._screen_top) / scale - top  # design pixels
        tallest = self._card_height(self._max_content_height())
        if tallest <= room:
            return None
        # Shorter by whole screen pixels, so the card keeps its exact sub-pixel position
        # (it renders pixel-identical wherever it fits). At least the header stays.
        cut = math.ceil((tallest - room) * scale - 1e-6) / scale
        return max(tallest - cut, self._card_height(0))

    def _place(self):
        center_x, pill_top = self._anchor
        _, _, bottom = self._margins()
        scale = self._settings["scale"]
        card_bottom = pill_top - GAP_ABOVE_PILL * scale
        self.move(round(center_x - self.width() / 2), round(card_bottom - (self._canvas_h - bottom) * scale))

    def _card_rect(self):
        side, _, bottom = self._margins()
        height = self._card_height(self._content_height())
        cap = self._card_cap()
        if cap is not None:
            height = min(height, cap)
        return QRectF(side, self._canvas_h - bottom - height, WIDTH, height)

    def _view_rect(self):
        """With FITS_SCREEN: the part of the card under the header where the content shows."""
        card = self._card_rect()
        top = card.top() + PAD + self._header_height() + SECTION_GAP
        return QRectF(card.left() + PAD, top, WIDTH - 2 * PAD, self._list_height())

    def _fit_scroll(self):
        """With FITS_SCREEN: after the content or the room changed, keep the scroll in range."""
        if not self.FITS_SCREEN:
            return
        self._total_h = self._content_height()
        if self._scroll.end > self._max_scroll():
            self._scroll_to(self._max_scroll(), animate=False)  # a glide still in range keeps going

    def _close_rect(self):
        card = self._card_rect()
        return QRectF(card.right() - PAD - CLOSE_BUTTON + 6, card.top() + PAD - 6, CLOSE_BUTTON, CLOSE_BUTTON)

    # ---- animation ----

    def _tick(self):
        now = _now_ms()
        moving = self._shown.update(now)
        if self._scroll.update(now):
            moving = True
            if self._mouse is not None:
                self._content_hover(self._mouse)  # the list moved under the mouse
        moving = self._animate(now) or moving
        self.update()
        if not moving:
            self._frame_timer.stop()
            if not self._open and self._shown.value <= 0.001:
                self.hide()

    def _start_frames(self):
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    # ---- scrolling ----

    def _max_scroll(self):
        return max(0.0, self._total_h - self._list_height())

    def _scroll_to(self, target, animate=True):
        target = max(0.0, min(self._max_scroll(), target))
        if animate:
            self._scroll.to(target, SCROLL_MS, self._open_curve, _now_ms())
            self._start_frames()
        else:
            self._scroll.jump(target)

    def wheelEvent(self, event):
        # Windows sends the wheel to the window under the cursor, so this works without focus.
        if not self._open or self._max_scroll() <= 0:
            event.ignore()
            return
        pixels = event.pixelDelta().y()
        delta = pixels if pixels else event.angleDelta().y() / 120 * SCROLL_STEP
        self._scroll_to(self._scroll.end - delta)
        event.accept()

    def _paint_fitted(self, painter, paint):
        """With FITS_SCREEN: run paint() for the content, clipped under the header while it scrolls."""
        scrolling = self._max_scroll() > 0
        if scrolling:  # only then, so a card that fits paints exactly as before
            painter.save()
            view = self._view_rect()
            painter.setClipRect(QRectF(view.left() - PAD, view.top(), view.width() + 2 * PAD, view.height()))
        paint()
        if scrolling:
            painter.restore()
            self._paint_scroll_edges(painter, self._view_rect())

    def _paint_scroll_edges(self, painter, list_rect):
        """Fade the list out at an edge that has more to scroll, and show where you are."""
        max_scroll = self._max_scroll()
        if max_scroll <= 0:
            return
        scroll = self._scroll.value
        for edge, strength in ((list_rect.top(), min(1.0, scroll / FADE)),
                               (list_rect.bottom(), min(1.0, (max_scroll - scroll) / FADE))):
            if strength <= 0.01:
                continue
            inward = FADE if edge == list_rect.top() else -FADE
            gradient = QLinearGradient(0, edge, 0, edge + inward)
            gradient.setColorAt(0, _color(BLUE, strength))
            gradient.setColorAt(1, _color(BLUE, 0))
            band = QRectF(list_rect.left() - 2, min(edge, edge + inward), list_rect.width() + 4, FADE)
            painter.fillRect(band, gradient)
        # A thin scroll indicator in the card's right padding
        track_h = list_rect.height()
        thumb_h = max(24.0, track_h * track_h / self._total_h)
        thumb_y = list_rect.top() + (track_h - thumb_h) * (scroll / max_scroll)
        painter.setBrush(_color(WHITE, 0.30))
        thumb = QRectF(list_rect.right() + 6, thumb_y, SCROLLBAR_W, thumb_h)
        painter.drawRoundedRect(thumb, SCROLLBAR_W / 2, SCROLLBAR_W / 2)

    # ---- mouse ----

    def _to_design(self, pos):
        scale = self._settings["scale"]
        return QPointF(pos.x() / scale, pos.y() / scale)

    def mouseMoveEvent(self, event):
        pos = self._to_design(event.position())
        self._mouse = pos if self._open else None
        hover = self._open and self._close_rect().contains(pos)
        if hover != self._hover_close:
            self._hover_close = hover
            self.update()
        self._content_hover(pos if self._open else None)

    def mousePressEvent(self, event):
        pos = self._to_design(event.position())
        if self._close_rect().contains(pos):
            self._pressed = "close"
        elif not self._card_rect().contains(pos):
            self._pressed = None
            self.close_panel()  # a click on the shadow counts as outside
        else:
            self._pressed = self._content_target(pos)

    def mouseReleaseEvent(self, event):
        pos = self._to_design(event.position())
        pressed, self._pressed = self._pressed, None
        if pressed == "close":
            if self._close_rect().contains(pos):
                self.close_panel()
        elif pressed is not None and self._content_target(pos) == pressed:
            self._content_release(pressed)
        QTimer.singleShot(100, self._check_focus)

    def leaveEvent(self, event):
        self._mouse = None
        if self._hover_close:
            self._hover_close = False
            self.update()
        self._content_hover(None)

    def _check_focus(self):
        """The hard rule: a panel must never become the active window."""
        if win32.foreground_window() == self._hwnd:
            print(f"FOCUS STOLEN: the {self.NAME} panel became the active window. Pastes would go to the wrong place!")
        elif self.debug:
            print(f"[panel] focus check ok (the {self.NAME} panel is not the active window)")

    def nativeEvent(self, event_type, message):
        # Belt and braces: when clicked, tell Windows "don't activate me".
        if win32.message_id(int(message)) == win32.WM_MOUSEACTIVATE:
            return True, win32.MA_NOACTIVATE
        return super().nativeEvent(event_type, message)

    # ---- painting ----

    def _ready_to_paint(self):
        return True

    def paintEvent(self, event):
        shown = self._shown.value
        if shown <= 0.001 or not self._ready_to_paint():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(Qt.PenStyle.NoPen)
        scale = self._settings["scale"]
        painter.scale(scale, scale)  # everything below is in design pixels
        painter.setOpacity(shown)

        card = self._card_rect()
        # Open/close motion, around the bottom center (closest to the pill).
        grow = START_SCALE + (1 - START_SCALE) * shown
        painter.translate(card.center().x(), card.bottom() + (1 - shown) * RISE)
        painter.scale(grow, grow)
        painter.translate(-card.center().x(), -card.bottom())

        paint_soft_shadow(painter, card, RADIUS, SHADOW_OPACITY, SHADOW_BLUR, SHADOW_OFFSET)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)
        painter.fillPath(path, QColor(*BLUE))

        x = card.left() + PAD
        width = WIDTH - 2 * PAD
        y = self._paint_header(painter, x, card.top() + PAD, width)
        self._paint_content(painter, x, y, width)
        painter.end()

    def _text(self, painter, rect, text, font, color=WHITE, alpha=1.0, align=Qt.AlignmentFlag.AlignLeft):
        painter.setFont(self._fonts[font])
        painter.setPen(_color(color, alpha))
        painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)
        painter.setPen(Qt.PenStyle.NoPen)

    def _icon(self, painter, name, size, center, color="#ffffff"):
        # Drawn at the final on-screen pixel size, so "scale" keeps icons sharp.
        image = icon_image(name, size, self.devicePixelRatioF() * self._settings["scale"], color)
        painter.drawImage(QRectF(center.x() - size / 2, center.y() - size / 2, size, size), image)

    def _paint_header(self, painter, x, y, width):
        line = self._line
        self._text(painter, QRectF(x, y, width, line["title"]), self.TITLE, "title")
        self._text(painter, QRectF(x, y + line["title"] + 2, width, line["subtitle"]), self._subtitle(), "subtitle",
                   alpha=SOFT)
        self._paint_header_extra(painter, x, y, width)
        close = self._close_rect()
        if self._hover_close:
            painter.setBrush(_color(WHITE, 0.18))
            painter.drawEllipse(close)
        self._icon(painter, "close", CLOSE_ICON, close.center())
        return y + self._header_height() + SECTION_GAP

    def _paint_banner(self, painter, x, y, width, icon, title, detail):
        """A highlighted notice under the header (a rate limit, "History is off")."""
        line = self._line
        height = self._banner_height()
        painter.setBrush(_color(WHITE, 0.10))
        painter.drawRoundedRect(QRectF(x, y, width, height), 8, 8)
        icon_x = x + 10
        self._icon(painter, icon, BANNER_ICON, QPointF(icon_x + BANNER_ICON / 2, y + height / 2))
        text_x = icon_x + BANNER_ICON + 8
        text_w = x + width - 10 - text_x
        self._text(painter, QRectF(text_x, y + BANNER_PAD, text_w, line["banner"]), title, "banner")
        self._text(painter, QRectF(text_x, y + BANNER_PAD + line["banner"] + 1, text_w, line["small"]), detail,
                   "small", alpha=SOFT)
        return y + height + SECTION_GAP


# ---- the Usage panel ----

METER_GAP = 11
LINE_GAP = 5  # between a meter's label row, its track and its note row
TRACK_H = 4
TILE_GAP = 6
TILE_PAD_X, TILE_PAD_Y = 10, 8
TILE_RADIUS = 9
DOT = 6

LIVE_MS = 1000

METERS = list(METER_LABELS.items())  # (key, label), in panel order

CONNECTION = {  # status: (dot color, dot opacity, text)
    "connected": (WHITE, 1.0, "Groq connected"),
    "unreachable": (AMBER, 1.0, "Groq unreachable"),
    "key_rejected": (ORANGE, 1.0, "Groq key rejected"),
    None: (WHITE, 0.4, "No requests yet"),
}


class UsagePanel(Panel):
    """How much of Groq's free tier is used. Opens from the pill's Usage button.

    While open, it reads UsageTracker.snapshot() once a second (which never
    touches the disk). While closed, it does nothing at all.
    """

    NAME = "usage"
    TITLE = "Usage"
    FITS_SCREEN = True  # on a small screen, everything under the header scrolls (M16)

    def __init__(self, tracker, debug=False):
        self._tracker_arg = tracker
        super().__init__(debug)

    def _init_content(self):
        self.tracker = self._tracker_arg
        self._snapshot = None
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_MS)
        self._live_timer.timeout.connect(self._refresh_data)

    def _subtitle(self):
        return "Groq free tier"

    def _on_open(self):
        self._scroll.jump(0.0)  # always open at the top
        self._refresh_data()
        self._live_timer.start()

    def _on_close(self):
        self._live_timer.stop()

    def _refresh_data(self):
        self._snapshot = self.tracker.snapshot()
        self._fit_scroll()  # the rate-limit banner may have come or gone
        self.update()

    def _ready_to_paint(self):
        return self._snapshot is not None

    def _content_height(self, with_banner=None):
        if with_banner is None:
            with_banner = self._snapshot is not None and self._snapshot["rate_limit"] is not None
        line = self._line
        meter = line["label"] + LINE_GAP + TRACK_H + LINE_GAP + line["small"]
        tile = TILE_PAD_Y + line["number"] + 1 + line["tile"] + TILE_PAD_Y
        height = (
            5 * meter + 4 * METER_GAP + SECTION_GAP
            + 1 + SECTION_GAP
            + 2 * tile + TILE_GAP + SECTION_GAP
            + line["small"]
        )
        if with_banner:
            height += self._banner_height() + SECTION_GAP
        return height

    def _max_content_height(self):
        return self._content_height(with_banner=True)

    def _paint_content(self, painter, x, y, width):
        self._paint_fitted(painter, lambda: self._paint_everything(painter, x, y - self._scroll.value, width))

    def _paint_everything(self, painter, x, y, width):
        """The banner, the meters, the tiles and the footer, from y down."""
        limit = self._snapshot["rate_limit"]
        if limit is not None:
            # Only while a rate limit is active: what was limited, and a live countdown.
            y = self._paint_banner(painter, x, y, width, "hourglass", f"Rate limit: {limit['name'] or 'unknown limit'}",
                                   f"Try again in {format_wait(limit['retry_in'])}")
        y = self._paint_meters(painter, x, y, width)
        painter.fillRect(QRectF(x, y, width, 1), _color(WHITE, 0.22))
        y = self._paint_tiles(painter, x, y + 1 + SECTION_GAP, width)
        self._paint_footer(painter, x, y, width)

    def _paint_meters(self, painter, x, y, width):
        line = self._line
        right = Qt.AlignmentFlag.AlignRight
        for i, (key, label) in enumerate(METERS):
            meter = self._snapshot["meters"][key]
            used, limit = meter["used"], meter["limit"]
            _, level, note = meter_style(used, limit)
            color = LEVEL_COLORS[level]

            row = QRectF(x, y, width, line["label"])
            self._text(painter, row, label, "label")
            self._text(painter, row, format_amount(key, used, limit), "amount", align=right)

            track_y = y + line["label"] + LINE_GAP
            painter.setBrush(_color(WHITE, 0.18))
            painter.drawRoundedRect(QRectF(x, track_y, width, TRACK_H), TRACK_H / 2, TRACK_H / 2)
            fill = width * min(1.0, used / limit) if limit > 0 else 0
            if fill >= 0.5:
                painter.setBrush(QColor(*color))
                painter.drawRoundedRect(QRectF(x, track_y, fill, TRACK_H), TRACK_H / 2, TRACK_H / 2)

            # Color never carries the meaning alone: the note always says it in words too.
            note_row = QRectF(x, track_y + TRACK_H + LINE_GAP, width, line["small"])
            self._text(painter, note_row, note, "small", color, 1.0 if level != "normal" else SOFT)
            if meter["refilled_in"] >= 1:
                self._text(painter, note_row, f"Fully refilled in {format_wait(meter['refilled_in'])}", "small",
                           alpha=SOFT, align=right)
            y = note_row.bottom() + (METER_GAP if i < len(METERS) - 1 else SECTION_GAP)
        return y

    def _paint_tiles(self, painter, x, y, width):
        today = self._snapshot["today"]
        latency = today["avg_latency_ms"]
        tiles = [
            (f"{today['dictations']:,}", "Dictations today"),
            (f"{today['words']:,}", "Words dictated"),
            (f"{latency / 1000:.2f}s" if latency is not None else "n/a", "Average latency"),
            (f"{today['rate_limits']:,}", "Rate limits today"),
        ]
        line = self._line
        tile_w = (width - TILE_GAP) / 2
        tile_h = TILE_PAD_Y + line["number"] + 1 + line["tile"] + TILE_PAD_Y
        for i, (number, label) in enumerate(tiles):
            tx = x + (i % 2) * (tile_w + TILE_GAP)
            ty = y + (i // 2) * (tile_h + TILE_GAP)
            painter.setBrush(_color(WHITE, 0.10))
            painter.drawRoundedRect(QRectF(tx, ty, tile_w, tile_h), TILE_RADIUS, TILE_RADIUS)
            inner_w = tile_w - 2 * TILE_PAD_X
            self._text(painter, QRectF(tx + TILE_PAD_X, ty + TILE_PAD_Y, inner_w, line["number"]), number, "number")
            self._text(painter, QRectF(tx + TILE_PAD_X, ty + TILE_PAD_Y + line["number"] + 1, inner_w, line["tile"]),
                       label, "tile", alpha=SOFT)
        return y + 2 * tile_h + TILE_GAP + SECTION_GAP

    def _paint_footer(self, painter, x, y, width):
        snap = self._snapshot
        height = self._line["small"]
        color, alpha, text = CONNECTION.get(snap["connection"], CONNECTION[None])
        painter.setBrush(_color(color, alpha))
        painter.drawEllipse(QRectF(x, y + height / 2 - DOT / 2, DOT, DOT))
        row = QRectF(x + DOT + 6, y, width - DOT - 6, height)
        self._text(painter, row, text, "small", alpha=SOFT)
        if snap["last_response_ago"] is not None:
            self._text(painter, row, f"Updated {format_ago(snap['last_response_ago'])}", "small", alpha=SOFT,
                       align=Qt.AlignmentFlag.AlignRight)
