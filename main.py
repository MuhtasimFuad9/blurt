"""Hold Ctrl+Win (or the shortcut chosen in config.json), speak, release: the cleaned-up
text is pasted at the cursor.

Threads:
- Main thread: the Qt GUI (the pill).
- Hook thread: the keyboard hook and its own Windows message loop. It does
  nothing else, so GUI work can never delay your keyboard.
- Worker thread: turns hook events into recording start/stop.
- Pipeline thread: transcribe, clean up, paste.
The worker and pipeline update the pill only through UiBridge signals, which
Qt delivers safely on the main thread.
"""

import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import autostart, focus, keystore, log, paths, shortcut
from app.config import DEFAULTS, ConfigWatcher, load_config_or_defaults, wrong_kinds
from app.history import HistoryStore
from app.hotkey import Hotkey
from app.pipeline import Pipeline
from app.recorder import SAMPLE_RATE, Recorder
from app.transcribe import warm_up as warm_up_groq
from app.ui.dictionary_panel import DictionaryPanel
from app.ui.history_panel import HistoryPanel
from app.ui.key_window import KeyFlow
from app.ui.notice import CancelNotice
from app.ui.panel import UsagePanel
from app.ui.pill import Pill, UiBridge
from app.ui.settings_panel import SettingsPanel
from app.ui.tray import Tray, tray_icon
from app.usage import UsageTracker

API_TIMEOUT_SECONDS = 8.0
USAGE_DB = paths.USAGE_DB

MIN_HOLD_SECONDS = 0.3
DEVICE_CHECK_SECONDS = 2.0
CONFIG_CHECK_SECONDS = 1.0  # how often the worker looks for a changed keep_mic_open
STUCK_CHECK_DELAY = 0.05  # give Windows a moment to process the key release first
HOOK_EXIT_WAIT_SECONDS = 2.0
QUIT_WAIT_SECONDS = 5.0  # on quit, how long a dictation still processing may take to paste
QUIET_IN_A_ROW = 2  # dictations in a row the silence check dropped before the quiet mic notification

MUTEX_NAME = f"Local\\{paths.APP_ID}.SingleInstance"
SHOW_EVENT_NAME = f"Local\\{paths.APP_ID}.AlreadyRunning"  # a second copy sets it, the first says so
QUIT_EVENT_NAME = f"Local\\{paths.APP_ID}.Quit"  # the installer sets it to close Blurt before replacing it
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long


