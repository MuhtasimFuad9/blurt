"""The build's safety check (tools/build.py check_bundle): the files the app needs are
in the built folder, and nothing personal (settings, History, logs, recordings, the
API key) ever is. Uses fake build folders in a temporary folder, no PyInstaller."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build  # noqa: E402

FAKE_KEY = "gsk_" + "A1b2C3d4" * 6  # shaped like a key, not a real one


class CheckBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "Blurt"
        for name in build.REQUIRED:
            self.write(name, "fine")
        self.write("_internal/PySide6/Qt6Core.dll", "binary stuff")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_good_build_passes(self):
        self.assertEqual(build.check_bundle(self.folder, modules={build.UIA_MODULE, "app.paths"}), [])

    def test_missing_files_are_reported(self):
        (self.folder / "_internal/prompts/cleanup.txt").unlink()
        self.assertEqual(build.check_bundle(self.folder), ["missing: _internal/prompts/cleanup.txt"])

    def test_missing_ui_automation_wrapper_is_reported(self):
        problems = build.check_bundle(self.folder, modules={"app.paths"})
        self.assertEqual(len(problems), 1)
        self.assertIn(build.UIA_MODULE, problems[0])

    def test_unknown_modules_are_not_a_problem(self):
        self.assertEqual(build.check_bundle(self.folder, modules=None), [])

    def test_personal_files_are_reported(self):
        for name in ("_internal/.env", "_internal/config.json", "_internal/dictionary.txt", "usage.db",
                     "_internal/usage.db-wal", "_internal/data/history.db", "logs/app.log",
                     "_internal/last_recording.wav"):
            self.write(name, "x")
        problems = "\n".join(build.check_bundle(self.folder))
        for name in (".env", "config.json", "dictionary.txt", "usage.db", "usage.db-wal", "history.db",
                     "app.log", "last_recording.wav", "_internal/data/", "logs/"):
            self.assertIn(name, problems)

    def test_the_cleanup_eval_files_are_reported(self):
        # Loose, outside any data/ or tools/ folder, so only their names can catch them
        names = ("cleanup_evals_private.json", "cleanup_eval_usage.json", "prompt_old_0a77b74.txt",
                 "cleanup_evals.json")
        for name in names:
            self.write(f"_internal/{name}", "x")
        problems = build.check_bundle(self.folder)
        self.assertEqual(problems, [f"shouldn't ship: _internal/{name}" for name in sorted(names)])

    def test_the_cleanup_eval_files_are_reported_in_their_folders(self):
        self.write("_internal/data/cleanup_evals_private.json", "x")
        self.write("_internal/tools/cleanup_evals.json", "x")
        problems = "\n".join(build.check_bundle(self.folder))
        for name in ("_internal/data/", "_internal/tools/", "data/cleanup_evals_private.json",
                     "tools/cleanup_evals.json"):
            self.assertIn(name, problems)

    def test_the_real_cleanup_prompt_is_allowed(self):
        self.assertNotIn("prompts/cleanup.txt", "\n".join(build.check_bundle(self.folder)))

    def test_the_examples_are_allowed(self):
        self.assertEqual(build.check_bundle(self.folder), [])  # config.example.json, dictionary.example.txt

    def test_anything_shaped_like_a_key_is_reported(self):
        self.write("_internal/some_library/data.txt", f"token = {FAKE_KEY}\n")
        self.assertEqual(build.check_bundle(self.folder),
                         ["contains an API key: _internal/some_library/data.txt"])

    def test_the_real_key_is_reported_even_without_the_prefix_shape(self):
        self.write("_internal/notes.bin", "abc SECRETVALUE123 def")
        self.assertEqual(build.check_bundle(self.folder, secret="SECRETVALUE123"),
                         ["contains an API key: _internal/notes.bin"])

    def test_the_problem_list_never_contains_the_key(self):
        self.write("_internal/x.txt", FAKE_KEY)
        self.assertNotIn(FAKE_KEY, "\n".join(build.check_bundle(self.folder, secret=FAKE_KEY)))


# Made-up personal words (the real ones live only in personal_words.txt, which Git ignores).
WORDS_FILE = """﻿# made up for the tests

