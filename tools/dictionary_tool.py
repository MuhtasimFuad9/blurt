"""Looks at and changes dictionary.txt from the console (the same code the Dictionary panel uses).

Safe while the app is running: the file is replaced in one step, and the app picks
up the change on the next dictation. Run it from the project folder:
    .\\.venv\\Scripts\\python.exe tools\\dictionary_tool.py list
    .\\.venv\\Scripts\\python.exe tools\\dictionary_tool.py add "Premiere Pro"
    .\\.venv\\Scripts\\python.exe tools\\dictionary_tool.py remove "Premiere Pro"
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.dictionary import (  # noqa: E402
    WHISPER_TOKEN_LIMIT, add_term, cleanup_tokens, estimate_tokens, load_terms, remove_term,
    whisper_prompt, whisper_terms,
)


def show():
    terms = load_terms()
    if not terms:
        print("The dictionary is empty.")
        return
    sent = whisper_terms(terms)
    for i, term in enumerate(terms):
        note = "" if i >= len(terms) - len(sent) else "   (doesn't fit in Whisper's hint)"
        print(f"  {term}{note}")
    print(f"\n{len(terms)} terms")
    print(f"Whisper hint: about {estimate_tokens(whisper_prompt(terms))} of {WHISPER_TOKEN_LIMIT} tokens")
    print(f"Adds about {cleanup_tokens(terms)} tokens to each cleanup")
    print(f"Whisper is sent: {whisper_prompt(terms)}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "add", "remove") or (sys.argv[1] != "list" and len(sys.argv) < 3):
        print(__doc__)
        return
    command = sys.argv[1]
    if command == "add":
        reason = add_term(sys.argv[2])
        print(f"Not added: {reason}" if reason else f"Added: {sys.argv[2].strip()}")
    elif command == "remove":
        print("Removed." if remove_term(sys.argv[2].strip()) else "Not found (it must match exactly).")
    if command != "list":
        print()
    show()


if __name__ == "__main__":
    main()
