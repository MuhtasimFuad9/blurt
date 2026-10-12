"""Renders the tray icon options and the paused idle pill options to data/tray_mockup.png.

Nothing is shown on screen. Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\render_tray.py

data/tray_mockup.png is the real on-screen size (what the taskbar would show at
100% scaling). data/tray_mockup_zoom.png is the same picture 4x bigger, with hard
pixel edges, so you can see every pixel.

Tray icons: three designs (A, B, C), normal and paused, at 16, 20, 24 and 32px
(16 is this laptop at 100% scaling; the others are 125%, 150% and 200%), each on a
light and a dark taskbar. Idle pill: today's look, then three paused looks, on a
light and a dark desktop.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.icons import icon_image  # noqa: E402

OUT = ROOT / "data" / "tray_mockup.png"
OUT_ZOOM = ROOT / "data" / "tray_mockup_zoom.png"
ZOOM = 4

BLUE = QColor("#1343d0")
GREY = QColor("#6b7385")
WHITE = QColor("#ffffff")
# The paused idle pill's outline: about 3.7:1 or better on white, light grey, dark and black
# (the pill blue is only 2.3:1 on dark).
PAUSED_OUTLINE = QColor("#3f68e6")
LIGHT_TASKBAR = QColor("#f3f3f3")
DARK_TASKBAR = QColor("#1f1f1f")
LIGHT_GLYPH = QColor("#1b1b1b")  # Windows' own tray icons on a light taskbar
LIGHT_DESKTOP = QColor("#dfe4ee")
DARK_DESKTOP = QColor("#15181f")

SIZES = [16, 20, 24, 32]
IDLE_OPACITY = 0.22


def _u(size):
    """One design pixel (the icons are designed on a 16 x 16 grid)."""
    return size / 16


def _bars(painter, size, heights, color, width=2, gap=2):
    """Vertical bars centered in the icon, snapped to whole pixels."""
    u = _u(size)
    w = max(1, round(width * u))
    g = max(1, round(gap * u))
    total = len(heights) * w + (len(heights) - 1) * g
    x = round((size - total) / 2)
    for h in heights:
        bar_h = max(2, round(h * u))
        y = round((size - bar_h) / 2)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, bar_h), w / 2, w / 2)
        painter.fillPath(path, color)
        x += w + g


def _square(painter, size, color):
    path = QPainterPath()
    radius = round(4 * _u(size))
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.fillPath(path, color)


def _circle(painter, size, color):
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    painter.fillPath(path, color)


WAVE = [6, 10, 6]
PAUSE = [9, 9]


def icon_a(painter, size, paused, dark):
    """A: blue rounded square with the waveform. Paused: the same square, pause bars."""
    _square(painter, size, BLUE)
    _bars(painter, size, PAUSE if paused else WAVE, WHITE, gap=3 if paused else 2)


def icon_a2(painter, size, paused, dark):
    """A2: like A, but paused turns grey, so it's clear at a glance."""
    _square(painter, size, GREY if paused else BLUE)
    _bars(painter, size, PAUSE if paused else WAVE, WHITE, gap=3 if paused else 2)


def icon_b(painter, size, paused, dark):
    """B: blue circle with a mic. Paused: grey circle, crossed out mic."""
    _circle(painter, size, GREY if paused else BLUE)
    glyph = round(size * 0.7)
    image = icon_image("mic_off" if paused else "mic", glyph, 1.0)
    painter.drawImage(round((size - glyph) / 2), round((size - glyph) / 2), image)


def icon_c(painter, size, paused, dark):
    """C: a plain glyph like Windows' own tray icons (follows the taskbar's light/dark)."""
    color = QColor(WHITE if dark else LIGHT_GLYPH)
    if paused:
        _bars(painter, size, PAUSE, color, gap=3)
    else:
        _bars(painter, size, [6, 12, 6], color)


DESIGNS = [
    ("A", "blue square, waveform", icon_a),
    ("A2", "blue square, paused = grey", icon_a2),
    ("B", "mic in a circle", icon_b),
    ("C", "plain glyph, like Windows' icons", icon_c),
]


def render_icon(draw, size, paused, dark):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(painter, size, paused, dark)
    painter.end()
    return image


# ---- idle pill, 40 x 6 ----


def pill_now(painter, rect):
    path = QPainterPath()
    path.addRoundedRect(rect, 3, 3)
    painter.fillPath(path, _alpha(BLUE, IDLE_OPACITY))