Quokka-Works
zorbleton
source:Wobbleford
  source:  Fernlight  \n"""


class PersonalWordsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.words_file = self.root / "personal_words.txt"
        self.words_file.write_text(WORDS_FILE, encoding="utf-8")
        self.source, self.bundle = build.load_personal_words(self.words_file)
        self.folder = self.root / "Blurt"
        for name in build.REQUIRED:
            self.write(name, "fine")

    def write(self, name, text, root=None):
        path = (root or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def check(self, code=None):
        return build.check_bundle(self.folder, personal=build.words_pattern(self.bundle), code=code)

    def test_the_file_is_read(self):
        self.assertEqual(self.source, ["Quokka-Works", "zorbleton", "Wobbleford", "Fernlight"])
        self.assertEqual(self.bundle, ["Quokka-Works", "zorbleton"])  # source: words stay out of the bundle check

    def test_a_missing_or_empty_file_is_refused(self):
        with self.assertRaises(OSError):
            build.load_personal_words(self.root / "nope.txt")
        self.words_file.write_text("# only a comment\n\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build.load_personal_words(self.words_file)

    def test_the_build_refuses_to_start_without_it(self):
        with mock.patch.object(build, "PERSONAL_WORDS", self.root / "nope.txt"), \
                mock.patch.object(build, "run", side_effect=AssertionError("nothing may run")), \
                mock.patch.object(sys, "argv", ["build.py"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(build.main(), 1)
        self.assertIn("personal_words.txt", out.getvalue())

    def test_a_personal_word_in_a_file_is_reported_any_case(self):
        self.write("_internal/some_library/notes.txt", "made by QUOKKA-works in 2026")
        self.assertEqual(self.check(), ["contains one of your personal words: _internal/some_library/notes.txt"])

    def test_near_misses_are_fine(self):
        # A hyphenated name only matches with its hyphen (Qt's DLLs hold names like that without
        # one), and a source: word is never searched in the bundle (Qt's time zone list has cities).
        self.write("_internal/PySide6/Qt6Core.dll", "XAxis QuokkaWorks Asia/Wobbleford Fernlight")
        self.assertEqual(self.check(), [])

    def test_the_code_inside_blurt_exe_is_checked(self):
        code = {"PYZ.pyz/app.fine": b"docstring", "PYZ.pyz/app.oops": b"e.g. zorbleton, Blender",
                "PYZ.pyz/app.key": FAKE_KEY.encode()}
        self.assertEqual(self.check(code), ["contains an API key: Blurt.exe: PYZ.pyz/app.key",
                                            "contains one of your personal words: Blurt.exe: PYZ.pyz/app.oops"])

    def test_problems_never_name_the_word(self):
        self.write("_internal/x.txt", "zorbleton")
        self.assertNotIn("zorbleton", "\n".join(self.check({"m": b"Quokka-Works"})).lower())

    def test_the_words_file_itself_must_not_ship(self):
        self.write("_internal/personal_words.txt", "x")
        self.assertIn("shouldn't ship: _internal/personal_words.txt", self.check())

    def test_own_code_and_shipped_text_are_checked_with_source_words(self):
        for name in build.OWN_TEXT + ("main.py",):
            self.write(name, "fine", self.root)
        self.write("app/fine.py", '"""Nothing personal."""', self.root)
        self.assertEqual(build.check_own_code(build.words_pattern(self.source), root=self.root), [])
        self.write("app/ui/oops.py", '"""e.g. Wobbleford"""', self.root)
        self.write("installer/blurt.iss", "AppPublisher=Zorbleton", self.root)
        self.assertEqual(build.check_own_code(build.words_pattern(self.source), root=self.root),
                         [f"contains one of your personal words: {Path('app/ui/oops.py')}",
                          f"contains one of your personal words: {Path('installer/blurt.iss')}"])


class OwnCodeTests(unittest.TestCase):
    def test_the_apps_code_has_no_key(self):
        self.assertEqual(build.check_own_code(), [])

    def test_the_apps_code_has_none_of_your_personal_words(self):
        """Reads your real personal_words.txt (never named here). Without it, only the build fails."""
        try:
            source, _ = build.load_personal_words()
        except (OSError, ValueError):
            self.skipTest("personal_words.txt is missing (tools/build.py refuses to build without it)")
        self.assertEqual(build.check_own_code(build.words_pattern(source)), [])


if __name__ == "__main__":
    unittest.main()
