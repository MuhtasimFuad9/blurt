"""Start with Windows (app/autostart.py). Runs against a throwaway registry key,
HKEY_CURRENT_USER\\Software\\Blurt (unit test ...), deleted after every test, so your
real startup list is never touched."""

import os
import unittest
import winreg
from unittest import mock

from app import autostart

TEST_ROOT = rf"Software\Blurt (unit test {os.getpid()})"
EXE = r"C:\Users\someone\AppData\Local\Programs\Blurt\Blurt.exe"
OTHER_EXE = r"D:\Apps\Blurt\Blurt.exe"
ENABLED_MARK = bytes([2]) + bytes(11)
DISABLED_MARK = bytes([3]) + bytes(11)


def delete_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                delete_tree(rf"{path}\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.run_key = rf"{TEST_ROOT}\Run"
        self.approved_key = rf"{TEST_ROOT}\StartupApproved\Run"
        patches = [mock.patch.object(autostart, "RUN_KEY", self.run_key),
                   mock.patch.object(autostart, "APPROVED_KEY", self.approved_key)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(delete_tree, TEST_ROOT)

    def run_value(self):
        return autostart._read(self.run_key, autostart.VALUE_NAME)

    def mark(self, data):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.approved_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, autostart.VALUE_NAME, 0, winreg.REG_BINARY, data)

    def test_off_when_nothing_is_there(self):
        self.assertFalse(autostart.enabled())

    def test_on_writes_the_quoted_path(self):
        autostart.set_enabled(True, exe=EXE)
        self.assertEqual(self.run_value(), f'"{EXE}"')
        self.assertTrue(autostart.enabled())

    def test_off_removes_it(self):
        autostart.set_enabled(True, exe=EXE)
        autostart.set_enabled(False)
        self.assertIsNone(self.run_value())
        self.assertFalse(autostart.enabled())

    def test_off_twice_is_fine(self):
        autostart.set_enabled(False)
        autostart.set_enabled(False)
        self.assertFalse(autostart.enabled())

    def test_disabled_in_task_manager_reads_off(self):
        autostart.set_enabled(True, exe=EXE)
        self.mark(DISABLED_MARK)
        self.assertTrue(autostart.disabled_by_windows())
        self.assertFalse(autostart.enabled())

    def test_an_enabled_mark_reads_on(self):
        autostart.set_enabled(True, exe=EXE)
        self.mark(ENABLED_MARK)
        self.assertTrue(autostart.enabled())

    def test_turning_it_on_clears_the_disabled_mark(self):
        autostart.set_enabled(True, exe=EXE)
        self.mark(DISABLED_MARK)
        autostart.set_enabled(True, exe=EXE)
        self.assertFalse(autostart.disabled_by_windows())
        self.assertTrue(autostart.enabled())

    def test_repair_does_nothing_when_its_off(self):
        self.assertFalse(autostart.repair(exe=EXE))
        self.assertIsNone(self.run_value())

    def test_repair_does_nothing_when_the_path_is_right(self):
        autostart.set_enabled(True, exe=EXE)
        self.assertFalse(autostart.repair(exe=EXE))
        self.assertFalse(autostart.repair(exe=EXE.upper()))  # Windows paths ignore case

    def test_repair_fixes_a_moved_install(self):
        autostart.set_enabled(True, exe=OTHER_EXE)
        self.assertTrue(autostart.repair(exe=EXE))
        self.assertEqual(self.run_value(), f'"{EXE}"')

    def test_repair_never_clears_the_disabled_mark(self):
        autostart.set_enabled(True, exe=OTHER_EXE)
        self.mark(DISABLED_MARK)
        self.assertTrue(autostart.repair(exe=EXE))
        self.assertEqual(self.run_value(), f'"{EXE}"')
        self.assertTrue(autostart.disabled_by_windows())
        self.assertFalse(autostart.enabled())


class AvailableTests(unittest.TestCase):
    def test_only_in_the_installed_app_and_never_in_a_test_run(self):
        for installed, sandbox, expected in ((False, False, False), (True, True, False), (True, False, True)):
            with mock.patch.object(autostart.paths, "INSTALLED", installed), \
                    mock.patch.object(autostart.paths, "SANDBOX", sandbox):
                self.assertEqual(autostart.available(), expected)


if __name__ == "__main__":
    unittest.main()
