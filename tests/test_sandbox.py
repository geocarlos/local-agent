"""Workspace sandbox: path resolution (sandbox.resolve), and enforcement through the loop.

Windows-only cases are marked; junction and short-name tests skip with the reason
when this machine cannot create the fixture (for example no 8.3 names on a volume).
"""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import text_reply, tool_reply

from wildcat_code_agent import sandbox, tools
from wildcat_code_agent.config import Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import ApprovalRequest, Decision, ToolResult
from wildcat_code_agent.frontends import cli
from wildcat_code_agent.permissions import Risk
from wildcat_code_agent.tools import TOOLS, tool
from wildcat_code_agent.tools.registry import ToolContext

NT = os.name == "nt"
requires_windows = pytest.mark.skipif(not NT, reason="Windows path rules")
SECRET = "outside secret"


# ---------------------------------------------------------------- fixtures and helpers

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """The workspace folder, and the process cwd (set to a different folder, to show the cwd is not used)."""
    folder = tmp_path / "work folder"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return folder


@pytest.fixture
def outside(tmp_path):
    """A file outside the workspace: tmp_path/outside/secret.txt."""
    folder = tmp_path / "outside"
    folder.mkdir()
    secret = folder / "secret.txt"
    secret.write_text(SECRET, encoding="utf-8")
    return secret


class Scripted:
    """Approver that answers from a list of decisions (default: refuse), and records each request."""

    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decisions.pop(0) if self.decisions else Decision.REJECT_ONCE


ALLOW = Decision.ALLOW_ONCE
REJECT = Decision.REJECT_ONCE


def run(client, ws: Path, approver=None, text: str = "go"):
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": text})
    loop.run_turn(messages, client=client, settings=Settings(workspace=ws), emit=events.append,
                  approver=approver)
    return messages, events


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


def real(path) -> str:
    return os.path.realpath(path)


def make_junction(link: Path, target: Path) -> None:
    if not NT:
        pytest.skip("junctions are a Windows feature")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"cannot create a junction here: {(done.stdout + done.stderr).strip()}")


def short_name(path: Path) -> str:
    """The 8.3 name of an existing path, or skip if this volume has none."""
    if not NT:
        pytest.skip("8.3 short names are a Windows feature")
    buf = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    if length == 0 or "~" not in buf.value:  # 8.3 names carry a "~N" tail
        pytest.skip(f"no 8.3 short name for {path} on this volume")
    return buf.value


# ---------------------------------------------------------------- resolve(): inside and outside

def test_relative_and_absolute_paths_inside_the_workspace(ws):
    (ws / "a.txt").write_text("x", encoding="utf-8")

    relative = sandbox.resolve("a.txt", ws)
    absolute = sandbox.resolve(str(ws / "a.txt"), ws)

    assert relative.inside and absolute.inside
    assert relative.path == Path(real(ws / "a.txt"))
    assert absolute.path == relative.path


def test_the_workspace_itself_is_inside(ws):
    assert sandbox.resolve(".", ws).inside
    assert sandbox.resolve(str(ws), ws).inside


def test_new_file_in_a_new_folder_inside_is_inside(ws):
    assert sandbox.resolve("new dir/new.txt", ws).inside


def test_forward_and_back_slashes_are_the_same_path(ws):
    assert sandbox.resolve("sub/a.txt", ws).path == sandbox.resolve("sub\\a.txt", ws).path


def test_dotdot_that_climbs_out_is_outside(ws, outside):
    item = sandbox.resolve("../outside/secret.txt", ws)

    assert not item.inside
    assert item.path == Path(real(outside))


def test_dotdot_that_stays_inside_is_inside(ws):
    assert sandbox.resolve("sub/../a.txt", ws).inside


def test_absolute_path_outside(ws, outside):
    assert not sandbox.resolve(str(outside), ws).inside


def test_prefix_trick_is_outside(ws, tmp_path):
    evil = tmp_path / "work folder-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text(SECRET, encoding="utf-8")

    assert not sandbox.resolve(str(evil / "secret.txt"), ws).inside


def test_the_parent_of_the_workspace_is_outside(ws):
    assert not sandbox.resolve("..", ws).inside
    assert not sandbox.resolve(str(ws.parent), ws).inside


def test_empty_nul_and_non_text_paths_are_refused(ws):
    for bad in ("", "a\x00b", 5, None, ["a.txt"]):
        with pytest.raises(sandbox.PathRefused):
            sandbox.resolve(bad, ws)


