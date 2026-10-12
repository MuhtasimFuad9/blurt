"""Saves GROQ_API_KEY from .env into Windows Credential Manager, where the installed
app looks for it. For development only (never bundled into the installer).

So you can test the built Blurt.exe with your key without copying it through the
clipboard. It uses the same code as the key window (keystore.save_key), and never
prints the key: only "saved" or what went wrong. Run from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\save_key_from_env.py

Afterwards your dev copy uses the saved key too (it's looked up before .env).
Uninstalling Blurt, or keystore.delete_key(), removes it again.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

from app import keystore, paths  # noqa: E402


def main():
    if paths.SANDBOX:
        print(f"error: {paths.HOME_VARIABLE} is set, so this would save the test key. Unset it first.")
        return 1
    try:
        key = keystore.clean_key(dotenv_values(paths.ENV_PATH).get("GROQ_API_KEY"))
    except OSError:
        key = ""
    if not key:
        print(f"error: no GROQ_API_KEY in {paths.ENV_PATH}")
        return 1
    if keystore.is_impossible_key(key):
        print("error: GROQ_API_KEY in .env can't be an API key (too long, or odd characters)")
        return 1
    if not keystore.looks_like_groq_key(key):
        print(f'error: GROQ_API_KEY in .env doesn\'t start with "{keystore.KEY_PREFIX}"')
        return 1
    try:
        keystore.save_key(key)
    except keystore.KeyStoreError as e:
        print(f"error: {e}")
        return 1
    print("saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
