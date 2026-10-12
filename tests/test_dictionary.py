"""Tests for the dictionary: adding and removing terms, Whisper's prompt limit, the
echo guard after changes, and the clipboard privacy rule for the Dictionary panel.

Temporary folders only; the real dictionary.txt and the real clipboard are never
touched. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_dictionary -v
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from app import dictionary
from app.dictionary import (
    MAX_TERM_LENGTH, WHISPER_PROMPT_BUDGET, add_term, check_new_term, cleanup_tokens,
    estimate_tokens, load_terms, remove_term, whisper_prompt, whisper_terms,
)
from app.paster import is_hidden
from app.transcribe import is_prompt_echo

SEED = (
    "# Words the app should always spell exactly like this. One per line.\n"
    "# Lines starting with # are ignored. Changes apply on the next dictation.\n"
    "Acme Creations\n"
    "Blender\n"
    "Groq\n"
    "\n"
    "# tools\n"
    "n8n\n"
    "Claude Code\n"
    "Claude\n"
    "WisprFlow\n"
)
SEED_TERMS = ["Acme Creations", "Blender", "Groq", "n8n", "Claude Code", "Claude", "WisprFlow"]


class DictionaryFileTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.path = folder / "dictionary.txt"
        self.path.write_text(SEED, encoding="utf-8", newline="")

    def text(self):
        return self.path.read_text(encoding="utf-8")

    def test_reads_terms_in_order(self):
        self.assertEqual(load_terms(self.path), SEED_TERMS)

    def test_add_goes_at_the_end_and_keeps_everything_else(self):
        self.assertIsNone(add_term("  Premiere   Pro ", self.path))
        self.assertEqual(self.text(), SEED + "Premiere Pro\n")
        self.assertEqual(load_terms(self.path), SEED_TERMS + ["Premiere Pro"])

    def test_add_to_a_file_without_a_final_newline(self):
        self.path.write_text("Blender", encoding="utf-8")
        add_term("Groq", self.path)
        self.assertEqual(load_terms(self.path), ["Blender", "Groq"])

    def test_add_keeps_windows_line_endings(self):
        self.path.write_bytes(SEED.replace("\n", "\r\n").encode("utf-8"))
        add_term("Premiere Pro", self.path)
        self.assertTrue(self.path.read_bytes().endswith(b"WisprFlow\r\nPremiere Pro\r\n"))

    def test_add_creates_a_missing_file(self):
        self.path.unlink()
        self.assertIsNone(add_term("Groq", self.path))
        self.assertEqual(load_terms(self.path), ["Groq"])

    def test_remove_deletes_only_that_line(self):
        self.assertTrue(remove_term("Groq", self.path))
        self.assertEqual(self.text(), SEED.replace("Groq\n", ""))
        self.assertEqual(load_terms(self.path), [t for t in SEED_TERMS if t != "Groq"])

    def test_remove_matches_whole_terms_only(self):
        # "Claude" must not take out "Claude Code", and a comment is never a term.
        self.assertTrue(remove_term("Claude", self.path))
        self.assertIn("Claude Code", load_terms(self.path))
        self.assertNotIn("Claude", load_terms(self.path))
        self.assertFalse(remove_term("# tools", self.path))
        self.assertFalse(remove_term("Missing", self.path))
        self.assertIn("# tools", self.text())

    def test_duplicates_are_refused_ignoring_case(self):
        self.assertEqual(add_term("groq", self.path), "Already in the dictionary")
        self.assertEqual(add_term(" CLAUDE code ", self.path), "Already in the dictionary")
        self.assertEqual(self.text(), SEED)

    def test_bad_terms_are_refused(self):
        refused = ["", "   ", "Groq\nBlender", "a" * (MAX_TERM_LENGTH + 1), "# comment", "Groq, Blender", "Gr\toq"]
        for text in refused:
            with self.subTest(text=text):
                self.assertIsNotNone(check_new_term(text, SEED_TERMS))
                self.assertIsNotNone(add_term(text, self.path))
        self.assertEqual(self.text(), SEED)
        self.assertIsNone(check_new_term("a" * MAX_TERM_LENGTH, SEED_TERMS))
        self.assertIsNone(check_new_term("Groq\n", SEED_TERMS[:2]))  # a copied line's own newline is fine

    def test_byte_order_mark_is_not_part_of_the_first_term(self):
        self.path.write_bytes(b"\xef\xbb\xbfBlender\nGroq\n")
        self.assertEqual(load_terms(self.path), ["Blender", "Groq"])
        self.assertEqual(add_term("Blender", self.path), "Already in the dictionary")

    def test_an_edit_made_in_notepad_is_not_lost(self):
        load_terms(self.path)  # cached
        self.path.write_text(SEED + "Edited In Notepad\n", encoding="utf-8")  # someone saves in Notepad
        add_term("Premiere Pro", self.path)
        self.assertEqual(load_terms(self.path)[-2:], ["Edited In Notepad", "Premiere Pro"])

    def test_changes_apply_right_away(self):
        self.assertEqual(load_terms(self.path), SEED_TERMS)
        # A same-second edit that keeps the modification time still shows up (the size changed).
        stat = self.path.stat()
        self.path.write_text(SEED + "Resolve\n", encoding="utf-8")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(load_terms(self.path)[-1], "Resolve")
        add_term("Premiere Pro", self.path)
        self.assertEqual(load_terms(self.path)[-1], "Premiere Pro")

    def test_no_temporary_file_left_behind(self):
        add_term("Premiere Pro", self.path)
        remove_term("Premiere Pro", self.path)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["dictionary.txt"])


class EchoGuardAfterChangesTests(unittest.TestCase):
    """Whisper echoes its hint in file order; the guard must still catch that after edits."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.path = folder / "dictionary.txt"
        self.path.write_text(SEED, encoding="utf-8")

    def echo_of_hint(self, terms):
        return whisper_prompt(terms)  # exactly what Whisper was sent

    def test_echo_caught_after_add(self):
        add_term("Premiere Pro", self.path)
        terms = load_terms(self.path)
        self.assertTrue(is_prompt_echo(self.echo_of_hint(terms), terms))
        self.assertTrue(is_prompt_echo("I, Claude, WisprFlow, Premiere Pro", terms))  # the in-order tail

    def test_echo_caught_after_remove(self):
        remove_term("Groq", self.path)
        terms = load_terms(self.path)
        self.assertTrue(is_prompt_echo(self.echo_of_hint(terms), terms))
        self.assertTrue(is_prompt_echo("I, Blender, n8n, Claude", terms))

    def test_real_sentence_with_the_new_term_still_passes(self):
        add_term("Premiere Pro", self.path)
        terms = load_terms(self.path)
        self.assertFalse(is_prompt_echo("I cut the Blender renders in Premiere Pro every day", terms))


