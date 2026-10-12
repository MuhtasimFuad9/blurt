"""The Groq API key: where it's saved, where it's looked up, and checking it works.

Saved in Windows Credential Manager (Control Panel > Credential Manager >
Windows Credentials, "Blurt/groq_api_key"). Windows encrypts it for your
account, so other accounts on the PC can't read it, unlike a plain .env file.
It's talked to directly through Windows' own functions (CredReadW, CredWriteW,
CredDeleteW), no extra package. A test run (BLURT_HOME, see paths.py) uses a
separate "Blurt (test)/groq_api_key", so it never touches your real key.

Lookup order (find_key):
1. The key saved in Credential Manager (from the key window).
2. From source only: GROQ_API_KEY in .env. Read without putting it in the
   environment. So a developer's .env works until a key is saved in the window.
A GROQ_API_KEY environment variable is ignored on purpose: the key is always
passed to the Groq client explicitly, so the SDK never picks up a stray one.
The same goes for GROQ_BASE_URL and GROQ_CUSTOM_HEADERS (see make_client), so
nothing in the environment can send the key anywhere but Groq. GROQ_LOG (the SDK
logging every request) is removed before groq is even imported (app/__init__.py).

The key is never printed. Error messages from here never contain it, and log.py
hides it from every output anyway (add_secret).
"""

import ctypes
import string
from ctypes import wintypes

import groq
from dotenv import dotenv_values

from app import log, paths

REAL_CREDENTIAL_NAME = f"{paths.APP_ID}/groq_api_key"
TEST_CREDENTIAL_NAME = f"{paths.APP_ID} (test)/groq_api_key"  # test runs (BLURT_HOME), and tools/sandbox.py
CREDENTIAL_NAME = TEST_CREDENTIAL_NAME if paths.SANDBOX else REAL_CREDENTIAL_NAME
CHECK_TIMEOUT_SECONDS = 8.0
GROQ_URL = "https://api.groq.com"  # passed explicitly, so GROQ_BASE_URL can't redirect the key
KEY_PREFIX = "gsk_"  # every Groq key starts with this; anything else gets a soft warning
MAX_KEY_LENGTH = 512

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this Windows account on this PC (not synced to other PCs)
ERROR_NOT_FOUND = 1168


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
advapi32.CredReadW.restype = wintypes.BOOL
advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
advapi32.CredWriteW.restype = wintypes.BOOL
advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
advapi32.CredDeleteW.restype = wintypes.BOOL
advapi32.CredFree.argtypes = [ctypes.c_void_p]
advapi32.CredFree.restype = None


class KeyStoreError(Exception):
    """Credential Manager said no. The message never contains the key."""


def read_saved_key(name=None):
    """The key saved in Credential Manager, or None."""
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(name or CREDENTIAL_NAME, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise KeyStoreError(f"couldn't read the saved API key (Windows error {error})")
    try:
        cred = pointer.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi32.CredFree(pointer)
    try:
        return blob.decode("utf-16-le") or None
    except UnicodeDecodeError:
        return None


def save_key(key, name=None):
    """Saves the key in Credential Manager, replacing an older one."""
    blob = key.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = name or CREDENTIAL_NAME
    cred.Comment = f"{paths.APP_DISPLAY_NAME}: Groq API key"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "groq_api_key"
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise KeyStoreError(f"couldn't save the API key (Windows error {ctypes.get_last_error()})")
    log.add_secret(key)


def delete_key(name=None):
    """Deletes the saved key. True if there was one. (Tests, and the M12 uninstaller.)"""
    if advapi32.CredDeleteW(name or CREDENTIAL_NAME, CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise KeyStoreError(f"couldn't delete the saved API key (Windows error {error})")


def _env_key():
    if paths.INSTALLED:
        return None
    try:
        return (dotenv_values(paths.ENV_PATH).get("GROQ_API_KEY") or "").strip() or None
    except OSError:
        return None


def find_key():
    """Returns (key, source): source is "windows" (Credential Manager), "env" (.env, from
    source only) or None when there's no key. Never raises."""
    try:
        key = read_saved_key()
    except KeyStoreError as e:
        print(e)
        key = None
    source = "windows" if key else None
    if not key:
        key = _env_key()
        source = "env" if key else None
    if key:
        log.add_secret(key)
    return key, source


def clean_key(text):
    """What the Paste key button and Save do to pasted text: drop spaces and line breaks
    anywhere (a key has none, but copying from a web page often adds them)."""
    return "".join(ch for ch in (text or "") if ch not in string.whitespace)


def looks_like_groq_key(key):
    return key.startswith(KEY_PREFIX)


def is_impossible_key(key):
    """True for text that can't be an API key: over MAX_KEY_LENGTH, or with anything but
    printable ASCII (a request header can't even carry that, so Groq would never see it).
    Refused before it's sent anywhere, so a copied document never goes to Groq."""
    return len(key) > MAX_KEY_LENGTH or any(not "!" <= ch <= "~" for ch in key)


class _GroqClient(groq.Groq):
    """groq.Groq without GROQ_CUSTOM_HEADERS. The SDK always reads that variable in __init__
    (no argument stops it), and those headers even win over the key's Authorization header.
    So they're emptied after every __init__, including the one copy()/with_options() runs.
    _custom_headers is private to the groq SDK (1.7.0, pinned in requirements.txt);
    tests/test_keystore.py sets the variable and checks the request, so an SDK upgrade that
    renames it fails loudly."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._custom_headers = {}


def make_client(key, **kwargs):
    """The app's Groq client. max_retries=0: app/api.py does the retrying.

    base_url is passed, so the SDK never reads GROQ_BASE_URL; GROQ_CUSTOM_HEADERS is dropped
    (_GroqClient). Proxies (HTTPS_PROXY) and SSL_CERT_FILE still work: they're httpx's, not the SDK's.
    """
    return _GroqClient(api_key=key, base_url=GROQ_URL, timeout=kwargs.pop("timeout", CHECK_TIMEOUT_SECONDS),
                       max_retries=0, **kwargs)


def check_key(key, client=None):
    """Asks Groq whether the key works, with models.list() (free: counts against no limit).
    Returns "works", "rejected" (401/403) or "unreachable" (no answer, or Groq had a
    problem, so the key couldn't be checked). Blocks up to CHECK_TIMEOUT_SECONDS: call it
    from a background thread. Never raises, never prints the key."""
    log.add_secret(key)
    try:
        (client or make_client(key)).models.list()
        return "works"
    except (groq.AuthenticationError, groq.PermissionDeniedError):
        return "rejected"
    except Exception:
        return "unreachable"
