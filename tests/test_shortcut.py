"""Tests for the hold-to-talk shortcut's rules and text (app/shortcut.py).

Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_shortcut -v
"""

import itertools
import unittest

from app import shortcut
from app.shortcut import ALLOWED, BLOCKED, DEFAULT, FAMILIES, ShortcutError, label, parse, problem, text


class ParseTest(unittest.TestCase):
    def test_the_five(self):
        self.assertEqual([text(s) for s in ALLOWED],
                         ["Ctrl+Win", "Alt+Win", "Ctrl+Alt+Win", "Ctrl+Shift+Win", "Alt+Shift+Win"])
        for families in ALLOWED:
            self.assertEqual(parse(text(families)), families)

    def test_case_spaces_and_order_are_flexible(self):
        for value in ("ctrl+win", "CTRL+WIN", " Ctrl + Win ", "Win+Ctrl", "win +ctrl"):
            self.assertEqual(parse(value), DEFAULT, value)
        self.assertEqual(parse("shift + WIN+ alt"), {"Alt", "Shift", "Win"})

    def test_only_the_four_names(self):
        for value in ("Control+Win", "Ctrl+Windows", "Ctl+Win", "Ctrl+Super", "Ctrl+Q", "Ctrl+Space", "LCtrl+Win"):
            with self.assertRaises(ShortcutError, msg=value) as caught:
                parse(value)
            self.assertIn("only Ctrl, Alt, Shift and Win", str(caught.exception))

    def test_mistakes(self):
        cases = {
            "Ctrl+Ctrl+Win": "twice",
            "Ctrl++Win": "empty part",
            "Ctrl+Win+": "empty part",
            "": "empty part",
            "Win": "at least two",
            "ctrl": "at least two",
        }
        for value, words in cases.items():
            with self.assertRaises(ShortcutError, msg=value) as caught:
                parse(value)
            self.assertIn(words, str(caught.exception), value)
        for value in (None, 5, True, ["Ctrl", "Win"]):
            with self.assertRaises(ShortcutError):
                parse(value)

    def test_every_blocked_one_says_why(self):
        expected = {
            "Ctrl+Shift": "text selection",
            "Alt+Shift": "keyboard layouts",
            "Ctrl+Alt": "AltGr",
            "Shift+Win": "screenshot",
            "Ctrl+Alt+Shift": "AltGr+Shift",
            "Ctrl+Alt+Shift+Win": "Office key",
        }
        self.assertEqual(sorted(text(b) for b in BLOCKED), sorted(expected))
        for value, words in expected.items():
            with self.assertRaises(ShortcutError, msg=value) as caught:
                parse(value)
            self.assertIn(words, str(caught.exception), value)

    def test_allowed_and_blocked_cover_every_combination(self):
        combos = {frozenset(c) for n in range(2, 5) for c in itertools.combinations(FAMILIES, n)}
        self.assertEqual(combos, set(ALLOWED) | set(BLOCKED))
        self.assertFalse(set(ALLOWED) & set(BLOCKED))
        for families in ALLOWED:
            self.assertIn("Win", families)  # the hook relies on it: Win is the only key it hides


class TextTest(unittest.TestCase):
    def test_canonical_order(self):
        self.assertEqual(text({"Win", "Shift", "Alt"}), "Alt+Shift+Win")
        self.assertEqual(label({"Win", "Shift", "Ctrl"}), "Ctrl + Shift + Win")
        self.assertEqual(label(DEFAULT), "Ctrl + Win")  # the pill's label, unchanged since M3
        self.assertEqual(text(DEFAULT), "Ctrl+Win")

    def test_problem_message(self):
        self.assertIsNone(problem("ctrl + win"))
        self.assertEqual(problem("Ctrl+Shift"), 'hotkey "Ctrl+Shift" isn\'t allowed: every Ctrl+Shift+Arrow '
                                                'text selection would start it; using "Ctrl+Win".')
        self.assertTrue(problem("Control+Win").endswith('using "Ctrl+Win".'))

    def test_default_is_allowed(self):
        self.assertIs(shortcut.ALLOWED[0], DEFAULT)


if __name__ == "__main__":
    unittest.main()
