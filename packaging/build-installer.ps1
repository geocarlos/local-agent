# Builds the machine-wide installer: dist\installer\local-agent-<version>-setup.exe.
# Run it from any folder; it works from the repo root.
#
# Steps: build dist\local-agent with packaging\build.ps1 (only when it is missing, or with -Rebuild),
# then compile packaging\local-agent.iss with Inno Setup 6 (ISCC.exe). The version comes from pyproject.toml.
#
# Requires Inno Setup 6 (ISCC.exe). It is not bundled and is not installed by this script:
#   winget install --id JRSoftware.InnoSetup -e
#   or download it from https://jrsoftware.org/isdl.php
# With Windows PowerShell 5.1 and a restrictive execution policy, run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build-installer.ps1

param(
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-InnoSetupCompiler {
    $Candidates = @()
    if (${env:ProgramFiles(x86)}) { $Candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
    if ($env:ProgramFiles) { $Candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }
    if ($env:LOCALAPPDATA) { $Candidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe" }
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) { return $Candidate }
    }
    $OnPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($OnPath) { return $OnPath.Source }
    return $null
}

$Pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
if ($Pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { throw "No version = ""..."" line in pyproject.toml" }
$Version = $Matches[1]

# Find the compiler before the slow build, so a missing install fails at once.
$Iscc = Find-InnoSetupCompiler
if (-not $Iscc) {
    throw @"
Inno Setup 6 (ISCC.exe) was not found. It is looked for in:
  %ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
  %ProgramFiles%\Inno Setup 6\ISCC.exe
  %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
  and on PATH.
Install it, then run this script again:
  winget install --id JRSoftware.InnoSetup -e
  (or download it from https://jrsoftware.org/isdl.php)
"@
}
Write-Host "Using $Iscc"

$Exe = Join-Path $Root "dist\local-agent\local-agent.exe"
if ($Rebuild -or -not (Test-Path $Exe)) {
    Write-Host "Building dist\local-agent with packaging\build.ps1"
    & (Join-Path $PSScriptRoot "build.ps1")
} else {
    # Reusing an existing build: warn if the source changed after it was built.
    $ExeTime = (Get-Item $Exe).LastWriteTime
    Write-Host ("Reusing {0} (built {1:yyyy-MM-dd HH:mm:ss})" -f $Exe, $ExeTime)
    $Newer = @(Get-ChildItem -Path (Join-Path $Root "src") -Recurse -File |
        Where-Object { $_.LastWriteTime -gt $ExeTime -and $_.FullName -notmatch '\\__pycache__\\' })
    if ($Newer.Count -gt 0) {
        $Names = ($Newer | Select-Object -First 3 -ExpandProperty Name) -join ", "
        Write-Warning ("The exe may be stale: {0} file(s) under src\ are newer than it, for example {1}. Run again with -Rebuild to rebuild dist\local-agent." -f $Newer.Count, $Names)
    }
}
if (-not (Test-Path $Exe)) { throw "Expected $Exe. Run packaging\build.ps1 first." }

$Script = Join-Path $PSScriptRoot "local-agent.iss"
Write-Host "Compiling $Script (version $Version)"
& $Iscc "/DAppVersion=$Version" $Script
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$Setup = Join-Path $Root "dist\installer\local-agent-$Version-setup.exe"
if (-not (Test-Path $Setup)) { throw "Expected $Setup after the compile" }
$Megabytes = (Get-Item $Setup).Length / 1MB
Write-Host ("Built {0} ({1:N1} MB)" -f $Setup, $Megabytes)
