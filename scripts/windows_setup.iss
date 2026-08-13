#ifndef SourceRoot
  #error SourceRoot must be supplied by build_windows_setup.ps1
#endif
#ifndef OutputRoot
  #error OutputRoot must be supplied by build_windows_setup.ps1
#endif
#ifndef SourceSha
  #error SourceSha must be supplied by build_windows_setup.ps1
#endif
#ifndef CoreBootstrap
  #error CoreBootstrap must be supplied by build_windows_setup.ps1
#endif
#ifndef ClassroomBootstrap
  #error ClassroomBootstrap must be supplied by build_windows_setup.ps1
#endif

#define SetupVersion "0.0.1"
#define SetupAsset "GamGUI-Setup-0.0.1-windows-x86_64"

[Setup]
AppId={{120C7CDF-EBCA-4D86-B724-FDBD4BE66E03}
AppName=GamGUI
AppVersion={#SetupVersion}
AppVerName=GamGUI {#SetupVersion} Windows Setup Preview
AppPublisher=GamGUI
AppPublisherURL=https://github.com/Sykezzz/gamgui
AppSupportURL=https://github.com/Sykezzz/gamgui/issues
DefaultDirName={localappdata}\GamGUI\installer
DefaultGroupName=GamGUI
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern dynamic
SetupLogging=yes
SolidCompression=yes
Compression=lzma2/ultra64
OutputDir={#OutputRoot}
OutputBaseFilename={#SetupAsset}
UninstallDisplayName=GamGUI
UninstallDisplayIcon={localappdata}\Programs\GamGUI\current\GamGUI.exe
Uninstallable=yes
CreateUninstallRegKey=yes
CloseApplications=yes
RestartApplications=no
RestartIfNeededByRun=no
ChangesAssociations=no
ChangesEnvironment=no
UsePreviousAppDir=no
UsePreviousTasks=no
VersionInfoVersion={#SetupVersion}.0
VersionInfoCompany=GamGUI
VersionInfoDescription=GamGUI per-user setup wizard
VersionInfoProductName=GamGUI
VersionInfoProductVersion={#SetupVersion}
VersionInfoCopyright=GamGUI contributors

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#CoreBootstrap}\*"; DestDir: "{tmp}\gamgui-bootstrap"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall; Check: UseCoreProfile
Source: "{#ClassroomBootstrap}\*"; DestDir: "{tmp}\gamgui-bootstrap"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall; Check: UseClassroomProfile

[Icons]
Name: "{userprograms}\GamGUI"; Filename: "{localappdata}\GamGUI\updater\GamGUIUpdater.exe"; Parameters: "--launch-installed"; WorkingDir: "{localappdata}\GamGUI\updater"
Name: "{userdesktop}\GamGUI"; Filename: "{localappdata}\GamGUI\updater\GamGUIUpdater.exe"; Parameters: "--launch-installed"; WorkingDir: "{localappdata}\GamGUI\updater"; Tasks: desktopicon

[Run]
Filename: "{localappdata}\GamGUI\updater\GamGUIUpdater.exe"; Parameters: "--launch-installed"; Description: "Launch GamGUI"; Flags: nowait postinstall skipifsilent shellexec

[Code]
var
  ProfilePage: TWizardPage;
  CoreRadio: TRadioButton;
  ClassroomRadio: TRadioButton;
  ProfileHelp: TNewStaticText;
  SummaryPage: TWizardPage;
  SummaryBody: TNewStaticText;
  DetailsButton: TNewButton;
  DetailsMemo: TNewMemo;
  TrustPage: TWizardPage;
  TrustCheck: TNewCheckBox;
  TrustTitle: TNewStaticText;
  TrustBody: TNewStaticText;
  ExistingPage: TWizardPage;
  ExistingDetails: TNewStaticText;
  OpenButton: TNewButton;
  UninstallButton: TNewButton;
  UninstallDataCheck: TNewCheckBox;
  ProgressReceipt: String;
  SilentSigner: String;
  SelectedProfile: String;
  ExistingInstall: Boolean;
  ExistingProfile: String;
  ExistingSha: String;
  BackendStarted: Boolean;
  OriginalNextCaption: String;

function FixedCurrent: String;
begin
  Result := ExpandConstant('{localappdata}\Programs\GamGUI\current');
end;

function FixedData: String;
begin
  Result := ExpandConstant('{localappdata}\GamGUI');
end;

function IsHex64(const Value: String): Boolean;
var
  I: Integer;
begin
  Result := Length(Value) = 64;
  if not Result then Exit;
  for I := 1 to Length(Value) do
    if Pos(Value[I], '0123456789abcdefABCDEF') = 0 then
    begin
      Result := False;
      Exit;
    end;
end;

function ReadIdentityField(const FileName, Marker: String): String;
var
  Body: AnsiString;
  StartAt, ValueAt, EndAt: Integer;
  Needle, Tail: String;
begin
  Result := '';
  if not LoadStringFromFile(FileName, Body) then Exit;
  Needle := '"' + Marker + '"';
  StartAt := Pos(Needle, String(Body));
  if StartAt = 0 then Exit;
  Tail := Copy(String(Body), StartAt + Length(Needle), MaxInt);
  ValueAt := Pos(':', Tail);
  if ValueAt = 0 then Exit;
  Tail := Copy(Tail, ValueAt + 1, MaxInt);
  ValueAt := Pos('"', Tail);
  if ValueAt = 0 then Exit;
  Tail := Copy(Tail, ValueAt + 1, MaxInt);
  EndAt := Pos('"', Tail);
  if EndAt = 0 then Exit;
  Result := Copy(Tail, 1, EndAt - 1);
end;

function UseCoreProfile: Boolean;
begin
  Result := SelectedProfile = 'core';
end;

function UseClassroomProfile: Boolean;
begin
  Result := SelectedProfile = 'classroom-oneroster';
end;

function GetCommandValue(const Name: String): String;
var
  Prefix: String;
  I: Integer;
begin
  Result := '';
  Prefix := '/' + Uppercase(Name) + '=';
  for I := 1 to ParamCount do
    if Pos(Prefix, Uppercase(ParamStr(I))) = 1 then
    begin
      Result := Copy(ParamStr(I), Length(Prefix) + 1, MaxInt);
      Exit;
    end;
end;

function SilentSignerIsAvailable: Boolean;
var
  Script, Probe, TrustChecks: String;
  ResultCode: Integer;
begin
  TrustChecks :=
    'if ((-not (Find-Certificate ''Root'' $false)) -or ' +
    '(-not (Find-Certificate ''TrustedPublisher'' $false))) { return $false };';
  Probe :=
    '$ErrorActionPreference=''Stop'';' +
    '$algorithm=[System.Security.Cryptography.HashAlgorithmName]::SHA256;' +
    'function Find-Certificate([string]$name,[bool]$privateKey) {' +
    '$store=[System.Security.Cryptography.X509Certificates.X509Store]::new($name,[System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser);' +
    'try {' +
    '$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly);' +
    '$candidates=$store.Certificates.Find([System.Security.Cryptography.X509Certificates.X509FindType]::FindBySubjectDistinguishedName,''CN=GamGUI Local'',$false);' +
    '$matches=@($candidates | Where-Object {' +
    '(([System.BitConverter]::ToString($_.GetCertHash($algorithm)) -replace ''-'','''').ToLowerInvariant() -eq $expected) -and ' +
    '((-not $privateKey) -or $_.HasPrivateKey)' +
    '}); return $matches.Count -eq 1' +
    '} finally { $store.Close() } };' +
    'if (-not (Find-Certificate ''My'' $true)) { return $false };' +
    TrustChecks + 'return $true';
  Script :=
    '$expected=''' + Lowercase(SilentSigner) + ''';' +
    '$probe=Start-Job -ScriptBlock { param($expected) ' + Probe + ' } -ArgumentList $expected;' +
    'if (-not (Wait-Job -Job $probe -Timeout 15)) { exit 2 };' +
    '$result=[bool](Receive-Job -Job $probe -ErrorAction SilentlyContinue);' +
    '$state=$probe.State; Remove-Job -Job $probe -Force -ErrorAction SilentlyContinue;' +
    'if (($state -ne ''Completed'') -or (-not $result)) { exit 1 }; exit 0';
  Result := Exec(
    'powershell.exe',
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ' + AddQuotes(Script),
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) and (ResultCode = 0);
end;

procedure OpenInstalledClick(Sender: TObject);
var
  ErrorCode: Integer;
begin
  ShellExec('', ExpandConstant('{localappdata}\GamGUI\updater\GamGUIUpdater.exe'),
    '--launch-installed', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure StartUninstallClick(Sender: TObject);
var
  ErrorCode: Integer;
begin
  if FileExists(ExpandConstant('{uninstallexe}')) then
    ShellExec('', ExpandConstant('{uninstallexe}'), '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode)
  else
    MsgBox('Windows could not find the GamGUI uninstaller. Open Apps & Features and remove GamGUI there.', mbError, MB_OK);
end;

procedure ToggleDetailsClick(Sender: TObject);
begin
  DetailsMemo.Visible := not DetailsMemo.Visible;
  if DetailsMemo.Visible then DetailsButton.Caption := 'Hide technical details'
  else DetailsButton.Caption := 'Show technical details';
end;

procedure UpdateSummary;
var
  FriendlyProfile: String;
begin
  if SelectedProfile = 'core' then FriendlyProfile := 'Core'
  else FriendlyProfile := 'Classroom + OneRoster';
  SummaryBody.Caption := 'Profile: ' + FriendlyProfile + #13#10#13#10 +
    'Application:' + #13#10 + '  ' + FixedCurrent + #13#10#13#10 +
    'Local data:' + #13#10 + '  ' + FixedData + #13#10#13#10 +
    'Setup does not contact Google, GAM, Keychain, or tenant services. It only installs and verifies local files.';
end;

procedure InitializeWizard;
var
  ProfilePath: String;
begin
  OriginalNextCaption := WizardForm.NextButton.Caption;
  WizardForm.WelcomeLabel2.Caption :=
    'Setup checks that this is Windows 11 x64, then installs GamGUI only for this Windows user. No administrator access is required.';
  ExistingInstall := DirExists(FixedCurrent);
  ExistingProfile := '';
  ExistingSha := '';
  ProfilePath := AddBackslash(FixedCurrent) + '_internal\resources\components\profile.json';
  if ExistingInstall then
  begin
    ExistingProfile := ReadIdentityField(ProfilePath, 'profile');
    ExistingSha := ReadIdentityField(ProfilePath, 'source_sha');
  end;

  ExistingPage := CreateCustomPage(wpWelcome, 'GamGUI is already installed',
    'Setup will not replace a working installation.');
  ExistingDetails := TNewStaticText.Create(ExistingPage);
  ExistingDetails.Parent := ExistingPage.Surface;
  ExistingDetails.Left := 0;
  ExistingDetails.Top := 8;
  ExistingDetails.Width := ExistingPage.SurfaceWidth;
  ExistingDetails.Height := 72;
  ExistingDetails.WordWrap := True;
  ExistingDetails.Caption := 'Installed profile: ' + ExistingProfile + #13#10 +
    'Source: ' + Copy(ExistingSha, 1, 12) + #13#10 +
    'Open GamGUI, start its uninstaller, or close Setup.';
  OpenButton := TNewButton.Create(ExistingPage);
  OpenButton.Parent := ExistingPage.Surface;
  OpenButton.Left := 0;
  OpenButton.Top := 96;
  OpenButton.Width := ScaleX(120);
  OpenButton.Caption := 'Open GamGUI';
  OpenButton.OnClick := @OpenInstalledClick;
  UninstallButton := TNewButton.Create(ExistingPage);
  UninstallButton.Parent := ExistingPage.Surface;
  UninstallButton.Left := OpenButton.Left + OpenButton.Width + ScaleX(12);
  UninstallButton.Top := OpenButton.Top;
  UninstallButton.Width := ScaleX(120);
  UninstallButton.Caption := 'Start uninstall';
  UninstallButton.OnClick := @StartUninstallClick;

  ProfilePage := CreateCustomPage(ExistingPage.ID, 'Choose what GamGUI should include',
    'You can use the full Classroom workflow or install the smaller Core profile.');
  ClassroomRadio := TRadioButton.Create(ProfilePage);
  ClassroomRadio.Parent := ProfilePage.Surface;
  ClassroomRadio.Left := 0;
  ClassroomRadio.Top := 12;
  ClassroomRadio.Width := ProfilePage.SurfaceWidth;
  ClassroomRadio.Caption := 'Classroom + OneRoster (recommended)';
  ClassroomRadio.Checked := True;
  CoreRadio := TRadioButton.Create(ProfilePage);
  CoreRadio.Parent := ProfilePage.Surface;
  CoreRadio.Left := 0;
  CoreRadio.Top := ClassroomRadio.Top + ScaleY(36);
  CoreRadio.Width := ProfilePage.SurfaceWidth;
  CoreRadio.Caption := 'Core';
  ProfileHelp := TNewStaticText.Create(ProfilePage);
  ProfileHelp.Parent := ProfilePage.Surface;
  ProfileHelp.Left := ScaleX(20);
  ProfileHelp.Top := CoreRadio.Top + ScaleY(34);
  ProfileHelp.Width := ProfilePage.SurfaceWidth - ScaleX(20);
  ProfileHelp.Height := ScaleY(80);
  ProfileHelp.WordWrap := True;
  ProfileHelp.Caption := 'Classroom + OneRoster adds guided roster imports. Core includes the GamGUI dashboard and standard GAM tools.';

  SummaryPage := CreateCustomPage(ProfilePage.ID, 'Review the installation',
    'GamGUI uses fixed per-user locations so updates and recovery stay reliable.');
  SummaryBody := TNewStaticText.Create(SummaryPage);
  SummaryBody.Parent := SummaryPage.Surface;
  SummaryBody.Left := 0;
  SummaryBody.Top := 8;
  SummaryBody.Width := SummaryPage.SurfaceWidth;
  SummaryBody.Height := ScaleY(158);
  SummaryBody.WordWrap := True;
  DetailsButton := TNewButton.Create(SummaryPage);
  DetailsButton.Parent := SummaryPage.Surface;
  DetailsButton.Left := 0;
  DetailsButton.Top := ScaleY(176);
  DetailsButton.Width := ScaleX(150);
  DetailsButton.Caption := 'Show technical details';
  DetailsButton.OnClick := @ToggleDetailsClick;
  DetailsMemo := TNewMemo.Create(SummaryPage);
  DetailsMemo.Parent := SummaryPage.Surface;
  DetailsMemo.Left := 0;
  DetailsMemo.Top := ScaleY(216);
  DetailsMemo.Width := SummaryPage.SurfaceWidth;
  DetailsMemo.Height := ScaleY(84);
  DetailsMemo.ReadOnly := True;
  DetailsMemo.ScrollBars := ssVertical;
  DetailsMemo.Text := 'Windows 11 x64' + #13#10 + 'Per-user installation' + #13#10 +
    'Exact source: {#SourceSha}' + #13#10 + 'Bundled GAM: 7.47.02';
  DetailsMemo.Visible := False;
  UpdateSummary;

  TrustPage := CreateCustomPage(SummaryPage.ID, 'Allow GamGUI to trust its local files',
    'This is the one manual trust step for an unsigned preview installer.');
  TrustTitle := TNewStaticText.Create(TrustPage);
  TrustTitle.Parent := TrustPage.Surface;
  TrustTitle.Left := 0;
  TrustTitle.Top := 8;
  TrustTitle.Width := TrustPage.SurfaceWidth;
  TrustTitle.Height := ScaleY(36);
  TrustTitle.Font.Style := [fsBold];
  TrustTitle.Caption := 'GamGUI creates a private signing identity for this Windows user.';
  TrustBody := TNewStaticText.Create(TrustPage);
  TrustBody.Parent := TrustPage.Surface;
  TrustBody.Left := 0;
  TrustBody.Top := ScaleY(52);
  TrustBody.Width := TrustPage.SurfaceWidth;
  TrustBody.Height := ScaleY(96);
  TrustBody.WordWrap := True;
  TrustBody.Caption := 'The private key cannot be exported. GamGUI uses it to sign and verify this installation and later local updates. Setup will add only the public certificate to this user''s trusted stores. It will never rotate or replace that identity silently.';
  TrustCheck := TNewCheckBox.Create(TrustPage);
  TrustCheck.Parent := TrustPage.Surface;
  TrustCheck.Left := 0;
  TrustCheck.Top := ScaleY(168);
  TrustCheck.Width := TrustPage.SurfaceWidth;
  TrustCheck.Height := ScaleY(44);
  TrustCheck.Caption := 'I understand and allow GamGUI to create and trust this local identity.';
  TrustCheck.Checked := False;

  ProgressReceipt := AddBackslash(FixedData) + 'updates\setup-progress.json';

end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  UninstallDataCheck := TNewCheckBox.Create(UninstallProgressForm);
  UninstallDataCheck.Parent := UninstallProgressForm.InnerPage;
  UninstallDataCheck.Left := ScaleX(20);
  UninstallDataCheck.Top := UninstallProgressForm.StatusLabel.Top + ScaleY(56);
  UninstallDataCheck.Width := UninstallProgressForm.InnerPage.Width - ScaleX(40);
  UninstallDataCheck.Height := ScaleY(40);
  UninstallDataCheck.Caption := 'Also delete local application data';
  UninstallDataCheck.Checked := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if ExistingInstall then
    Result := PageID <> ExistingPage.ID
  else
    Result := PageID = ExistingPage.ID;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  WizardForm.NextButton.Caption := OriginalNextCaption;
  if ExistingInstall and (CurPageID = ExistingPage.ID) then
    WizardForm.NextButton.Caption := 'Close Setup'
  else if CurPageID = TrustPage.ID then
    WizardForm.NextButton.Caption := 'Trust and install';
  if CurPageID = SummaryPage.ID then UpdateSummary;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if ExistingInstall and (CurPageID = ExistingPage.ID) then
  begin
    WizardForm.Close;
    Result := False;
    Exit;
  end;
  if CurPageID = ProfilePage.ID then
  begin
    if CoreRadio.Checked then SelectedProfile := 'core'
    else SelectedProfile := 'classroom-oneroster';
  end;
  if (CurPageID = TrustPage.ID) and (not TrustCheck.Checked) then
  begin
    MsgBox('Nothing has been installed. Check the consent box only when you are ready to create and trust the local GamGUI identity.', mbInformation, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if ExistingInstall then
    Result := 'GamGUI is already installed. Open it or uninstall it before running Setup again.';
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  SelectedProfile := 'classroom-oneroster';
  SilentSigner := '';
  if WizardSilent then
  begin
    if DirExists(FixedCurrent) then
    begin
      Log('Refusing silent setup because GamGUI is already installed.');
      Result := False;
      Exit;
    end;
    SelectedProfile := Lowercase(GetCommandValue('PROFILE'));
    SilentSigner := GetCommandValue('PINNEDSIGNERSHA256');
    if GetCommandValue('CIEPHEMERALSIGNER') <> '' then
    begin
      Log('Refusing a validation-only signer switch in public Setup.');
      Result := False;
      Exit;
    end;
    if ((SelectedProfile <> 'core') and (SelectedProfile <> 'classroom-oneroster')) or
       (not IsHex64(SilentSigner)) then
    begin
      Log('Refusing silent setup because /PROFILE or /PINNEDSIGNERSHA256 is invalid. Silent setup never creates or trusts a certificate.');
      Result := False;
      Exit;
    end;
    if not SilentSignerIsAvailable then
    begin
      Log('Refusing silent setup because the pinned private-key certificate is missing or is not trusted in every required current-user store.');
      Result := False;
      Exit;
    end;
  end;
end;

function ProgressMessage(const Body: String): String;
begin
  if Pos('SETUP-CHECKING-PACKAGE', Body) > 0 then Result := 'Checking package'
  else if Pos('SETUP-CREATING-IDENTITY', Body) > 0 then Result := 'Creating local identity'
  else if Pos('SETUP-SIGNING-FILES', Body) > 0 then Result := 'Signing local files'
  else if Pos('SETUP-VERIFYING-INSTALLATION', Body) > 0 then Result := 'Verifying installation'
  else if Pos('SETUP-RUNNING-SELF-TEST', Body) > 0 then Result := 'Running offline self-test'
  else if Pos('SETUP-INSTALLATION-COMPLETE', Body) > 0 then Result := 'Finishing'
  else Result := 'Preparing GamGUI';
end;

procedure RunTransactionalBackend;
var
  Arguments, Body: String;
  RawBody: AnsiString;
  ErrorCode, WaitCount: Integer;
begin
  if BackendStarted then Exit;
  BackendStarted := True;
  DeleteFile(ProgressReceipt);
  Arguments := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(AddBackslash(ExpandConstant('{tmp}\gamgui-bootstrap')) + 'install.ps1') +
    ' -NoShortcuts -ProgressReceipt ' + AddQuotes(ProgressReceipt) +
    ' -AdditionalFileToSign ' + AddQuotes(ExpandConstant('{uninstallexe}'));
  if WizardSilent then
  begin
    Arguments := Arguments + ' -TrustMode Pretrusted -PretrustedSignerSha256 ' + AddQuotes(SilentSigner);
  end
  else
    Arguments := Arguments + ' -TrustMode Interactive -TrustApproved';

  WizardForm.StatusLabel.Caption := 'Checking package';
  WizardForm.FilenameLabel.Caption := 'Only local files are being installed and verified.';
  if not ShellExec('', 'powershell.exe', Arguments, '', SW_HIDE, ewNoWait, ErrorCode) then
    RaiseException('GamGUI setup could not start its local installation backend.');

  for WaitCount := 1 to 7200 do
  begin
    Sleep(250);
    WizardForm.Refresh;
    if LoadStringFromFile(ProgressReceipt, RawBody) then
    begin
      Body := String(RawBody);
      WizardForm.StatusLabel.Caption := ProgressMessage(Body);
      if Pos('"status":"complete"', Body) > 0 then Exit;
      if Pos('"status":"failed"', Body) > 0 then
        RaiseException('GamGUI could not complete its protected local installation. No working installation was replaced.');
    end;
  end;
  RaiseException('GamGUI setup timed out while waiting for local verification. Run Setup again to reconcile the saved installation journal.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not ExistingInstall) then
    RunTransactionalBackend;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Arguments: String;
  ExitCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Arguments := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
      AddQuotes(ExpandConstant('{localappdata}\GamGUI\updater\uninstall.ps1'));
    if UninstallDataCheck.Checked then Arguments := Arguments + ' -RemoveData';
    if not Exec('powershell.exe', Arguments, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
      MsgBox('GamGUI files could not be fully removed. Local application data was left in place.', mbError, MB_OK);
  end;
end;
