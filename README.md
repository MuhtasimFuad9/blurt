# Blurt

Blurt is a voice dictation app for Windows and an almost free alternative to WisprFlow. It runs on your own free Groq API key. As of October 2026, Groq's free plan allows 2,000 transcription requests and about 8 hours of audio per day, which is far more than most people dictate. If you ever hit a limit, Blurt shows a notice. Groq can change its free limits at any time, so check [Groq's rate limits page](https://console.groq.com/docs/rate-limits) for the current numbers. Blurt is not affiliated with WisprFlow or Groq.

Hold **Ctrl+Win** anywhere (or another shortcut you pick in Settings), speak, let go, and the cleaned-up text is typed where your cursor is. Filler words ("um", "uh") and false starts are removed, punctuation is added, and your own words are kept.

Blurt uses [Groq](https://groq.com) to turn your voice into text and tidy it up. You need a free Groq API key (see below).

Tested on Windows 11. Windows 10 was not retested since 0.9.3.

## Install

1. Download `Blurt-Setup-1.0.0.exe` and open it.
2. The installer is unsigned, so Windows shows "Windows protected your PC". Click **More info**, then **Run anyway**.
3. Click through the installer. It installs for your Windows account only and doesn't ask for admin rights. Leave **Start Blurt when I sign in to Windows** ticked if you want Blurt ready every time you use your PC.
4. Blurt starts at the end. Its icon sits next to the clock; if you don't see it, click the **^** arrow there.

## Your Groq API key

With no Groq key Blurt starts paused and opens the Welcome window with **Get a free key**.

1. Click **Get a free key** in Blurt's window, or go to [console.groq.com/keys](https://console.groq.com/keys). Sign in and create a key.
2. Copy the key, then click **Paste key** in Blurt's window and **Save**. Blurt checks it with Groq before saving.
3. **Then delete the key from your clipboard history.** Copying puts it in Windows' clipboard history: press **Win+V** and delete it there (and in Ditto or any other clipboard manager you use).

The key is saved in Windows Credential Manager, encrypted for your Windows account: other Windows accounts can't read it, but any program running under your account can. To change it later: Blurt's tray icon, **Change API key** (or Settings, Groq API key).

**If your key leaks:** delete it at [console.groq.com/keys](https://console.groq.com/keys), create a new one, and save it with **Change API key**.

The free tier is generous for dictation. The Usage panel shows how much you've used; Blurt tells you when a limit is almost reached. If you upgrade your Groq account to a paid plan, Groq bills you for what you use.

## How to use it

- **Close other dictation apps first:** they can use the same keys, and Blurt may stop working while both are open.
- **Hold Ctrl+Win** (or your shortcut), speak, and **let go**. The small bar at the bottom of the screen shows the recording, then three dots while Blurt works, then a checkmark when the text is in.
- If the cursor isn't in a text field, Blurt copies the text instead of typing it: you'll see a copy icon, and you can paste it with Ctrl+V. The same happens if you switch windows while Blurt is working.
- **Other keys during a dictation:** in its first second, another key cancels it, so Windows shortcuts like Ctrl+Win+D still work (a small notice says so if you held the shortcut 0.3 seconds or more). After that, other keys are held back until you let go of them, so a stray key never throws a dictation away, and Ctrl+Win+D does nothing.
- If you press Ctrl+Alt+Del during a dictation, Blurt keeps what you said, but it may be copied instead of pasted because the window changed.
- **The bar hides** while a fullscreen app (a video, a game, a browser in fullscreen) is the active window on your main screen. It still shows while you dictate.
- **Hover the small bar** for the menu: Usage, History, Dictionary, Settings, Pause.
- **History** keeps your last dictations (7 days, on this PC only), each with a Copy button. **Original** shows what the speech recognition (Whisper, on Groq) heard, before cleanup tidied it up; it appears when cleanup changed your words. A dictation typed without cleanup is marked **Not cleaned up** (see Known issues). If Groq couldn't be reached, Retry sends the recording again.
- **Dictionary**: names and words Blurt should always spell your way. Copy a word anywhere, then click Add in the Dictionary panel.
- **Settings**: the shortcut, language, how pasting works, the microphone, Start with Windows, and more. To dictate in a language other than English, set Language to Auto-detect. Auto-detect is less reliable than English, especially for short clips, and some languages may come out wrong (for example in the wrong script, or turned into English).
- **Pause** stops Blurt from listening until you resume it (the hover menu or the tray icon).

Blurt doesn't work while a program running as administrator is the active window: your shortcut doesn't reach Blurt there.

## Changing the shortcut

Settings, **Shortcut**, **Change**. Pick Ctrl+Win (the default), Alt+Win, Ctrl+Alt+Win, Ctrl+Shift+Win or Alt+Shift+Win, then **Save**. It works right away. Other combinations aren't offered because they clash with typing or with Windows: Shift+Win, for example, would start with every Win+Shift+S screenshot.

**The right Alt key on some keyboard layouts:** on layouts with an AltGr key, such as English (United Kingdom), the right Alt key is AltGr, and Windows sends it together with a fake left Ctrl. So use the left Alt key for Alt+Win and Alt+Shift+Win; with such a layout, the right Alt key counts as Ctrl+Alt.

## Start with Windows

Settings, **Start with Windows**, turns it on or off. You can also turn it off in Task Manager's Startup tab; Blurt's Settings then shows it as off too.

## Privacy

- **What goes to Groq:** your voice recordings, your Dictionary words (as a spelling hint), the language if you picked one, and the text the speech recognition heard (for cleanup). Nothing else is sent anywhere.
- **What stays on this PC**, in `%LOCALAPPDATA%\Blurt`: History, usage numbers, your dictionary, settings and the log. History is `data\history.db`, a plain SQLite file (not encrypted) with the text of your dictations: the last 200 at most, none older than 7 days. **Clear** in the History panel erases it for good. You can turn History off in Settings.
- Recordings aren't saved, except when Groq couldn't be reached or a limit was hit: then History keeps that one recording (in `data\failed`) so you can Retry, and deletes it once the retry works or the dictation leaves History.
- **The log** is `logs\app.log`, at most about 1 MB; then it becomes `app.log.1` (replacing an older one) and a new app.log starts.
- **Uninstalling with Keep my files** leaves all of this on the PC, including the text in History.
- **The clipboard:** Blurt types text by putting it on the clipboard and pressing Ctrl+V, then puts back what you had copied before. The text it types is marked so Windows keeps it out of clipboard history (Win+V) and cloud clipboard sync. It also carries the markers that clipboard managers such as Ditto respect; a clipboard manager that ignores them may still save it. When Blurt copies instead of typing, the text goes into Win+V history like anything you copy, so you can find it there, but it's never synced to your other devices.

## Warnings from Windows and antivirus

Blurt isn't signed with a paid code-signing certificate yet, and it does things that security software watches closely: it listens for your shortcut on the whole keyboard, uses the microphone, and uses the clipboard to type text. So you may see:

- **"Windows protected your PC"** (SmartScreen). It means Windows doesn't know the publisher yet, not that it found something. Click **More info**, then **Run anyway**. You only see it once per download.
- **Smart App Control** (on some new Windows 11 PCs) blocks unsigned apps with no "Run anyway" button. Blurt can't run while it's on. You can turn it off in Windows Security, App & browser control, Smart App Control settings; check Microsoft's current guidance before you do.
- **Antivirus quarantines Blurt.** This is a false alarm on a new, unsigned app. Restore it from your antivirus's quarantine and allow it. If it's Microsoft Defender, you can report the false positive at [microsoft.com/wdsi/filesubmission](https://www.microsoft.com/wdsi/filesubmission), which helps everyone.

Blurt only reads the keyboard to see your shortcut (and other keys only to let shortcuts like Ctrl+Win+D through). It never records or sends your typing.

## If something goes wrong

- **Blurt doesn't hear you:** check Windows Settings, Privacy & security, Microphone: **Microphone access** and **Let desktop apps access your microphone** must be on. With **Show mic name** on in Settings (the default), Blurt shows the microphone's name above the bar when you start recording, so you can see which one it uses.
- **Every dictation fails:** check your internet connection and Groq's status page. With History on, **Retry** sends a failed one again.
- **Your shortcut stopped working:** quit Blurt from its tray icon and start it again from the Start menu.
- **Pressing Ctrl alone starts a recording** (or your shortcut's other key alone): tap the Win key once (if the Start menu opens, press Esc). This can happen in rare cases after Windows locks the screen or shows an administrator prompt while you're holding your shortcut.
- **Blurt is stuck and won't quit:** open Task Manager (Ctrl+Shift+Esc), find **Blurt**, and click **End task**. That's safe: Windows releases the keyboard and microphone.
- **The log file**, if you need to report a problem: `%LOCALAPPDATA%\Blurt\logs\app.log`. Send it, plus `app.log.1` next to it if there is one. It never contains your API key or what you dictated (only how many words). It can contain the names of programs you dictated into and of your microphones, and a microphone's name can include your first name, so read it before you send it.
- **After a crash** (Blurt closed by itself): start Blurt once more before you send the log. That start copies a short report of the crash into app.log.

## Known issues

- **Cleanup can occasionally drop a sentence**, especially when you say something twice in different words: it can mistake the first version for a false start. Read longer dictations before you send them. If something is missing, open **History** and press **Original** on that dictation: it shows exactly what the speech recognition heard, before cleanup, and **Copy** then copies that. When cleanup's result is missing a large part of what you said, Blurt notices and types what the speech recognition heard instead; a smaller loss can still slip through.
- **Sometimes the text isn't cleaned up at all:** when cleanup's result looks wrong (much shorter or much longer than what you said), when you've used up Groq's free limit for now (the bar shows an hourglass), or when cleanup fails. Blurt then types exactly what the speech recognition heard, filler words and all, and History marks it **Not cleaned up**.
- **Blurt can misspell names and rare words.** Add them in the Dictionary. It helps a lot, but it's a hint, not a guarantee.
- **Gaming laptops can turn the Win key off.** Many have a Win key lock, for example Fn+Win on ASUS laptops with Armoury Crate, and it's easy to hit while pressing your shortcut (on some keyboards Fn sits between Ctrl and Win). If Blurt and Windows' own Win shortcuts all stop working, turn the lock off again.

## Update

Download the new installer and run it. It closes Blurt, updates it and keeps your settings, dictionary, History and key.

## Uninstall

Windows Settings, Apps, Installed apps, **Blurt**, Uninstall. It asks "Keep your Blurt files?" with **Keep my files** and **Delete them**. Both remove the program, the saved key and the startup entry; only **Delete them** removes the data folder (`%LOCALAPPDATA%\Blurt`). If you keep your files, installing Blurt again picks up where you left off.

## Known limitations

- Names and rare words can be misspelled. The Dictionary helps.
- Blurt runs on Windows only.
- Blurt needs Groq and is capped by Groq's limits.
- The installer is unsigned, so Windows shows a warning when you install it.

## For developers

Blurt is Python (PySide6 for the interface, sounddevice for the microphone, the Groq SDK).

- Run from source: create a virtual environment, `pip install -r requirements.txt`, put `GROQ_API_KEY=...` in a `.env` file in the project folder (or save a key in the app's key window), then `python main.py`.
- Tests: `python -m unittest discover -s tests -t .`
- Build the installer: `pip install -r requirements-build.txt`, install [Inno Setup 6](https://jrsoftware.org/isinfo.php), then `python tools\build.py`. The result is `dist\Blurt-Setup-<version>.exe`. The version is set in `app/version.py`.
- Before building, create `personal_words.txt` in the project folder (next to `.env`): words that identify you (your name, Windows user name, business), one per line. The build refuses to start without it, and refuses a build whose code or files contain one of them. It's in `.gitignore`, so it's never committed. `#` starts a comment. Put `source:` in front of a word that a library really contains (a city name is in Qt's time zone list), so only Blurt's own files are checked for it, and write hyphenated names with their hyphen.

## Planned for the future

These are ideas, not promises.

- Smarter automatic spelling of names and rare words.
- Cleaner paragraphs and bullet points for longer dictations.
- Cleanup that keeps everything you said.
- A nicer look.
