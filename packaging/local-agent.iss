; Inno Setup 6 script for the machine-wide local-agent installer (T13).
;
; Build it with packaging\build-installer.ps1 (it passes /DAppVersion and runs ISCC.exe).
; Input: dist\local-agent\ (the T12 --onedir build). Output: dist\installer\local-agent-<version>-setup.exe.
;
; What it does:
; - Installs for all users, so it needs an administrator (PrivilegesRequired=admin) and goes to
;   {autopf}\local-agent (C:\Program Files\local-agent on 64-bit Windows).
; - Adds that folder to the machine PATH (HKLM ...\Session Manager\Environment, value Path), once.
; - On uninstall, removes exactly that PATH entry and nothing else.
; - Creates nothing under any user profile. All state lives in %APPDATA%\local-agent, made by the app.
;
; NOT COMPILED in the T13 session: Inno Setup was not installed on the build machine. See TASKS.md (T13).

; The default must match pyproject.toml; packaging\build-installer.ps1 always passes the real value.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "local-agent"

[Setup]
; Fixed for the life of the product, so upgrades and uninstalls find the same installation.
AppId={{46E84234-28BF-4ECA-AEAB-BACB30902964}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=local-agent
UninstallDisplayName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
; Machine-wide install: needs an administrator, and the PATH change is HKLM.
PrivilegesRequired=admin
; 64-bit only. Installs to Program Files (not Program Files (x86)) and does not redirect HKLM.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Tells Windows to broadcast the PATH change so new terminals see it (see [Code]).
ChangesEnvironment=yes
OutputDir=..\dist\installer
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\dist\local-agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Code]
const
  // Where Windows keeps the machine PATH.
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

// Two PATH entries are the same folder when they differ only in case and trailing backslashes
// (and spaces around them). Keep this in step with normalize() in tests/installer_path.py.
function NormalizeDir(const Entry: string): string;
var
  T: string;
begin
  T := Trim(Entry);
  while (Length(T) > 0) and (T[Length(T)] = '\') do
  begin
    Delete(T, Length(T), 1);
    T := Trim(T);
  end;
  Result := Lowercase(T);
end;

// True when one of the ';'-separated entries of PathValue is Dir. Empty entries never match.
// The sentinel ';' makes the loop see every entry, including a trailing empty one.
function PathHasDir(const PathValue, Dir: string): Boolean;
var
  Rest, Part, Target: string;
  P: Integer;
begin
  Result := False;
  Target := NormalizeDir(Dir);
  Rest := PathValue + ';';
  P := Pos(';', Rest);
  while P > 0 do
  begin
    Part := Copy(Rest, 1, P - 1);
    Rest := Copy(Rest, P + 1, Length(Rest) - P);
    if (NormalizeDir(Part) <> '') and (NormalizeDir(Part) = Target) then
    begin
      Result := True;
      Exit;
    end;
    P := Pos(';', Rest);
  end;
end;

// PathValue with Dir appended, or unchanged when Dir is already an entry.
function AddDirToPath(const PathValue, Dir: string): string;
begin
  if PathHasDir(PathValue, Dir) then
    Result := PathValue
  else if Trim(PathValue) = '' then
    Result := Dir
  else if PathValue[Length(PathValue)] = ';' then
    Result := PathValue + Dir
  else
    Result := PathValue + ';' + Dir;
end;

// PathValue without the entries that are Dir. Other entries keep their order and text,
// including empty ones, so a trailing ';' survives.
function RemoveDirFromPath(const PathValue, Dir: string): string;
var
  Rest, Part, Target: string;
  P: Integer;
  First: Boolean;
begin
  Target := NormalizeDir(Dir);
  Result := '';
  First := True;
  Rest := PathValue + ';';
  P := Pos(';', Rest);
  while P > 0 do
  begin
    Part := Copy(Rest, 1, P - 1);
    Rest := Copy(Rest, P + 1, Length(Rest) - P);
    if (NormalizeDir(Part) = '') or (NormalizeDir(Part) <> Target) then
    begin
      if First then
        Result := Part
      else
        Result := Result + ';' + Part;
      First := False;
    end;
    P := Pos(';', Rest);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  OldPath, NewPath: string;
begin
  if CurStep = ssPostInstall then
  begin
    if RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', OldPath) then
    begin
      NewPath := AddDirToPath(OldPath, ExpandConstant('{app}'));
      if NewPath <> OldPath then
        RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', NewPath);
    end
    else
      RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', ExpandConstant('{app}'));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  OldPath, NewPath: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', OldPath) then
    begin
      if PathHasDir(OldPath, ExpandConstant('{app}')) then
      begin
        NewPath := RemoveDirFromPath(OldPath, ExpandConstant('{app}'));
        RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', NewPath);
      end;
    end;
  end;
end;
