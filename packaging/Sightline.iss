#define MyAppVersion GetEnv("SIGHTLINE_VERSION")
#if MyAppVersion == ""
  #error SIGHTLINE_VERSION must be set
#endif
#define MyBuildCommit GetEnv("SIGHTLINE_BUILD_COMMIT")
#if MyBuildCommit == ""
  #define MyBuildCommit "development"
#endif
#define MyAppSource GetEnv("SIGHTLINE_APP_DIR")
#if MyAppSource == ""
  #define MyAppSource "..\dist\Sightline"
#endif
#define MyOutputDirectory GetEnv("SIGHTLINE_INSTALLER_OUTPUT")
#if MyOutputDirectory == ""
  #define MyOutputDirectory "..\dist\installer"
#endif
#define MySignedUninstallerDirectory GetEnv("SIGHTLINE_SIGNED_UNINSTALLER_DIR")
#if MySignedUninstallerDirectory == ""
  #define MySignedUninstallerDirectory "..\build\signed-uninstallers"
#endif
#define MySignedUninstaller GetEnv("SIGHTLINE_SIGNED_UNINSTALLER")
#if MySignedUninstaller == ""
  #define MySignedUninstaller "no"
#endif

[Setup]
AppId={{26A72B37-30B0-4B02-BD36-8368273C864D}
AppName=Sightline
AppVersion={#MyAppVersion}
AppVerName=Sightline {#MyAppVersion}
AppPublisher=Sightline contributors
AppPublisherURL=https://github.com/Sightline-Tools/sightline
AppSupportURL=https://github.com/Sightline-Tools/sightline/issues
AppUpdatesURL=https://github.com/Sightline-Tools/sightline/releases
DefaultDirName={localappdata}\Programs\Sightline
DefaultGroupName=Sightline
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir={#MyOutputDirectory}
OutputBaseFilename=Sightline-Setup-x64-v{#MyAppVersion}
SetupIconFile=..\build\Sightline.ico
UninstallDisplayIcon={app}\Sightline.exe
SignedUninstaller={#MySignedUninstaller}
SignedUninstallerDir={#MySignedUninstallerDirectory}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=Sightline per-user installer
VersionInfoProductName=Sightline
VersionInfoProductVersion={#MyAppVersion}
VersionInfoTextVersion={#MyAppVersion} ({#MyBuildCommit})
VersionInfoProductTextVersion={#MyAppVersion} ({#MyBuildCommit})

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#MyAppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Sightline"; Filename: "{app}\Sightline.exe"; IconFilename: "{app}\Sightline.exe"
Name: "{autodesktop}\Sightline"; Filename: "{app}\Sightline.exe"; IconFilename: "{app}\Sightline.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Sightline.exe"; Description: "Launch Sightline"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\Sightline.exe"; Parameters: "--shutdown"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "SightlineShutdown"

[Code]
function HasCommandLineSwitch(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), Value) = 0 then
      Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempts: Integer;
  RuntimeState: String;
  ShutdownSucceeded: Boolean;
begin
  Result := '';
  RuntimeState := ExpandConstant('{localappdata}\Sightline\runtime.json');
  if FileExists(ExpandConstant('{app}\Sightline.exe')) then
  begin
    ResultCode := -1;
    ShutdownSucceeded := Exec(ExpandConstant('{app}\Sightline.exe'), '--shutdown', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if ShutdownSucceeded and (ResultCode = 0) then
    begin
      for Attempts := 1 to 100 do
      begin
        if not FileExists(RuntimeState) then
          break;
        Sleep(100);
      end;
      if FileExists(RuntimeState) then
        Result := 'Sightline is still running. Exit it from the tray icon and retry the upgrade.';
    end;
    if ShutdownSucceeded and (ResultCode = 2) and FileExists(RuntimeState) then
    begin
      if not DeleteFile(RuntimeState) then
        Result := 'Sightline has stale runtime state that could not be cleared. Close any Sightline process and retry the upgrade.';
    end
    else if (not ShutdownSucceeded) or ((ResultCode <> 0) and (ResultCode <> 2)) then
      Result := 'Sightline is still running. Exit it from the tray icon and retry the upgrade.';
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RemoveData: Boolean;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveData := HasCommandLineSwitch('/REMOVEUSERDATA');
    if (not RemoveData) and (not UninstallSilent) then
      RemoveData := MsgBox(
        'Remove all Sightline settings, encounter data, logs, exports, and backups?' + #13#10 +
        'Choose No to preserve your data for a future installation.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    if RemoveData then
      DelTree(ExpandConstant('{localappdata}\Sightline'), True, True, True);
  end;
end;
