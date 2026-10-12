; Inno Setup script for Blurt's installer. Build with tools\build.py, which passes
; the version (/DAppVersion=..., from app/version.py) and the output folder (dist).
;
; - Installs for the current Windows account only, no admin: into
;   %LOCALAPPDATA%\Programs\Blurt. Your files (settings, dictionary, History, usage,
;   logs) are in %LOCALAPPDATA%\Blurt, a different folder, so installing a new
;   version never touches them.
; - A running Blurt is asked to quit through the event Local\Blurt.Quit (main.py
;   listens for it) and gets up to 10s to finish a dictation and remove its keyboard
;   hook. CloseApplications=no: Windows' Restart Manager must not kill it first.
; - The uninstaller deletes the saved API key and the Start with Windows entry, and
;   asks whether to delete your files too (default: keep them).
;
; The names below must match the app (tests/test_installer.py checks them):
; Blurt is paths.APP_ID, Blurt/groq_api_key is keystore.REAL_CREDENTIAL_NAME.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Permanent: Windows knows the installed app by it. Never change it.
#define AppGuid "81CAD8CB-A4F2-4A86-84B2-96D9AA9FED4B"

[Setup]
AppId={{{#AppGuid}}
AppName=Blurt
AppVersion={#AppVersion}
AppVerName=Blurt {#AppVersion}
AppPublisher=Blurt
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Blurt
VersionInfoProductName=Blurt
VersionInfoDescription=Blurt installer
DefaultDirName={autopf}\Blurt
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=Blurt-Setup-{#AppVersion}
SetupIconFile=blurt.ico
UninstallDisplayIcon={app}\Blurt.exe
UninstallDisplayName=Blurt
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Only on a first install: after that, the Settings toggle is in charge.
Name: "startup"; Description: "Start Blurt when I sign in to Windows"; Check: IsFreshInstall
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; The previous version's program files, so nothing left over from it gets loaded.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Blurt\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The AppUserModelID matches main.set_app_identity(), so notifications show Blurt's name and icon.
Name: "{autoprograms}\Blurt"; Filename: "{app}\Blurt.exe"; AppUserModelID: "Blurt"
Name: "{autodesktop}\Blurt"; Filename: "{app}\Blurt.exe"; AppUserModelID: "Blurt"; Tasks: desktopicon

[Registry]
; Start with Windows (app/autostart.py reads and writes the same value).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Blurt"; ValueData: """{app}\Blurt.exe"""; Tasks: startup

[Run]
Filename: "{app}\Blurt.exe"; Description: "Start Blurt"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; The saved API key (Credential Manager). cmdkey is Windows' own tool, so this works
; even if Blurt's files are damaged.
Filename: "{sys}\cmdkey.exe"; Parameters: "/delete:Blurt/groq_api_key"; Flags: runhidden; RunOnceId: "DeleteApiKey"

[Code]
const
  MutexName = 'Local\Blurt.SingleInstance';
  QuitEventName = 'Local\Blurt.Quit';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  ApprovedKey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';
  RunValue = 'Blurt';
  EVENT_MODIFY_STATE = $0002;
  QuitWaitSteps = 100; { x 100ms = 10s }
  ComtypesPrefix = 'Blurt-'; { comtypes' cache folder: the exe's name, "-", the Python version (Blurt-314) }

function OpenEvent(dwDesiredAccess: Cardinal; bInheritHandle: Integer; lpName: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: THandle): Integer;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Integer;
  external 'CloseHandle@kernel32.dll stdcall';

function IsFreshInstall(): Boolean;
begin
  Result := not RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#AppGuid}' + '}_is1');
end;

{ Asks a running Blurt to quit and waits for it. True when it's not running (any more). }
function CloseBlurt(): Boolean;
var
  Handle: THandle;
  Step: Integer;
begin
  Result := True;
  if not CheckForMutexes(MutexName) then
    Exit;
  Log('Blurt is running: asking it to quit');
  Handle := OpenEvent(EVENT_MODIFY_STATE, 0, QuitEventName);
  if Handle <> 0 then
  begin
    SetEvent(Handle);
    CloseHandle(Handle);
  end;
  for Step := 1 to QuitWaitSteps do
  begin
    Sleep(100);
    if not CheckForMutexes(MutexName) then
    begin
      Sleep(500); { let the process finish exiting, so its files can be replaced }
      Log('Blurt quit');
      Exit;
    end;
  end;
  Result := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseBlurt() then
    Result := 'Blurt is still running and couldn''t be closed. Quit it from its tray icon ' +
      '(next to the clock), then click Back and Next to try again.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  while not CloseBlurt() do
  begin
    if SuppressibleMsgBox('Blurt is still running and couldn''t be closed. Quit it from its tray icon ' +
        '(next to the clock), then click Retry.', mbError, MB_RETRYCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ The temp folder Python (and so comtypes) uses: the first of TMPDIR, TEMP and TMP that is a
  folder, the order of Python's tempfile.gettempdir(). Never Inno's tmp constant: that's
  Uninstall's own working folder, not the user's TEMP. '' if none. }
function PythonTempDir(): String;
begin
  Result := GetEnv('TMPDIR');
  if (Result <> '') and DirExists(Result) then
    Exit;
  Result := GetEnv('TEMP');
  if (Result <> '') and DirExists(Result) then
    Exit;
  Result := GetEnv('TMP');
  if (Result <> '') and DirExists(Result) then
    Exit;
  Result := '';
end;

{ True only for "Blurt-" followed by digits and nothing else, like Blurt-314. So another
  app's folder ("Blurt-3-314" from a "Blurt-3.exe") never counts. }
function IsComtypesCacheName(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := (Length(Name) > Length(ComtypesPrefix)) and (Copy(Name, 1, Length(ComtypesPrefix)) = ComtypesPrefix);
  if not Result then
    Exit;
  for I := Length(ComtypesPrefix) + 1 to Length(Name) do
    if (Name[I] < '0') or (Name[I] > '9') then
    begin
      Result := False;
      Exit;
    end;
end;

{ RemoveDir only ever removes an empty folder: anything inside, and the folder stays. }
procedure RemoveIfEmpty(const Dir: String);
begin
  if RemoveDir(Dir) then
    Log('Removed ' + Dir)
  else
    Log('Left ' + Dir + ' (not empty, or in use)');
end;

{ The smart paste part (comtypes) makes an empty folder %TEMP%\comtypes_cache\Blurt-314 on
  every run. Only RemoveDir is used: no file is ever deleted here, so nothing of another app's
  can go, and a folder with anything in it simply stays. }
procedure RemoveComtypesCache();
var
  Cache: String;
  FindRec: TFindRec;
  Found: TStringList;
  I: Integer;
begin
  Cache := PythonTempDir();
  if Cache = '' then
    Exit;
  Cache := AddBackslash(Cache) + 'comtypes_cache';
  if not DirExists(Cache) then
    Exit;
  Found := TStringList.Create;
  try
    if FindFirst(Cache + '\' + ComtypesPrefix + '*', FindRec) then
    begin
      try
        repeat
          { The wildcard also matches 8.3 short names; Name is the real (long) one. Folders
            only, and never a link (a reparse point) to somewhere else. }
          if IsComtypesCacheName(FindRec.Name)
              and (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0)
              and (FindRec.Attributes and FILE_ATTRIBUTE_REPARSE_POINT = 0) then
            Found.Add(Cache + '\' + FindRec.Name);
        until not FindNext(FindRec);
      finally
        FindClose(FindRec);
      end;
    end;
    for I := 0 to Found.Count - 1 do
      RemoveIfEmpty(Found[I]);
  finally
    Found.Free;
  end;
  RemoveIfEmpty(Cache);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserFolder: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  RegDeleteValue(HKCU, RunKey, RunValue);
  RegDeleteValue(HKCU, ApprovedKey, RunValue);
  RemoveComtypesCache();
  UserFolder := ExpandConstant('{localappdata}\Blurt');
  if UninstallSilent() or not DirExists(UserFolder) then
    Exit;
  { Real button labels, not Yes/No: this comes right after Inno's own "Are you sure?" Yes/No
    box, so a quick second Yes mustn't delete anything. "Keep my files" is first, so it's the
    default (Enter); it answers IDYES, "Delete them" answers IDNO. }
  if TaskDialogMsgBox('Keep your Blurt files?',
      'Your settings, dictionary, History and usage are in ' + UserFolder + '. History holds the text ' +
      'of your recent dictations. If you keep them, installing Blurt again picks up where you left off.',
      mbConfirmation, MB_YESNO, ['Keep my files', 'Delete them'], 0) = IDNO then
  begin
    if DelTree(UserFolder, True, True, True) then
      Log('Deleted ' + UserFolder)
    else
      MsgBox('Some files in ' + UserFolder + ' couldn''t be deleted. You can delete the folder yourself.',
        mbInformation, MB_OK);
  end;
end;
