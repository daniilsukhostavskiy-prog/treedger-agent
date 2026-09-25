; Treedger — installer for the local MT5 sync program (Inno Setup 6).
; quick 260925-qhs. Phase 40 D-13 (%LOCALAPPDATA% per-user folder, never admin) is
; REVOKED by the owner: the program installs into Program Files with ONE UAC prompt at
; install/update, so no process running as the user can silently overwrite the exe.
; The program itself still never needs admin to RUN; settings and the token stay in
; %APPDATA%\TreedgerAgent (per user) and are NOT removed on uninstall.
;
; Built by .github/workflows/build-release.yml (repo root layout of treedger-agent):
;   ISCC /DAppVersion=1.2.3 /DSourceDir=<abs>\release_staging\Treedger /O<abs out> /F<base> packaging\installer\treedger.iss
; Proven before any tag by .github/workflows/installer-smoke.yml.
; Kept in step with agent/autostart.py and agent/single_instance.py by
; agent/tests/test_installer_script.py (value name, command format, AppMutex).
;
; This file is UTF-8 WITH a BOM on purpose — Inno Setup reads it as UTF-8 only then,
; and the Russian/Ukrainian custom messages below would otherwise be garbled.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#ifndef SourceDir
  #error SourceDir must be passed on the ISCC command line (/DSourceDir=...): the folder that contains Treedger.exe
#endif

[Setup]
; NEVER change this GUID — upgrades and uninstall key on it. The doubled opening
; brace is Inno's escape for a literal "{".
AppId={{4EDEEDCA-12E3-4635-BCD3-E35AEB1DD185}
AppName=Treedger
AppVersion={#AppVersion}
AppPublisher=Treedger
AppPublisherURL=https://treedger.com
DefaultDirName={autopf}\Treedger
DefaultGroupName=Treedger
DisableProgramGroupPage=yes
; Per-machine only. No PrivilegesRequiredOverridesAllowed: a per-user install would put
; the exe back into a user-writable folder — exactly what revoking D-13 removes.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The program's own single-instance mutex is Local\TreedgerAgent-SingleInstance
; (agent/single_instance.py). An unprefixed name resolves in the session namespace, i.e.
; the SAME kernel object. Setup AND Uninstall both refuse to continue while the program
; runs and ask the user to close it.
AppMutex=TreedgerAgent-SingleInstance
; Nothing is ever closed automatically. The user closes Treedger themselves at the
; AppMutex prompt; the MetaTrader 5 terminal lives outside {app} and is never touched.
CloseApplications=no
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=Treedger
UninstallDisplayIcon={app}\Treedger.exe
SetupLogging=yes
; The autostart value is written under HKCU. Under normal UAC consent (the same user
; clicks «Да») that is the installing user's own hive. With over-the-shoulder admin
; credentials it would be the ADMIN's hive — a documented limitation (agent/README.md);
; the in-app checkbox always writes the right user's value.
UsedUserAreasWarning=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
russian.AutostartGroup=Автозапуск:
russian.AutostartTask=Запускать вместе с Windows (программа будет переключать счета в терминале — не включайте, если торгуете с этого компьютера)
ukrainian.AutostartGroup=Автозапуск:
ukrainian.AutostartTask=Запускати разом з Windows (програма перемикатиме рахунки в терміналі — не вмикайте, якщо торгуєте з цього комп'ютера)
english.AutostartGroup=Autostart:
english.AutostartTask=Start with Windows (the program will switch accounts in the terminal — do not enable this if you trade from this computer)

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Opt-in only, exactly like the in-app checkbox (Phase 40 D-22).
Name: "autostart"; Description: "{cm:AutostartTask}"; GroupDescription: "{cm:AutostartGroup}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Treedger"; Filename: "{app}\Treedger.exe"
Name: "{autodesktop}\Treedger"; Filename: "{app}\Treedger.exe"; Tasks: desktopicon

[Registry]
; The SAME value agent/autostart.py writes: name TreedgerAgent, data "<exe>" --minimized.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TreedgerAgent"; ValueData: """{app}\Treedger.exe"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; runasoriginaluser: the program — and any MetaTrader 5 terminal it later starts — must
; never run elevated. A non-elevated autostart instance could not attach to a terminal
; that an elevated first run had started.
Filename: "{app}\Treedger.exe"; Description: "{cm:LaunchProgram,Treedger}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
// Removes a TreedgerAgent scheduled task left by the old autostart mechanism (it
// registered a Task Scheduler task until quick 260925-qhs). Usually there is none;
// the result is ignored either way.
procedure DeleteLegacyScheduledTask();
var
  ResultCode: Integer;
begin
  if not Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN TreedgerAgent /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('legacy scheduled task delete could not run');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // The person just chose autostart: a stale Task Manager "disabled" marker must not
    // silently neutralize that choice.
    if WizardIsTaskSelected('autostart') then
      if RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'TreedgerAgent') then
        Log('removed a stale StartupApproved marker');
    DeleteLegacyScheduledTask();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    // Also covers a Run value the in-app checkbox created, which this uninstaller's own
    // log does not know about. %APPDATA%\TreedgerAgent (token, settings, logs) is kept
    // on purpose — see agent/README.md "Uninstalling".
    if RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'TreedgerAgent') then
      Log('removed the autostart Run value');
    if RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'TreedgerAgent') then
      Log('removed the StartupApproved marker');
    DeleteLegacyScheduledTask();
  end;
end;
