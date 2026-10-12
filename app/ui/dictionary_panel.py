"""The Dictionary panel: the terms the app always spells exactly, newest first.

Opens from the pill's Dictionary button. Built on Panel (panel.py): same look,
same no-focus window, same scrolling list, and History's light cards.

- Typing would need keyboard focus, which a panel must never take. So a term is
  added from the clipboard: copy the word anywhere, and the Add button shows it
  ("Add "Groq""). If it can't be added, the panel says why instead. The Notepad
  button (file icon, in the header) is there for bulk edits.
- Clipboard text marked private (a copied password, our own pasted dictations)
  is never shown: the panel only says "Clipboard content is hidden".
- While open, a 0.5s timer checks Windows' clipboard change counter (cheap) and
  reads the clipboard only when it changed, trying once so the GUI never waits.
  The same timer re-reads dictionary.txt when it changed (a Notepad edit).
- Each term has an X. Removing takes two clicks ("Remove?" for 3s), because a
  confirm dialog would take focus.
- The footer estimates what the dictionary costs: Whisper's hint budget and the
  tokens added to every cleanup. A banner shows when the oldest terms no longer
  fit in Whisper's hint.
"""

import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetricsF

from app.dictionary import (
    DICTIONARY_PATH,
    WHISPER_PROMPT_BUDGET,
    add_term,
    check_new_term,
    cleanup_tokens,
    estimate_tokens,
    load_terms,
    normalize,
    remove_term,
    whisper_terms,
)
from app.paster import clipboard_sequence, read_text_for_dictionary
from app.ui.history_panel import HIGHLIGHT_FADE_MS, HIGHLIGHT_HOLD_MS, _css, _mix
from app.ui.history_panel import PALETTE as HISTORY_PALETTE
from app.ui.panel import AMBER, PAD, SECTION_GAP, SOFT, WIDTH, Panel, _font
from app.ui.pill import BLUE, WHITE, Tween, _color, _now_ms

LIST_MAX_H = 232  # about 6.5 terms; longer lists scroll
EMPTY_H = 56
TERM_H = 32
TERM_GAP = 4
TERM_PAD_X = 10
TERM_RADIUS = 9
REMOVE = 24  # the X button
REMOVE_ICON = 12
REMOVE_PAD = 9  # inside the "Remove?" pill
ADD_H = 48  # the add card, the same height in every state so nothing jumps
ADD_PAD_X = 10
ADD_BUTTON_H = 28
ADD_ICON = 13
ADD_ICON_GAP = 5
ADD_PAD_LEFT, ADD_PAD_RIGHT = 9, 12
HINT_ICON = 14
HINT_ICON_GAP = 8
HEADER_BUTTON = 28
HEADER_ICON = 14
TIP_H = 22  # the "Open in Notepad" label, left of its button
TIP_PAD = 8
TIP_GAP = 4
TIP_MS = 120
TAG_H = 16
TAG_PAD = 6
FOOTER_GAP = 2

LIVE_MS = 500
REMOVE_CONFIRM_MS = 3000
ALMOST_FULL = 80  # percent of Whisper's budget where the footer turns amber

# History's cards and text colors, plus the "Remove?" pill.
PALETTE = dict(HISTORY_PALETTE)
PALETTE.update({
    "remove": ((0xB0, 0x3A, 0x0A), 1.0),
    "remove_text": (WHITE, 1.0),
    "add": (BLUE, 1.0),
    "add_hover": ((0x0E, 0x34, 0xA8), 1.0),
    "add_text": (WHITE, 1.0),
})


