"""Tests for reading and writing config.json (app/config.py), and the settings that now apply right away.

Temporary folders only; the real config.json is never touched. Run from the project folder:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_config -v
"""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import config as config_module
from app.config import (
    DEFAULTS, ConfigError, ConfigWatcher, clear_debug_setting, config_problem, load_config, load_config_or_defaults,
    set_setting, wrong_kinds,
)
from app.pipeline import Pipeline
from app.recorder import Recorder
from tests.test_pause import fake_open_stream

LAYOUT = """{
  "keep_mic_open": true,
  "language": "en",
  "paste_always_apps": ["WindowsTerminal.exe"],
  "history": true,
  "pill": {
    "scale": 1.0,
    "idle_opacity": 0.22,
    "resize_easing": [0.2, 0.8, 0.2, 1.0],
    "mic_label": true
  }
}
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "config.json"

    def write(self, text, bom=False):
        self.path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))

    def read(self):
        return self.path.read_bytes().decode("utf-8")


class SetSettingTest(ConfigTestCase):
    def test_only_that_value_changes(self):
        self.write(LAYOUT)
        set_setting("history", False, path=self.path)
        self.assertEqual(self.read(), LAYOUT.replace('"history": true', '"history": false'))

    def test_a_value_in_the_pill_section(self):
        self.write(LAYOUT)
        set_setting("idle_opacity", 0.35, section="pill", path=self.path)
        self.assertEqual(self.read(), LAYOUT.replace('"idle_opacity": 0.22', '"idle_opacity": 0.35'))

    def test_a_top_level_name_inside_a_section_is_left_alone(self):
        self.write('{\n  "pill": {"mic_label": true},\n  "mic_label": 5\n}\n')
        set_setting("mic_label", False, section="pill", path=self.path)
        self.assertEqual(self.read(), '{\n  "pill": {"mic_label": false},\n  "mic_label": 5\n}\n')

    def test_text_values_and_strings_with_braces(self):
        self.write('{\n  "note": "a { tricky, \\"quoted\\" : string }",\n  "language": "en"\n}\n')
        set_setting("language", "auto", path=self.path)
        self.assertEqual(self.read(), '{\n  "note": "a { tricky, \\"quoted\\" : string }",\n  "language": "auto"\n}\n')

    def test_a_missing_setting_is_added_in_the_same_indent(self):
        self.write('{\n    "language": "en",\n    "history": true\n}\n')
        set_setting("keep_mic_open", False, path=self.path)
        self.assertEqual(self.read(), '{\n    "language": "en",\n    "history": true,\n    "keep_mic_open": false\n}\n')

    def test_a_missing_setting_in_a_one_line_section(self):
        self.write('{\n  "pill": {"scale": 1.0}\n}\n')
        set_setting("idle_opacity", 0.5, section="pill", path=self.path)
        self.assertEqual(self.read(), '{\n  "pill": {"scale": 1.0, "idle_opacity": 0.5}\n}\n')

    def test_a_missing_section_is_added(self):
        self.write('{\n  "language": "en"\n}\n')
        set_setting("idle_opacity", 0.5, section="pill", path=self.path)
        self.assertEqual(self.read(), '{\n  "language": "en",\n  "pill": {"idle_opacity": 0.5}\n}\n')

    def test_an_empty_file_object(self):
        self.write("{}\n")
        set_setting("history", False, path=self.path)
        self.assertEqual(load_config(self.path)["history"], False)

    def test_no_file_yet(self):
        set_setting("history", False, path=self.path)
        self.assertEqual(load_config(self.path)["history"], False)

    def test_windows_line_endings_are_kept(self):
        self.write(LAYOUT.replace("\n", "\r\n"))
        set_setting("keep_awake", True, path=self.path)
        text = self.read()
        self.assertNotIn("\n", text.replace("\r\n", ""))  # no lone \n added
        self.assertTrue(load_config(self.path)["keep_awake"])

    def test_a_byte_order_mark_is_kept(self):
        self.write(LAYOUT, bom=True)
        set_setting("history", False, path=self.path)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw[3:].decode("utf-8"), LAYOUT.replace('"history": true', '"history": false'))

    def test_no_temp_file_is_left_behind(self):
        self.write(LAYOUT)
        set_setting("history", False, path=self.path)
        self.assertEqual(os.listdir(self.folder), ["config.json"])

    def test_an_edit_saved_just_before_is_kept(self):
        self.write(LAYOUT)
        # You change the language in Notepad and save; then Settings changes history.
        self.write(LAYOUT.replace('"language": "en"', '"language": "auto"'))
        set_setting("history", False, path=self.path)
        config = load_config(self.path)
        self.assertEqual((config["language"], config["history"]), ("auto", False))

    def test_a_save_while_we_work_is_redone_on_the_new_version(self):
        self.write(LAYOUT)
        real_read = config_module._read
        calls = []

        def notepad_saves_meanwhile(path):
            result = real_read(path)
            if not calls:
                calls.append(1)
                time.sleep(0.02)  # a different modification time
                self.write(LAYOUT.replace('"language": "en"', '"language": "auto"') + " ")
            return result

        with mock.patch.object(config_module, "_read", notepad_saves_meanwhile):
            set_setting("history", False, path=self.path)
        config = load_config(self.path)
        self.assertEqual((config["language"], config["history"]), ("auto", False))

    def test_a_broken_file_is_never_written(self):
        broken = LAYOUT.replace('"en",', '"en"')
        self.write(broken)
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError) as caught:
            set_setting("history", False, path=self.path)
        self.assertIn("missing comma", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.folder), ["config.json"])

    def test_a_busy_file_is_retried_once(self):
        self.write(LAYOUT)
        real_replace = os.replace
        calls = []

        def busy_once(src, dst):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(13, "The process cannot access the file")
            real_replace(src, dst)

        with mock.patch.object(config_module.os, "replace", busy_once), \
                mock.patch.object(config_module.time, "sleep") as sleep:
            set_setting("history", False, path=self.path)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(config_module.REPLACE_RETRY_SECONDS)
        self.assertFalse(load_config(self.path)["history"])

    def test_a_file_that_stays_busy_is_refused_cleanly(self):
        self.write(LAYOUT)
        denied = PermissionError(13, "The process cannot access the file")
        with mock.patch.object(config_module.os, "replace", side_effect=denied), \
                mock.patch.object(config_module.time, "sleep"):
            with self.assertRaises(ConfigError) as caught:
                set_setting("history", False, path=self.path)
        self.assertIn("busy", str(caught.exception))
        self.assertEqual(self.read(), LAYOUT)
        self.assertEqual(os.listdir(self.folder), ["config.json"])  # temp file cleaned up

    def test_clear_debug_setting_uses_the_same_writer(self):
        self.write('{\n  "debug_fake_rate_limit": "cleanup",\n  "debug_other": "keep"\n}\n')
        clear_debug_setting("debug_fake_rate_limit", self.path)
        self.assertEqual(self.read(), '{\n  "debug_fake_rate_limit": null,\n  "debug_other": "keep"\n}\n')
        before = self.read()
        clear_debug_setting("debug_fake_rate_limit", self.path)  # already null: nothing written
        self.assertEqual(self.read(), before)


class FriendlyMessageTest(ConfigTestCase):
    def problem(self, text):
        self.write(text)
        return config_problem(self.path)

    def test_missing_comma_names_both_lines(self):
        message = self.problem('{\n  "language": "en"\n  "history": true\n}\n')
        self.assertEqual(message, "config.json has a mistake on line 3 (or the end of line 2): probably a missing comma.")

    def test_extra_comma(self):
        message = self.problem('{\n  "language": "en",\n}\n')
        self.assertIn("line 2", message)
        self.assertIn("extra comma", message)

    def test_single_quotes(self):
        self.assertIn("single quotes", self.problem('{\n  "language": \'en\'\n}\n'))
        self.assertIn("without double quotes", self.problem("{\n  'language': \"en\"\n}\n"))

    def test_missing_closing_quote(self):
        message = self.problem('{\n  "language": "en,\n  "history": true\n}\n')
        self.assertIn("line 2", message)
        self.assertIn("closing quote", message)

    def test_missing_colon_and_extra_text(self):
        self.assertIn("missing colon", self.problem('{\n  "language" "en"\n}\n'))
        self.assertIn("after the last }", self.problem('{\n  "language": "en"\n}\n}\n'))

    def test_not_an_object(self):
        self.assertEqual(self.problem("[1, 2]"), "config.json should start with { and end with }.")

    def test_a_fine_file(self):
        self.assertIsNone(self.problem(LAYOUT))

    def test_the_error_carries_the_line(self):
        self.write('{\n  "language": "en"\n  "history": true\n}\n')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path)
        self.assertEqual(caught.exception.line, 3)
        self.assertIsInstance(caught.exception, ValueError)  # old "except ValueError" callers still catch it


class StartupTest(ConfigTestCase):
    def test_a_broken_file_gives_the_defaults_and_the_problem(self):
        self.write('{\n  "language": "auto"\n  "history": false\n}\n')
        config, problem = load_config_or_defaults(self.path)
        self.assertEqual(config["language"], DEFAULTS["language"])
        self.assertEqual(config["pill"], DEFAULTS["pill"])
        self.assertIn("line 3", problem)

    def test_a_fine_file_gives_its_settings(self):
        self.write('{\n  "language": "auto",\n  "pill": {"idle_opacity": 0.5}\n}\n')
        config, problem = load_config_or_defaults(self.path)
        self.assertIsNone(problem)
        self.assertEqual((config["language"], config["pill"]["idle_opacity"]), ("auto", 0.5))
        self.assertEqual(config["pill"]["scale"], DEFAULTS["pill"]["scale"])  # merged key by key

    def test_no_file_gives_the_defaults_without_a_problem(self):
        config, problem = load_config_or_defaults(self.path)
        self.assertIsNone(problem)
        self.assertEqual(config["history"], DEFAULTS["history"])


class WrongKindTest(ConfigTestCase):
    def test_a_wrong_kind_gets_its_default_and_a_message(self):
        self.write('{"post_roll_ms": "250", "history": "yes", "groq_limits": 5, "paste_never_apps": "blender.exe",'
                   ' "language": "auto"}')
        config = load_config(self.path)
        self.assertEqual(config["post_roll_ms"], DEFAULTS["post_roll_ms"])
        self.assertIs(config["history"], True)
        self.assertEqual(config["groq_limits"], DEFAULTS["groq_limits"])
        self.assertEqual(config["paste_never_apps"], DEFAULTS["paste_never_apps"])
        self.assertEqual(config["language"], "auto")  # the right ones are kept
        problems = wrong_kinds(self.path)
        self.assertEqual(len(problems), 4)
        self.assertIn("post_roll_ms should be a number, not text; using 250.", problems)
        self.assertIn("groq_limits should be a section, not a number; using the defaults.", problems)

    def test_true_is_not_a_number(self):
        self.write('{"clipboard_restore_ms": true, "silence_level": 1}')
        config = load_config(self.path)
        self.assertEqual(config["clipboard_restore_ms"], DEFAULTS["clipboard_restore_ms"])
        self.assertEqual(config["silence_level"], 1)  # a whole number where a decimal is expected is fine

    def test_null_settings_take_their_kind_or_null(self):
        self.write('{"dictionary_only_min_loudest": 0.02, "debug_fake_rate_limit": null,'
                   ' "debug_fake_limit_warning": 7}')
        config = load_config(self.path)
        self.assertEqual(config["dictionary_only_min_loudest"], 0.02)
        self.assertIsNone(config["debug_fake_rate_limit"])
        self.assertIsNone(config["debug_fake_limit_warning"])
        self.assertEqual(len(wrong_kinds(self.path)), 1)

    def test_a_fine_or_broken_file_has_no_wrong_kinds(self):
        self.write(LAYOUT)
        self.assertEqual(wrong_kinds(self.path), [])
        self.write('{"history": }')
        self.assertEqual(wrong_kinds(self.path), [])  # that's config_problem's job

    def test_the_real_example_and_defaults_are_fine(self):
        from app.paths import CONFIG_EXAMPLE
        self.assertEqual(wrong_kinds(CONFIG_EXAMPLE), [])


class HotkeyValueTest(ConfigTestCase):
    """The "hotkey" setting (M14): one of the five, else Ctrl+Win and a message."""

    def test_default(self):
        self.assertEqual(DEFAULTS["hotkey"], "Ctrl+Win")
        self.write(LAYOUT)  # no hotkey in the file
        self.assertEqual(load_config(self.path)["hotkey"], "Ctrl+Win")

    def test_an_allowed_one_is_written_the_canonical_way(self):
        self.write('{"hotkey": " win + SHIFT+alt "}')
        self.assertEqual(load_config(self.path)["hotkey"], "Alt+Shift+Win")
        self.assertEqual(wrong_kinds(self.path), [])

    def test_a_blocked_or_unknown_one_gives_ctrl_win_and_a_message(self):
        cases = {"Ctrl+Shift": "text selection", "Shift+Win": "screenshot", "Control+Win": "only Ctrl, Alt, Shift",
                 "Win": "at least two", "Ctrl+Space": "only Ctrl, Alt, Shift"}
        for value, words in cases.items():
            self.write('{"hotkey": "%s"}' % value)
            self.assertEqual(load_config(self.path)["hotkey"], "Ctrl+Win", value)
            problems = wrong_kinds(self.path)
            self.assertEqual(len(problems), 1, value)
            self.assertTrue(problems[0].startswith(f'hotkey "{value}" isn\'t allowed:'), problems[0])
            self.assertIn(words, problems[0])
            self.assertTrue(problems[0].endswith('using "Ctrl+Win".'), problems[0])

    def test_not_text(self):
        self.write('{"hotkey": 5}')
        self.assertEqual(load_config(self.path)["hotkey"], "Ctrl+Win")
        self.assertEqual(wrong_kinds(self.path), ['hotkey should be text, not a number; using "Ctrl+Win".'])

    def test_a_broken_file_still_runs_on_ctrl_win(self):
        self.write('{"hotkey": "Alt+Win",}')
        config, problem = load_config_or_defaults(self.path)
        self.assertIsNotNone(problem)
        self.assertEqual(config["hotkey"], "Ctrl+Win")

    def test_settings_writes_one_value(self):
        self.write(LAYOUT)
        set_setting("hotkey", "Alt+Win", path=self.path)
        self.assertEqual(load_config(self.path)["hotkey"], "Alt+Win")
        self.assertEqual(self.read().count('"hotkey": "Alt+Win"'), 1)

    def test_the_watcher_reports_a_bad_value_on_save(self):
        self.write(LAYOUT)
        watcher = ConfigWatcher(self.path)
        set_setting("hotkey", "Ctrl+Shift", path=self.path)
        config = watcher.changed()
        self.assertEqual(config["hotkey"], "Ctrl+Win")
        self.assertEqual(len(watcher.problems), 1)
        set_setting("hotkey", "Alt+Win", path=self.path)
        self.assertEqual(watcher.changed()["hotkey"], "Alt+Win")
        self.assertEqual(watcher.problems, [])


class WatcherTest(ConfigTestCase):
    def test_reports_each_save_once(self):
        self.write(LAYOUT)
        watcher = ConfigWatcher(self.path)
        self.assertIsNone(watcher.changed())
        set_setting("keep_mic_open", False, path=self.path)
        self.assertFalse(watcher.changed()["keep_mic_open"])
        self.assertIsNone(watcher.changed())

    def test_a_broken_save_is_ignored(self):
        self.write(LAYOUT)
        watcher = ConfigWatcher(self.path)
        self.write(LAYOUT.replace('"en",', '"en"') + " ")
        self.assertIsNone(watcher.changed())


class FreshLanguageTest(unittest.TestCase):
    def test_language_is_read_on_every_dictation(self):
        pipeline = Pipeline(client=None, hotkey=None, config={"language": "en", "history": True}, tracker=None)
        with mock.patch("app.pipeline.load_config", return_value={"language": "auto"}):
            self.assertEqual(pipeline._fresh("language"), "auto")

    def test_a_broken_file_falls_back_to_the_startup_setting(self):
        pipeline = Pipeline(client=None, hotkey=None, config={"language": "en", "history": True}, tracker=None)
        with mock.patch("app.pipeline.load_config", side_effect=ConfigError("broken")):
            self.assertEqual(pipeline._fresh("language"), "en")


class KeepOpenTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch("app.recorder.device_fingerprint", lambda: ("mic A",)),
            mock.patch.object(Recorder, "_open_stream", fake_open_stream),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_off_closes_the_mic_and_on_reopens_it(self):
        recorder = Recorder(keep_open=True)
        recorder.open()
        self.assertTrue(recorder.is_open())
        recorder.set_keep_open(False)
        self.assertFalse(recorder.is_open())
        recorder.set_keep_open(True)
        self.assertTrue(recorder.is_open())

    def test_a_recording_keeps_its_mic_until_it_ends(self):
        recorder = Recorder(keep_open=True)
        recorder.open()
        recorder.start()
        recorder.set_keep_open(False)
        self.assertTrue(recorder.is_open())
        recorder.stop()
        self.assertFalse(recorder.is_open())

    def test_on_while_paused_stays_closed(self):
        recorder = Recorder(keep_open=False)
        with contextlib.redirect_stdout(io.StringIO()):
            recorder.set_paused(True)
        recorder.set_keep_open(True)
        self.assertFalse(recorder.is_open())
        recorder.set_paused(False)
        self.assertTrue(recorder.is_open())


if __name__ == "__main__":
    unittest.main()
