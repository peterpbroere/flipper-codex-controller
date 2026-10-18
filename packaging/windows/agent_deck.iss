#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define BuildDir AddBackslash(SourcePath) + "..\\..\\dist\\AgentDeck"

[Setup]
AppId={{EA114F9C-9822-4CC8-B37A-8E0041010576}
AppName=Agent Deck
AppVersion={#AppVersion}
AppPublisher=Agent Deck contributors
DefaultDirName={localappdata}\Programs\Agent Deck
DefaultGroupName=Agent Deck
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\release
OutputBaseFilename=AgentDeck-Setup-{#AppVersion}
UninstallDisplayIcon={app}\AgentDeckCompanion.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Agent Deck"; Filename: "{app}\AgentDeckCompanion.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\Agent Deck"; Filename: "{app}\AgentDeckCompanion.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\AgentDeckCompanion.exe"; Description: "Launch Agent Deck"; Flags: postinstall nowait skipifsilent unchecked

[Code]
function InitializeUninstall(): Boolean;
var
  Response: Integer;
  ResultCode: Integer;
begin
  Result := True;
  if not FileExists(ExpandConstant('{app}\AgentDeck.exe')) then
    Exit;

  Response := MsgBox(
    'Remove Agent Deck entries from your Codex hooks before uninstalling? The hooks file will be backed up, and other hooks will be preserved. Choose No to leave Codex configuration unchanged, or Cancel to stop uninstalling.',
    mbConfirmation, MB_YESNOCANCEL);
  if Response = IDCANCEL then begin
    Result := False;
    Exit;
  end;

  if Response = IDYES then begin
    if (not Exec(ExpandConstant('{app}\AgentDeck.exe'), 'uninstall-hooks --apply',
                 ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or
       (ResultCode <> 0) then begin
      Response := MsgBox(
        'Agent Deck could not update the Codex hooks file. Continue uninstalling anyway? Choose No to cancel and keep Agent Deck installed.',
        mbError, MB_YESNO);
      Result := Response = IDYES;
    end;
  end;
end;