def test_the_check_uses_the_workspace_not_the_process_cwd(ws, outside, monkeypatch):
    # The process cwd is `elsewhere`; `a.txt` must mean ws/a.txt, not elsewhere/a.txt.
    (ws / "a.txt").write_text("in workspace", encoding="utf-8")
    (Path.cwd() / "a.txt").write_text("in cwd", encoding="utf-8")

    item = sandbox.resolve("a.txt", ws)

    assert item.inside
    assert item.path == Path(real(ws / "a.txt"))


# ---------------------------------------------------------------- Windows: drives, UNC, prefixes

@requires_windows
def test_case_of_the_path_and_the_drive_letter_is_ignored(ws):
    assert sandbox.resolve(str(ws).swapcase() + "\\a.txt", ws).inside


@requires_windows
def test_drive_relative_path_on_the_workspace_drive_is_relative_to_the_workspace(ws):
    drive = ws.drive  # e.g. "C:"
    item = sandbox.resolve(f"{drive}a.txt", ws)

    assert item.inside
    assert item.path == Path(real(ws / "a.txt"))


@requires_windows
def test_drive_relative_path_on_another_drive_is_outside(ws):
    other = "Z:" if ws.drive.upper() != "Z:" else "Y:"

    item = sandbox.resolve(f"{other}secret.txt", ws)

    assert not item.inside


@requires_windows
def test_root_relative_path_is_on_the_workspace_drive_root(ws):
    # "\\x.txt" means the root of the workspace's drive, which is outside a folder on that drive.
    assert not sandbox.resolve("\\x.txt", ws).inside


@requires_windows
def test_unc_path_is_outside(ws):
    assert not sandbox.resolve("\\\\nonexistent-host.invalid\\share\\a.txt", ws).inside


@requires_windows
def test_verbatim_prefix_is_removed_before_the_check(ws, outside):
    assert sandbox.resolve("\\\\?\\" + str(ws / "a.txt"), ws).inside
    assert not sandbox.resolve("\\\\?\\" + str(outside), ws).inside


@requires_windows
def test_verbatim_unc_prefix_is_outside(ws):
    item = sandbox.resolve("\\\\?\\UNC\\nonexistent-host.invalid\\share\\a.txt", ws)

    assert not item.inside
    assert str(item.path).startswith("\\\\nonexistent-host.invalid\\share")


@requires_windows
def test_device_namespace_paths_are_refused(ws):
    with pytest.raises(sandbox.PathRefused):
        sandbox.resolve("\\\\.\\C:\\a.txt", ws)


# ---------------------------------------------------------------- Windows: names Windows rewrites

@requires_windows
def test_alternate_data_stream_names_are_refused(ws):
    for bad in ("a.txt:stream", "sub\\a.txt:Zone.Identifier", str(ws / "a.txt") + ":hidden"):
        with pytest.raises(sandbox.PathRefused, match="alternate data streams"):
            sandbox.resolve(bad, ws)


@requires_windows
def test_names_ending_in_dot_or_space_are_refused(ws):
    # Windows drops trailing dots and spaces, so "jn." can open the junction "jn".
    for bad in ("a.txt.", "a.txt ", "sub./a.txt", "sub /a.txt", "..."):
        with pytest.raises(sandbox.PathRefused, match="dot or space"):
            sandbox.resolve(bad, ws)


@requires_windows
def test_reserved_device_names_are_refused(ws):
    for bad in ("NUL", "con", "sub\\PRN", "COM1.txt", "lpt9", "aux.log"):
        with pytest.raises(sandbox.PathRefused, match="reserved"):
            sandbox.resolve(bad, ws)


@requires_windows
def test_names_that_only_look_like_reserved_names_are_fine(ws):
    assert sandbox.resolve("CONSOLE.txt", ws).inside
    assert sandbox.resolve("comment.txt", ws).inside


@requires_windows
def test_8_3_short_name_of_an_inside_file_is_inside(ws):
    target = ws / "a long workspace file name.txt"
    target.write_text("x", encoding="utf-8")
    short = Path(short_name(target))  # the 8.3 form of the file's own path, folders included

    item = sandbox.resolve(str(short), ws)

    assert item.inside
    assert item.path == Path(real(target))


