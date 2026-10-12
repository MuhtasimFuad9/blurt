"""The installer script (installer/blurt.iss) uses the same names as the app, so a
rename can't silently break closing Blurt, the Start menu identity, Start with
Windows or deleting the saved key on uninstall."""

import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import main
from app import autostart, keystore, paths

ISS = (Path(__file__).resolve().parent.parent / "installer" / "blurt.iss").read_text(encoding="utf-8")


class InstallerNamesTests(unittest.TestCase):
    def test_the_saved_key_is_deleted_by_its_real_name(self):
        self.assertIn(f"/delete:{keystore.REAL_CREDENTIAL_NAME}", ISS)

    def test_it_closes_blurt_with_the_apps_own_names(self):
        self.assertIn(f"MutexName = '{main.MUTEX_NAME}'", ISS)
        self.assertIn(f"QuitEventName = '{main.QUIT_EVENT_NAME}'", ISS)

    def test_start_with_windows_uses_the_same_value(self):
        self.assertIn(f"RunKey = '{autostart.RUN_KEY}'", ISS)
        self.assertIn(f"ApprovedKey = '{autostart.APPROVED_KEY}'", ISS)
        self.assertIn(f"RunValue = '{autostart.VALUE_NAME}'", ISS)
        self.assertIn(f'Subkey: "{autostart.RUN_KEY}"; ValueType: string; ValueName: "{autostart.VALUE_NAME}"', ISS)
        self.assertIn(f'ValueData: """{{app}}\\{paths.APP_ID}.exe"""', ISS)  # the same quoting as command()

    def test_shortcuts_carry_the_app_identity(self):
        self.assertEqual(ISS.count(f'AppUserModelID: "{paths.APP_ID}"'), 2)

    def test_the_user_folder_it_offers_to_delete_is_the_apps(self):
        self.assertIn(f"ExpandConstant('{{localappdata}}\\{paths.APP_ID}')", ISS)

    def test_the_files_question_has_real_labels_and_keeps_by_default(self):
        self.assertIn("['Keep my files', 'Delete them']", ISS)  # the first button is the default
        self.assertIn("['Keep my files', 'Delete them'], 0) = IDNO then", ISS)  # only "Delete them" deletes
        self.assertIn("if UninstallSilent() or not DirExists(UserFolder) then", ISS)

    def test_restart_manager_cant_kill_blurt_first(self):
        self.assertIn("CloseApplications=no", ISS)

    def test_no_admin(self):
        self.assertIn("PrivilegesRequired=lowest", ISS)


def pascal_block(name):
    """The text of one function or procedure in blurt.iss, from its header to its last "end;"."""
    start = re.search(rf"^(function|procedure) {name}\b", ISS, re.MULTILINE)
    end = re.search(r"^end;", ISS[start.start():], re.MULTILINE)
    return ISS[start.start():start.start() + end.end()]


class ComtypesCacheTests(unittest.TestCase):
    """M17: the uninstaller removes %TEMP%\\comtypes_cache\\Blurt-<digits> and comtypes_cache, but
    only with RemoveDir, which never removes a folder with anything in it. These read blurt.iss as
    text: the Pascal code itself only runs in a real uninstall (checked by hand, M15 step 9)."""

    def test_it_runs_on_every_uninstall(self):
        steps = pascal_block("CurUninstallStepChanged")
        self.assertLess(steps.index("RemoveComtypesCache();"), steps.index("if UninstallSilent()"))

    def test_only_removedir_never_a_file_delete(self):
        code = "".join(pascal_block(name) for name in
                       ("PythonTempDir", "IsComtypesCacheName", "RemoveIfEmpty", "RemoveComtypesCache"))
        self.assertIn("RemoveDir(Dir)", code)
        for forbidden in ("DelTree", "DeleteFile", "{tmp}", "ExpandConstant"):
            self.assertNotIn(forbidden, code)

    def test_pythons_temp_folder_order(self):
        temp = pascal_block("PythonTempDir")
        order = [temp.index(f"GetEnv('{name}')") for name in ("TMPDIR", "TEMP", "TMP")]
        self.assertEqual(order, sorted(order))  # tempfile.gettempdir()'s order
        self.assertIn("for envname in 'TMPDIR', 'TEMP', 'TMP':", Path(tempfile.__file__).read_text(encoding="utf-8"))

    def test_the_folder_name_needs_digits_after_the_prefix(self):
        self.assertIn("ComtypesPrefix = 'Blurt-';", ISS)
        name = pascal_block("IsComtypesCacheName")
        self.assertIn("(Length(Name) > Length(ComtypesPrefix))", name)  # "Blurt-" alone isn't enough
        self.assertIn("if (Name[I] < '0') or (Name[I] > '9') then", name)
        # It matches what comtypes really names the folder: the exe name, "-", the Python version.
        self.assertRegex(f"{paths.APP_ID}-{sys.version_info[0]}{sys.version_info[1]}", r"^Blurt-\d+$")

    def test_folders_only_never_links_and_the_search_is_closed(self):
        cache = pascal_block("RemoveComtypesCache")
        self.assertIn("FILE_ATTRIBUTE_DIRECTORY <> 0", cache)
        self.assertIn("FILE_ATTRIBUTE_REPARSE_POINT = 0", cache)
        self.assertIn("FindClose(FindRec);", cache)
        self.assertIn("RemoveIfEmpty(Cache);", cache)  # comtypes_cache itself, last, if empty


class QuitEventTests(unittest.TestCase):
    def test_the_installers_event_quits_the_app(self):
        name = f"Local\\{paths.APP_ID} (unit test).Quit"
        quit_called = threading.Event()
        with mock.patch.object(main, "QUIT_EVENT_NAME", name):
            self.assertTrue(main.listen_for_installer(quit_called.set))
        handle = main.kernel32.OpenEventW(main.EVENT_MODIFY_STATE, False, name)
        self.assertTrue(handle)
        try:
            main.kernel32.SetEvent(handle)
        finally:
            main.kernel32.CloseHandle(handle)
        self.assertTrue(quit_called.wait(2))


if __name__ == "__main__":
    unittest.main()
