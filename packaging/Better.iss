#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
[Setup]
AppId={{DC9A7B21-676D-494C-99CC-7E5F8246E4B8}
AppName=Better
AppVersion={#AppVersion}
AppPublisher=almahub
AppPublisherURL=https://github.com/almahub/bettor
DefaultDirName={localappdata}\Programs\Better
DefaultGroupName=Better
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=Better-Setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\Better.exe
[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
[Files]
Source: "..\dist\Better\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\download-update.ps1"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{userdesktop}\Better"; Filename: "{app}\Better.exe"; Check: not WizardNoIcons
Name: "{group}\Better"; Filename: "{app}\Better.exe"; Check: not WizardNoIcons
Name: "{group}\Configurazione e dati Better"; Filename: "{localappdata}\Better"; Check: not WizardNoIcons
Name: "{group}\Controlla aggiornamenti Better"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NoExit -File ""{app}\download-update.ps1"""; Check: not WizardNoIcons
[Run]
Filename: "{app}\Better.exe"; Description: "Avvia Better"; Flags: nowait postinstall skipifsilent
[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\Better.exe')) then
    if not Exec(ExpandConstant('{app}\Better.exe'), '--backup-only', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := 'Impossibile verificare Better e creare il backup. Installazione interrotta.'
    else if Code <> 0 then
      Result := 'Chiudi Better con Ctrl+C dopo la conclusione del collector, poi riprova. Dati non modificati.';
end;