@requires_windows
def test_8_3_short_name_of_the_workspace_itself_is_inside(ws):
    short = Path(short_name(ws))

    assert sandbox.resolve(str(short / "new.txt"), ws).inside


@requires_windows
def test_8_3_short_name_of_an_outside_folder_is_outside(ws, tmp_path):
    folder = tmp_path / "outside folder with a long name"
    folder.mkdir()
    (folder / "secret.txt").write_text(SECRET, encoding="utf-8")
    short = Path(short_name(folder)) / "secret.txt"

    assert not sandbox.resolve(str(short), ws).inside


# ---------------------------------------------------------------- junctions and symlinks

@requires_windows
def test_junction_inside_the_workspace_pointing_outside_is_outside(ws, outside):
    link = ws / "link"
    make_junction(link, outside.parent)

    assert not sandbox.resolve("link", ws).inside
    assert not sandbox.resolve("link/secret.txt", ws).inside
    assert not sandbox.resolve(str(link / "secret.txt"), ws).inside


@requires_windows
def test_junction_reached_by_its_short_name_is_outside(ws, outside):
    link = ws / "a long junction name"
    make_junction(link, outside.parent)
    short = Path(short_name(link))

    assert not sandbox.resolve(str(short / "secret.txt"), ws).inside


@requires_windows
def test_dotdot_after_a_junction_follows_win32_rules(ws):
    # Win32 applies ".." before following the junction, so "link\\..\\a.txt" opens ws\a.txt.
    # The check resolves it the same way, so it is inside (verified against open() on Windows 11).
    (ws / "a.txt").write_text("in workspace", encoding="utf-8")
    make_junction(ws / "link", ws.parent)

    item = sandbox.resolve("link/../a.txt", ws)

    assert item.inside
    assert item.path == Path(real(ws / "a.txt"))


@requires_windows
def test_a_junction_to_a_folder_inside_the_workspace_is_inside(ws):
    inner = ws / "inner"
    inner.mkdir()
    make_junction(ws / "alias", inner)

    assert sandbox.resolve("alias/a.txt", ws).inside


def test_file_symlink_inside_pointing_outside_is_outside(ws, outside):
    link = ws / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as e:
        pytest.skip(f"cannot create a symlink here (needs Developer Mode or admin on Windows): {e}")

    assert not sandbox.resolve("link.txt", ws).inside


# ---------------------------------------------------------------- check_arguments and requests

def test_check_arguments_rewrites_paths_and_lists_outside_ones(ws, outside):
    args, outside_items = sandbox.check_arguments(
        ("path",), {"path": "../outside/secret.txt", "content": "x"}, ws)

    assert args["path"] == real(outside)
    assert args["content"] == "x"
    assert [item.inside for item in outside_items] == [False]


def test_check_arguments_skips_missing_path_arguments(ws):
    args, outside_items = sandbox.check_arguments(("path",), {"command": "dir"}, ws)

    assert args == {"command": "dir"}
    assert outside_items == []


def test_outside_request_names_the_resolved_path_and_the_workspace(ws, outside):
    item = sandbox.resolve("../outside/secret.txt", ws)

    request = sandbox.outside_request("1.1", "read_file", "read", item, ws)

    assert request.scope == "outside_workspace"
    assert request.tool_name == "read_file"
    assert real(outside) in request.title
    assert real(ws) in request.title
    assert "requested as '../outside/secret.txt'" in request.title


def test_renderer_shows_an_outside_banner_only_for_outside_requests(capsys, ws, outside):
    item = sandbox.resolve(str(outside), ws)
    outside_request = sandbox.outside_request("1.1", "read_file", "read", item, ws)
    tool_request = ApprovalRequest(tool_call_id="1.1", tool_name="run_command", risk="exec", title="Run")

    cli.Renderer()(tool_request)
    assert "OUTSIDE WORKSPACE" not in capsys.readouterr().out

    cli.Renderer()(outside_request)
    assert "[OUTSIDE WORKSPACE] read_file" in capsys.readouterr().out


# ---------------------------------------------------------------- enforcement through the loop

def test_inside_read_asks_nothing_for_relative_or_absolute_paths(fake_ollama, ws):
    (ws / "a.txt").write_text("inside body", encoding="utf-8")
    approver = Scripted()
    client = fake_ollama([tool_reply(("read_file", {"path": "a.txt"})),
                          tool_reply(("read_file", {"path": str(ws / "a.txt")})),
                          text_reply("done")])

    _, events = run(client, ws, approver)

    assert approver.requests == []
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.content for r in results] == ["inside body", "inside body"]


