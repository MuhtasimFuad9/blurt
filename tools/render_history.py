"""Renders the History panel with fake entries to data/history_mockup.png, to review its look.

Nothing is shown on screen and the real history.db is never touched. Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\render_history.py

Four states side by side: opened from the "!" pill (newest entry highlighted, mouse
on a Copy button), scrolled down with a retry running, History off with Clear
clicked once, and empty. A second row shows Show original (M13): the mouse on
Original, a card switched to Whisper's raw transcript, and Copy on it. The text contrast ratios (WCAG) are printed and drawn too. data/history_mockup.png is at 2x for detail;
data/history_mockup_1x.png is the real on-screen size.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import load_config  # noqa: E402
from app.history import HistoryStore  # noqa: E402
from app.ui.pill import _now_ms  # noqa: E402

OUT = ROOT / "data" / "history_mockup.png"
SCALE = 2.0
BACKGROUND = QColor("#e8ebf1")  # a light desktop, so the shadow shows

NOW = datetime.now().replace(hour=18, minute=5, second=0, microsecond=0)


class FakeTracker:
    """A transcription rate limit with 24s left, for the countdown."""

    def snapshot(self):
        return {"rate_limit": {"kind": "transcription", "retry_in": 24.0}}


def at(days_ago, hour, minute):
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=minute).timestamp()


def fill(store, clock):
    def same(text):  # raw with the same words as the cleaned text, so no Original button
        return dict(raw=text.lower(), cleaned=text, cleanup="ok")

    entries = [
        (at(6, 9, 3), dict(status="pasted", **same("Remind me to renew the domain for Acme Creations before Friday."))),
        (at(1, 23, 40), dict(status="copied", **same("Can you check whether the Blender render farm finished overnight?"))),
        (at(1, 16, 12), dict(status="pasted", raw="so the new intro animation should ease out over about three hundred milliseconds and then hold", cleaned=None, cleanup="limited")),
        (at(0, 14, 41), dict(status="pasted", **same("Okay, here's the plan for the launch video. We open on the logo reveal, then cut to the product shots, and end with the call to action and the website link on screen for at least three seconds."))),
        (at(0, 16, 58), dict(status="copied", **same("Thanks, that works for me."))),
        (at(0, 17, 52), dict(status="failed", error="Nothing heard on retry", wav_bytes=b"x")),
        (at(0, 18, 1), dict(status="limited", error="Rate limit: requests per minute", wav_bytes=b"x")),
        (at(0, 18, 4), dict(status="failed", error="No answer from Groq", wav_bytes=b"x")),
    ]
    ids = []
    for ts, fields in entries:
        clock[0] = ts
        ids.append(store.add(**fields))
    store.update(ids[1], retried_ts=at(1, 23, 41))
    clock[0] = NOW.timestamp()
    return ids


DROPPED_RAW = ("I want to build an installer so other people can use the app without setting up Python. "
               "Basically anyone should be able to download one file and start dictating right away.")


def fill_original(store, clock):
    """Entries for the Show original rows. Returns the id of the one cleanup cut a sentence from."""
    entries = [
        (at(0, 17, 20), dict(status="pasted", raw="thanks that works for me", cleaned="Thanks, that works for me.",
                             cleanup="ok")),  # same words: no button
        (at(0, 17, 35), dict(status="pasted", raw="so the new intro should ease out over about three hundred milliseconds",
                             cleaned=None, cleanup="limited")),  # not cleaned up: no button
        (at(0, 17, 48), dict(status="pasted", raw="um so the render finished I think around like two a m and uh the colors look a bit off",
                             cleaned="The render finished around 2 AM, and the colors look a bit off.", cleanup="ok")),
        (at(0, 18, 2), dict(status="pasted", raw=DROPPED_RAW,
                            cleaned="Anyone should be able to download one file and start dictating right away.",
                            cleanup="ok")),
    ]
    ids = []
    for ts, fields in entries:
        clock[0] = ts
        ids.append(store.add(**fields))
    clock[0] = NOW.timestamp()
    return ids[-1]


def render(panel, setup):
    panel._on_open()
    setup(panel)
    panel._shown.jump(1.0)
    panel._open = True
    panel._scroll.jump(panel._scroll.end)
    image = QImage(panel.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    panel.render(image)
    return image


def _luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _over(color, background):
    """A (rgb, alpha) color painted over a solid rgb background."""
    rgb, alpha = color
    return tuple(round(c * alpha + bg * (1 - alpha)) for c, bg in zip(rgb, background))


def contrast(color, background, card):
    """WCAG contrast ratio of a (rgb, alpha) text color on a (rgb, alpha) background,
    which sits on the entry card, which sits on the blue panel."""
    from app.ui.pill import BLUE

    back = _over(background, _over(card, BLUE))
    front = _over(color, back)
    light, dark = sorted((_luminance(front), _luminance(back)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def contrast_lines(palette):
    rows = [
        ("Dictation text", "text", "card"),
        ("Time, reasons", "soft", "card"),
        ("Failed", "failed", "card"),
        ("Rate limited", "limited", "card"),
        ("Tag text", "tag_text", "tag"),
        ("Retry label", "retry_text", "retry"),
        ("Text, highlighted", "text", "highlight"),
        ("Original button", "original", "card"),
        ("Original, hovered", "tag_text", "hover"),
        ("Original, on", "original_on_text", "original_on"),
    ]
    return [f"{label}: {contrast(palette[front], palette[back], palette['card']):.1f}:1"
            for label, front, back in rows]


def main():
    app = QApplication(sys.argv)  # noqa: F841
    from app.ui.history_panel import PALETTE, HistoryPanel  # after QApplication (fonts)

    folder = Path(tempfile.mkdtemp())
    clock = [NOW.timestamp()]
    settings = dict(load_config()["pill"], scale=SCALE)

    store = HistoryStore(folder / "history.db", folder / "failed", clock=lambda: clock[0])
    ids = fill(store, clock)
    empty_store = HistoryStore(folder / "empty.db", folder / "empty_failed")

    def opened_from_pill(p):
        p._open = True  # so show_entry doesn't show a real window
        p.show_entry(ids[-1])
        p._highlight_start = _now_ms()  # still in the full-highlight part
        p._hover = ("copy", ids[3])

    def scrolled(p):
        store.set_retrying(ids[5], True)
        p._refresh()
        p._scroll.jump(150.0)

    def history_off(p):
        store.set_retrying(ids[5], False)
        p._history_on = False
        p._refresh()
        p._clear_armed_until = _now_ms() + 3000

    captions = ['Opened from the "!" pill: newest entry highlighted, mouse on Copy',
                "Scrolled down, a retry in progress", 'History off, "Clear" clicked once', "Empty"]
    rows = []
    panel = HistoryPanel(store, FakeTracker(), debug=False, clock=lambda: clock[0])
    panel.set_settings(settings)
    empty = HistoryPanel(empty_store, None, debug=False)
    empty.set_settings(settings)
    shots = [render(panel, opened_from_pill), render(panel, scrolled), render(panel, history_off),
             render(empty, lambda p: None)]
    lines = contrast_lines(PALETTE)
    print("History panel: " + " | ".join(lines[:-3]))
    rows.append(("History panel", shots, lines[:-3], captions))

    # Show original (M13): switching a card to Whisper's raw transcript.
    original_store = HistoryStore(folder / "original.db", folder / "original_failed", clock=lambda: clock[0])
    cut = fill_original(original_store, clock)

    def mouse_on_original(p):
        p._hover = ("original", cut)

    def switched(p):
        p._original.add(cut)
        p._layout()

    def switched_mouse_on_copy(p):
        switched(p)
        p._hover = ("copy", cut)

    original_captions = ["Mouse on Original (cleanup cut the first sentence)",
                         "Switched to the original, mouse elsewhere",
                         "Mouse on Copy: copies the original",
                         ]
    p = HistoryPanel(original_store, None, debug=False, clock=lambda: clock[0])
    p.set_settings(settings)
    shots = [render(p, mouse_on_original), render(p, switched), render(p, switched_mouse_on_copy)]
    rows.append(("Show original", shots, lines[-3:], original_captions))
    print("Original button: " + " | ".join(lines[-3:]))

    gap = 24 * SCALE
    caption_h = 40 * SCALE
    side_w = 190 * SCALE  # the row title and contrast numbers
    shot_w = max(img.width() for _, shots, _, _ in rows for img in shots)
    shot_h = max(img.height() for _, shots, _, _ in rows for img in shots)
    width = int(side_w + 4 * shot_w + gap * 5)
    row_h = shot_h + caption_h
    height = int(len(rows) * row_h + gap * (len(rows) + 1))
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(BACKGROUND)
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    small, big = QFont("Segoe UI"), QFont("Segoe UI")
    small.setPixelSize(int(12 * SCALE))
    big.setPixelSize(int(17 * SCALE))
    big.setWeight(QFont.Weight.DemiBold)
    ink = QColor("#3a4150")
    for r, (title, shots, lines, captions) in enumerate(rows):
        top = gap + r * (row_h + gap)
        painter.setPen(ink)
        painter.setFont(big)
        painter.drawText(QRectF(gap, top, side_w, 60 * SCALE), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, title)
        painter.setFont(small)
        text = "Contrast (WCAG):\n" + "\n".join(lines)
        painter.drawText(QRectF(gap, top + 66 * SCALE, side_w, row_h), Qt.AlignmentFlag.AlignLeft, text)
        x = gap + side_w + gap
        for img, caption in zip(shots, captions):
            painter.drawImage(QPointF(x, top + shot_h - img.height()), img)  # bottoms line up, like on the pill
            painter.drawText(QRectF(x, top + shot_h, img.width(), caption_h),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                             caption)
            x += shot_w + gap
    painter.end()

    OUT.parent.mkdir(exist_ok=True)
    sheet.save(str(OUT))
    sheet.scaled(int(width / SCALE), int(height / SCALE), Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation).save(str(OUT.with_name("history_mockup_1x.png")))
    store.close()
    empty_store.close()
    original_store.close()
    print(f"saved {OUT} (2x) and {OUT.with_name('history_mockup_1x.png')}")


if __name__ == "__main__":
    main()
