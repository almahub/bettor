#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
[Setup]
AppId={{DC9A7B21-676D-494C-99CC-7E5F8246E4B8}
AppName=Bettor
AppVersion={#AppVersion}
AppPublisher=almahub
AppPublisherURL=https://github.com/almahub/bettor
DefaultDirName={localappdata}\Programs\Better
DefaultGroupName=Bettor
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=Better-Setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\better\web\brand\bettor.ico
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\Better.exe
[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
[Files]
Source: "..\dist\Better\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\download-update.ps1"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{userdesktop}\Bettor"; Filename: "{app}\Better.exe"; Check: not WizardNoIcons
Name: "{group}\Bettor"; Filename: "{app}\Better.exe"; Check: not WizardNoIcons
Name: "{group}\Configurazione e dati Bettor"; Filename: "{localappdata}\Better"; Check: not WizardNoIcons
Name: "{group}\Configurazione iniziale Bettor"; Filename: "{app}\Better.exe"; Parameters: "--setup"; Check: not WizardNoIcons
Name: "{group}\Arresta servizio Bettor"; Filename: "{app}\Better.exe"; Parameters: "--stop"; Check: not WizardNoIcons
Name: "{group}\Controlla aggiornamenti Bettor"; Filename: "{app}\Better.exe"; Parameters: "--updates"; Check: not WizardNoIcons
[Run]
Filename: "{app}\Better.exe"; Description: "Avvia Bettor"; Flags: nowait postinstall skipifsilent
[InstallDelete]
Type: files; Name: "{userdesktop}\Better.lnk"
Type: files; Name: "{userprograms}\Better\Better.lnk"
Type: files; Name: "{userprograms}\Better\Configurazione e dati Better.lnk"
Type: files; Name: "{userprograms}\Better\Configurazione iniziale Better.lnk"
Type: files; Name: "{userprograms}\Better\Arresta servizio Better.lnk"
Type: files; Name: "{userprograms}\Better\Controlla aggiornamenti Better.lnk"
[Code]
function InitializeSetup(): Boolean;
var Version: String;
begin
  Result := (RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
  if not Result then
    MsgBox('Bettor richiede Microsoft Edge WebView2 Runtime. Installalo da https://developer.microsoft.com/microsoft-edge/webview2/ e riesegui il setup. Nessun dato Bettor viene modificato.', mbError, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if FindWindowByWindowName('Bettor') <> 0 then
  begin
    Result := 'Chiudi la finestra Bettor e arresta il servizio prima di aggiornare.';
    Exit;
  end;
  if FileExists(ExpandConstant('{app}\Better.exe')) then
    if not Exec(ExpandConstant('{app}\Better.exe'), '--backup-only', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := 'Impossibile verificare Bettor e creare il backup. Installazione interrotta.'
    else if Code <> 0 then
      Result := 'Chiudi Bettor e arresta il servizio dopo la conclusione del collector, poi riprova. Dati non modificati.';
end;

function InitializeUninstall(): Boolean;
var Code: Integer;
begin
  Result := True;
  if FindWindowByWindowName('Bettor') <> 0 then
  begin
    MsgBox('Chiudi la finestra Bettor e riprova. I dati saranno conservati.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if FileExists(ExpandConstant('{app}\Better.exe')) then
  begin
    if not Exec(ExpandConstant('{app}\Better.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := False
    else
      Result := Code = 0;
    if not Result then
      MsgBox('Impossibile arrestare Bettor. Attendi la conclusione delle analisi e dei checkpoint, chiudi la finestra Bettor e riprova.', mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    DeleteFile(ExpandConstant('{localappdata}\Better\desktop-onboarding.json'));
end;