def claim_single_instance():
    """True if no other copy is running. Windows frees the name when we exit, even on a crash."""
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return bool(handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def tell_running_copy():
    """We're the second copy: ask the first to show "already running". False if it can't be reached."""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def listen_for_event(name, on_signal):
    """Call on_signal() (from a background thread) whenever another program sets the named
    event. Windows deletes the event when we exit. Returns False if it couldn't be made."""
    handle = kernel32.CreateEventW(None, False, False, name)  # auto-reset
    if not handle:
        return False

    def wait():
        while kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
            on_signal()

    threading.Thread(target=wait, daemon=True).start()
    return True


def listen_for_second_copies(on_second_copy):
    """We're the first copy: on_second_copy() whenever another copy is started."""
    return listen_for_event(SHOW_EVENT_NAME, on_second_copy)


def listen_for_installer(on_quit):
    """The installer (and uninstaller) sets QUIT_EVENT_NAME to close Blurt before replacing
    its files: on_quit() then runs the normal ordered quit (a dictation still processing
    gets pasted, the hook is removed)."""

    def quit_for_installer():
        print("the installer asked Blurt to quit")
        on_quit()

    return listen_for_event(QUIT_EVENT_NAME, quit_for_installer)


def repair_autostart():
    """Installed: if Start with Windows points to another Blurt.exe (the install moved), fix
    the path. Never turns it on, and never undoes turning it off in Task Manager."""
    if not autostart.available():
        return
    try:
        if autostart.repair():
            print("Start with Windows: pointed it at this Blurt.exe")
    except OSError as e:
        print(f"Start with Windows: couldn't check it ({e})")


def set_app_identity():
    """Tells Windows which app this is, so notifications can carry its name instead of
    "Python". From source Windows may still say "Python": the name is only registered
    for sure by the installer's Start menu shortcut (M12). Call before any window exists."""
    shell32.SetCurrentProcessExplicitAppUserModelID(paths.APP_ID)


class PauseReasons:
    """Dictation is paused while any reason holds:
    - "user": the Pause button on the pill, or the tray menu
    - "no_key": there's no API key yet
    - "key_window": the key window is open, so a dictation can never be pasted into it
    So closing the key window never resumes a pause you chose yourself.
    apply(paused) does the real work when the overall answer changes. GUI thread only;
    other threads may read reasons.
    """

    def __init__(self, apply):
        self._apply = apply
        self.reasons = frozenset()

    @property
    def paused(self):
        return bool(self.reasons)

    def set(self, reason, on):
        before = self.paused
        self.reasons = self.reasons | {reason} if on else self.reasons - {reason}
        if self.paused != before:
            self._apply(self.paused)

    def user_toggle(self, paused):
        """The Pause/Resume button. Resume only lifts your own pause: with no API key,
        dictation stays paused."""
        self.set("user", paused)
        if not paused and self.paused:
            print("still paused: no Groq API key yet" if "no_key" in self.reasons
                  else "still paused while the API key window is open")


KEY_BLOCKS = {"no_key", "key_window"}  # pause reasons only the key window can end


class QuietMic:
    """Notices a mic that's too quiet: after QUIET_IN_A_ROW dictations in a row that the
    silence check dropped ("quiet"), notify() runs, once per run of the app. Any dictation
    that heard something (every other result) starts the count again. GUI thread only.
    """

    def __init__(self, notify, in_a_row=QUIET_IN_A_ROW):
        self._notify = notify
        self._in_a_row = in_a_row
        self._count = 0
        self._notified = False

    def job_finished(self, result, entry_id=0):
        """Connected to UiBridge.job_finished, next to the pill."""
        if result != "quiet":
            self._count = 0
            return
        self._count += 1
        if self._count >= self._in_a_row and not self._notified:
            self._notified = True
            self._notify()


def press_pause_button(pause, paused, open_key_window):
    """The pill's Pause/Resume button and the tray's. Resume always lifts your own pause; if
    dictation is still paused because of the key, the key window opens (or comes to the
    front, so a window hidden behind others never leaves dictation paused for no visible reason)."""
    pause.user_toggle(paused)
    if not paused and pause.reasons & KEY_BLOCKS:
        open_key_window()


def press_while_paused(pause, key_window_open, open_key_window, show_missing_key):
    """The shortcut while paused. The key window is open (maybe behind other windows, or
    minimized): bring it to the front. No key and no window: the notification."""
    if key_window_open:
        open_key_window()
    elif "no_key" in pause.reasons:
        show_missing_key()


def usage_limits(config):
    """The "groq_limits" settings, with any missing or broken number replaced by its default."""
    limits = dict(DEFAULTS["groq_limits"])
    for key, value in config["groq_limits"].items():
        if key in limits and isinstance(value, (int, float)) and value > 0:
            limits[key] = value
        else:
            print(f"groq_limits.{key} should be a number above 0, using {limits.get(key)} instead")
    return limits


def warm_up(pipeline, tracker):
    """Warm up Groq's connection in the background (skipped while there's no API key)."""
    client = pipeline.client
    if client is not None:
        threading.Thread(target=warm_up_groq, args=(client, tracker), daemon=True).start()


def cancel_notice_wanted(held):
    """A cancelled recording shows the notice only if it was held at least MIN_HOLD_SECONDS:
    a quick Ctrl+Win+D or Ctrl+Win+Arrow is a Windows shortcut used on purpose, not an accident."""
    return held >= MIN_HOLD_SECONDS


def apply_new_hotkey(config, hotkey, on_change):
    """config.json has another hotkey (Settings or a Notepad edit): the hook switches to it
    between key presses, and on_change("Alt+Win") updates the pill and the tray. Called on
    the worker thread. True if it changed."""
    new = shortcut.parse(config["hotkey"])  # always allowed: load_config put Ctrl+Win back otherwise
    if new == hotkey.requested:
        return False
    hotkey.set_shortcut(new)
    print(f"shortcut is now {shortcut.text(new)} (hold it to dictate)")
    on_change(shortcut.text(new))
    return True


def worker(events, hotkey, recorder, pipeline, ui, tracker, post_roll, pause):
    started_at = 0.0
    mic_ok = True
    stuck_check = None  # (when to run it, press counts at the time)
    post_roll_end = None  # when to stop the recording, after the keys were released
    held = 0.0
    target = None  # the window you dictated into (see Pipeline.submit)
    next_device_check = time.monotonic() + DEVICE_CHECK_SECONDS
    config_watcher = ConfigWatcher()
    next_config_check = time.monotonic() + CONFIG_CHECK_SECONDS
    shown_problems = set(wrong_kinds())  # already printed at startup; each message shows once

    def finish_recording():
        """Called when the post-roll is over: collect the audio and send it off."""
        nonlocal post_roll_end
        post_roll_end = None
        audio = recorder.stop()
        seconds = len(audio) / SAMPLE_RATE
        print(f"stopped: held {held:.2f}s, audio {seconds:.2f}s. Processing...")
        pipeline.submit(audio, target)

    while True:
        timeout = 0.5
        for deadline in (stuck_check and stuck_check[0], post_roll_end):
            if deadline:
                timeout = min(timeout, max(0.0, deadline - time.monotonic()))
        try:
            kind, at, detail = events.get(timeout=timeout)
        except queue.Empty:
            kind = None

        if kind == "start":
            if post_roll_end is not None:
                finish_recording()  # pressed again during the post-roll: send the last one first
            started_at = at
            mic_ok = recorder.start()
            ui.recording_started.emit()
            # Get Groq's connection and the focused app's accessibility info (for smart
            # paste) ready now, while you speak, so they're fast when needed.
            warm_up(pipeline, tracker)
            threading.Thread(target=focus.warm_up, daemon=True).start()
            if mic_ok:
                print("recording...")
            else:
                print("recording... (no mic available)")
        elif kind == "stop":
            held = at - started_at
            if not mic_ok:
                recorder.stop()
                print("nothing recorded (no mic available)")
                ui.recording_ended.emit(False)
                ui.error.emit()
            elif held < MIN_HOLD_SECONDS:
                recorder.stop()
                print(f"discarded (tap, held {held:.2f}s)")
                ui.recording_ended.emit(False)
            else:
                # Tell the pill now (it shows processing right away, and counts this job
                # before the result can arrive), but keep recording a moment longer so
                # the last word isn't cut off.
                ui.recording_ended.emit(True)
                # Remember where you were when you let go: the text is only pasted there.
                target = focus.foreground_root()
                post_roll_end = time.monotonic() + post_roll
        elif kind == "cancel":
            # Another key in the recording's first second (after that the hook swallows other
            # keys and the recording is kept): you meant a Windows shortcut, so it's thrown away.
            recorder.stop()
            ui.recording_ended.emit(False)
            held = at - started_at
            if cancel_notice_wanted(held):
                ui.dictation_cancelled.emit()
            print(f"cancelled: another key was pressed after {held:.2f}s")
        elif kind == "paused_press":
            ui.paused_press.emit()
            name = shortcut.text(hotkey.shortcut)
            if "no_key" in pause.reasons:
                print(f"no Groq API key yet: {name} ignored")
            elif "key_window" in pause.reasons:
                print(f"the API key window is open: {name} ignored")
            else:
                print(f"paused: {name} ignored (resume from the pill's menu or the tray icon)")
        elif kind == "pause":
            recorder.set_paused(detail)  # closes the mic, or reopens it
            print(f"paused: mic closed, {shortcut.text(hotkey.shortcut)} does nothing" if detail else "resumed")
        elif kind == "check_stuck":
            stuck_check = (time.monotonic() + STUCK_CHECK_DELAY, hotkey.press_counts())
        elif kind == "hook_error":
            report_hook_error(detail)
        elif kind == "debug":
            print(f"[hotkey] {detail}")

        now = time.monotonic()
        if post_roll_end is not None and now >= post_roll_end:
            finish_recording()
        if stuck_check and now >= stuck_check[0]:
            hotkey.release_stuck_modifiers(stuck_check[1])
            stuck_check = None
        if now >= next_device_check:
            recorder.check_devices()
            next_device_check = now + DEVICE_CHECK_SECONDS
        if now >= next_config_check:
            # keep_mic_open and hotkey apply right away (Settings or Notepad); the worker owns the mic.
            next_config_check = now + CONFIG_CHECK_SECONDS
            config = config_watcher.changed()
            if config is not None:
                for problem in config_watcher.problems:
                    if problem not in shown_problems:
                        print(f"config.json: {problem}")
                shown_problems = set(config_watcher.problems)
                apply_new_hotkey(config, hotkey, ui.shortcut_changed.emit)
            if config is not None and bool(config["keep_mic_open"]) != recorder.keep_open:
                recorder.set_keep_open(bool(config["keep_mic_open"]))
                print("keep_mic_open is on: mic stays open" if recorder.keep_open
                      else "keep_mic_open is off: mic opens only while recording")


def report_hook_error(error):
    """An error the keyboard hook caught (it let the key through and kept going). Printed like
    any other caught crash: the stack, and wherever output is kept, not the message."""
    print("keyboard hook error (the key was let through):")
    log.print_exception(error)


def run_worker(events, hotkey, *args):
    try:
        worker(events, hotkey, *args)
    except Exception as e:
        log.print_exception(e)
        print("Worker crashed. Quitting so the keyboard hook doesn't linger.")
        hotkey.stop()  # ends the hook thread, which then quits the app


def run_hook(hotkey, ready, on_exit):
    """The hook thread: install the hook here, then run its message loop until stop()."""
    try:
        hotkey.install()
    except Exception as e:
        ready.put(e)
        return
    ready.put(None)
    try:
        hotkey.run_loop()
    finally:
        # Order matters: remove the hook first, then fix any stuck keys.
        hotkey.uninstall()
        hotkey.release_stuck_modifiers()
        on_exit()  # Ctrl+C or a worker crash: close the GUI too


def stop_hook(hotkey, hook_thread):
    """End the hook thread, which unhooks and then repairs stuck keys. Does it here if that thread hangs."""
    hotkey.stop()
    if hook_thread.ident is not None:
        hook_thread.join(HOOK_EXIT_WAIT_SECONDS)
        if hook_thread.is_alive():
            # The hook thread didn't finish its own cleanup in time: do it here.
            hotkey.uninstall()
            hotkey.release_stuck_modifiers()


def shutdown(hotkey, hook_thread, pipeline, recorder, stores, before=None):
    """Quit in order. Every way of quitting ends here (tray Quit, Ctrl+C, a worker crash).

    1. before() (hide the tray icon and pill), and pause: no new recording can start.
    2. Wait for a dictation still processing, so it pastes and restores the clipboard.
    3. Unhook the keyboard, then repair stuck keys. This runs even if an earlier step failed.
    4. Close the mic, then the databases.
    """
    try:
        try:
            if before is not None:
                before()
        finally:
            hotkey.paused = True
        if pipeline is not None and not pipeline.wait_until_idle(QUIT_WAIT_SECONDS):
            print(f"quit: a dictation was still processing after {QUIT_WAIT_SECONDS:.0f}s and was dropped")
    finally:
        stop_hook(hotkey, hook_thread)
        for close in (recorder.close, *(store.close for store in stores)):
            try:
                close()
            except Exception as e:
                log.print_exception(e)


def main():
    log.start()  # no console (installed, pythonw): printed lines go to logs\app.log
    if not claim_single_instance():
        told = tell_running_copy()
        print(f"{paths.APP_DISPLAY_NAME} is already running." + ("" if told else " (couldn't reach it)"))
        return
    log.watch_for_crashes()  # only now: crash.log belongs to the copy that holds the lock
    set_app_identity()
    if paths.SANDBOX:
        print(f"TEST RUN: using {paths.USER_DIR} and the test API key, not your real files")

    created = paths.ensure_user_files()  # first run: config.json and dictionary.txt from the examples
    if created:
        print(f"created {', '.join(created)} in {paths.USER_DIR}")
    paths.remove_old_recording()
    api_key, key_source = keystore.find_key()
    if key_source == "env":
        print("API key: from .env")
    elif key_source == "windows":
        print("API key: saved in Windows (Credential Manager)")

    config, config_problem = load_config_or_defaults()
    if config_problem:
        print(f"{config_problem} Running on the default settings until it's fixed and the app restarts.")
    kind_problems = [] if config_problem else wrong_kinds()
    for problem in kind_problems:
        print(f"config.json: {problem}")
    # Create the Qt app before any window exists, so Qt sets up screen scaling first.
    app = QApplication(sys.argv)
    recorder = Recorder(keep_open=config["keep_mic_open"])
    tracker = UsageTracker(USAGE_DB, usage_limits(config))
    history = HistoryStore()
    usage_panel = UsagePanel(tracker, debug=config["debug_pill"])
    history_panel = HistoryPanel(history, tracker, debug=config["debug_pill"])
    dictionary_panel = DictionaryPanel(debug=config["debug_pill"])
    key_flow = None  # the API key window's controller, made below (the panels need key_status first)

    def key_status():
        """For the Settings row: "windows", "env", None (no key) or "rejected"."""
        source = key_source if key_flow is None else key_flow.key_source
        if source is None:
            return None
        if tracker.snapshot()["connection"] == "key_rejected":
            return "rejected"
        return source

    # Quit: the same ordered quit. The API key row asks key_status where the key comes from.
    settings_panel = SettingsPanel(on_quit=app.quit, debug=config["debug_pill"],
                                   key_status=key_status, on_change_key=lambda: key_flow.open(),
                                   autostart=autostart)
    pill = Pill(
        level_source=lambda: recorder.level,
        debug=config["debug_pill"],
        panels={"usage": usage_panel, "history": history_panel, "dictionary": dictionary_panel,
                "settings": settings_panel},
        mic_source=lambda: recorder.mic_label,
        hotkey=config["hotkey"],
    )
    ui = UiBridge()
    ui.attach(pill)
    ui.quit_requested.connect(app.quit, Qt.ConnectionType.QueuedConnection)
    # Its own window in the idle pill's spot: "Dictation cancelled: another key was pressed".
    cancel_notice = CancelNotice(pill, debug=config["debug_pill"])
    ui.dictation_cancelled.connect(cancel_notice.show_notice, Qt.ConnectionType.QueuedConnection)

    events = queue.Queue()
    # Ctrl+C quits through Qt, so shutdown() runs in order while the hook still works.
    hotkey = Hotkey(events, debug=config["debug_hotkey"], on_console_close=ui.quit_requested.emit,
                    shortcut=shortcut.parse(config["hotkey"]))
    panels = (usage_panel, history_panel, dictionary_panel, settings_panel)

    def set_paused(paused):
        """Pause or resume everything together. Only through PauseReasons below."""
        hotkey.paused = paused  # the hook: the shortcut shows the pause hint instead of recording
        events.put(("pause", time.perf_counter(), paused))  # the worker closes or reopens the mic
        pill.set_paused(paused)
        tray.set_paused(paused)

    pause = PauseReasons(set_paused)

    def pause_button(paused):
        """The pill's Pause/Resume button and the tray's."""
        press_pause_button(pause, paused, lambda: key_flow.open())

    tray = Tray(tracker, pill, on_pause=pause_button, on_quit=app.quit, on_key=lambda: key_flow.open(),
                debug=config["debug_pill"], hotkey=config["hotkey"])
    ui.second_copy.connect(tray.show_already_running, Qt.ConnectionType.QueuedConnection)
    ui.shortcut_changed.connect(tray.set_shortcut, Qt.ConnectionType.QueuedConnection)
    quiet_mic = QuietMic(tray.show_quiet_mic)
    ui.job_finished.connect(quiet_mic.job_finished, Qt.ConnectionType.QueuedConnection)
    # The tray is how you quit now; hiding every window (the panels, fullscreen) must never quit.
    app.setQuitOnLastWindowClosed(False)

    pill.on_pause = pause_button
    # One client for everything, so the connection is reused (faster). None until there's a key.
    # max_retries=0: app/api.py does the retrying (the SDK's own retry can sleep 60s on a rate limit).
    client = keystore.make_client(api_key, timeout=API_TIMEOUT_SECONDS) if api_key else None
    pipeline = Pipeline(client, hotkey, config, tracker, on_result=ui.job_finished.emit, history=history)
    history_panel.on_retry = pipeline.retry  # Retry in History: back through the pipeline's queue

    def use_new_key(key):
        """The key window checked and saved a key: switch to it, no restart."""
        pipeline.set_client(keystore.make_client(key, timeout=API_TIMEOUT_SECONDS))
        tracker.forget_connection()  # "key rejected" was about the old key
        tray.set_key_missing(False)
        pause.set("no_key", False)
        warm_up(pipeline, tracker)

    key_flow = KeyFlow(pause, on_new_key=use_new_key, focus_home=lambda: tray.last_window,
                       key_source=key_source, window_icon=tray_icon(False))

    def paused_press():
        """The shortcut while paused: the key window to the front, or the missing key notification."""
        press_while_paused(pause, key_flow.is_open(), key_flow.open, tray.show_missing_key)

    ui.paused_press.connect(paused_press, Qt.ConnectionType.QueuedConnection)

    def hide_windows():
        key_flow.close()
        tray.hide()
        pill.hide()
        cancel_notice.hide_now()
        for panel in panels:
            panel.hide()

    ready = queue.Queue()
    hook_thread = threading.Thread(target=run_hook, args=(hotkey, ready, ui.quit_requested.emit), daemon=True)
    try:
        recorder.open()
        hook_thread.start()
        error = ready.get()
        if error is not None:
            print(f"Couldn't install the keyboard hook: {error}")
            return
        pipeline.start()
        warm_up(pipeline, tracker)
        post_roll = max(0, config["post_roll_ms"]) / 1000
        worker_args = (events, hotkey, recorder, pipeline, ui, tracker, post_roll, pause)
        threading.Thread(target=run_worker, args=worker_args, daemon=True).start()
        pill.start()
        tray.start()
        listen_for_second_copies(ui.second_copy.emit)
        listen_for_installer(ui.quit_requested.emit)
        repair_autostart()
        if not api_key:
            pause.set("no_key", True)
            tray.set_key_missing(True)
            key_flow.open(first_run=True)  # you just started the app, so it may come to the front
        if config_problem:
            tray.show_config_problem(config_problem)
        elif kind_problems:
            tray.show_wrong_kinds(kind_problems)
        if api_key:
            mode = "mic always open" if recorder.keep_open else "mic opens on demand"
            status = f"Ready ({mode}). Hold {config['hotkey']} to record."
        else:
            status = "Started, waiting for a Groq API key: dictation is paused until one is saved."
            if not paths.INSTALLED:
                status += f" (From source you can also put GROQ_API_KEY in {paths.ENV_PATH}.)"
        if log.hides_text():  # no console: Ctrl+C can't reach us
            print(f"{status} Quit from the tray icon, or if it's stuck, end it in Task Manager.")
        else:
            print(f"{status} Quit from the tray icon, or press Ctrl+C here (twice if it's stuck).")
        app.exec()
    finally:
        shutdown(hotkey, hook_thread, pipeline, recorder, (tracker, history), before=hide_windows)
    log.stop_watching_for_crashes()  # a clean quit: no crash to report next time
    print("Bye.")


if __name__ == "__main__":
    main()
