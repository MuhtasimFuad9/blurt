"""Renders the API key window to data/key_window_mockup.png, to review its look.

The real window code, drawn off screen (nothing appears, nothing is saved, no
network, the key shown is fake). Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\render_key_window.py

States side by side: empty (first run), checking, key works, key rejected,
couldn't reach Groq, and the "Change API key" version with the "doesn't look
like a Groq key" warning. The title bar above each is drawn here to show where
it goes; the real one is Windows' own (light or dark, like your other windows).
The text contrast ratios (WCAG) are printed and drawn too.
data/key_window_mockup.png is at 2x; data/key_window_mockup_1x.png is the real size.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

SCALE = 2
os.environ["QT_SCALE_FACTOR"] = str(SCALE)  # draw everything at 2x, before Qt starts

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from render_history import contrast  # noqa: E402

OUT = ROOT / "data" / "key_window_mockup.png"
BACKGROUND = QColor("#e8ebf1")
TITLE_BAR_H = 32
FAKE_KEY = "gsk_EXAMPLEnotArealKEYjustFORtheMOCKUP0123456789ab"


def title_bar(painter, x, y, width, title, icon):
    """Roughly what Windows draws above the window (light mode)."""
    painter.fillRect(QRectF(x, y, width, TITLE_BAR_H), QColor("#ffffff"))
    painter.drawPixmap(QRectF(x + 10, y + 8, 16, 16).toRect(), icon.pixmap(32, 32))
    font = QFont("Segoe UI")
    font.setPixelSize(12)
    painter.setFont(font)
    painter.setPen(QColor("#1b1b1b"))
    painter.drawText(QRectF(x + 34, y, width, TITLE_BAR_H), Qt.AlignmentFlag.AlignVCenter, title)
    pen = QPen(QColor("#1b1b1b"), 1)
    painter.setPen(pen)
    cx, cy = x + width - 23, y + TITLE_BAR_H / 2
    painter.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
    painter.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
    mx = cx - 46
    painter.drawLine(QPointF(mx - 5, cy), QPointF(mx + 5, cy))


def contrast_lines():
    from app.ui.history_panel import NAVY, SLATE
    from app.ui.history_panel import PALETTE as HISTORY
    from app.ui.pill import BLUE, WHITE

    card, blue, none = HISTORY["card"], (BLUE, 0.0), (BLUE, 0.0)
    white = (WHITE, 1.0)
    rows = [  # (label, text, drawn on, which sits on)
        ("Title", white, none, blue),
        ("Subtitle", (WHITE, 0.82), none, blue),
        ("Labels", (NAVY, 1.0), none, card),
        ("Hints, checking", (SLATE, 1.0), none, card),
        ("Key text (in the white box)", (NAVY, 1.0), white, card),
        ("Get a free key, key works icon", (BLUE, 1.0), none, card),
        ("Paste key, chosen language", white, (BLUE, 1.0), card),
        ("Other language", (NAVY, 1.0), (NAVY, 0.07), card),
        ("Rejected (orange)", HISTORY["failed"], none, card),
        ("Couldn't reach, warning (amber)", HISTORY["limited"], none, card),
        ("Save (blue on white)", (BLUE, 1.0), white, blue),
        ("Not now", white, none, blue),
    ]
    return [f"{label}: {contrast(front, back, under):.1f}:1" for label, front, back, under in rows]


def main():
    app = QApplication(sys.argv)  # noqa: F841
    from app.ui.key_window import KeyWindow  # after QApplication (fonts, screen)
    from app.ui.tray import tray_icon

    icon = tray_icon(False)

    def shot(status, key="", shown=False, first_run=True):
        window = KeyWindow(first_run=first_run, window_icon=icon)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        if key:
            window.field.setText(key)
            window._edited()
        window.show_key(shown)
        window.field.home(False)  # show the start of the key
        if status != "auto":
            window.set_status(status)
        app.processEvents()
        image = window.grab().toImage()
        title = window.windowTitle()
        window.close()
        return image, title

    shots = [
        (shot("empty"), "Empty (first run). Save is off until there's a key."),
        (shot("checking", FAKE_KEY), "Checking (after Save). The box and buttons wait."),
        (shot("works", FAKE_KEY), "Key works: saved, the window closes a moment later."),
        (shot("rejected", FAKE_KEY[:30], shown=True), "Key rejected, with Show on (a key cut short)."),
        (shot("unreachable", FAKE_KEY), "Couldn't reach Groq: Save becomes Save anyway."),
        (shot("auto", "sk-proj-abc123def456", shown=True, first_run=False),
         "From Settings (Change API key), a pasted key that isn't a Groq key."),
    ]
    lines = contrast_lines()
    print("Key window contrast:\n  " + "\n  ".join(lines))

    ratio = SCALE
    gap, side_w, caption_h = 24, 190, 44
    shot_w = max(img.width() for (img, _), _ in shots) / ratio
    shot_h = max(img.height() for (img, _), _ in shots) / ratio + TITLE_BAR_H
    columns = 3
    rows = (len(shots) + columns - 1) // columns
    width = side_w + columns * shot_w + gap * (columns + 2)
    height = rows * (shot_h + caption_h + gap) + gap
    sheet = QImage(int(width * ratio), int(height * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    sheet.setDevicePixelRatio(ratio)
    sheet.fill(BACKGROUND)
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    small, big = QFont("Segoe UI"), QFont("Segoe UI")
    small.setPixelSize(12)
    big.setPixelSize(17)
    big.setWeight(QFont.Weight.DemiBold)
    painter.setPen(QColor("#3a4150"))
    painter.setFont(big)
    painter.drawText(QRectF(gap, gap, side_w, 30), Qt.AlignmentFlag.AlignLeft, "API key window")
    painter.setFont(small)
    painter.drawText(QRectF(gap, gap + 36, side_w, height), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                     "Contrast (WCAG):\n" + "\n".join(lines)
                     + "\n\nThe white title bar is drawn here as a stand-in: the real one is Windows' own.")
    for i, ((img, title), caption) in enumerate(shots):
        column, row = i % columns, i // columns
        x = gap + side_w + gap + column * (shot_w + gap)
        y = gap + row * (shot_h + caption_h + gap)
        img_w = img.width() / ratio
        title_bar(painter, x, y, img_w, title, icon)
        img.setDevicePixelRatio(ratio)
        painter.drawImage(QPointF(x, y + TITLE_BAR_H), img)
        painter.setPen(QColor("#3a4150"))
        painter.setFont(small)
        painter.drawText(QRectF(x, y + shot_h, img_w, caption_h),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                         caption)
    painter.end()

    OUT.parent.mkdir(exist_ok=True)
    sheet.save(str(OUT))
    small_out = OUT.with_name("key_window_mockup_1x.png")
    sheet.scaled(int(width), int(height), Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation).save(str(small_out))
    print(f"saved {OUT} (2x) and {small_out}")


if __name__ == "__main__":
    main()