def pill_outline(painter, rect):
    """P1: hollow. The same capsule, only a 1px outline in a lighter blue that shows on light and dark."""
    painter.setPen(QPen(PAUSED_OUTLINE, 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 2.5, 2.5)
    painter.setPen(Qt.PenStyle.NoPen)


def pill_split(painter, rect):
    """P2: split in two with a gap in the middle, like a sideways pause sign."""
    half = (rect.width() - 6) / 2
    for x in (rect.left(), rect.left() + half + 6):
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, rect.top(), half, rect.height()), 3, 3)
        painter.fillPath(path, _alpha(BLUE, IDLE_OPACITY))


def pill_short(painter, rect):
    """P3: shrinks to a short 14 x 6 capsule."""
    short = QRectF(rect.center().x() - 7, rect.top(), 14, rect.height())
    path = QPainterPath()
    path.addRoundedRect(short, 3, 3)
    painter.fillPath(path, _alpha(BLUE, IDLE_OPACITY))


PILLS = [
    ("today", "not paused", pill_now),
    ("P1", "paused: outline", pill_outline),
    ("P2", "paused: split", pill_split),
    ("P3", "paused: short", pill_short),
]


def _alpha(color, opacity):
    c = QColor(color)
    c.setAlphaF(opacity)
    return c


def main():
    app = QApplication(sys.argv)  # noqa: F841 (Qt needs it for fonts and SVG)
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    font.setPixelSize(11)

    label_w = 190
    cell = 44  # room for one icon on its taskbar strip
    strip_w = len(SIZES) * cell
    row_h = 48
    head_h = 34
    icon_block_h = head_h + len(DESIGNS) * 2 * row_h
    pill_row_h = 40
    pill_block_h = 30 + len(PILLS) * pill_row_h
    width = label_w + 2 * strip_w + 30
    height = icon_block_h + 20 + pill_block_h + 10

    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#ffffff"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setFont(font)
    ink = QColor("#20242c")

    # Column headers
    p.setPen(ink)
    for i, (title, x0) in enumerate((("Light taskbar", label_w), ("Dark taskbar", label_w + strip_w + 20))):
        p.drawText(QRectF(x0, 2, strip_w, 14), Qt.AlignmentFlag.AlignLeft, title)
        for j, size in enumerate(SIZES):
            p.drawText(QRectF(x0 + j * cell, 16, cell, 14), Qt.AlignmentFlag.AlignCenter, f"{size}px")

    y = head_h
    for key, desc, draw in DESIGNS:
        for paused in (False, True):
            p.setPen(ink)
            p.drawText(
                QRectF(6, y, label_w - 10, row_h),
                Qt.AlignmentFlag.AlignVCenter,
                f"{key}  {'paused' if paused else 'normal'}\n{desc if not paused else ''}".rstrip(),
            )
            for s, (bg, dark) in enumerate(((LIGHT_TASKBAR, False), (DARK_TASKBAR, True))):
                x0 = label_w + s * (strip_w + 20)
                p.fillRect(QRectF(x0, y + 2, strip_w, row_h - 4), bg)
                for j, size in enumerate(SIZES):
                    icon = render_icon(draw, size, paused, dark)
                    ix = x0 + j * cell + (cell - size) // 2
                    iy = y + (row_h - size) // 2
                    p.drawImage(int(ix), int(iy), icon)
            y += row_h

    # Idle pill options
    y += 20
    p.setPen(ink)
    p.drawText(QRectF(6, y, width, 16), Qt.AlignmentFlag.AlignLeft, "Idle pill (40 x 6), real size")
    y += 30
    for key, desc, draw in PILLS:
        p.setPen(ink)
        p.drawText(QRectF(6, y, label_w - 10, pill_row_h), Qt.AlignmentFlag.AlignVCenter, f"{key}  {desc}")
        for s, bg in enumerate((LIGHT_DESKTOP, DARK_DESKTOP)):
            x0 = label_w + s * (strip_w + 20)
            p.fillRect(QRectF(x0, y + 2, strip_w, pill_row_h - 4), bg)
            p.setPen(Qt.PenStyle.NoPen)
            rect = QRectF(round(x0 + strip_w / 2 - 20), round(y + pill_row_h / 2 - 3), 40, 6)
            draw(p, rect)
        y += pill_row_h
    p.end()

    OUT.parent.mkdir(exist_ok=True)
    sheet.save(str(OUT))
    zoomed = sheet.scaled(width * ZOOM, height * ZOOM, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    zoomed.save(str(OUT_ZOOM))
    print(f"saved {OUT}")
    print(f"saved {OUT_ZOOM}")


if __name__ == "__main__":
    main()
