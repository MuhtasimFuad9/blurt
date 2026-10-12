"""Makes installer/blurt.ico, the app icon (the exe, the Start menu, the installer,
the Apps list), from the tray icon's own drawing (_paint_icon in app/ui/tray.py),
so they always match. Run from the project folder after changing the tray design:
    .\\.venv\\Scripts\\python.exe tools\\make_icon.py

The .ico is committed, so a build doesn't need to run this. An .ico file is a small
header, then one directory entry per size, then the images; each image here is a
PNG, which Windows reads at every size since Vista. No extra package needed.
"""

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

from app.ui.tray import _paint_icon  # noqa: E402

OUT = ROOT / "installer" / "blurt.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)  # 256 for big Explorer views and the Start menu


def png_bytes(size):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _paint_icon(painter, size, paused=False)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def ico_bytes(images):
    """images: [(size, png bytes)]. The ICONDIR header, the entries, then the PNGs."""
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type 1 = icon, count
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size  # 0 means 256 in an .ico entry
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + entries + blobs


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 (painting needs one)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(ico_bytes([(size, png_bytes(size)) for size in SIZES]))
    print(f"saved {OUT} ({', '.join(str(s) for s in SIZES)} px)")


if __name__ == "__main__":
    main()