class WhisperLimitTests(unittest.TestCase):
    def many_terms(self, count):
        return [f"Product Name {i:03d}" for i in range(count)]

    def test_small_dictionary_is_sent_whole(self):
        self.assertEqual(whisper_terms(SEED_TERMS), SEED_TERMS)
        self.assertEqual(whisper_prompt(SEED_TERMS), ", ".join(SEED_TERMS))

    def test_seed_dictionary_is_far_from_the_limit(self):
        self.assertLess(estimate_tokens(whisper_prompt(SEED_TERMS)), WHISPER_PROMPT_BUDGET / 4)

    def test_big_dictionary_drops_the_oldest_terms_only(self):
        terms = self.many_terms(80)
        kept = whisper_terms(terms)
        self.assertLess(len(kept), len(terms))
        self.assertEqual(kept, terms[len(terms) - len(kept):])  # the newest ones, same order
        self.assertLessEqual(estimate_tokens(", ".join(kept)), WHISPER_PROMPT_BUDGET)

    def test_echo_of_a_trimmed_hint_is_still_caught(self):
        terms = self.many_terms(80)
        self.assertTrue(is_prompt_echo(whisper_prompt(terms), terms))

    def test_estimate_errs_high(self):
        # "Acme Creations, Blender, Groq" is about 10 real Whisper tokens.
        self.assertGreaterEqual(estimate_tokens("Acme Creations, Blender, Groq"), 10)

    def test_cleanup_cost(self):
        self.assertEqual(cleanup_tokens([]), 0)
        self.assertGreater(cleanup_tokens(SEED_TERMS + ["Premiere Pro"]), cleanup_tokens(SEED_TERMS))


class ClipboardPrivacyTests(unittest.TestCase):
    """is_hidden decides whether the Dictionary panel may show the clipboard text."""

    ZERO = (0).to_bytes(4, "little")
    ONE = (1).to_bytes(4, "little")

    def test_no_markers_shows_the_text(self):
        self.assertFalse(is_hidden({}))

    def test_exclude_from_monitoring_hides_it_whatever_its_value(self):
        for value in (self.ZERO, self.ONE, b"", None):
            with self.subTest(value=value):
                self.assertTrue(is_hidden({"ExcludeClipboardContentFromMonitorProcessing": value}))

    def test_history_or_cloud_set_to_0_hides_it(self):
        self.assertTrue(is_hidden({"CanIncludeInClipboardHistory": self.ZERO}))
        self.assertTrue(is_hidden({"CanUploadToCloudClipboard": self.ZERO}))

    def test_history_and_cloud_set_to_1_show_it(self):
        self.assertFalse(is_hidden({"CanIncludeInClipboardHistory": self.ONE, "CanUploadToCloudClipboard": self.ONE}))

    def test_one_marker_at_0_is_enough(self):
        self.assertTrue(is_hidden({"CanIncludeInClipboardHistory": self.ONE, "CanUploadToCloudClipboard": self.ZERO}))

    def test_unreadable_marker_counts_as_hidden(self):
        self.assertTrue(is_hidden({"CanIncludeInClipboardHistory": None}))
        self.assertTrue(is_hidden({"CanUploadToCloudClipboard": b"\x01"}))

    def test_our_own_paste_markers_count_as_hidden(self):
        # paster._mark_private puts all of these on pasted dictations, each with 0.
        ours = {name: self.ZERO for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard",
                                             "ExcludeClipboardContentFromMonitorProcessing")}
        self.assertTrue(is_hidden(ours))


if __name__ == "__main__":
    unittest.main()
