# Smoke test for dist\local-agent\local-agent.exe, run after packaging\build.ps1 (the release workflow runs it).
#
# 1. `local-agent.exe --help` exits 0 and prints the usage line.
# 2. `serve --stdio` starts with a temp plugin folder holding packaging\smoke\smoke_stdlib.py. Stdin is empty,
#    so the server gets EOF at once and must exit 0 after loading the plugins.
# 3. The plugin's results file says which standard-library modules import inside the frozen app.
#    REQUIRED modules are the ones packaging\local-agent.spec lists in hiddenimports (sqlite3 included): a failure
#    fails this script. OPTIONAL modules (ctypes, bz2, lzma) are reported as warnings only: the build does not
#    promise them yet (see README, "Standard-library DLLs").
#
# Usage: packaging\smoke-exe.ps1 [-Exe <path>]   (default: dist\local-agent\local-agent.exe under the repo root)

param(
    [string]$Exe
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $Root "dist\local-agent\local-agent.exe" }
if (-not (Test-Path $Exe)) { throw "No exe at $Exe. Run packaging\build.ps1 first." }
$Exe = (Resolve-Path $Exe).Path

$Required = @("sqlite3", "xml.etree.ElementTree", "xml.dom.minidom", "csv", "decimal", "zipfile", "unittest")
$Optional = @("ctypes", "bz2", "lzma")

$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("local-agent-smoke-" + [guid]::NewGuid().ToString("N"))
$PluginDir = Join-Path $Work "plugins"
New-Item -ItemType Directory -Path $PluginDir -Force | Out-Null
$Results = Join-Path $Work "results.json"
$Config = Join-Path $Work "config.toml"
$Empty = Join-Path $Work "empty.txt"
$StdOut = Join-Path $Work "stdout.txt"
$StdErr = Join-Path $Work "stderr.txt"
New-Item -ItemType File -Path $Empty -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot "smoke\smoke_stdlib.py") $PluginDir

# TOML literal strings (single quotes) take Windows paths without escaping.
Set-Content -Path $Config -Value "plugin_dir = '$PluginDir'" -Encoding utf8

$Failures = @()
$Report = @()

# 1. --help
$Help = Start-Process -FilePath $Exe -ArgumentList "--help" -WorkingDirectory $Work -NoNewWindow -PassThru `
    -RedirectStandardOutput (Join-Path $Work "help.txt") -RedirectStandardError (Join-Path $Work "help-err.txt")
if (-not $Help.WaitForExit(120000)) { $Help.Kill(); throw "--help did not exit within 120 s" }
$HelpText = Get-Content (Join-Path $Work "help.txt") -Raw
if ($Help.ExitCode -ne 0 -or $HelpText -notmatch "usage: local-agent") {
    $Failures += "--help: exit $($Help.ExitCode), no usage line"
} else {
    Write-Host "--help: ok"
}

# 2. serve --stdio with plugins, EOF on stdin. The plugin reads its results path from SMOKE_RESULTS.
$env:SMOKE_RESULTS = $Results
$Serve = Start-Process -FilePath $Exe -PassThru -NoNewWindow -WorkingDirectory $Work `
    -ArgumentList @("serve", "--stdio", "--cwd", $Work, "--config", $Config, "--host", "http://127.0.0.1:1") `
    -RedirectStandardInput $Empty -RedirectStandardOutput $StdOut -RedirectStandardError $StdErr
if (-not $Serve.WaitForExit(120000)) { $Serve.Kill(); throw "serve --stdio did not exit after EOF within 120 s" }
Remove-Item Env:\SMOKE_RESULTS -ErrorAction SilentlyContinue
# The startup log may go to either stream, so read both.
$ServeLog = (Get-Content $StdErr, $StdOut -Raw -ErrorAction SilentlyContinue) -join "`n"
if ($Serve.ExitCode -ne 0) { $Failures += "serve --stdio: exit $($Serve.ExitCode) (log: $ServeLog)" }
if ($ServeLog -match "\[plugins\] loaded: .*smoke_stdlib") { Write-Host "plugins: smoke_stdlib loaded" }
else { $Failures += "serve --stdio: the smoke plugin was not reported as loaded (log: $ServeLog)" }

# 3. module results
if (-not (Test-Path $Results)) {
    $Failures += "the smoke plugin did not write its results file"
} else {
    $Table = Get-Content $Results -Raw | ConvertFrom-Json
    foreach ($Name in $Required) {
        $Status = $Table.$Name
        if ($Status -eq "ok") { $Report += "| $Name | required | ok |" }
        else { $Report += "| $Name | required | FAIL: $Status |"; $Failures += "required module ${Name}: $Status" }
    }
    foreach ($Name in $Optional) {
        $Status = $Table.$Name
        if ($Status -eq "ok") { $Report += "| $Name | optional | ok |" }
        else {
            $Report += "| $Name | optional | not bundled: $Status |"
            Write-Host "::warning title=Optional module not bundled::$Name did not import in the frozen app: $Status"
        }
    }
}

Write-Host "Module results (plugin imports in the frozen app):"
$Report | ForEach-Object { Write-Host $_ }

if ($env:GITHUB_STEP_SUMMARY) {
    $Lines = @("### Frozen exe smoke test", "", "| Module | Kind | Result |", "|---|---|---|") + $Report
    Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value ($Lines -join "`n")
}

Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue

if ($Failures.Count -gt 0) {
    $Failures | ForEach-Object { Write-Host "::error::$_" }
    exit 1
}
Write-Host "Smoke test passed."