def test_outside_read_approved_runs_with_the_resolved_path(fake_ollama, ws, outside):
    client = fake_ollama([tool_reply(("read_file", {"path": "../outside/secret.txt"})), text_reply("ok")])
    approver = Scripted(ALLOW)

    _, events = run(client, ws, approver)

    assert len(approver.requests) == 1
    request = approver.requests[0]
    assert request.scope == "outside_workspace"
    assert real(outside) in request.title
    assert result_of(events).content == SECRET
    assert result_of(events).status == "completed"


def test_outside_read_refused_does_not_read_the_file(fake_ollama, ws, outside):
    client = fake_ollama([tool_reply(("read_file", {"path": str(outside)})), text_reply("ok")])
    approver = Scripted(REJECT)

    messages, events = run(client, ws, approver)

    assert len(approver.requests) == 1
    assert SECRET not in result_of(events).content
    assert result_of(events).content.startswith("Access to ")
    assert "was not approved" in result_of(events).content
    assert result_of(events).status == "failed"
    assert messages[-2]["role"] == "tool"  # the model gets the message back as the tool result


def test_outside_read_with_no_approver_is_denied(fake_ollama, ws, outside):
    client = fake_ollama([tool_reply(("read_file", {"path": str(outside)})), text_reply("ok")])

    _, events = run(client, ws, approver=None)

    assert SECRET not in result_of(events).content
    assert "was not approved" in result_of(events).content


def test_prefix_trick_asks_for_approval(fake_ollama, ws, tmp_path):
    evil = tmp_path / "work folder-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text(SECRET, encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(evil / "secret.txt")})), text_reply("ok")])
    approver = Scripted(REJECT)

    _, events = run(client, ws, approver)

    assert len(approver.requests) == 1
    assert SECRET not in result_of(events).content


def test_write_outside_asks_twice_and_writes_only_when_both_are_approved(fake_ollama, ws, tmp_path):
    target = tmp_path / "outside" / "new.txt"
    client = fake_ollama([tool_reply(("write_file", {"path": str(target), "content": "hi"})),
                          text_reply("saved")])
    approver = Scripted(ALLOW, ALLOW)

    _, events = run(client, ws, approver)

    assert [r.scope for r in approver.requests] == ["outside_workspace", "tool"]
    assert approver.requests[1].title == f"Write 2 chars to '{real(target)}'"
    assert target.read_text(encoding="utf-8") == "hi"
    assert result_of(events).status == "completed"


def test_write_outside_refused_at_the_first_question_never_asks_for_the_write(fake_ollama, ws, tmp_path):
    target = tmp_path / "outside" / "new.txt"
    client = fake_ollama([tool_reply(("write_file", {"path": str(target), "content": "hi"})),
                          text_reply("ok")])
    approver = Scripted(REJECT)

    _, events = run(client, ws, approver)

    assert [r.scope for r in approver.requests] == ["outside_workspace"]
    assert not target.exists()
    assert "was not approved" in result_of(events).content


def test_write_outside_approved_but_write_refused_writes_nothing(fake_ollama, ws, tmp_path):
    target = tmp_path / "outside" / "new.txt"
    client = fake_ollama([tool_reply(("write_file", {"path": str(target), "content": "hi"})),
                          text_reply("ok")])
    approver = Scripted(ALLOW, REJECT)

    _, events = run(client, ws, approver)

    assert [r.scope for r in approver.requests] == ["outside_workspace", "tool"]
    assert not target.exists()
    assert result_of(events).content == "User denied file write."


def test_write_inside_asks_only_for_the_write(fake_ollama, ws):
    target = ws / "new.txt"
    client = fake_ollama([tool_reply(("write_file", {"path": "new.txt", "content": "hi"})), text_reply("ok")])
    approver = Scripted(ALLOW)

    run(client, ws, approver)

    assert [r.scope for r in approver.requests] == ["tool"]
    assert target.read_text(encoding="utf-8") == "hi"


def test_outside_write_without_an_approver_is_refused(fake_ollama, ws, tmp_path):
    target = tmp_path / "outside" / "new.txt"
    client = fake_ollama([tool_reply(("write_file", {"path": str(target), "content": "hi"})), text_reply("ok")])

    _, events = run(client, ws, approver=None)

    assert not target.exists()
    assert "was not approved" in result_of(events).content


