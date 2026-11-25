# Builds local-agent.exe (PyInstaller, --onedir) into dist\local-agent\.
# Run it from any folder; it works from the repo root.
#
# Requires uv on PATH. The build tool is the `pyinstaller` dev dependency, so nothing else is installed.
# Build time and size of the last run are recorded in TASKS.md (T12 notes).
#
# Close any running local-agent.exe started from the repo's .venv first: Windows locks it, and `uv sync` fails
# with "being used by another process".
# With Windows PowerShell 5.1 and a restrictive execution policy, run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1
# (pwsh 7 runs packaging\build.ps1 directly.)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Syncing locked dependencies (uv sync --locked)"
uv sync --locked
if ($LASTEXITCODE -ne 0) { throw "uv sync --locked failed" }

Write-Host "Running PyInstaller with packaging/local-agent.spec"
uv run pyinstaller packaging/local-agent.spec --noconfirm --clean --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Exe = Join-Path $Root "dist\local-agent\local-agent.exe"
if (-not (Test-Path $Exe)) { throw "Expected $Exe after the build" }

$Bytes = (Get-ChildItem (Join-Path $Root "dist\local-agent") -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Built {0} ({1:N1} MB in dist\local-agent)" -f $Exe, ($Bytes / 1MB))
