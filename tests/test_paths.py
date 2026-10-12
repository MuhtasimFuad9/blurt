"""Tests for where the app keeps its files (paths.py) and the shipped example files.

Temporary folders only; your real config.json and dictionary.txt are never touched.
Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_paths -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import paths
from app.config import DEFAULTS, load_config, load_config_or_defaults
from app.dictionary import load_terms


class ExampleFilesTests(unittest.TestCase):
    def test_example_config_has_every_setting(self):
        example = json.loads(paths.CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(example), sorted(DEFAULTS))
        for section in ("groq_limits", "pill"):
            self.assertEqual(sorted(example[section]), sorted(DEFAULTS[section]), section)

    def test_example_config_loads_with_debug_off(self):
        config = load_config(paths.CONFIG_EXAMPLE)
        self.assertFalse(config["debug_hotkey"] or config["debug_pill"] or config["debug_paste"])
        self.assertFalse(config["debug_save_discarded"])

    def test_every_default_equals_the_example(self):
        """A first run copies the example; a broken config.json runs on DEFAULTS. They must agree,
        or a broken file quietly changes behavior (M15: it turned the debug lines on)."""
        example = json.loads(paths.CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        for key, default in DEFAULTS.items():
            if isinstance(default, dict):
                for inner, value in default.items():
                    self.assertEqual(example[key][inner], value, f"{key}.{inner}")
            else:
                self.assertEqual(example[key], default, key)

    def test_a_broken_config_runs_with_debug_off(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "config.json"
            broken.write_text('{"history": true,,}\n', encoding="utf-8")
            config, problem = load_config_or_defaults(broken)
        self.assertIsNotNone(problem)
        for key in DEFAULTS:
            if key.startswith("debug_"):
                self.assertFalse(config[key], key)

    def test_example_dictionary_has_no_terms(self):
        self.assertEqual(load_terms(paths.DICTIONARY_EXAMPLE), [])


class UserFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "Blurt"
        for name, value in [
            ("USER_DIR", self.folder),
            ("CONFIG_PATH", self.folder / "config.json"),
            ("DICTIONARY_PATH", self.folder / "dictionary.txt"),
            ("OLD_LAST_RECORDING", self.folder / "data" / "last_recording.wav"),
        ]:
            mock.patch.object(paths, name, value).start()
        self.addCleanup(mock.patch.stopall)

    def test_first_run_copies_the_examples(self):
        self.assertEqual(sorted(paths.ensure_user_files()), ["config.json", "dictionary.txt"])
        self.assertEqual(paths.CONFIG_PATH.read_bytes(), paths.CONFIG_EXAMPLE.read_bytes())

    def test_existing_files_are_never_touched(self):
        self.folder.mkdir(parents=True)
        paths.CONFIG_PATH.write_text("{\"history\": false}\n", encoding="utf-8")
        paths.DICTIONARY_PATH.write_text("Blender\n", encoding="utf-8")
        self.assertEqual(paths.ensure_user_files(), [])
        self.assertEqual(paths.CONFIG_PATH.read_text(encoding="utf-8"), "{\"history\": false}\n")
        self.assertEqual(paths.DICTIONARY_PATH.read_text(encoding="utf-8"), "Blender\n")

    def test_an_unwritable_folder_does_not_crash(self):
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.write_bytes(b"")  # a file where the folder should be
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(paths.ensure_user_files(), [])

    def test_old_last_recording_is_deleted(self):
        paths.OLD_LAST_RECORDING.parent.mkdir(parents=True)
        paths.OLD_LAST_RECORDING.write_bytes(b"audio")
        paths.remove_old_recording()
        self.assertFalse(paths.OLD_LAST_RECORDING.exists())
        paths.remove_old_recording()  # already gone: fine

    def test_from_source_everything_stays_in_the_project_folder(self):
        with mock.patch.object(paths, "INSTALLED", False):
            self.assertEqual(paths._user_dir({}), paths.APP_DIR)

    def test_installed_uses_local_app_data(self):
        with mock.patch.object(paths, "INSTALLED", True):
            environ = {"LOCALAPPDATA": r"C:\Users\someone\AppData\Local"}
            self.assertEqual(paths._user_dir(environ), Path(r"C:\Users\someone\AppData\Local\Blurt"))


class HomeOverrideTests(unittest.TestCase):
    """BLURT_HOME: a test run (tools/sandbox.py) with its own files."""

    def test_it_replaces_the_user_folder(self):
        folder = Path(tempfile.mkdtemp())
        for installed in (False, True):
            with mock.patch.object(paths, "INSTALLED", installed):
                self.assertEqual(paths._user_dir({"BLURT_HOME": str(folder)}), folder.resolve())

    def test_blank_means_not_set(self):
        with mock.patch.object(paths, "INSTALLED", False):
            self.assertEqual(paths._user_dir({"BLURT_HOME": "  "}), paths.APP_DIR)
        self.assertIsNone(paths._home_override({"BLURT_HOME": ""}))

    def test_the_names(self):
        self.assertEqual(paths.APP_ID, "Blurt")
        self.assertEqual(paths.HOME_VARIABLE, "BLURT_HOME")


if __name__ == "__main__":
    unittest.main()