def test_a_refused_path_never_reaches_the_approver(fake_ollama, ws):
    client = fake_ollama([tool_reply(("read_file", {"path": "a.txt.:secret"})), text_reply("ok")])
    approver = Scripted(ALLOW)

    _, events = run(client, ws, approver)

    assert approver.requests == []
    assert result_of(events).content.startswith("Refused: ")
    assert result_of(events).status == "failed"


def test_a_non_text_path_is_refused_without_asking(fake_ollama, ws):
    client = fake_ollama([tool_reply(("read_file", {"path": 5})), text_reply("ok")])
    approver = Scripted(ALLOW)

    _, events = run(client, ws, approver)

    assert approver.requests == []
    assert result_of(events).content.startswith("Refused: a path must be text")


def test_tool_receives_the_resolved_absolute_path(fake_ollama, ws, monkeypatch):
    seen: list[str] = []
    table: dict = {}

    @tool(risk=Risk.READ, paths=("path",), registry=table)
    def probe(path: str) -> str:
        """Record the path the tool receives."""
        seen.append(path)
        return "ok"

    monkeypatch.setattr(tools, "TOOLS", table)
    client = fake_ollama([tool_reply(("probe", {"path": "sub/../a.txt"})), text_reply("ok")])

    run(client, ws)

    assert seen == [real(ws / "a.txt")]


def test_tools_without_path_arguments_are_not_checked(fake_ollama, ws, monkeypatch):
    seen: list = []
    table: dict = {}

    @tool(risk=Risk.READ, registry=table)
    def echo(path: str) -> str:
        """Echo the argument unchanged."""
        seen.append(path)
        return "ok"

    monkeypatch.setattr(tools, "TOOLS", table)
    client = fake_ollama([tool_reply(("echo", {"path": "../far/away"})), text_reply("ok")])
    approver = Scripted()

    run(client, ws, approver)

    assert approver.requests == []
    assert seen == ["../far/away"]


def test_run_command_starts_in_the_workspace_not_the_cwd(fake_ollama, ws):
    command = f'& "{sys.executable}" -c "import os; print(os.getcwd())"'  # PowerShell (the default) needs &
    client = fake_ollama([tool_reply(("run_command", {"command": command, "shell": "powershell"})),
                          text_reply("ok")])
    approver = Scripted(ALLOW)

    _, events = run(client, ws, approver)

    assert os.path.normcase(real(ws)) in os.path.normcase(result_of(events).content)
    assert os.path.normcase(real(Path.cwd())) not in os.path.normcase(result_of(events).content)


def test_run_command_default_context_starts_in_the_cwd():
    assert ToolContext().workspace == Path.cwd()
    assert TOOLS["run_command"].path_args == ()


def test_read_file_and_write_file_declare_their_path_argument():
    assert TOOLS["read_file"].path_args == ("path",)
    assert TOOLS["write_file"].path_args == ("path",)


def test_paths_must_name_a_str_parameter():
    def no_such(path: str) -> str:
        """Doc."""
        return path

    def not_text(count: int) -> str:
        """Doc."""
        return str(count)

    with pytest.raises(TypeError, match="not a parameter"):
        tool(risk=Risk.READ, paths=("nope",), registry={})(no_such)
    with pytest.raises(TypeError, match="must be str"):
        tool(risk=Risk.READ, paths=("count",), registry={})(not_text)

def test_check_arguments_checks_every_path_in_a_list(ws, outside):
    args, outside_items = sandbox.check_arguments(
        ("paths",), {"paths": ["a.txt", str(outside / "secret.txt")]}, ws)

    assert [Path(p) for p in args["paths"]] == [Path(real(ws / "a.txt")), Path(real(outside / "secret.txt"))]
    assert [item.requested for item in outside_items] == [str(outside / "secret.txt")]


def test_a_refused_name_in_a_list_refuses_the_whole_call(ws):
    with pytest.raises(sandbox.PathRefused):
        sandbox.check_arguments(("paths",), {"paths": ["a.txt", "CON"]}, ws)


def test_a_list_argument_that_is_not_text_is_refused(ws):
    with pytest.raises(sandbox.PathRefused):
        sandbox.check_arguments(("paths",), {"paths": [5]}, ws)
