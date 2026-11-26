# PyInstaller spec for wildcat-code-agent, in --onedir mode, with three executables that share one folder:
#   wildcat-code-agent.exe      a console app: chat, serve --stdio, serve --web and gui (run from a terminal).
#   wca.exe                     the same console app under the short name `wca` (T24), so it is on PATH after an install.
#   wildcat-code-agent-gui.exe  the same program with no console window (packaging/gui_launcher.py): gui only,
#                        for the Start menu (T18). Its output goes to gui.log in the data folder.
#
# Run from the repo root with packaging\build.ps1 (it runs `pyinstaller packaging/wildcat-code-agent.spec`).
# Output: dist/wildcat-code-agent/ with the three exes and the files they need. MERGE keeps the shared modules and
# binaries (pythonnet, WebView2 loader, uvicorn, ...) in one copy.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is defined by PyInstaller when it reads the spec)

# Packages whose installed metadata is read at runtime (importlib.metadata.version / entry_points).
# Without their dist-info the frozen app cannot find its own version, and the plugin loader's
# entry_points() call would fail.
METADATA = [
    "wildcat-code-agent",  # User-Agent of web_fetch and the ACP agentInfo version
    "mcp",
    "mcp-types",
    "ollama",
    "httpx",
    "platformdirs",
    "pydantic",
    "anyio",
]

hiddenimports = [
    # Plugin files do `from wildcat_code_agent.plugin import tool`, which nothing in the app imports statically.
    *collect_submodules("wildcat_code_agent"),
    # Standard-library modules that plugin files may import. The app does not import them itself, so
    # PyInstaller would not bundle them. (ctypes, bz2 and lzma are not listed: see README.)
    "sqlite3",
    "xml.etree.ElementTree",
    "xml.dom.minidom",
    "unittest",
    "csv",
    "decimal",
    "zipfile",
]

datas = []
for dist in METADATA:
    datas += copy_metadata(dist)
# The web UI (T16): the static page and its scripts. In the bundle they sit in wildcat_code_agent/web, which
# webassets.web_root() finds under sys._MEIPASS.
datas.append((str(ROOT / "src" / "wildcat_code_agent" / "web"), "wildcat_code_agent/web"))
# The desktop window (T18) needs pywebview and its Windows backend (pythonnet, clr_loader, the WebView2
# loader DLLs). Their PyInstaller hooks are in pyinstaller-hooks-contrib (hook-webview, hook-clr,
# hook-clr_loader) and are found automatically; the smoke test imports them in the frozen app.


def _analysis(script: Path) -> Analysis:  # noqa: F821
    return Analysis(  # noqa: F821
        [str(script)],
        pathex=[str(ROOT / "src")],
        binaries=[],
        datas=list(datas),
        hiddenimports=list(hiddenimports),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=["tkinter"],
        noarchive=False,
        optimize=0,
    )


console_analysis = _analysis(ROOT / "packaging" / "launcher.py")
window_analysis = _analysis(ROOT / "packaging" / "gui_launcher.py")
# The second and third names are the script names (the file stems); MERGE then makes both executables share
# the modules and binaries that they have in common.
MERGE(  # noqa: F821
    (console_analysis, "launcher", "launcher"),
    (window_analysis, "gui_launcher", "gui_launcher"),
)

console_pyz = PYZ(console_analysis.pure)  # noqa: F821
console_exe = EXE(  # noqa: F821
    console_pyz,
    console_analysis.scripts,
    [],
    exclude_binaries=True,
    name="wildcat-code-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
window_pyz = PYZ(window_analysis.pure)  # noqa: F821
window_exe = EXE(  # noqa: F821
    window_pyz,
    window_analysis.scripts,
    [],
    exclude_binaries=True,
    name="wildcat-code-agent-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window: the Start menu shortcut opens the window only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# wca.exe (T24): the same console program under the short name. It reuses the console analysis and PYZ, so it has
# the same code and shares _internal\. Its name is the only difference: the program reads its command name from
# sys.argv[0] (frontends/cli.py command_name), which the bootloader sets to the path it was started with.
wca_exe = EXE(  # noqa: F821
    console_pyz,
    console_analysis.scripts,
    [],
    exclude_binaries=True,
    name="wca",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821
    console_exe,
    wca_exe,
    console_analysis.binaries,
    console_analysis.datas,
    window_exe,
    window_analysis.binaries,
    window_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wildcat-code-agent",
)
