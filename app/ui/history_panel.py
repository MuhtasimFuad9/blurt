"""The History panel: recent dictations, with Copy, and Retry for failed ones.

Opens from the pill's History button, or from the "!" / hourglass pill at that
dictation's entry (show_entry). Built on Panel (panel.py): same look, same
no-focus window.

- Reads HistoryStore.entries() (memory only, never the disk) when it opens and
  whenever the store's version changes. A 0.5s timer runs only while open.
- Each entry: time, what happened, tags, the text in at most 2 lines ending
  in "…", and Copy (always the full text) / Retry buttons.
- Original (M13): on entries where cleanup changed the words, a button switches the
  card to Whisper's raw transcript and back, so a sentence cleanup dropped can be
  recovered, and you can see whether Whisper or cleanup made a mistake. Copy copies
  what's shown. Memory only: every entry shows the cleaned text again when the panel opens.
- The list scrolls with the mouse wheel (Panel's shared scrolling list).
- Clear needs two clicks ("Clear all?"), because a confirm dialog would take focus.
"""

import re
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetricsF, QTextLayout

from app.config import load_config
from app.history import entry_text, format_entry_time
from app.paster import copy_only
from app.ui.panel import PAD, SECTION_GAP, SOFT, WIDTH, Panel, _font
from app.ui.pill import BLUE, WHITE, _color, _now_ms
from app.usage import format_wait

LIST_MAX_H = 330  # taller lists scroll
EMPTY_H = 64
ENTRY_GAP = 6
ENTRY_PAD_X, ENTRY_PAD_Y = 10, 7
ENTRY_RADIUS = 9
ROW_H = 24  # an entry's top row: time, status, tags and buttons
TEXT_GAP = 2  # between the top row and the text
TEXT_LINES = 2
ORIGINAL_LINES = 5  # a card showing its original: you switched to read it, often for a dropped sentence
ACTION = 24  # the Copy button, and the Retry pill's height
ACTION_ICON = 14
ACTION_GAP = 4
RETRY_PAD_LEFT, RETRY_PAD_RIGHT = 8, 10
RETRY_ICON = 13
RETRY_ICON_GAP = 5
ORIGINAL_PAD = 8  # the Original button: its label, padded each side
STATUS_ICON = 12
DOT = 2.5  # between the time and the status
TAG_H = 16
TAG_PAD = 6
TAG_GAP = 5
CLEAR_H = 24
CLEAR_PAD = 9

CLEAR_CONFIRM_MS = 3000
COPIED_MS = 1200
HIGHLIGHT_HOLD_MS = 500  # "open at this entry": full highlight, then it fades
HIGHLIGHT_FADE_MS = 1100
LIVE_MS = 500

# How the entries look: near-white cards with dark text on the blue panel, because
# History is paragraphs you read (white on blue was about 6.3:1 contrast). Measured
# with tools/render_history.py: text 15.7:1, time and reasons 7.6:1, Retry 7.7:1,
# statuses and tags 5.4 to 6.1:1. Each color is (rgb, alpha).
NAVY = (0x10, 0x1B, 0x3F)
SLATE = (0x44, 0x4F, 0x6B)
PALETTE = {
    "card": ((0xF5, 0xF7, 0xFC), 1.0),
    "highlight": ((0xFF, 0xEF, 0xC2), 1.0),  # "opened at this entry": the card turns warm, then fades back
    "text": (NAVY, 1.0),
    "soft": (SLATE, 1.0),
    "dot": (SLATE, 0.6),
    "failed": ((0xB0, 0x3A, 0x0A), 1.0),
    "limited": ((0x96, 0x55, 0x00), 1.0),
    "hover": (BLUE, 0.10),
    "retry": (BLUE, 1.0),
    "retry_hover": ((0x0E, 0x34, 0xA8), 1.0),
    "retry_text": (WHITE, 1.0),
    "tag": (BLUE, 0.10),
    "tag_text": (BLUE, 1.0),
    "original": (SLATE, 1.0),  # the Original button's label while off
    "original_on": (BLUE, 1.0),  # solid while the card shows the original, like Retry
    "original_on_hover": ((0x0E, 0x34, 0xA8), 1.0),
    "original_on_text": (WHITE, 1.0),
}


