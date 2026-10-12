"""Line icons (white by default), copied from the approved mockup's SVGs.

Each icon is drawn once per size into an image at the screen's real pixel
density (1.25x at 125% scaling), so it stays sharp and painting stays cheap.
"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

# The inside of each <svg>, in a 24 x 24 box.
PATHS = {
    "usage": '<path d="M5 20V11"/><path d="M12 20V5"/><path d="M19 20v-6"/>',
    "history": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "dictionary": (
        '<path d="M5 5.5A1.5 1.5 0 0 1 6.5 4H19v14H6.5A1.5 1.5 0 0 0 5 19.5z"/>'
        '<path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19"/>'
    ),
    "settings": (
        '<path d="M4 7h10"/><path d="M18 7h2"/><circle cx="16" cy="7" r="2"/>'
        '<path d="M4 17h2"/><path d="M10 17h10"/><circle cx="8" cy="17" r="2"/>'
    ),
    "pause": '<path d="M9 6v12"/><path d="M15 6v12"/>',
    "play": '<path d="M8.5 5.8v12.4a.8.8 0 0 0 1.2.7l9.6-6.2a.8.8 0 0 0 0-1.4L9.7 5.1a.8.8 0 0 0-1.2.7z"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "close": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    "mic": (
        '<rect x="9" y="3" width="6" height="11.5" rx="3"/>'
        '<path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/>'
    ),
    "mic_off": (  # the mic, crossed out: "No microphone"
        '<rect x="9" y="3" width="6" height="11.5" rx="3"/>'
        '<path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/><path d="M4 4l16 16"/>'
    ),
    "copy": (
        '<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/>'
        '<path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>'
    ),
    "plus": '<path d="M12 5.5v13"/><path d="M5.5 12h13"/>',
    "file": (  # a page with lines: "open the file in Notepad"
        '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/>'
        '<path d="M14 3.5V8h4.5"/><path d="M9 12.5h6"/><path d="M9 16h4"/>'
    ),
    "retry":'<path d="M4.5 12a7.5 7.5 0 1 0 7.5-7.5 8.2 8.2 0 0 0-5.6 2.3L4.5 8.7"/><path d="M4.5 4.5v4.2h4.2"/>',
    "alert": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.8v4.9"/><path d="M12 16.3h.01"/>',
    "hourglass": (
        '<path d="M6.5 3.5h11"/><path d="M6.5 20.5h11"/>'
        '<path d="M8 3.5v2.8a4 4 0 0 0 1.5 3.1L12 12l2.5-2.6A4 4 0 0 0 16 6.3V3.5"/>'
        '<path d="M8 20.5v-2.8a4 4 0 0 1 1.5-3.1L12 12l2.5 2.6a4 4 0 0 1 1.5 3.1v2.8"/>'
    ),
    # Show / hide the API key (the key window)
    "eye": '<path d="M2.8 12S6.3 5.8 12 5.8 21.2 12 21.2 12 17.7 18.2 12 18.2 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="2.8"/>',
    "eye_off": (
        '<path d="M2.8 12S6.3 5.8 12 5.8 21.2 12 21.2 12 17.7 18.2 12 18.2 2.8 12 2.8 12z"/>'
        '<circle cx="12" cy="12" r="2.8"/><path d="M4.5 4.5l15 15"/>'
    ),
    "paste": (  # a clipboard: "Paste key"
        '<rect x="5.5" y="5" width="13" height="15.5" rx="2"/>'
        '<path d="M9 5V4.2A1.2 1.2 0 0 1 10.2 3h3.6A1.2 1.2 0 0 1 15 4.2V5"/><path d="M9 10.5h6"/><path d="M9 14h4"/>'
    ),
    "external": '<path d="M13.5 5.5h5v5"/><path d="M18.5 5.5L11 13"/><path d="M16.5 14v4.5h-11v-11H10"/>',
}

_cache = {}


def icon_image(name, size, pixel_ratio, color="#ffffff"):
    """An icon as a QImage, size x size logical pixels, drawn in color (white unless told otherwise)."""
    key = (name, size, pixel_ratio, color)
    if key not in _cache:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            f"{PATHS[name]}</svg>"
        )
        pixels = round(size * pixel_ratio)
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(QByteArray(svg.encode())).render(painter)
        painter.end()
        image.setDevicePixelRatio(pixel_ratio)
        _cache[key] = image
    return _cache[key]
