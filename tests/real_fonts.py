"""Are the panels measured with the real Windows fonts? (M16)

Every test that measures a panel's size depends on this. Under Qt's "offscreen"
platform (QT_QPA_PLATFORM=offscreen) text gets other widths and line heights, so a
card would be measured at the wrong height and a fit test could pass for the wrong
reason. problems() says what's wrong; the measuring tests call require_real_fonts()
in setUpClass, so they fail loudly instead of measuring the wrong fonts.

The pinned numbers were measured on Windows 11 with the panels' own font (the Settings
row font, 13px medium) on Sept 30, 2026. Widths and the line height get a range of
3% either side, so a Windows font update doesn't break the tests. Ordinary words are
no good here: "Start with Windows" is within 1% in Segoe, Arial and Tahoma (what Qt
uses for a font name that doesn't exist). Digits, a row of "i"s and the line height
do tell them apart:

    text / line height   Segoe UI Variable Text   Arial              missing font (Tahoma)
    "1234567890"         67.922                   72.188 (+6.3%)     70.938 (+4.4%)
    "iiiiiiiiii"         31.406                   28.750 (-8.5%)     29.828 (-5.0%)
    line height          17.266                   14.516 (-15.9%)    15.672 (-9.2%)
"""

from PySide6.QtGui import QFont, QFontInfo, QFontMetricsF, QGuiApplication

from app.ui.panel import _font

FAMILIES = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")  # what _font asks for, in order
PX, WEIGHT = 13, QFont.Weight.Medium  # the Settings row font
WIDTHS = {"1234567890": 67.922, "iiiiiiiiii": 31.406}
LINE_HEIGHT = 17.266
TOLERANCE = 0.03


def panel_font():
    return _font(PX, WEIGHT)


def out_of_range(value, pinned):
    return abs(value / pinned - 1) > TOLERANCE


def measure(font):
    """{"1234567890": width, "iiiiiiiiii": width, "line height": height} for a font."""
    metrics = QFontMetricsF(font)
    sizes = {text: metrics.horizontalAdvance(text) for text in WIDTHS}
    sizes["line height"] = metrics.height()
    return sizes


def problems():
    """What's wrong with the fonts the panels get, as a list of sentences ([] = all real)."""
    found = []
    platform = QGuiApplication.platformName()
    if platform != "windows":
        found.append(f"Qt platform is {platform!r}, not 'windows' (QT_QPA_PLATFORM set?)")
    font = panel_font()
    family = QFontInfo(font).family()
    if family not in FAMILIES:
        found.append(f"the panel font resolved to {family!r}, not one of {FAMILIES}")
    pinned = dict(WIDTHS, **{"line height": LINE_HEIGHT})
    for name, value in measure(font).items():
        if out_of_range(value, pinned[name]):
            found.append(f"{name!r} measures {value:.3f}, expected {pinned[name]} (within {TOLERANCE:.0%})")
    return found


def require_real_fonts():
    """For setUpClass: raise (so every test in the class fails) unless the fonts are the real ones."""
    found = problems()
    if found:
        raise AssertionError("not measuring with the real Windows fonts: " + "; ".join(found))
