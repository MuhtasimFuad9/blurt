"""Builds Blurt: the app folder (dist\\Blurt\\Blurt.exe) and the installer. Run from
the project folder:
    .\\.venv\\Scripts\\python.exe tools\\build.py                  (everything)
    .\\.venv\\Scripts\\python.exe tools\\build.py --no-installer   (only dist\\Blurt)
    .\\.venv\\Scripts\\python.exe tools\\build.py --skip-tests     (don't run the tests first)

Steps, each stops the build if it fails:
0. personal_words.txt (next to .env, in .gitignore) must exist: the words that identify
   you, one per line. Without it the build refuses to start.
1. The whole test suite.
2. The app's own code is checked for anything shaped like a Groq key, and for your
   personal words.
3. comtypes' UI Automation wrapper is generated in the venv (smart paste needs it,
   and the installed app can't generate it by itself).
4. PyInstaller builds dist\\Blurt from installer\\blurt.spec: one folder, no console.
5. The output is checked (check_bundle): the files the app needs are there, and
   nothing of yours is (.env, config.json, dictionary, History, usage, logs,
   recordings, your API key, your personal words), also inside the Python code
   packed into Blurt.exe.
6. Inno Setup turns dist\\Blurt into dist\\Blurt-Setup-<version>.exe.

build\\ (PyInstaller's work folder) and dist\\ are in .gitignore.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.version import VERSION  # noqa: E402

PYTHON = sys.executable
SPEC = ROOT / "installer" / "blurt.spec"
ISS = ROOT / "installer" / "blurt.iss"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP_FOLDER = DIST / "Blurt"
UIA_MODULE = "comtypes.gen.UIAutomationClient"

KEY_PATTERN = re.compile(rb"gsk_[A-Za-z0-9]{20,}")
# Inside dist\Blurt (paths relative to it, with forward slashes)
REQUIRED = (
    "Blurt.exe",
    "_internal/prompts/cleanup.txt",
    "_internal/config.example.json",
    "_internal/dictionary.example.txt",
)
FORBIDDEN_NAMES = {".env", "config.json", "dictionary.txt", "history.db", "app.log", "app.log.1",
                   "crash.log", "personal_words.txt",
                   # the cleanup eval (M13): real dictations, the token ledger, the test cases
                   "cleanup_evals_private.json", "cleanup_eval_usage.json", "cleanup_evals.json"}
FORBIDDEN_FOLDERS = {"data", "logs", "tests", "tools", "backups", ".venv"}
FORBIDDEN_SUFFIXES = (".wav", ".log")
FORBIDDEN_PREFIXES = ("usage.db", ".env", "prompt_old_")  # prompt_old_: saved old cleanup prompts (M13)
# The app's own code that gets bundled (it's inside Blurt.exe compressed, so it's checked here instead)
OWN_CODE = ("main.py", "app")
# The app's own text that ships: the bundled data files, and the installer script (its text is in the Setup exe)
OWN_TEXT = ("prompts/cleanup.txt", "config.example.json", "dictionary.example.txt", "installer/blurt.iss")

# Your personal words live in a file Git ignores, so this code (which will be public) never names
# them. The built app is searched byte by byte, third-party files included, and Qt's DLLs hold
# enum names and a time zone list with city names. Two rules keep that from giving false alarms:
# a hyphenated name is written with its hyphen (Qt has one such name without it), and a word a
# library really contains is marked "source:", so only Blurt's own files are checked for it.
PERSONAL_WORDS = ROOT / "personal_words.txt"
SOURCE_ONLY = "source:"


def load_personal_words(path=None):
    """(words for Blurt's own files, words for the whole built app) from personal_words.txt.

    Raises OSError when the file is missing or unreadable, ValueError when it has no words:
    the build must never quietly skip this check."""
    path = Path(path or PERSONAL_WORDS)
    source, bundle = [], []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        if word.lower().startswith(SOURCE_ONLY):
            word = word[len(SOURCE_ONLY):].strip()
            if word:
                source.append(word)
            continue
        source.append(word)
        bundle.append(word)
    if not source:
        raise ValueError(f"{path.name} has no words in it")
    return source, bundle


def words_pattern(words):
    """A bytes pattern matching any of the words, ignoring case; None for no words."""
    if not words:
        return None
    return re.compile(b"|".join(re.escape(word.encode("utf-8")) for word in words), re.IGNORECASE)


def check_bundle(folder, secret=None, modules=None, personal=None, code=None):
    """Problems with a built app folder, as a list of sentences (empty = fine).

    secret: your real API key, if known; no file may contain it.
    modules: the module names bundled inside Blurt.exe, if known; the UI Automation
    wrapper must be one of them.
    personal: words_pattern() of your personal words; no file may contain one.
    code: {name: bytes} of everything packed inside Blurt.exe, unpacked (bundled_code), if
    known. Checked for keys and personal words too: Blurt.exe itself only holds it compressed.
    Problems name the file or module, never what was found.
    """
    folder = Path(folder)
    problems = []
    for name in REQUIRED:
        if not (folder / name).is_file():
            problems.append(f"missing: {name}")
    if modules is not None and UIA_MODULE not in modules:
        problems.append(f"missing: {UIA_MODULE} (smart paste would always copy)")
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        name = path.name.lower()
        if path.is_dir():
            if path.name in FORBIDDEN_FOLDERS and relative.count("/") <= 1:
                problems.append(f"shouldn't ship: {relative}/")
            continue
        if (name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES)
                or name.startswith(FORBIDDEN_PREFIXES)):
            problems.append(f"shouldn't ship: {relative}")
        try:
            data = path.read_bytes()
        except OSError as e:
            problems.append(f"couldn't read {relative}: {e}")
            continue
        problems += _secrets_in(data, relative, secret, personal)
    for name, data in sorted((code or {}).items()):
        problems += _secrets_in(data, f"Blurt.exe: {name}", secret, personal)
    return problems


def _secrets_in(data, where, secret, personal):
    problems = []
    if KEY_PATTERN.search(data) or (secret and secret.encode() in data):
        problems.append(f"contains an API key: {where}")
    if personal is not None and personal.search(data):
        problems.append(f"contains one of your personal words: {where}")
    return problems


def check_own_code(personal=None, root=None):
    """The app's own source files and shipped text must not contain anything shaped like a Groq
    key, or one of your personal words (personal: words_pattern() of them)."""
    root = Path(root or ROOT)
    problems = []
    for entry in OWN_CODE + OWN_TEXT:
        path = root / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            data = file.read_bytes()
            relative = file.relative_to(root)
            if KEY_PATTERN.search(data):
                problems.append(f"contains something shaped like an API key: {relative}")
            if personal is not None and personal.search(data):
                problems.append(f"contains one of your personal words: {relative}")
    return problems


def bundled_modules(exe):
    """The module names inside Blurt.exe's code archive, or None if they can't be read."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(exe))
        pyz = next(name for name in archive.toc if name.endswith(".pyz"))
        return set(archive.open_embedded_archive(pyz).toc)
    except Exception as e:
        print(f"  (couldn't list the modules inside Blurt.exe: {e})")
        return None


def bundled_code(exe):
    """{name: bytes} of everything packed inside Blurt.exe, unpacked: its own entries and every
    module in its code archive (marshalled code, where strings like docstrings are plain bytes).
    None if it can't be read."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(exe))
        code = {}
        for name in archive.toc:
            code[name] = archive.extract(name) or b""
        pyz = next(name for name in archive.toc if name.endswith(".pyz"))
        modules = archive.open_embedded_archive(pyz)
        for name in modules.toc:
            code[f"{pyz}/{name}"] = modules.extract(name, raw=True) or b""
        return code
    except Exception as e:
        print(f"  (couldn't unpack the code inside Blurt.exe: {e})")
        return None


def env_key():
    """Your key from .env, only to make sure it isn't in the build. Never printed."""
    try:
        from dotenv import dotenv_values
        return (dotenv_values(ROOT / ".env").get("GROQ_API_KEY") or "").strip() or None
    except Exception:
        return None


def find_iscc():
    """Inno Setup's compiler, from its usual install folders."""
    folders = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
               str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")]
    for base in filter(None, folders):
        for name in ("Inno Setup 6", "Inno Setup 7"):
            candidate = Path(base) / name / "ISCC.exe"
            if candidate.is_file():
                return candidate
    return None


def step(title):
    print(f"\n== {title}", flush=True)


def run(command):
    """Runs a command in the project folder; True if it worked."""
    return subprocess.run([str(part) for part in command], cwd=ROOT).returncode == 0


def app_folder_in_use():
    """True if dist\\Blurt\\Blurt.exe is running (Windows won't let a running exe be opened for writing)."""
    exe = APP_FOLDER / "Blurt.exe"
    if not exe.exists():
        return False
    try:
        with open(exe, "ab"):
            return False
    except PermissionError:
        return True


def main():
    parser = argparse.ArgumentParser(description="Build Blurt and its installer.")
    parser.add_argument("--skip-tests", action="store_true", help="don't run the test suite first")
    parser.add_argument("--no-installer", action="store_true", help="only build dist\\Blurt")
    args = parser.parse_args()
    print(f"Building Blurt {VERSION}")

    try:
        source_words, bundle_words = load_personal_words()
    except (OSError, ValueError) as e:
        print(f"\n*** personal_words.txt: {e} ***\n"
              "The build checks itself for words that identify you (your name, user name, business),\n"
              "and it won't build without that list. Create personal_words.txt in the project folder\n"
              "(next to .env), one word per line. It's in .gitignore, so it's never committed or shipped.\n"
              "See README.md, For developers.")
        return 1

    if app_folder_in_use():
        print("dist\\Blurt\\Blurt.exe is running. Quit it from the tray icon first.")
        return 1

    if args.skip_tests:
        step("Tests: skipped")
    else:
        step("Tests")
        if not run([PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "."]):
            print("Tests failed, so nothing was built.")
            return 1

    step("Checking the app's code for API keys and your personal words")
    problems = check_own_code(words_pattern(source_words))
    if problems:
        print("\n".join(problems))
        return 1
    print(f"  none found ({len(source_words)} personal words checked)")

    step("Generating the UI Automation wrapper (comtypes)")
    generate = ("import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll'); "
                f"import {UIA_MODULE}; print('  ok')")
    if not run([PYTHON, "-c", generate]):
        return 1

    step("PyInstaller")
    if not run([PYTHON, "-m", "PyInstaller", SPEC, "--clean", "--noconfirm",
                "--distpath", DIST, "--workpath", WORK]):
        print("PyInstaller failed.")
        return 1

    step("Checking dist\\Blurt")
    exe = APP_FOLDER / "Blurt.exe"
    code = bundled_code(exe)
    problems = check_bundle(APP_FOLDER, secret=env_key(), modules=bundled_modules(exe),
                            personal=words_pattern(bundle_words), code=code)
    if code is None:
        problems.append("couldn't unpack the code inside Blurt.exe, so it wasn't checked")
    else:
        print(f"  unpacked {len(code)} files and modules from Blurt.exe to check them too")
    if problems:
        print("\n".join(f"  {problem}" for problem in problems))
        print("Build checks failed: don't share this build.")
        return 1
    print("  build checks passed")

    if args.no_installer:
        step("Installer: skipped (--no-installer)")
    elif not ISS.exists():
        step("Installer: skipped (installer\\blurt.iss isn't written yet)")
    else:
        step("Inno Setup")
        iscc = find_iscc()
        if iscc is None:
            print("Inno Setup isn't installed (ISCC.exe not found). See README.md, Building.")
            return 1
        if not run([iscc, f"/DAppVersion={VERSION}", f"/O{DIST}", ISS]):
            print("Inno Setup failed.")
            return 1
        print(f"  made dist\\Blurt-Setup-{VERSION}.exe")

    print(f"\nDone: {APP_FOLDER}\\Blurt.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
