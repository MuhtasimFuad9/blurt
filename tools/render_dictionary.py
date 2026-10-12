"""Renders the Dictionary panel with fake terms to data/dictionary_mockup.png, to review its look.

Nothing is shown on screen and the real dictionary.txt is never touched (the fake
terms live in a temporary folder). Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\render_dictionary.py

Four states side by side: a new word on the clipboard (mouse on Add), a word
that's already there, a long list scrolled down (Whisper's hint full, a "Remove?"
armed, clipboard hidden), and empty. The text contrast ratios (WCAG) are printed
and drawn too. data/dictionary_mockup.png is at 2x for detail;
data/dictionary_mockup_1x.png is the real on-screen size.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import load_config  # noqa: E402
from app.ui.pill import BLUE, _now_ms  # noqa: E402
from render_history import contrast  # noqa: E402

OUT = ROOT / "data" / "dictionary_mockup.png"
SCALE = 2.0
BACKGROUND = QColor("#e8ebf1")  # a light desktop, so the shadow shows

HEADER = "# Words the app should always spell exactly like this. One per line.\n# Lines starting with # are ignored.\n"
SHORT = ["Acme Creations", "Blender", "n8n", "Claude Code", "WisprFlow", "Premiere Pro", "DaVinci Resolve"]
LONG = SHORT + ["Groq", "After Effects", "Cinema 4D", "Houdini", "Substance Painter", "ZBrush", "Unreal Engine",
                "Octane Render", "Redshift", "Marmoset Toolbag", "Nuke", "Fusion", "Boot.dev", "PySide6",
                "sounddevice", "Sam", "Lisbon", "Supabase", "Vercel", "Tailwind", "Figma", "Framer", "Webflow",
                "Notion", "Obsidian", "Midjourney", "Runway", "ElevenLabs", "Kling", "Hailuo", "Topaz Video AI",
                "Frame.io", "Lottie", "Rive", "Spline", "Three.js", "GSAP", "Remotion", "Cavalry", "Motion Canvas",
                "Plasticity", "EmberGen", "Blackmagic Design", "Adobe Media Encoder", "Handbrake"]


def write_terms(folder, name, terms):
    path = folder / name
    path.write_text(HEADER + "".join(t + "\n" for t in terms), encoding="utf-8")
    return path


def render(panel, setup):
    panel._on_open()
    setup(panel)
    panel._shown.jump(1.0)
    panel._open = True
    image = QImage(panel.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    panel.render(image)
    return image


def contrast_lines(palette):
    card = palette["card"]
    panel_bg = (BLUE, 0.0)  # text straight on the blue panel
    on_blue = (BLUE, 1.0)
    rows = [
        ("Term", "text", "card", card),
        ("Reasons, hints", "soft", "card", card),
        ("Tag text", "tag_text", "tag", card),
        ("Add label", "add_text", "add", card),
        ("Remove? label", "remove_text", "remove", card),
    ]
    lines = [f"{label}: {contrast(palette[front], palette[back], under):.1f}:1" for label, front, back, under in rows]
    from app.ui.panel import AMBER, SOFT, WHITE

    for label, color in (("Footer", (WHITE, SOFT)), ("Footer, almost full or full", (AMBER, 1.0))):
        lines.append(f"{label}: {contrast(color, panel_bg, on_blue):.1f}:1")
    return lines


def main():
    app = QApplication(sys.argv)  # noqa: F841
    from app.ui.dictionary_panel import PALETTE, DictionaryPanel  # after QApplication (fonts)

    folder = Path(tempfile.mkdtemp())
    settings = dict(load_config()["pill"], scale=SCALE)

    def panel_for(terms, name):
        panel = DictionaryPanel(write_terms(folder, name, terms), debug=False)
        panel.set_settings(settings)
        return panel

    def ready(p):
        p.set_clipboard("text", "Groq")
        p._hover = "add"

    def refused(p):
        p.set_clipboard("text", "groq")
        p._hover = "notepad"
        p._tip.jump(1.0)

    def long_list(p):
        p.set_clipboard("hidden")
        p._scroll.jump(p._max_scroll())
        p._armed = p._items[-3]["term"]
        p._armed_until = _now_ms() + 3000

    shots = [
        render(panel_for(SHORT, "short.txt"), ready),
        render(panel_for(SHORT + ["Groq"], "groq.txt"), refused),
        render(panel_for(LONG, "long.txt"), long_list),
        render(panel_for([], "empty.txt"), lambda p: p.set_clipboard("none")),
    ]
    captions = ['A new word on the clipboard: "Add" ready, mouse on it',
                "A word that's already there (any capitals), mouse on the Notepad button",
                'Long list scrolled down: Whisper\'s hint full, "Remove?" clicked once, clipboard private',
                "Empty, nothing on the clipboard"]
    lines = contrast_lines(PALETTE)
    title = "Dictionary panel"
    print(f"{title}: " + " | ".join(lines))

    gap = 24 * SCALE
    caption_h = 44 * SCALE
    side_w = 190 * SCALE  # the title and contrast numbers
    shot_w = max(img.width() for img in shots)
    shot_h = max(img.height() for img in shots)
    width = int(side_w + 4 * shot_w + gap * 5)
    height = int(shot_h + caption_h + 2 * gap)
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(BACKGROUND)
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    small, big = QFont("Segoe UI"), QFont("Segoe UI")
    small.setPixelSize(int(12 * SCALE))
    big.setPixelSize(int(17 * SCALE))
    big.setWeight(QFont.Weight.DemiBold)
    painter.setPen(QColor("#3a4150"))
    painter.setFont(big)
    painter.drawText(QRectF(gap, gap, side_w, 30 * SCALE), Qt.AlignmentFlag.AlignLeft, title)
    painter.setFont(small)
    painter.drawText(QRectF(gap, gap + 36 * SCALE, side_w, shot_h), Qt.AlignmentFlag.AlignLeft,
                     "Contrast (WCAG):\n" + "\n".join(lines))
    x = gap + side_w + gap
    for img, caption in zip(shots, captions):
        painter.drawImage(QPointF(x, gap + shot_h - img.height()), img)  # bottoms line up, like on the pill
        painter.drawText(QRectF(x, gap + shot_h, img.width(), caption_h),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                         caption)
        x += shot_w + gap
    painter.end()

    OUT.parent.mkdir(exist_ok=True)
    sheet.save(str(OUT))
    small_out = OUT.with_name("dictionary_mockup_1x.png")
    sheet.scaled(int(width / SCALE), int(height / SCALE), Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation).save(str(small_out))
    print(f"saved {OUT} (2x) and {small_out}")


if __name__ == "__main__":
    main()
