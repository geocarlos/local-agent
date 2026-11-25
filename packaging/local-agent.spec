# PyInstaller spec for local-agent: a console app built in --onedir mode.
#
# Run from the repo root with packaging\build.ps1 (it runs `pyinstaller packaging/local-agent.spec`).
# Output: dist/local-agent/local-agent.exe and the files it needs next to it.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is defined by PyInstaller when it reads the spec)

# Packages whose installed metadata is read at runtime (importlib.metadata.version / entry_points).
# Without their dist-info the frozen app cannot find its own version, and the plugin loader's
# entry_points() call would fail.
METADATA = [
    "local-agent",  # User-Agent of web_fetch and the ACP agentInfo version
    "mcp",
    "mcp-types",
    "ollama",
    "httpx",
    "platformdirs",
    "pydantic",
    "anyio",
]

hiddenimports = [
    # Plugin files do `from local_agent.plugin import tool`, which nothing in the app imports statically.
    *collect_submodules("local_agent"),
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

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="local-agent",
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
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="local-agent",
)
