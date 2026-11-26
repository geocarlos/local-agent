# Smoke test for dist\wildcat-code-agent\wildcat-code-agent.exe, run after packaging\build.ps1 (the release workflow runs it).
#
# 1. `wildcat-code-agent.exe --help` exits 0 and prints the usage line.
# 2. `serve --stdio` starts with a temp plugin folder holding packaging\smoke\smoke_stdlib.py. Stdin is empty,
#    so the server gets EOF at once and must exit 0 after loading the plugins.
# 3. The plugin's results file says which standard-library modules import inside the frozen app.
#    REQUIRED modules are the ones packaging\wildcat-code-agent.spec lists in hiddenimports (sqlite3 included), plus
#    ctypes, which the app itself needs: click, a dependency of uvicorn, imports it for `serve --web`, and the
#    window backend (webview, clr, webview.platforms.winforms; T18). A failure fails this script. OPTIONAL modules (bz2, lzma) are reported as warnings only: the build does not promise
#    them yet (see README, "Standard-library DLLs").
# 4. `serve --web` starts on 127.0.0.1 with a temp empty config and an unreachable Ollama. The script waits
#    (90 s at most) for the `WebSocket: ws://127.0.0.1:PORT/...` line on stderr, checks that GET / answers 200,
#    then stops that process by its own handle. A failure here means the WebSocket stack did not load in the exe.
#
# 5. wildcat-code-agent-gui.exe (T18) is a GUI-subsystem program, and its `--help` exits 0. Opening a window is not
#    tested here (a CI runner has no screen to look at); see tests/test_gui_window.py and the T18 notes.
# 6. wca.exe (T24), the short name of the console exe: `wca.exe --help` exits 0, its usage line says `usage: wca `,
#    and it is a console program like wildcat-code-agent.exe.
#
# Usage: packaging\smoke-exe.ps1 [-Exe <path>]   (default: dist\wildcat-code-agent\wildcat-code-agent.exe under the repo root)

param(
    [string]$Exe
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $Root "dist\wildcat-code-agent\wildcat-code-agent.exe" }
if (-not (Test-Path $Exe)) { throw "No exe at $Exe. Run packaging\build.ps1 first." }
$Exe = (Resolve-Path $Exe).Path

$Required = @("sqlite3", "xml.etree.ElementTree", "xml.dom.minidom", "csv", "decimal", "zipfile", "unittest", "ctypes",
    "webview", "clr", "webview.platforms.winforms")
$Optional = @("bz2", "lzma")

