"""Start with Windows: the value "Blurt" in your startup list in the registry
(HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run), holding the
path of Blurt.exe. Only your Windows account, no admin. It's what Task Manager's
Startup tab and Settings > Apps > Startup show.

The registry is the only place this is kept (not config.json), so the Settings
toggle always shows what Windows will really do:
- Turning it off in Task Manager or Windows Settings doesn't delete our value;
  Windows marks it disabled in ...\\Explorer\\StartupApproved\\Run instead. So
  enabled() is "our value is there and Windows hasn't disabled it".
- The Settings toggle is the only thing that clears that mark (turning it on).
  repair() at startup only fixes the path, and never re-enables what you disabled.

Only the installed app uses it (available()). From source, a startup entry for
python.exe main.py would fight the installed copy for the single-instance lock,
and a test run (BLURT_HOME) must never change your real startup list.

The installer's "Start Blurt when I sign in" box writes the same value on a first
install, and the uninstaller removes it (installer/blurt.iss).
"""

import os
import sys
import winreg

from app import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = paths.APP_ID  # permanent, like APP_ID: the installer and uninstaller use it too


def available():
    """True only in the installed app, and never in a test run."""
    return paths.INSTALLED and not paths.SANDBOX


def command(exe=None):
    """What goes in the startup list: Blurt.exe's full path, quoted (it may contain spaces)."""
    return f'"{exe or sys.executable}"'


def _read(key_path, name):
    """A value from HKEY_CURRENT_USER, or None if it (or its key) doesn't exist."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def _delete(key_path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def _write_run(value):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)


def disabled_by_windows():
    """True if Task Manager or Windows Settings turned our startup entry off. Windows keeps
    a small binary value there: an odd first byte (3, 7) means disabled, even (2, 6) enabled."""
    data = _read(APPROVED_KEY, VALUE_NAME)
    return isinstance(data, bytes) and len(data) > 0 and data[0] & 1 == 1


def enabled():
    """True when Blurt will start at sign-in. Raises OSError if the registry can't be read."""
    return _read(RUN_KEY, VALUE_NAME) is not None and not disabled_by_windows()


def set_enabled(on, exe=None):
    """The Settings toggle. On: write our path and clear a "disabled" mark Windows may have
    set (you asked for it, so it should really start). Off: remove both. Raises OSError."""
    if on:
        _write_run(command(exe))
        _delete(APPROVED_KEY, VALUE_NAME)
    else:
        _delete(RUN_KEY, VALUE_NAME)
        _delete(APPROVED_KEY, VALUE_NAME)


def repair(exe=None):
    """At startup: if our startup entry exists but points to another Blurt.exe (the install
    moved), point it at this one. Only the path: a "disabled" mark stays as it is, and a
    missing entry stays missing. Returns True if it rewrote the path. Raises OSError."""
    current = _read(RUN_KEY, VALUE_NAME)
    wanted = command(exe)
    if current is None or os.path.normcase(str(current).strip()) == os.path.normcase(wanted):
        return False
    _write_run(wanted)
    return True
