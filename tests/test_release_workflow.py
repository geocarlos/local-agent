"""T14 release workflow checks: the structure of .github/workflows/release.yml, read as YAML.

These check the properties the release depends on (triggers, tag check, scoped write permission, release only on
tags, artifact names, pinned actions, no untrusted input in scripts). They do not run the workflow: a real run
happens on GitHub (the pull_request trigger is the dry run).
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def triggers(wf):
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    return wf.get("on", wf.get(True))


def _steps(job):
    return job["steps"]


def _run_text(job):
    return "\n".join(step["run"] for step in _steps(job) if "run" in step)


def _all_run_text(wf):
    return "\n".join(_run_text(job) for job in wf["jobs"].values())


# --- triggers ----------------------------------------------------------------------------------


def test_runs_on_version_tag_push_only(triggers):
    assert triggers["push"]["tags"] == ["v*"]
    assert "branches" not in triggers["push"]


def test_pull_request_is_limited_to_release_inputs(triggers):
    paths = triggers["pull_request"]["paths"]
    assert set(paths) == {
        "packaging/**",
        ".github/workflows/release.yml",
        "pyproject.toml",
        "uv.lock",
        "extensions/vscode/**",
    }


def test_manual_dry_run_is_available(triggers):
    assert "workflow_dispatch" in triggers


# --- permissions -------------------------------------------------------------------------------


def test_default_permission_is_read_only(wf):
    assert wf["permissions"] == {"contents": "read"}


def test_no_job_writes_with_the_built_in_token(wf):
    # The release is created with the owner's RELEASE_TOKEN, so the built-in token never needs write access (and a
    # release made with it would show github-actions[bot] as its author).
    writers = [name for name, job in wf["jobs"].items()
               if job.get("permissions", {}).get("contents") == "write"]
    assert writers == []


def test_the_release_is_created_with_the_owners_token_and_stops_without_it(wf):
    publish = wf["jobs"]["publish"]
    steps = _steps(publish)
    create = [step for step in steps if "gh release create" in step.get("run", "")]
    assert create and create[0]["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_TOKEN }}"
    assert "github.token" not in _run_text(publish) and "GITHUB_TOKEN" not in str(publish)
    guard = steps.index(next(step for step in steps if "RELEASE_TOKEN is not set" in step.get("run", "")))
    assert guard < steps.index(create[0])  # it fails before anything is downloaded or published
    assert guard == 0


def test_publish_job_is_only_for_version_tag_pushes(wf):
    publish = wf["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert "github.event_name == 'push'" in publish["if"]
    assert "startsWith(github.ref, 'refs/tags/v')" in publish["if"]


def test_build_job_never_creates_a_release(wf):
    assert "gh release" not in _run_text(wf["jobs"]["build"])
    assert "gh release create" in _run_text(wf["jobs"]["publish"])


# --- build job ---------------------------------------------------------------------------------


def test_build_job_runs_on_windows_with_managed_python(wf):
    build = wf["jobs"]["build"]
    assert build["runs-on"] == "windows-latest"
    assert wf["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"
    text = _run_text(build)
    assert "uv python install 3.14" in text
    assert "uv sync --locked" in text


def test_tests_run_before_the_build(wf):
    text = _run_text(wf["jobs"]["build"])
    assert text.index("uv run --locked pytest -q") < text.index(r".\packaging\build.ps1")


def test_frozen_exe_tests_and_smoke_run_after_the_build(wf):
    text = _run_text(wf["jobs"]["build"])
    build = text.index(r".\packaging\build.ps1")
    assert build < text.index("tests/test_frozen_exe.py") < text.index(r".\packaging\smoke-exe.ps1")
    assert "WILDCAT_CODE_AGENT_EXE" in text


def test_tag_must_equal_the_pyproject_version(wf):
    steps = {step.get("id"): step for step in _steps(wf["jobs"]["build"]) if step.get("id")}
    run = steps["version"]["run"]
    assert "GITHUB_REF_TYPE" in run
    assert 'if ($Tag -ne "v$Version")' in run
    assert "exit 1" in run


def test_installer_and_zip_are_built_and_checksummed(wf):
    text = _run_text(wf["jobs"]["build"])
    assert r".\packaging\build-installer.ps1" in text
    assert "wildcat-code-agent-$Version-win-x64.zip" in text
    assert "wildcat-code-agent-$Version-setup.exe" in text
    assert "SHA256SUMS.txt" in text


def test_the_vsix_is_built_with_npm_package_and_checksummed(wf):
    # T22: the VS Code extension is packaged with `npm run package` (compile, then vsce) and listed with the others.
    build = wf["jobs"]["build"]
    text = _run_text(build)
    assert "npm run package" in text
    assert "npm ci" in text
    assert r"extensions\vscode" in text
    assert "$Extension" in text
    assert "foreach ($File in @($Setup, $Zip, $Extension))" in text
    setup_node = [step for step in _steps(build) if step.get("uses", "").startswith("actions/setup-node@")]
    assert setup_node and setup_node[0]["with"]["node-version"] == 26


def test_the_extension_folder_changes_start_a_dry_run(triggers):
    assert "extensions/vscode/**" in triggers["pull_request"]["paths"]


def test_inno_setup_is_pinned_and_skipped_when_present(wf):
    text = _run_text(wf["jobs"]["build"])
    assert "choco install innosetup --version=6.7.1" in text
    assert "Inno Setup 6\\ISCC.exe" in text


def test_artifacts_are_uploaded_on_every_run(wf):
    uploads = [step for step in _steps(wf["jobs"]["build"])
               if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "wildcat-code-agent-release-${{ steps.version.outputs.version }}"
    assert uploads[0]["with"]["path"] == "dist/release/"
    assert "if" not in uploads[0]  # runs whenever the build succeeded, tag or not


# --- publish job -------------------------------------------------------------------------------


def test_release_attaches_the_release_files_and_marks_prereleases(wf):
    publish = wf["jobs"]["publish"]
    run = _run_text(publish)
    assert "--verify-tag" in run
    assert "release/*" in run
    # No checkout in the publish job, so gh needs the repository from the environment (v0.1.0's first publish failed
    # with "not a git repository" without it).
    assert not any(step.get("uses", "").startswith("actions/checkout@") for step in _steps(publish))
    create = [step for step in _steps(publish) if "gh release create" in step.get("run", "")]
    assert create and create[0]["env"]["GH_REPO"] == "${{ github.repository }}"
    assert "--prerelease" in run
    assert "*-*" in run
    downloads = [step for step in _steps(publish) if step.get("uses", "").startswith("actions/download-artifact@")]
    assert downloads and downloads[0]["with"]["name"] == "wildcat-code-agent-release-${{ needs.build.outputs.version }}"


# --- safety ------------------------------------------------------------------------------------


def test_no_expression_is_interpolated_into_a_script(wf):
    # Values reach scripts through env: (ref names, step outputs), never through ${{ }} in run:.
    assert "${{" not in _all_run_text(wf)


def test_third_party_actions_are_pinned_to_a_commit(wf):
    uses = [step["uses"] for job in wf["jobs"].values() for step in _steps(job) if "uses" in step]
    assert uses, "expected some actions"
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_checkout_does_not_keep_credentials(wf):
    checkout = [step for step in _steps(wf["jobs"]["build"]) if step.get("uses", "").startswith("actions/checkout@")]
    assert checkout[0]["with"]["persist-credentials"] is False


def test_referenced_scripts_exist():
    for rel in ["packaging/build.ps1", "packaging/build-installer.ps1", "packaging/smoke-exe.ps1",
                "packaging/smoke/smoke_stdlib.py", "tests/test_frozen_exe.py"]:
        assert (ROOT / rel).is_file(), rel


def test_smoke_script_checks_the_window_backend_and_the_windowed_exe():
    text = (ROOT / "packaging" / "smoke-exe.ps1").read_text(encoding="utf-8")
    required_line = next(line for line in text.splitlines() if line.startswith("$Required"))
    following = text[text.index(required_line):text.index(required_line) + 300]
    for module in ("webview", "clr", "webview.platforms.winforms"):  # T18: pywebview's Windows backend loads
        assert f'"{module}"' in following
    assert "wildcat-code-agent-gui.exe" in text
    assert "$ConsoleSubsystem -ne 3" in text and "$GuiSubsystem -ne 2" in text  # console vs windowed exe


def test_smoke_script_requires_ctypes_and_starts_serve_web_on_its_own_process():
    text = (ROOT / "packaging" / "smoke-exe.ps1").read_text(encoding="utf-8")
    required_line = next(line for line in text.splitlines() if line.startswith("$Required"))
    assert '"ctypes"' in required_line  # click (uvicorn's dependency) needs it in the frozen app
    assert '"serve", "--web"' in text and "--no-plugins" in text
    assert "WebSocket: ws://127" in text  # the banner the script waits for
    assert "$Web.Kill()" in text  # stops its own process by handle
    assert "Stop-Process" not in text and "taskkill" not in text.lower() and "Get-Process" not in text


def test_smoke_step_runs_the_script_that_checks_wca(wf):
    # T24: the smoke step runs packaging\smoke-exe.ps1, and that script checks wca.exe (--help exit 0, the usage
    # line, a console subsystem). The workflow itself does not need to name wca.
    smoke = [step for step in _steps(wf["jobs"]["build"]) if "smoke-exe.ps1" in step.get("run", "")]
    assert len(smoke) == 1
    text = (ROOT / "packaging" / "smoke-exe.ps1").read_text(encoding="utf-8")
    assert 'Join-Path (Split-Path -Parent $Exe) "wca.exe"' in text
    assert "usage: wca " in text
    assert "Get-PeSubsystem $WcaExe" in text
