"""Renders the Settings panel to data/settings_mockup.png, to review its look.

Nothing is shown on screen and the real config.json is never touched: each state
reads a copy in a temporary folder (your real settings, then a few changes). Run
from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\render_settings.py

States side by side: your settings, the opacity slider mid-drag, config.json
broken (banner, controls locked), and "Quit?" armed with a hand-set language and
the off toggles. Start with Windows is faked (the registry is never touched): on,
off, on while config.json is broken, and locked (running from source). The text contrast ratios (WCAG) are printed and drawn too.
data/settings_mockup.png is at 2x for detail; data/settings_mockup_1x.png is the
real on-screen size.

With --shortcut it renders the Shortcut row (M14) instead, to
data/settings_shortcut_mockup.png (and _1x): closed, the list open, a pick with
Save on, the longest name, a hotkey config.json doesn't allow, and config.json
broken, with the contrast and the panel's tallest height. Then the hover pill with
today's label and the two longest, to data/pill_shortcut_mockup.png (and _1x).
    .\\.venv\\Scripts\\python.exe tools\\render_settings.py --shortcut

The config copies live in a temporary folder that is deleted when the tool ends.
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

from app.config import CONFIG_PATH, load_config_or_defaults, set_setting  # noqa: E402
from app.ui.pill import _now_ms  # noqa: E402
from render_history import contrast  # noqa: E402

OUT = ROOT / "data" / "settings_mockup.png"
SCALE = 2.0
BACKGROUND = QColor("#e8ebf1")  # a light desktop, so the shadow shows


def config_copy(folder, name, changes=(), broken=False):
    """A copy of the real config.json with some settings changed (the same code the panel will use)."""
    path = folder / name
    path.write_bytes(CONFIG_PATH.read_bytes())
    for key, value, section in changes:
        set_setting(key, value, section=section, path=path)
    if broken:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"language": "en",', '"language": "en"', 1), encoding="utf-8")
    return path


class FakeAutostart:
    """Start with Windows without the registry."""

    def __init__(self, on, installed=True):
        self.on, self.installed = on, installed

    def available(self):
        return self.installed

    def enabled(self):
        return self.on

    def set_enabled(self, on):
        self.on = on


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
    """Contrast of each kind of text. On a card, text sits on the card, which sits on the blue."""
    from app.ui.pill import BLUE, WHITE

    blue, card, on_card = (BLUE, 1.0), palette["card"], (BLUE, 0.0)
    rows = [  # (label, text color, what it's drawn on, what that sits on)
        ("Labels, banner text", palette["label"], on_card, card),
        ("Hints", palette["hint"], on_card, card),
        ("Chosen button", palette["seg_on_text"], palette["seg_on"], card),
        ("Other button", palette["seg_text"], palette["seg_track"], card),
        ("Open in Notepad", palette["button_text"], palette["button"], card),
        ("Banner icon (amber)", palette["warning"], on_card, card),
        ("Footer, Quit", (WHITE, 1.0), on_card, blue),
    ]
    return [f"{label}: {contrast(front, back, under):.1f}:1" for label, front, back, under in rows]


def main():
    app = QApplication(sys.argv)  # noqa: F841
    with tempfile.TemporaryDirectory() as name:  # the config copies, deleted when done
        if "--shortcut" in sys.argv[1:]:
            render_shortcut(Path(name))
        else:
            render_settings(Path(name))


def render_settings(folder):
    from app.ui.settings_panel import PALETTE, SettingsPanel  # after QApplication (fonts)

    settings = dict(load_config_or_defaults()[0]["pill"], scale=SCALE)

    def panel_for(path, autostart):
        panel = SettingsPanel(path, debug=False, autostart=autostart)
        panel.set_settings(settings)
        return panel

    def dragging(p):
        p._drag = 0.45

    def quit_armed(p):
        p._quit_armed_until = _now_ms() + 3000
        p._hover = ("language", "auto")

    shots = [
        render(panel_for(config_copy(folder, "yours.json"), FakeAutostart(True)), lambda p: None),
        render(panel_for(config_copy(folder, "drag.json", [("paste_mode", "always", None),
                                                           ("keep_mic_open", False, None)]),
                         FakeAutostart(False)), dragging),
        render(panel_for(config_copy(folder, "broken.json", broken=True), FakeAutostart(True)), lambda p: None),
        render(panel_for(config_copy(folder, "bengali.json", [("language", "bn", None), ("history", False, None),
                                                              ("mic_label", False, "pill")]),
                         FakeAutostart(False, installed=False)), quit_armed),
    ]
    captions = ["Your settings, installed: Start with Windows on",
                "Start with Windows off; dragging the slider to 45%; Paste set to Always, mic ready off",
                "config.json broken: controls locked, but Start with Windows still works (it's not in config.json)",
                "Running from source: Start with Windows dimmed, does nothing; "
                '"bn" as a 3rd language; history and mic name off; "Quit?" armed']
    lines = contrast_lines(PALETTE)
    print("Settings panel contrast:\n  " + "\n  ".join(lines))
    save_sheet(shots, captions, "Settings panel", ["Contrast (WCAG):"] + lines, OUT)


def save_sheet(shots, captions, title, lines, out):
    """The shots side by side with captions, lines of notes on the left: out at 2x, out_1x at 1x."""
    gap = 24 * SCALE
    caption_h = 44 * SCALE
    side_w = 190 * SCALE
    shot_w = max(img.width() for img in shots)
    shot_h = max(img.height() for img in shots)
    width = int(side_w + len(shots) * shot_w + gap * (len(shots) + 2))
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
    painter.drawText(QRectF(gap, gap + 36 * SCALE, side_w, shot_h), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                     "\n".join(lines))
    x = gap + side_w + gap
    for img, caption in zip(shots, captions):
        painter.drawImage(QPointF(x, gap + shot_h - img.height()), img)  # bottoms line up, like on the pill
        painter.drawText(QRectF(x, gap + shot_h, img.width(), caption_h),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                         caption)
        x += shot_w + gap
    painter.end()

    out.parent.mkdir(exist_ok=True)
    sheet.save(str(out))
    small_out = out.with_name(out.stem + "_1x.png")
    sheet.scaled(int(width / SCALE), int(height / SCALE), Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation).save(str(small_out))
    print(f"saved {out} (2x) and {small_out}")


def shortcut_contrast_lines(palette):
    """The Shortcut row's own text (M14). On a card, text sits on the card, which sits on the blue."""
    from app.ui.pill import BLUE

    card, on_card = palette["card"], (BLUE, 0.0)
    rows = [  # (label, text color, what it's drawn on, what that sits on)
        ("Hint", palette["hint"], on_card, card),
        ("Hint, not allowed (dark amber)", palette["warning"], on_card, card),
        ("Shortcut names", palette["seg_text"], on_card, card),
        ("Shortcut names, hovered", palette["seg_text"], palette["seg_hover"], card),
        ("Default tag", palette["tag_text"], palette["tag"], card),
        ('"in use", note', palette["hint"], on_card, card),
        ("Cancel", palette["link"], on_card, card),
        ("Cancel, hovered", palette["link"], palette["seg_hover"], card),
        ("Change, Save", palette["button_text"], palette["button"], card),
    ]
    return [f"{label}: {contrast(front, back, under):.1f}:1" for label, front, back, under in rows]


