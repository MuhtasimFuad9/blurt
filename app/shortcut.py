"""The hold-to-talk shortcut: which combinations are allowed, and how they're written.

A shortcut is a set of modifier keys ("families"), each meaning either side: "Ctrl" is
Left or Right Ctrl. Only five combinations are allowed. The others clash with typing
or with Windows itself, and each has a reason people can read.

Written in config.json as text like "Ctrl+Win" (the canonical form, always in the
order Ctrl, Alt, Shift, Win). Reading is forgiving about case, spaces and order,
but only these four names are accepted. Pure: no Windows calls, so it's easy to test.
"""

import json

FAMILIES = ("Ctrl", "Alt", "Shift", "Win")  # the canonical order

DEFAULT = frozenset({"Ctrl", "Win"})

# Offered in Settings, in this order. Every one includes Win, so the hook only ever
# hides Win from Windows (Ctrl, Alt and Shift always stay visible).
ALLOWED = (
    DEFAULT,
    frozenset({"Alt", "Win"}),
    frozenset({"Ctrl", "Alt", "Win"}),
    frozenset({"Ctrl", "Shift", "Win"}),
    frozenset({"Alt", "Shift", "Win"}),
)

# Every other combination of two or more, and why it isn't offered.
BLOCKED = {
    frozenset({"Ctrl", "Shift"}): "every Ctrl+Shift+Arrow text selection would start it",
    frozenset({"Alt", "Shift"}): "Windows uses it to switch keyboard layouts",
    frozenset({"Ctrl", "Alt"}): "that's AltGr on many keyboards, so typing @ or € would start it",
    frozenset({"Shift", "Win"}): "every Win+Shift+S screenshot would start it",
    frozenset({"Ctrl", "Alt", "Shift"}): "AltGr+Shift types letters on some keyboards",
    frozenset({"Ctrl", "Alt", "Shift", "Win"}): "the Office key sends it",
}

_BY_LOWER = {name.lower(): name for name in FAMILIES}


class ShortcutError(ValueError):
    """The text isn't an allowed shortcut. str() says why, in words."""


def text(families):
    """The canonical form for config.json: "Ctrl+Win"."""
    return "+".join(name for name in FAMILIES if name in families)


def label(families):
    """The pill's form, with spaces: "Ctrl + Win"."""
    return " + ".join(name for name in FAMILIES if name in families)


def parse(value):
    """"ctrl + WIN" -> frozenset({"Ctrl", "Win"}). Raises ShortcutError if it isn't allowed."""
    if not isinstance(value, str):
        raise ShortcutError("it should be text like \"Ctrl+Win\"")
    families = set()
    for part in value.split("+"):
        name = _BY_LOWER.get(part.strip().lower())
        if name is None:
            if not part.strip():
                raise ShortcutError("it has an empty part (two + signs in a row, or one at an end)")
            raise ShortcutError(f"\"{part.strip()}\" isn't a key it can use: only Ctrl, Alt, Shift and Win")
        if name in families:
            raise ShortcutError(f"{name} is in it twice")
        families.add(name)
    families = frozenset(families)
    if len(families) < 2:
        raise ShortcutError("it needs at least two keys, like Ctrl+Win")
    if families in BLOCKED:
        raise ShortcutError(BLOCKED[families])
    if families not in ALLOWED:  # can't happen: ALLOWED and BLOCKED cover every combination
        raise ShortcutError("it isn't one of the shortcuts Blurt offers")
    return families


def problem(value):
    """The friendly config.json message for a value that isn't allowed, or None if it's fine."""
    try:
        parse(value)
    except ShortcutError as e:
        # Quoted like every other setting's default in config.py's _fix_kinds: using "Ctrl+Win".
        return f"hotkey {json.dumps(value, ensure_ascii=False)} isn't allowed: {e}; using {json.dumps(text(DEFAULT))}."
    return None
