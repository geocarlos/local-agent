"""T13 installer checks: PATH edit rules (via the Python mirror of the .iss code), and the .iss and build script text.

No Inno Setup compiler and no build runs here.
"""

import re
import tomllib
from pathlib import Path

import pytest

from installer_path import add_dir, normalize, path_has_dir, remove_dir

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "packaging" / "local-agent.iss"
BUILD_INSTALLER = ROOT / "packaging" / "build-installer.ps1"
DIR = r"C:\Program Files\local-agent"


# --- PATH add -------------------------------------------------------------------------------


def test_add_to_empty_path():
    assert add_dir("", DIR) == DIR


def test_add_to_whitespace_only_path():
    assert add_dir("   ", DIR) == DIR


def test_add_appends_with_semicolon():
    assert add_dir(r"C:\Windows;C:\Tools", DIR) == rf"C:\Windows;C:\Tools;{DIR}"


def test_add_after_trailing_semicolon_adds_no_extra_separator():
    assert add_dir(r"C:\Windows;", DIR) == rf"C:\Windows;{DIR}"


def test_add_is_idempotent():
    once = add_dir(r"C:\Windows", DIR)
    assert add_dir(once, DIR) == once


@pytest.mark.parametrize(
    "existing",
    [
        DIR,
        DIR + "\\",
        DIR.upper(),
        DIR.lower() + "\\\\",
        DIR + ";",
        "  " + DIR + "  ",
    ],
)
def test_add_skips_existing_entry_in_any_variant(existing):
    value = rf"C:\Windows;{existing};C:\Tools"
    assert add_dir(value, DIR) == value


def test_add_skips_entry_in_the_middle():
    value = rf"C:\A;{DIR};C:\B"
    assert add_dir(value, DIR) == value


def test_add_does_not_treat_a_longer_path_as_a_match():
    value = r"C:\Program Files\local-agent-tools"
    assert add_dir(value, DIR) == rf"{value};{DIR}"


def test_add_does_not_treat_a_subfolder_as_a_match():
    value = rf"{DIR}\bin"
    assert add_dir(value, DIR) == rf"{value};{DIR}"


def test_add_keeps_empty_entries_that_already_exist():
    assert add_dir(r"C:\A;;C:\B", DIR) == rf"C:\A;;C:\B;{DIR}"


# --- PATH remove ----------------------------------------------------------------------------


def test_remove_from_empty_path():
    assert remove_dir("", DIR) == ""


def test_remove_absent_entry_changes_nothing():
    value = r"C:\Windows;C:\Tools"
    assert remove_dir(value, DIR) == value


def test_remove_only_entry_gives_empty_path():
    assert remove_dir(DIR, DIR) == ""


def test_remove_from_start():
    assert remove_dir(rf"{DIR};C:\Windows", DIR) == r"C:\Windows"


def test_remove_from_end():
    assert remove_dir(rf"C:\Windows;{DIR}", DIR) == r"C:\Windows"


def test_remove_from_middle_keeps_order_of_the_rest():
    assert remove_dir(rf"C:\A;{DIR};C:\B;C:\C", DIR) == r"C:\A;C:\B;C:\C"


@pytest.mark.parametrize(
    "variant",
    [DIR, DIR + "\\", DIR.upper(), DIR.lower(), DIR + "\\\\", "  " + DIR + " "],
)
def test_remove_matches_any_variant(variant):
    assert remove_dir(rf"C:\A;{variant};C:\B", DIR) == r"C:\A;C:\B"


def test_remove_keeps_trailing_semicolon_of_the_original():
    assert remove_dir(rf"C:\A;{DIR};", DIR) == r"C:\A;"


def test_remove_keeps_similar_paths():
    value = rf"{DIR}-old;{DIR}\bin;{DIR}"
    assert remove_dir(value, DIR) == rf"{DIR}-old;{DIR}\bin"


def test_remove_removes_every_duplicate():
    assert remove_dir(rf"{DIR};C:\A;{DIR}", DIR) == r"C:\A"


def test_add_then_remove_restores_a_path_without_trailing_semicolon():
    original = r"C:\Windows;C:\Tools"
    assert remove_dir(add_dir(original, DIR), DIR) == original


def test_remove_never_touches_other_entries_on_a_real_style_path():
    original = r"C:\WINDOWS\system32;C:\Program Files\Git\cmd;%SystemRoot%\System32\OpenSSH"
    assert remove_dir(add_dir(original, DIR), DIR) == original


def test_normalize_drops_trailing_backslashes_and_case():
    assert normalize(r"  C:\Foo\\ ") == r"c:\foo"


def test_path_has_dir_ignores_empty_entries():
    assert path_has_dir(";;", "") is False


# --- The .iss file --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def iss_text():
    return ISS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "directive",
    [
        "PrivilegesRequired=admin",
        "ChangesEnvironment=yes",
        "ArchitecturesAllowed=x64compatible",
        "ArchitecturesInstallIn64BitMode=x64compatible",
        "DefaultDirName={autopf}\\{#AppName}",
        "Source: \"..\\dist\\local-agent\\*\"",
        "OutputBaseFilename={#AppName}-{#AppVersion}-setup",
    ],
)
def test_iss_has_required_directive(iss_text, directive):
    assert directive in iss_text


def test_iss_uses_the_machine_path_key(iss_text):
    assert "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment" in iss_text
    assert "HKEY_LOCAL_MACHINE" in iss_text
    assert "HKEY_CURRENT_USER" not in iss_text
    assert "HKCU" not in iss_text


def test_iss_never_writes_under_a_user_profile(iss_text):
    for token in ("{userappdata}", "{localappdata}", "{userdocs}", "{usersendto}", "{userpf}", "{userprograms}"):
        assert token not in iss_text


def test_iss_app_id_is_a_fixed_guid(iss_text):
    match = re.search(r"^AppId=\{\{([0-9A-Fa-f-]{36})\}", iss_text, re.MULTILINE)
    assert match, "AppId must be a GUID"


def test_iss_default_version_matches_pyproject(iss_text):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    match = re.search(r'#define AppVersion "([^"]+)"', iss_text)
    assert match and match.group(1) == pyproject["project"]["version"]


def test_iss_has_the_path_functions(iss_text):
    for name in ("AddDirToPath", "RemoveDirFromPath", "PathHasDir", "CurStepChanged", "CurUninstallStepChanged"):
        assert f"function {name}" in iss_text or f"procedure {name}" in iss_text


def test_iss_removes_path_only_after_uninstall(iss_text):
    assert "usPostUninstall" in iss_text


# --- The build script -----------------------------------------------------------------------


def test_build_installer_script_exists_and_names_iscc():
    text = BUILD_INSTALLER.read_text(encoding="utf-8")
    assert "ISCC.exe" in text
    assert "Inno Setup 6" in text
    assert "JRSoftware.InnoSetup" in text
    assert "/DAppVersion=" in text
    assert "dist\\installer\\local-agent-$Version-setup.exe" in text


def test_build_installer_warns_when_reused_exe_is_older_than_src():
    text = BUILD_INSTALLER.read_text(encoding="utf-8")
    assert "LastWriteTime" in text
    assert "under src\\ are newer than it" in text
    assert "-Rebuild to rebuild" in text