class DictionaryPanel(Panel):
    NAME = "dictionary"
    TITLE = "Dictionary"

    def __init__(self, path=None, debug=False):
        self._path_arg = path
        super().__init__(debug)

    # ---- set up ----

    def _extra_fonts(self):
        return {
            "term": _font(13),
            "hint": _font(13, QFont.Weight.Medium),
            "meta": _font(11),
            "button": _font(11.5, QFont.Weight.Medium),
            "tag": _font(10),
            "tip": _font(11),
        }

    def _init_content(self):
        self.path = Path(self._path_arg or DICTIONARY_PATH)
        self._terms = None  # file order (oldest first); None = not read yet
        self._items = []  # layout, newest first: dicts with term, top, left_out
        self._clip = ("none", None)  # ("text", text), ("hidden", None) or ("none", None)
        self._clip_seq = None  # the clipboard change counter when it was last read
        self._just_added = None  # the term added from the current clipboard ("Added" instead of "Already in")
        self._hover = None  # "add", "notepad", ("remove", term)
        self._tip = Tween(0.0)  # the "Open in Notepad" label, 0 = hidden
        self._armed = None  # the term showing "Remove?"
        self._armed_until = 0.0
        self._highlight_term = None  # a term just added glows, then fades
        self._highlight_start = 0.0
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_MS)
        self._live_timer.timeout.connect(self._poll)

    def _subtitle(self):
        return "Always spelled exactly like this"

    # ---- data ----

    def set_clipboard(self, kind, text=None):
        """What's on the clipboard: ("text", text), ("hidden", None) or ("none", None)."""
        clip = (kind, text if kind == "text" else None)
        if clip != self._clip:
            self._just_added = None
        self._clip = clip
        self.update()

    def _on_open(self):
        self._armed = None
        self._highlight_term = None
        self._clip_seq = None  # read the clipboard right away
        self._terms = None
        self._poll()
        self._scroll.jump(0.0)  # always open at the newest
        self._live_timer.start()

    def _on_close(self):
        self._live_timer.stop()
        self._hover = None
        self._armed = None
        self._tip.jump(0.0)

    def _poll(self):
        """The clipboard (only when Windows says it changed) and the file (only when it changed)."""
        sequence = clipboard_sequence()
        if sequence != self._clip_seq:
            kind, text = read_text_for_dictionary()
            if kind != "busy":  # busy: another app has it open; try again next tick
                self._clip_seq = sequence
                self.set_clipboard(kind, text)
        self._refresh()

    def _refresh(self):
        terms = load_terms(self.path)  # cheap: re-reads the file only when it changed
        if terms != self._terms:
            self._terms = terms
            self._layout()
            self._scroll_to(self._scroll.end, animate=False)  # stay in range if the list got shorter
        if self._armed_until and _now_ms() > self._armed_until:
            self._armed = None
            self._armed_until = 0.0
        self.update()

    def _layout(self):
        kept = len(whisper_terms(self._terms))
        left_out = len(self._terms) - kept  # the oldest ones, at the start of the file
        self._items = []
        top = 0.0
        for index in range(len(self._terms) - 1, -1, -1):
            self._items.append({"term": self._terms[index], "top": top, "left_out": index < left_out})
            top += TERM_H + TERM_GAP
        self._total_h = max(0.0, top - TERM_GAP)

    def _left_out(self):
        return sum(1 for item in self._items if item["left_out"])

    def _add_state(self):
        """(ready, line1, line2) for the add card. ready = the clipboard text can be added."""
        kind, text = self._clip
        if kind == "hidden":
            return False, "Clipboard content is hidden", "It's marked private. Copy a word to add it"
        shown = re.sub(r"\s+", " ", text or "").strip()
        if kind != "text" or not shown:
            return False, "Nothing to add yet", "Copy a word anywhere, then add it here"
        reason = check_new_term(text, self._terms or [])
        if reason and self._just_added and normalize(text).lower() == self._just_added.lower():
            reason = "Added to the dictionary"
        if reason:
            return False, f"“{shown}”", reason
        return True, shown, None

    # ---- layout ----

    def _banner_block(self):
        return self._banner_height() + SECTION_GAP if self._left_out() else 0

    def _list_height(self):
        if not self._items:
            return EMPTY_H
        return min(self._total_h, LIST_MAX_H)

    def _footer_height(self):
        return 2 * self._line["small"] + FOOTER_GAP

    def _content_height(self):
        height = self._banner_block() + ADD_H + SECTION_GAP + self._list_height()
        if self._items:
            height += SECTION_GAP + self._footer_height()
        return height

    def _max_content_height(self):
        return self._banner_height() + SECTION_GAP + ADD_H + SECTION_GAP + LIST_MAX_H + SECTION_GAP + \
            self._footer_height()

    def _top(self):
        card = self._card_rect()
        return card.top() + PAD + self._header_height() + SECTION_GAP

    def _add_rect(self):
        card = self._card_rect()
        return QRectF(card.left() + PAD, self._top() + self._banner_block(), WIDTH - 2 * PAD, ADD_H)

    def _add_button_rect(self):
        """The blue Add pill (only when the clipboard text can be added)."""
        ready, text, _ = self._add_state()
        if not ready:
            return None
        card = self._add_rect()
        label = self._add_label(text)
        width = ADD_PAD_LEFT + ADD_ICON + ADD_ICON_GAP + QFontMetricsF(self._fonts["button"]).horizontalAdvance(label) \
            + ADD_PAD_RIGHT
        return QRectF(card.left() + ADD_PAD_X, card.center().y() - ADD_BUTTON_H / 2, width, ADD_BUTTON_H)

    def _add_label(self, text):
        """Add "text", with the text cut short ("…") so the button fits in the card."""
        metrics = QFontMetricsF(self._fonts["button"])
        room = WIDTH - 2 * PAD - 2 * ADD_PAD_X - ADD_PAD_LEFT - ADD_ICON - ADD_ICON_GAP - ADD_PAD_RIGHT
        room -= metrics.horizontalAdvance("Add “”")
        return f"Add “{metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)}”"

    def _list_rect(self):
        card = self._card_rect()
        top = self._add_rect().bottom() + SECTION_GAP
        return QRectF(card.left() + PAD, top, WIDTH - 2 * PAD, self._list_height())

    def _term_rect(self, item, list_rect):
        return QRectF(list_rect.left(), list_rect.top() + item["top"] - self._scroll.value, list_rect.width(), TERM_H)

    def _remove_rect(self, item, rect):
        center_y = rect.center().y()
        if item["term"] == self._armed:
            width = QFontMetricsF(self._fonts["button"]).horizontalAdvance("Remove?") + 2 * REMOVE_PAD
            return QRectF(rect.right() - 4 - width, center_y - REMOVE / 2, width, REMOVE)
        return QRectF(rect.right() - 4 - REMOVE, center_y - REMOVE / 2, REMOVE, REMOVE)

    def _notepad_rect(self):
        close = self._close_rect()
        return QRectF(close.left() - 2 - HEADER_BUTTON, close.top(), HEADER_BUTTON, HEADER_BUTTON)

    # ---- animation ----

    def _animate(self, now):
        moving = self._tip.update(now)
        if self._highlight_term is not None:
            if now - self._highlight_start < HIGHLIGHT_HOLD_MS + HIGHLIGHT_FADE_MS:
                moving = True
            else:
                self._highlight_term = None
        return moving

    def _highlight(self, term, now):
        if term != self._highlight_term:
            return 0.0
        t = now - self._highlight_start - HIGHLIGHT_HOLD_MS
        if t <= 0:
            return 1.0
        return 1.0 - self._close_curve.valueForProgress(min(1.0, t / HIGHLIGHT_FADE_MS))

    # ---- mouse ----

    def _content_target(self, pos):
        if self._notepad_rect().contains(pos):
            return "notepad"
        button = self._add_button_rect()
        if button is not None and button.contains(pos):
            return "add"
        list_rect = self._list_rect()
        if not list_rect.contains(pos):
            return None
        for item in self._items:
            rect = self._term_rect(item, list_rect)
            if rect.contains(pos):
                return ("remove", item["term"]) if self._remove_rect(item, rect).contains(pos) else None
        return None

    def _content_hover(self, pos):
        target = self._content_target(pos) if pos is not None else None
        if target == self._hover:
            return
        was_notepad = self._hover == "notepad"
        self._hover = target
        if (target == "notepad") != was_notepad:
            self._tip.to(1.0 if target == "notepad" else 0.0, TIP_MS, self._open_curve, _now_ms())
            self._start_frames()
        self.update()

    def _content_release(self, target):
        if target == "notepad":
            self._open_in_notepad()
        elif target == "add":
            self._add()
        elif isinstance(target, tuple) and target[0] == "remove":
            self._remove(target[1])
        self.update()

    def _add(self):
        kind, text = self._clip
        if kind != "text":
            return
        term = normalize(text)
        reason = add_term(text, self.path)  # re-checks against the file on disk right now
        if reason:
            if self.debug:
                print(f"[panel] dictionary: not added ({reason})")
        else:
            self._just_added = term
            self._highlight_term = term
            self._highlight_start = _now_ms()
            if self.debug:
                print(f"[panel] dictionary: added {term!r}")
        self._refresh()
        self._scroll_to(0.0)  # the newest term is at the top
        self._start_frames()

    def _remove(self, term):
        if self._armed != term:
            self._armed = term  # first click: ask
            self._armed_until = _now_ms() + REMOVE_CONFIRM_MS
            QTimer.singleShot(REMOVE_CONFIRM_MS + 20, self._refresh)
            return
        self._armed = None
        self._armed_until = 0.0
        removed = remove_term(term, self.path)
        if self.debug:
            print(f"[panel] dictionary: removed {term!r}" if removed
                  else f"[panel] dictionary: {term!r} was already gone from the file")
        self._refresh()

    def _open_in_notepad(self):
        self.close_panel()  # Notepad takes focus, which is fine: you chose to go there
        try:
            subprocess.Popen(["notepad.exe", str(self.path)])
        except OSError as e:
            print(f"[panel] dictionary: couldn't open Notepad ({e})")

    # ---- painting ----

    def _fill(self, painter, key, rect, radius=0.0, ellipse=False):
        rgb, alpha = PALETTE[key]
        painter.setBrush(_color(rgb, alpha))
        if ellipse:
            painter.drawEllipse(rect)
        else:
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
        tip = self._tip.value
        if tip > 0.01:
            # A painted label, not a real tooltip window: nothing new that could take focus.
            text = "Open in Notepad"
            tip_w = QFontMetricsF(self._fonts["tip"]).horizontalAdvance(text) + 2 * TIP_PAD
            rect = QRectF(button.left() - TIP_GAP - tip_w, button.center().y() - TIP_H / 2, tip_w, TIP_H)
            painter.save()
            painter.setOpacity(painter.opacity() * tip)
            self._fill(painter, "card", rect, TIP_H / 2)
            self._say(painter, rect, text, "tip", "text", Qt.AlignmentFlag.AlignHCenter)
            painter.restore()

    def _paint_content(self, painter, x, y, width):
        left_out = self._left_out()
        if left_out:
            detail = ("Skips the oldest term. Cleanup uses all." if left_out == 1
                      else f"Skips the {left_out} oldest terms. Cleanup uses all.")
            y = self._paint_banner(painter, x, y, width, "alert", "Whisper's hint is full", detail)
        self._paint_add(painter)
        list_rect = self._list_rect()
        if not self._items:
            self._text(painter, list_rect, "No terms yet", "meta", alpha=SOFT, align=Qt.AlignmentFlag.AlignHCenter)
            return
        now = _now_ms()
        painter.save()
        painter.setClipRect(list_rect.adjusted(-2, 0, 2, 0))
        for item in self._items:
            rect = self._term_rect(item, list_rect)
            if rect.bottom() < list_rect.top() or rect.top() > list_rect.bottom():
                continue
            self._paint_term(painter, rect, item, now)
        painter.restore()
        self._paint_scroll_edges(painter, list_rect)
        self._paint_footer(painter, x, list_rect.bottom() + SECTION_GAP, width)

    def _paint_add(self, painter):
        card = self._add_rect()
        self._fill(painter, "card", card, TERM_RADIUS)
        ready, line1, line2 = self._add_state()
        if ready:
            button = self._add_button_rect()
            self._fill(painter, "add_hover" if self._hover == "add" else "add", button, ADD_BUTTON_H / 2)
            icon_center = QPointF(button.left() + ADD_PAD_LEFT + ADD_ICON / 2, button.center().y())
            self._icon(painter, "plus", ADD_ICON, icon_center, _css(PALETTE["add_text"][0]))
            label_x = button.left() + ADD_PAD_LEFT + ADD_ICON + ADD_ICON_GAP
            self._say(painter, QRectF(label_x, button.top(), button.right() - label_x, ADD_BUTTON_H),
                      self._add_label(line1), "button", "add_text")
            hint = "from your clipboard"
            hint_w = QFontMetricsF(self._fonts["meta"]).horizontalAdvance(hint)
            if button.right() + 8 + hint_w <= card.right() - ADD_PAD_X:
                self._say(painter, QRectF(card.right() - ADD_PAD_X - hint_w - 1, card.top(), hint_w + 1, ADD_H), hint,
                          "meta", "soft")
            return
        # Not addable: a clipboard icon, then what's there (or isn't) and why.
        left = card.left() + ADD_PAD_X
        self._icon(painter, "copy", HINT_ICON, QPointF(left + HINT_ICON / 2, card.center().y()),
                   _css(PALETTE["soft"][0]))
        text_x = left + HINT_ICON + HINT_ICON_GAP
        text_w = card.right() - ADD_PAD_X - text_x
        line = self._line
        block = line["hint"] + line["meta"]
        top = card.center().y() - block / 2
        first = QFontMetricsF(self._fonts["hint"]).elidedText(line1, Qt.TextElideMode.ElideMiddle, text_w)
        self._say(painter, QRectF(text_x, top, text_w, line["hint"]), first, "hint", "text")
        self._say(painter, QRectF(text_x, top + line["hint"], text_w, line["meta"]), line2, "meta", "soft")

    def _paint_term(self, painter, rect, item, now):
        (card, card_alpha), (glow, _) = PALETTE["card"], PALETTE["highlight"]
        painter.setBrush(_color(_mix(card, glow, self._highlight(item["term"], now)), card_alpha))
        painter.drawRoundedRect(rect, TERM_RADIUS, TERM_RADIUS)
        remove = self._remove_rect(item, rect)
        right = remove.left() - 6
        left = rect.left() + TERM_PAD_X
        if item["left_out"]:
            # Too old to fit in Whisper's hint: say so, right next to the X.
            tag = "Cleanup only"
            tag_w = QFontMetricsF(self._fonts["tag"]).horizontalAdvance(tag) + 2 * TAG_PAD
            tag_rect = QRectF(right - tag_w, rect.center().y() - TAG_H / 2, tag_w, TAG_H)
            self._fill(painter, "tag", tag_rect, TAG_H / 2)
            self._say(painter, tag_rect, tag, "tag", "tag_text", Qt.AlignmentFlag.AlignHCenter)
            right = tag_rect.left() - 6
        text = QFontMetricsF(self._fonts["term"]).elidedText(item["term"], Qt.TextElideMode.ElideRight, right - left)
        self._say(painter, QRectF(left, rect.top(), right - left, TERM_H), text, "term", "text")

        if item["term"] == self._armed:
            self._fill(painter, "remove", remove, REMOVE / 2)
            self._say(painter, remove, "Remove?", "button", "remove_text", Qt.AlignmentFlag.AlignHCenter)
            return
        if self._hover == ("remove", item["term"]):
            self._fill(painter, "hover", remove, ellipse=True)
        self._icon(painter, "close", REMOVE_ICON, remove.center(), _css(PALETTE["soft"][0]))

    def _paint_footer(self, painter, x, y, width):
        """What the dictionary costs, estimated: Whisper's hint budget, and tokens per cleanup."""
        # Amber for both warnings: orange on blue is only 3.3:1, too faint for small text.
        if self._left_out():
            hint, color, alpha = "Whisper hint: over the limit", AMBER, 1.0  # the banner explains it
        else:
            used = estimate_tokens(", ".join(self._terms))
            hint, color, alpha = f"Whisper hint: about {used} of {WHISPER_PROMPT_BUDGET} tokens", WHITE, SOFT
            if used * 100 // WHISPER_PROMPT_BUDGET >= ALMOST_FULL:
                hint, color, alpha = hint + ", almost full", AMBER, 1.0
        small = self._line["small"]
        self._text(painter, QRectF(x, y, width, small), hint, "small", color, alpha)
        self._text(painter, QRectF(x, y + small + FOOTER_GAP, width, small),
                   f"Adds about {cleanup_tokens(self._terms)} tokens to each cleanup", "small", alpha=SOFT)
