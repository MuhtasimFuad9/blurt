"""Runs the app as if on a brand-new PC, without touching your real files or key.

It makes an empty folder, points BLURT_HOME at it (see app/paths.py), and starts
main.py. That run gets its own config.json, dictionary.txt, History, usage, logs
and its own saved key ("Blurt (test)/groq_api_key" in Credential Manager). It
never reads your .env: it looks for one in the empty folder.

When the app quits (tray > Quit), the folder and the test key are deleted,
unless you pass --keep (then run it again with --folder to test a second launch).

Quit your real app first: only one copy can run (both would record on Ctrl+Win).
Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\sandbox.py
    .\\.venv\\Scripts\\python.exe tools\\sandbox.py --no-console     (pythonw: tests the log file)
    .\\.venv\\Scripts\\python.exe tools\\sandbox.py --keep
    .\\.venv\\Scripts\\python.exe tools\\sandbox.py --folder <the kept folder> [--keep]
"""

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import keystore, paths  # noqa: E402

PREFIX = "blurt-sandbox-"
SYNCHRONIZE = 0x00100000
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def app_is_running():
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, f"Local\\{paths.APP_ID}.SingleInstance")
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def clean_up(folder):
    shutil.rmtree(folder, ignore_errors=True)
    had_key = keystore.delete_key(keystore.TEST_CREDENTIAL_NAME)
    print(f"deleted {folder}" + (" and the test API key" if had_key else ""))


def main():
    parser = argparse.ArgumentParser(description="Run the app on empty test files.")
    parser.add_argument("--no-console", action="store_true", help="run with pythonw (output goes to the log file)")
    parser.add_argument("--keep", action="store_true", help="keep the folder and the test key afterwards")
    parser.add_argument("--folder", help="reuse a folder kept with --keep")
    args = parser.parse_args()

    if app_is_running():
        print(f"{paths.APP_DISPLAY_NAME} is already running. Quit it first (tray icon > Quit).")
        return 1
    if args.folder:
        folder = Path(args.folder).resolve()
        # The folder is deleted afterwards, so only accept one this tool made.
        if not folder.name.startswith(PREFIX) or not folder.is_dir():
            print(f"--folder must be a folder this tool made (named {PREFIX}...).")
            return 1
    else:
        folder = Path(tempfile.mkdtemp(prefix=PREFIX))

    env = dict(os.environ)
    env[paths.HOME_VARIABLE] = str(folder)
    env.pop("GROQ_API_KEY", None)
    python = ROOT / ".venv" / "Scripts" / ("pythonw.exe" if args.no_console else "python.exe")
    print(f"test folder: {folder}")
    print("starting the app. Quit it from the tray icon when you're done.")
    try:
        process = subprocess.Popen([str(python), str(ROOT / "main.py")], cwd=ROOT, env=env)
        while True:
            try:
                process.wait()
                break
            except KeyboardInterrupt:
                if args.no_console:
                    print("the app has no console: quit it from the tray icon")
    finally:
        log_path = folder / "logs" / "app.log"
        if args.no_console and log_path.exists():
            print(f"--- {log_path} ---")
            print(log_path.read_text(encoding="utf-8", errors="replace").rstrip())
            print("--- end of log ---")
        if args.keep:
            print(f"kept {folder} and the test key. Next time: --folder \"{folder}\"")
        else:
            clean_up(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