$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("wildcat-code-agent-smoke-" + [guid]::NewGuid().ToString("N"))
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
if ($Help.ExitCode -ne 0 -or $HelpText -notmatch "usage: wildcat-code-agent") {
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

# 4. serve --web: the WebSocket stack (uvicorn, websockets, click through uvicorn) must import and listen.
$WebConfig = Join-Path $Work "web-config.toml"
New-Item -ItemType File -Path $WebConfig -Force | Out-Null  # empty: the user's own config is never read
$WebOut = Join-Path $Work "web-out.txt"
$WebErr = Join-Path $Work "web-err.txt"
$Web = Start-Process -FilePath $Exe -PassThru -NoNewWindow -WorkingDirectory $Work `
    -ArgumentList @("serve", "--web", "--port", "0", "--no-plugins", "--cwd", $Work, "--config", $WebConfig, "--host", "http://127.0.0.1:1") `
    -RedirectStandardInput $Empty -RedirectStandardOutput $WebOut -RedirectStandardError $WebErr
$WebPort = $null
$WebText = ""
$Deadline = (Get-Date).AddSeconds(90)
while (-not $WebPort -and -not $Web.HasExited -and (Get-Date) -lt $Deadline) {
    Start-Sleep -Milliseconds 250
    $WebText = (Get-Content $WebErr -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content $WebOut -Raw -ErrorAction SilentlyContinue)
    if ($WebText -match 'WebSocket: ws://127\.0\.0\.1:(\d+)/ws\?token=') { $WebPort = $Matches[1] }
}
if (-not $WebPort) {
    if ($Web.HasExited) { $Failures += "serve --web: exit $($Web.ExitCode) before it listened (log: $WebText)" }
    else { $Failures += "serve --web: no WebSocket line on stderr within 90 s (log: $WebText)" }
} else {
    try {
        $Page = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort/" -UseBasicParsing -TimeoutSec 15
        if ($Page.StatusCode -eq 200) { Write-Host "serve --web: listening on port $WebPort, GET / ok" }
        else { $Failures += "serve --web: GET / returned $($Page.StatusCode)" }
    } catch {
        $Failures += "serve --web: GET / failed: $($_.Exception.Message)"
    }
}
# Stop only the process this script started, through its own handle.
if (-not $Web.HasExited) { $Web.Kill() }
$Web.WaitForExit(30000) | Out-Null

# 5. The windowed exe (T18): wildcat-code-agent-gui.exe must be a GUI-subsystem program (no console window), and
#    `--help` through the gui command must exit 0 (its output goes nowhere, as it has no console).
function Get-PeSubsystem([string]$Path) {
    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    $PeOffset = [BitConverter]::ToInt32($Bytes, 0x3C)
    return [BitConverter]::ToUInt16($Bytes, $PeOffset + 24 + 68)  # 2 = GUI, 3 = console
}

$GuiExe = Join-Path (Split-Path -Parent $Exe) "wildcat-code-agent-gui.exe"
if (-not (Test-Path $GuiExe)) {
    $Failures += "wildcat-code-agent-gui.exe is not in the build folder"
} else {
    $ConsoleSubsystem = Get-PeSubsystem $Exe
    $GuiSubsystem = Get-PeSubsystem $GuiExe
    if ($ConsoleSubsystem -ne 3) { $Failures += "wildcat-code-agent.exe is not a console program (subsystem $ConsoleSubsystem)" }
    if ($GuiSubsystem -ne 2) { $Failures += "wildcat-code-agent-gui.exe is not a windowed program (subsystem $GuiSubsystem)" }
    $GuiHelp = Start-Process -FilePath $GuiExe -ArgumentList "--help" -WorkingDirectory $Work -PassThru
    if (-not $GuiHelp.WaitForExit(120000)) { $GuiHelp.Kill(); $Failures += "wildcat-code-agent-gui.exe --help did not exit within 120 s" }
    elseif ($GuiHelp.ExitCode -ne 0) { $Failures += "wildcat-code-agent-gui.exe --help: exit $($GuiHelp.ExitCode)" }
    else { Write-Host "wildcat-code-agent-gui.exe: windowed subsystem, --help exit 0" }
}

# 6. wca.exe (T24): the console exe under its short name. Same program, so --help must exit 0 and name `wca`.
$WcaExe = Join-Path (Split-Path -Parent $Exe) "wca.exe"
if (-not (Test-Path $WcaExe)) {
    $Failures += "wca.exe is not in the build folder"
} else {
    $WcaHelpOut = Join-Path $Work "wca-help.txt"
    $WcaHelp = Start-Process -FilePath $WcaExe -ArgumentList "--help" -WorkingDirectory $Work -NoNewWindow -PassThru `
        -RedirectStandardOutput $WcaHelpOut -RedirectStandardError (Join-Path $Work "wca-help-err.txt")
    if (-not $WcaHelp.WaitForExit(120000)) { $WcaHelp.Kill(); $Failures += "wca.exe --help did not exit within 120 s" }
    elseif ($WcaHelp.ExitCode -ne 0) { $Failures += "wca.exe --help: exit $($WcaHelp.ExitCode)" }
    elseif ((Get-Content $WcaHelpOut -Raw) -notmatch "usage: wca ") { $Failures += "wca.exe --help: no 'usage: wca' line" }
    elseif ((Get-PeSubsystem $WcaExe) -ne 3) { $Failures += "wca.exe is not a console program (subsystem $(Get-PeSubsystem $WcaExe))" }
    else { Write-Host "wca.exe: --help exit 0, usage names wca, console subsystem" }
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
