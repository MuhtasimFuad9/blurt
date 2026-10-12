"""Looks at and changes config.json from the console (the same code the Settings panel uses).

Safe while the app is running: only that one value changes, the rest of the file keeps
its layout, and the file is replaced in one step. A broken config.json is never written
to. Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\config_tool.py check
    .\\.venv\\Scripts\\python.exe tools\\config_tool.py get history
    .\\.venv\\Scripts\\python.exe tools\\config_tool.py set history false
    .\\.venv\\Scripts\\python.exe tools\\config_tool.py set language auto
    .\\.venv\\Scripts\\python.exe tools\\config_tool.py set pill.idle_opacity 0.3

Values are read as JSON (false, 0.3, null); anything else is taken as text.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigError, config_problem, load_config, set_setting  # noqa: E402


def split_name(name):
    """"pill.idle_opacity" -> ("idle_opacity", "pill"); "history" -> ("history", None)."""
    section, _, key = name.rpartition(".")
    return key, section or None


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text  # PowerShell drops the quotes around text like "auto"


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("check", "get", "set") or (args[0] == "get" and len(args) != 2) \
            or (args[0] == "set" and len(args) != 3):
        print(__doc__)
        return
    problem = config_problem()
    if args[0] == "check":
        print(problem or "config.json is fine.")
        return
    if args[0] == "get":
        if problem:
            print(problem)
            return
        key, section = split_name(args[1])
        config = load_config()
        value = (config.get(section) or {}).get(key) if section else config.get(key)
        print(json.dumps(value, ensure_ascii=False))
        return
    key, section = split_name(args[1])
    value = parse_value(args[2])
    try:
        set_setting(key, value, section=section)
    except ConfigError as e:
        print(f"Not saved. {e}")
        return
    print(f"Saved: {args[1]} = {json.dumps(value, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
