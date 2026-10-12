"""Where the app keeps its files. Every other module gets its paths from here.

Two folders:
- APP_DIR: the program's own files (the cleanup prompt, the example config and
  dictionary). Read only once installed.
- USER_DIR: your files (settings, dictionary, .env, History, usage, logs).
  Running from source that's the project folder, as it always was. Installed (a
  frozen exe) it's %LOCALAPPDATA%\\Blurt, which only your Windows account can
  read, and which is writable (Program Files isn't).

The BLURT_HOME environment variable replaces USER_DIR with any folder, and the
saved API key with a separate test one (keystore.py). tools/sandbox.py uses it
to test a first run without touching your real files.

Two names: APP_ID is permanent (the data folder, the saved key's name in
Credential Manager, the single-instance lock, the notification identity).
Changing it after release would leave people's settings and key behind.
APP_DISPLAY_NAME is only what you see, and can change any time.

On first run, config.json and dictionary.txt are copied into USER_DIR from the
shipped examples (ensure_user_files). Existing files are never touched.
"""

import os
import shutil
import sys
from pathlib import Path

APP_ID = "Blurt"  # permanent: never change it once people have installed the app
APP_DISPLAY_NAME = "Blurt"
HOME_VARIABLE = "BLURT_HOME"
INSTALLED = bool(getattr(sys, "frozen", False))  # set by the installer's build (M12)
# Installed, the program's files are in the build's _internal folder (PyInstaller's sys._MEIPASS).
APP_DIR = Path(sys._MEIPASS) if INSTALLED else Path(__file__).resolve().parent.parent


def _home_override(environ=None):
    """The folder BLURT_HOME points to, or None."""
    value = (os.environ if environ is None else environ).get(HOME_VARIABLE, "").strip()
    return Path(value).resolve() if value else None


def _user_dir(environ=None):
    environ = os.environ if environ is None else environ
    override = _home_override(environ)
    if override is not None:
        return override
    if not INSTALLED:
        return APP_DIR
    base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


SANDBOX = _home_override() is not None  # a test run (BLURT_HOME): its own files and its own saved key
USER_DIR = _user_dir()

# Yours
CONFIG_PATH = USER_DIR / "config.json"
DICTIONARY_PATH = USER_DIR / "dictionary.txt"
ENV_PATH = USER_DIR / ".env"
USAGE_DB = USER_DIR / "usage.db"
DATA_DIR = USER_DIR / "data"
HISTORY_DB = DATA_DIR / "history.db"
FAILED_DIR = DATA_DIR / "failed"  # audio of failed dictations, for Retry in History
LAST_DISCARDED = DATA_DIR / "last_discarded.wav"  # only with debug_save_discarded on
OLD_LAST_RECORDING = DATA_DIR / "last_recording.wav"  # no longer written; deleted at startup and on Clear
LOG_PATH = USER_DIR / "logs" / "app.log"  # only when there's no console (log.py)
CRASH_PATH = USER_DIR / "logs" / "crash.log"  # a hard crash's report, copied into app.log next start (log.py)

# The program's
PROMPT_PATH = APP_DIR / "prompts" / "cleanup.txt"
CONFIG_EXAMPLE = APP_DIR / "config.example.json"
DICTIONARY_EXAMPLE = APP_DIR / "dictionary.example.txt"


def ensure_user_files():
    """Makes USER_DIR and copies in the example config.json and dictionary.txt if they're missing.

    Returns the names of the files it created. Never raises: a folder that can't be
    written just means the app runs on the default settings.
    """
    created = []
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"couldn't create {USER_DIR}: {e}")
        return created
    for example, target in ((CONFIG_EXAMPLE, CONFIG_PATH), (DICTIONARY_EXAMPLE, DICTIONARY_PATH)):
        if target.exists() or not example.exists():
            continue
        try:
            shutil.copyfile(example, target)
            created.append(target.name)
        except OSError as e:
            print(f"couldn't create {target}: {e}")
    return created


def remove_old_recording():
    """data/last_recording.wav was written by older versions on every dictation. Delete it."""
    try:
        OLD_LAST_RECORDING.unlink(missing_ok=True)
    except OSError as e:
        print(f"couldn't delete {OLD_LAST_RECORDING}: {e}")