def _css(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _mix(a, b, t):
    """Color a moved toward color b by t (0..1)."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _words(text):
    return re.findall(r"\w+", (text or "").lower())


def has_original(entry):
    """True if cleanup changed the words (not just punctuation or capitals), so the
    raw transcript is worth showing."""
    return bool(entry["cleaned"] and entry["raw"]) and _words(entry["cleaned"]) != _words(entry["raw"])


STATUS = {  # status: (word, icon)
    "pasted": ("Pasted", None),
    "copied": ("Copied", None),
    "failed": ("Failed", "alert"),
    "limited": ("Rate limited", "hourglass"),
}


class HistoryPanel(Panel):
    NAME = "history"
    TITLE = "History"

    def __init__(self, store, tracker=None, on_retry=None, debug=False, clock=time.time):
        self._args = (store, tracker, on_retry, clock)
        super().__init__(debug)

    # ---- set up ----

    def _extra_fonts(self):
        return {
            "entry": _font(13),
            "meta": _font(11),
            "status": _font(11, QFont.Weight.Medium),
            "tag": _font(10),
            "clear": _font(11, QFont.Weight.Medium),
        }

    def _init_content(self):
        self.store, self.tracker, self.on_retry, self._clock = self._args
        self._entries = []
        self._by_id = {}
        self._items = []  # layout: dicts with id, top (inside the list), height, lines, soft
        self._version = None
        self._history_on = True
        self._rate_wait = None  # seconds until a transcription rate limit is over, or None
        self._wrap_cache = {}
        self._hover = None  # what the mouse is over: "clear", ("copy", id), ("retry", id), ("original", id)
        self._original = set()  # entry ids showing Whisper's raw transcript
        self._copied = {}  # entry id: when its "copied" checkmark ends (ms)
        self._highlight_id = None
        self._highlight_start = 0.0
        self._clear_armed_until = 0.0
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_MS)
        self._live_timer.timeout.connect(self._refresh)

    def _subtitle(self):
        return "Last 7 days, only on this device"

    # ---- what the pill calls ----

    def show_entry(self, entry_id):
        """Open (if needed) and bring this entry into view, briefly highlighted."""
        self.open_panel()
        item = next((i for i in self._items if i["id"] == entry_id), None)
        if item is None:
            return
        list_h = self._list_height()
        if not (self._scroll.end <= item["top"] and item["top"] + item["height"] <= self._scroll.end + list_h):
            self._scroll_to(item["top"] - (list_h - item["height"]) / 2, animate=False)
        self._highlight_id = entry_id
        self._highlight_start = _now_ms()
        self._start_frames()

    # ---- data ----

    def _on_open(self):
        try:
            self._history_on = bool(load_config()["history"])
        except (OSError, ValueError, KeyError):
            self._history_on = True
        self._clear_armed_until = 0.0
        self._highlight_id = None
        self._original.clear()
        self._version = None
        self._refresh()
        self._scroll.jump(0.0)  # always open at the newest
        self._live_timer.start()

    def _on_close(self):
        self._live_timer.stop()
        self._hover = None

    def _shown_text(self, entry):
        """What the card shows and Copy copies: the raw transcript while switched to Original."""
        if entry["id"] in self._original and has_original(entry):
            return entry["raw"]
        return entry_text(entry)

    def _refresh(self):
        """Re-reads the store if it changed, and the rate limit countdown. Cheap (memory only)."""
        wait = None
        if self.tracker is not None:
            limit = self.tracker.snapshot()["rate_limit"]
            if limit is not None and limit["kind"] == "transcription" and limit["retry_in"] is not None:
                wait = limit["retry_in"]
        version = self.store.version
        if version != self._version or wait != self._rate_wait:
            self._version = version
            self._rate_wait = wait
            self._entries = self.store.entries()
            self._by_id = {e["id"]: e for e in self._entries}
            self._layout()
            self._scroll_to(self._scroll.end, animate=False)  # stay in range if the list got shorter
        if self._clear_armed_until and _now_ms() > self._clear_armed_until:
            self._clear_armed_until = 0.0
        self.update()

    # ---- layout ----

    def _wrap(self, text, font_name, width, max_lines):
        """Text broken into at most max_lines lines; the last one ends in "…" if there's more."""
        key = (text, font_name, width, max_lines)
        if key in self._wrap_cache:
            return self._wrap_cache[key]
        font = self._fonts[font_name]
        layout = QTextLayout(text, font)
        layout.beginLayout()
        spans = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            spans.append((line.textStart(), line.textLength()))
        layout.endLayout()
        lines = [text[start:start + length].rstrip() for start, length in spans[:max_lines]]
        if len(spans) > max_lines:
            rest = text[spans[max_lines - 1][0]:]
            lines[-1] = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
        if len(self._wrap_cache) > 1000:
            self._wrap_cache.clear()
        self._wrap_cache[key] = lines
        return lines

    def _display(self, entry, width):
        """(lines, soft): the text shown under an entry's top row."""
        text = re.sub(r"\s+", " ", self._shown_text(entry)).strip()
        if text:
            lines = ORIGINAL_LINES if entry["id"] in self._original else TEXT_LINES
            return self._wrap(text, "entry", width, lines), False
        reason = entry["error"] or "Nothing was transcribed"
        if entry["status"] == "limited" and self._rate_wait is not None and entry["can_retry"]:
            # The countdown gets its own line, so it never wraps mid-phrase.
            lines = self._wrap(reason, "entry", width, 1)
            return lines + [f"Try again in {format_wait(self._rate_wait)}"], True
        return self._wrap(reason, "entry", width, TEXT_LINES), True

    def _layout(self):
        width = self._list_width() - 2 * ENTRY_PAD_X
        line_h = self._line["entry"]
        top = 0.0
        self._items = []
        for entry in self._entries:
            lines, soft = self._display(entry, width)
            height = ENTRY_PAD_Y + ROW_H + TEXT_GAP + len(lines) * line_h + ENTRY_PAD_Y
            self._items.append({"id": entry["id"], "top": top, "height": height, "lines": lines, "soft": soft})
            top += height + ENTRY_GAP
        self._total_h = max(0.0, top - ENTRY_GAP)

    def _list_width(self):
        return WIDTH - 2 * PAD

    def _list_height(self):
        if not self._items:
            return EMPTY_H
        return min(self._total_h, LIST_MAX_H)

    def _banner_block(self):
        return 0 if self._history_on else self._banner_height() + SECTION_GAP

    def _content_height(self):
        return self._banner_block() + self._list_height()

    def _max_content_height(self):
        return self._banner_height() + SECTION_GAP + LIST_MAX_H

    def _list_rect(self):
        card = self._card_rect()
        top = card.top() + PAD + self._header_height() + SECTION_GAP + self._banner_block()
        return QRectF(card.left() + PAD, top, self._list_width(), self._list_height())

    def _clear_rect(self):
        if not self._entries:
            return None
        close = self._close_rect()
        text = "Clear all?" if self._clear_armed_until else "Clear"
        width = QFontMetricsF(self._fonts["clear"]).horizontalAdvance(text) + 2 * CLEAR_PAD
        return QRectF(close.left() - 2 - width, close.center().y() - CLEAR_H / 2, width, CLEAR_H)

    def _entry_rect(self, item, list_rect):
        return QRectF(list_rect.left(), list_rect.top() + item["top"] - self._scroll.value, list_rect.width(),
                      item["height"])

    def _buttons(self, entry, rect):
        """{"copy": rect, "retry": rect} for the buttons this entry shows, right aligned in its top row."""
        buttons = {}
        center_y = rect.top() + ENTRY_PAD_Y + ROW_H / 2
        right = rect.right() - 5
        names = []
        if entry_text(entry):
            names.append("copy")
        if has_original(entry):
            names.append("original")
        if entry["can_retry"] and not entry["retrying"]:
            names.append("retry")
        widths = {"retry": self._retry_width, "original": self._original_width}
        for name in names:  # right to left: Copy at the edge, then Original or the Retry pill
            width = widths[name]() if name in widths else ACTION
            buttons[name] = QRectF(right - width, center_y - ACTION / 2, width, ACTION)
            right -= width + ACTION_GAP
        return buttons

    def _original_width(self):
        return QFontMetricsF(self._fonts["clear"]).horizontalAdvance("Original") + 2 * ORIGINAL_PAD

    def _retry_width(self):
        label = QFontMetricsF(self._fonts["clear"]).horizontalAdvance("Retry")
        return RETRY_PAD_LEFT + RETRY_ICON + RETRY_ICON_GAP + label + RETRY_PAD_RIGHT

    # ---- animation ----

    def _animate(self, now):
        moving = False
        if self._highlight_id is not None:
            if now - self._highlight_start < HIGHLIGHT_HOLD_MS + HIGHLIGHT_FADE_MS:
                moving = True
            else:
                self._highlight_id = None
        return moving

    def _highlight(self, entry_id, now):
        if entry_id != self._highlight_id:
            return 0.0
        t = now - self._highlight_start - HIGHLIGHT_HOLD_MS
        if t <= 0:
            return 1.0
        progress = min(1.0, t / HIGHLIGHT_FADE_MS)
        return 1.0 - self._close_curve.valueForProgress(progress)

    # ---- mouse ----

    def _content_target(self, pos):
        clear = self._clear_rect()
        if clear is not None and clear.contains(pos):
            return "clear"
        list_rect = self._list_rect()
        if not list_rect.contains(pos):
            return None
        for item in self._items:
            rect = self._entry_rect(item, list_rect)
            if rect.bottom() < list_rect.top() or rect.top() > list_rect.bottom():
                continue
            if rect.contains(pos):
                for name, button in self._buttons(self._by_id[item["id"]], rect).items():
                    if button.contains(pos):
                        return (name, item["id"])
                return None
        return None

    def _set_hover(self, target):
        if target != self._hover:
            self._hover = target
            self.update()

    def _content_hover(self, pos):
        self._set_hover(self._content_target(pos) if pos is not None else None)

    def _content_release(self, target):
        if target == "clear":
            if self._clear_armed_until:
                self._clear_armed_until = 0.0
                self.store.clear()
                if self.debug:
                    print("[panel] history cleared")
            else:
                self._clear_armed_until = _now_ms() + CLEAR_CONFIRM_MS
                QTimer.singleShot(CLEAR_CONFIRM_MS + 20, self._refresh)
            self._refresh()
            return
        name, entry_id = target
        if name == "original":
            self._original ^= {entry_id}  # switch this card between the cleaned text and the original
            self._layout()
            self._scroll_to(self._scroll.end, animate=False)  # stay in range if the list got shorter
            if self.debug:
                shown = "original" if entry_id in self._original else "cleaned text"
                print(f"[panel] history: entry {entry_id} shows its {shown}")
        elif name == "copy":
            entry = self.store.entry(entry_id)
            if entry and copy_only(self._shown_text(entry)):
                self._copied[entry_id] = _now_ms() + COPIED_MS
                QTimer.singleShot(COPIED_MS + 20, self.update)
                if self.debug:
                    what = " (original)" if entry_id in self._original else ""
                    print(f"[panel] history: copied entry {entry_id}{what}")
        elif name == "retry" and self.on_retry is not None:
            self.on_retry(entry_id)  # marks it "retrying" right away
            self._refresh()
        self.update()

    # ---- painting ----

    def _paint_header_extra(self, painter, x, y, width):
        clear = self._clear_rect()
        if clear is None:
            return
        armed = bool(self._clear_armed_until)
        if armed or self._hover == "clear":
            painter.setBrush(_color(WHITE, 0.22 if armed else 0.14))
            painter.drawRoundedRect(clear, CLEAR_H / 2, CLEAR_H / 2)
        self._text(painter, clear, "Clear all?" if armed else "Clear", "clear", align=Qt.AlignmentFlag.AlignHCenter)

    def _paint_content(self, painter, x, y, width):
        if not self._history_on:
            y = self._paint_banner(painter, x, y, width, "history", "History is off",
                                   "New dictations aren't saved. Turn it on in Settings")
        list_rect = self._list_rect()
        if not self._items:
            self._text(painter, list_rect, "No dictations yet", "meta", alpha=SOFT,
                       align=Qt.AlignmentFlag.AlignHCenter)
            return
        now = _now_ms()
        painter.save()
        painter.setClipRect(list_rect.adjusted(-2, 0, 2, 0))
        for item in self._items:
            rect = self._entry_rect(item, list_rect)
            if rect.bottom() < list_rect.top() or rect.top() > list_rect.bottom():
                continue
            self._paint_entry(painter, rect, item, self._by_id[item["id"]], now)
        painter.restore()
        self._paint_scroll_edges(painter, list_rect)

    def _fill(self, painter, key, rect=None, radius=0.0, ellipse=False):
        rgb, alpha = PALETTE[key]
        painter.setBrush(_color(rgb, alpha))
        if ellipse:
            painter.drawEllipse(rect)
        elif rect is not None:
            painter.drawRoundedRect(rect, radius, radius)

    def _say(self, painter, rect, text, font, key, align=Qt.AlignmentFlag.AlignLeft):
        rgb, alpha = PALETTE[key]
        self._text(painter, rect, text, font, rgb, alpha, align)

    def _paint_entry(self, painter, rect, item, entry, now):
        pal = PALETTE
        highlight = self._highlight(entry["id"], now)
        (card, card_alpha), (glow, _) = pal["card"], pal["highlight"]
        painter.setBrush(_color(_mix(card, glow, highlight), card_alpha))
        painter.drawRoundedRect(rect, ENTRY_RADIUS, ENTRY_RADIUS)

        row_y = rect.top() + ENTRY_PAD_Y
        center_y = row_y + ROW_H / 2
        buttons = self._buttons(entry, rect)
        row_right = min((b.left() for b in buttons.values()), default=rect.right() - ENTRY_PAD_X) - 4
        cx = rect.left() + ENTRY_PAD_X

        # Time, a dot, then what happened
        cx = self._row_text(painter, cx, row_y, format_entry_time(entry["ts"], self._clock()), "meta", "soft")
        self._fill(painter, "dot")
        painter.drawEllipse(QPointF(cx + 5, center_y), DOT / 2, DOT / 2)
        cx += 10
        word, icon = STATUS.get(entry["status"], (entry["status"], None))
        tone = entry["status"] if entry["status"] in ("failed", "limited") else "text"
        if entry["retrying"]:
            word, icon, tone = "Retrying…", "retry", "text"
        if icon:
            self._icon(painter, icon, STATUS_ICON, QPointF(cx + STATUS_ICON / 2, center_y), _css(pal[tone][0]))
            cx += STATUS_ICON + 4
        cx = self._row_text(painter, cx, row_y, word, "status", tone)

        tags = []
        if entry["cleanup"] in ("limited", "failed") and entry["raw"]:
            tags.append("Not cleaned up")
        if entry["retried_ts"] and entry["status"] == "copied":
            tags.append("Retried")
        metrics = QFontMetricsF(self._fonts["tag"])
        for tag in tags:
            tag_w = metrics.horizontalAdvance(tag) + 2 * TAG_PAD
            if cx + TAG_GAP + tag_w > row_right:
                break  # no room: tags are extra information, the status word says the main thing
            tag_rect = QRectF(cx + TAG_GAP, center_y - TAG_H / 2, tag_w, TAG_H)
            self._fill(painter, "tag", tag_rect, TAG_H / 2)
            self._say(painter, tag_rect, tag, "tag", "tag_text", Qt.AlignmentFlag.AlignHCenter)
            cx = tag_rect.right()

        for name, button in buttons.items():
            hovered = self._hover == (name, entry["id"])
            if name == "retry":
                # The main action on a failed entry: a labeled pill, like "Clear all?".
                self._fill(painter, "retry_hover" if hovered else "retry", button, ACTION / 2)
                icon_center = QPointF(button.left() + RETRY_PAD_LEFT + RETRY_ICON / 2, button.center().y())
                self._icon(painter, "retry", RETRY_ICON, icon_center, _css(pal["retry_text"][0]))
                label_x = button.left() + RETRY_PAD_LEFT + RETRY_ICON + RETRY_ICON_GAP
                self._say(painter, QRectF(label_x, button.top(), button.right() - label_x, ACTION), "Retry", "clear",
                          "retry_text")
                continue
            if name == "original":
                on = entry["id"] in self._original
                if on:
                    self._fill(painter, "original_on_hover" if hovered else "original_on", button, ACTION / 2)
                elif hovered:
                    self._fill(painter, "hover", button, ACTION / 2)
                text_key = "original_on_text" if on else ("tag_text" if hovered else "original")
                self._say(painter, button, "Original", "clear", text_key, Qt.AlignmentFlag.AlignHCenter)
                continue
            if hovered:
                self._fill(painter, "hover", button, ellipse=True)
            icon_name = "check" if self._copied.get(entry["id"], 0) > now else "copy"
            self._icon(painter, icon_name, ACTION_ICON, button.center(), _css(pal["text"][0]))

        line_h = self._line["entry"]
        text_y = row_y + ROW_H + TEXT_GAP
        text_w = rect.width() - 2 * ENTRY_PAD_X
        for i, line in enumerate(item["lines"]):
            self._say(painter, QRectF(rect.left() + ENTRY_PAD_X, text_y + i * line_h, text_w, line_h), line,
                      "entry", "soft" if item["soft"] else "text")

    def _row_text(self, painter, x, row_y, text, font, key):
        width = QFontMetricsF(self._fonts[font]).horizontalAdvance(text)
        self._say(painter, QRectF(x, row_y, width + 1, ROW_H), text, font, key)
        return x + width