def render_shortcut(folder):
    """The Shortcut row (M14) in six states, then the hover pill with the longest names."""
    from app.ui import panel as panel_module
    from app.ui.settings_panel import PALETTE, SettingsPanel  # after QApplication (fonts)

    settings = dict(load_config_or_defaults()[0]["pill"], scale=SCALE)

    def panel_for(path):
        panel = SettingsPanel(path, debug=False, key_status=lambda: "windows", autostart=FakeAutostart(True))
        panel.set_settings(settings)
        return panel

    def opened(pick=None, hover=None):
        def setup(p):
            p._open_shortcut_list(True)  # the app's own path (keeps the scroll in step, M16)
            p._shortcut_pick = pick or p._hotkey()
            p._hover = hover
        return setup

    def closed(p):
        pass

    default = config_copy(folder, "default.json", [("hotkey", "Ctrl+Win", None)])
    states = [  # (config, setup, caption)
        (default, closed, "1. Closed, Ctrl+Win: the hint names it, Change opens the list"),
        (default, opened(), "2. Change: the list replaces the cards; the one in use picked, Save off"),
        (default, opened("Alt+Shift+Win", "hotkey_save"), "3. Alt+Shift+Win picked: Save on (hovered)"),
        (config_copy(folder, "longest.json", [("hotkey", "Ctrl+Shift+Win", None)]), closed,
         "4. Closed, Ctrl+Shift+Win: the longest hint"),
        (config_copy(folder, "blocked.json", [("hotkey", "Ctrl+Shift", None)]), closed,
         '5. config.json says "Ctrl+Shift": shows Ctrl+Win (what the app uses), dark amber hint'),
        (config_copy(folder, "broken.json", [("hotkey", "Alt+Win", None)], broken=True), closed,
         "6. config.json broken: the row is locked and shows no value, like the other rows"),
    ]
    shots, captions, heights = [], [], []
    for path, setup, caption in states:
        panel = panel_for(path)
        shots.append(render(panel, setup))
        heights.append(panel._card_height(panel._content_height()))
        captions.append(caption)
    gap_above_taskbar = load_config_or_defaults()[0]["pill"]["gap_above_taskbar"]

    def needed(card):  # the card, the gap, the hover pill (34) and its gap above the taskbar
        return round(card + panel_module.GAP_ABOVE_PILL + 34 + gap_above_taskbar)

    size_lines = ["Card heights at 1x: " + ", ".join(str(round(h)) for h in heights),
                  "Card + hover pill + gaps, against a 1366 x 768 laptop (about 720px above the taskbar):"]
    for label, height in (("closed", heights[0]), ("list open", heights[1]), ("config.json broken", heights[5])):
        size_lines.append(f"  {label}: {needed(height)}px, " + ("fits" if needed(height) <= 720 else "DOES NOT FIT"))
    lines = shortcut_contrast_lines(PALETTE)
    print("Shortcut row contrast:\n  " + "\n  ".join(lines))
    print("\n".join(size_lines))
    save_sheet(shots, captions, "Settings: Shortcut", ["Contrast (WCAG):"] + lines + [""] + size_lines,
               ROOT / "data" / "settings_shortcut_mockup.png")
    render_pills()


def render_pills():
    """The hover pill with today's label and the two longest, on a light and a dark strip."""
    from app.ui import pill as pill_module

    base = _now_ms()
    shots, captions = [], []
    for hotkey in ("Ctrl+Win", "Ctrl+Shift+Win", "Alt+Shift+Win"):
        pill = pill_module.Pill(hotkey=hotkey)
        size = pill._sizes["hover"]  # design pixels, before the 2x
        pill._settings = dict(pill._settings, scale=SCALE)
        pill._apply_layout()
        pill._hovered = True
        real_now = pill_module._now_ms
        try:
            for step in range(1, 5):  # a fake clock, so every animation has finished
                pill_module._now_ms = lambda step=step: base + step * 5000
                if step == 1:
                    pill._refresh()
                pill._tick()
        finally:
            pill_module._now_ms = real_now
        image = QImage(pill.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        pill.render(image)
        for background in ("#e8ebf1", "#1f2330"):
            framed = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
            framed.fill(QColor(background))
            painter = QPainter(framed)
            painter.drawImage(0, 0, image)
            painter.end()
            shots.append(framed)
            captions.append(f"{hotkey}: hover pill {size[0]} x {size[1]}")
        pill.deleteLater()
    save_sheet(shots, captions, "Hover pill", ["The menu names the shortcut; a longer name makes the pill wider."],
               ROOT / "data" / "pill_shortcut_mockup.png")


if __name__ == "__main__":
    main()
