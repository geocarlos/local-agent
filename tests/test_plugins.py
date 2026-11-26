"""Plugin loading: the tools folder, entry points, failure isolation, name clashes, risk, the self-modification guard."""

import importlib.metadata
import sys
import textwrap
from pathlib import Path

import pytest

from fakes import FakeOllama, text_reply, tool_reply
from wildcat_code_agent import config
from wildcat_code_agent.config import ConfigError, Settings, load_settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import Decision
from wildcat_code_agent.frontends import cli
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools import mcp_tools, profiles
from wildcat_code_agent.tools.plugins import ENTRY_POINT_GROUP, describe_loaded, load_plugins
from wildcat_code_agent.tools.registry import REGISTRY

SAMPLE = Path(__file__).parent / "plugin_fixtures" / "sample_plugin.py"
ENTRY_SAMPLE = "plugin_fixtures.entry_sample"


class Approver:
    """Records each question and answers with `answer`."""

    def __init__(self, answer: Decision = Decision.ALLOW_ONCE) -> None:
        self.answer = answer
        self.requests: list = []

    def decide(self, request) -> Decision:
        self.requests.append(request)
        return self.answer


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "plugin-folder"
    folder.mkdir()
    return folder


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    return ws


def _write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(textwrap.dedent(text), encoding="utf-8")


def _settings(folder: Path, ws: Path, **kwargs) -> Settings:
    return Settings(plugin_dir=folder, workspace=ws, **kwargs)


def _tool_messages(messages: list) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]


def _turn(settings: Settings, replies: list, approver: Approver, session: SessionApprovals | None = None):
    """Run one user turn with scripted model replies. Returns (messages, fake client)."""
    client = FakeOllama(replies)
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "go"})
    loop.run_turn(messages, client=client, settings=settings, approver=approver, session=session)
    return messages, client


# --- the sample plugin: loaded, offered, approved as its risk says ---------------------------------------

def test_sample_plugin_tools_register_and_are_sent_to_the_model(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    settings = _settings(folder, _workspace(tmp_path))

    runtime = load_plugins(settings)

    assert [p.name for p in runtime.loaded] == ["sample_plugin"]
    assert runtime.loaded[0].tools == ("sample_count_words", "sample_write_note")
    assert "sample_count_words" in REGISTRY and "sample_write_note" in REGISTRY
    _, client = _turn(settings, [text_reply("ok")], Approver())
    offered = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert {"sample_count_words", "sample_write_note", "read_file"} <= offered


def test_the_read_plugin_tool_runs_without_asking(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    settings = _settings(folder, _workspace(tmp_path))
    load_plugins(settings)
    approver = Approver()

    messages, _ = _turn(settings, [tool_reply(("sample_count_words", {"text": "one two three"})),
                                   text_reply("done")], approver)

    assert approver.requests == []
    assert _tool_messages(messages) == ["3 words"]


def test_the_write_plugin_tool_asks_and_writes_only_when_approved(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    ws = _workspace(tmp_path)
    settings = _settings(folder, ws)
    load_plugins(settings)

    refused = Approver(Decision.REJECT_ONCE)
    _turn(settings, [tool_reply(("sample_write_note", {"path": "note.txt", "note": "hi"})), text_reply("x")],
          refused)
    assert [r.tool_name for r in refused.requests] == ["sample_write_note"]
    assert refused.requests[0].risk == "write"
    assert not (ws / "note.txt").exists()

    allowed = Approver(Decision.ALLOW_ONCE)
    _turn(settings, [tool_reply(("sample_write_note", {"path": "note.txt", "note": "hi"})), text_reply("x")],
          allowed)
    assert (ws / "note.txt").read_text(encoding="utf-8") == "hi"


def test_profile_filters_plugin_tools_by_risk(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    settings = _settings(folder, _workspace(tmp_path), tool_profile="minimal")
    load_plugins(settings)

    offered = profiles.offered_tools(REGISTRY, settings)

    assert "sample_count_words" in offered
    assert "sample_write_note" not in offered


def test_plugin_path_parameters_get_the_workspace_sandbox(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "peek.py", """
        from pathlib import Path
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ, paths=("path",))
        def peek(path: str) -> str:
            \"\"\"Read the start of a file.\"\"\"
            return Path(path).read_text(encoding="utf-8")[:20]
    """)
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    settings = _settings(folder, ws)
    load_plugins(settings)
    approver = Approver(Decision.REJECT_ONCE)

    messages, _ = _turn(settings, [tool_reply(("peek", {"path": str(outside)})), text_reply("x")], approver)

    assert [r.scope for r in approver.requests] == ["outside_workspace"]  # asked, though read tools run unasked
    assert "secret" not in _tool_messages(messages)[0]


# --- entry points -------------------------------------------------------------------------------------

def _fake_entry_points(monkeypatch, *entries: importlib.metadata.EntryPoint) -> None:
    def entry_points(**kwargs):
        return list(entries) if kwargs.get("group") == ENTRY_POINT_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)


def test_an_entry_point_module_registers_its_tools(monkeypatch, tmp_path):
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))
    settings = _settings(_folder(tmp_path), _workspace(tmp_path))

    runtime = load_plugins(settings)

    assert runtime.warnings == []
    assert [(p.name, p.source, p.tools) for p in runtime.loaded] == [
        ("entry_sample", "entry point", ("entry_echo",))]
    assert REGISTRY["entry_echo"].risk.value == "read"
    runtime.close()
    assert "entry_echo" not in REGISTRY


def test_an_entry_point_that_is_not_a_module_is_a_warning(monkeypatch, tmp_path):
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="not_a_module", value=f"{ENTRY_SAMPLE}:entry_echo", group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(_folder(tmp_path), _workspace(tmp_path)))

    assert runtime.loaded == []
    assert "Warning: plugin 'not_a_module' was not loaded" in runtime.warnings[0]
    assert "must name a module" in runtime.warnings[0]
    assert "entry_echo" not in REGISTRY


def test_a_module_attr_entry_point_is_refused_before_its_module_is_imported(monkeypatch, tmp_path):
    # Two entry points on the same module: the attr form is refused, and the module form must still load its tools.
    _fake_entry_points(monkeypatch,
                       importlib.metadata.EntryPoint(name="attr_form", value=f"{ENTRY_SAMPLE}:entry_echo",
                                                     group=ENTRY_POINT_GROUP),
                       importlib.metadata.EntryPoint(name="module_form", value=ENTRY_SAMPLE,
                                                     group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(_folder(tmp_path), _workspace(tmp_path)))

    assert len(runtime.warnings) == 1
    assert "Warning: plugin 'attr_form' was not loaded" in runtime.warnings[0]
    assert "must name a module" in runtime.warnings[0]
    assert [(p.name, p.tools) for p in runtime.loaded] == [("module_form", ("entry_echo",))]
    assert REGISTRY["entry_echo"].risk.value == "read"
    runtime.close()
    assert "entry_echo" not in REGISTRY


def test_a_module_attr_entry_point_leaves_its_module_unimported(monkeypatch, tmp_path):
    sys.modules.pop(ENTRY_SAMPLE, None)
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="attr_form", value=f"{ENTRY_SAMPLE}:entry_echo", group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(_folder(tmp_path), _workspace(tmp_path)))

    assert runtime.loaded == []
    assert ENTRY_SAMPLE not in sys.modules
    assert "entry_echo" not in REGISTRY


def test_a_plugin_file_and_an_entry_point_both_load_in_order(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert [p.name for p in runtime.loaded] == ["sample_plugin", "entry_sample"]  # folder first, then entry points
    assert describe_loaded(runtime) == ("[plugins] loaded: sample_plugin (2 tools), entry_sample (1 tool)")


# --- failure isolation ---------------------------------------------------------------------------------

def test_broken_plugins_warn_and_the_others_still_load(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "a_good.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def good_one(text: str) -> str:
            \"\"\"A good tool.\"\"\"
            return text
    """)
    _write(folder, "b_syntax.py", "def broken(:\n    pass\n")
    _write(folder, "c_import.py", "import wildcat_code_agent_no_such_module_for_tests\n")
    _write(folder, "d_raises.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def d_registered(text: str) -> str:
            \"\"\"Registered before the failure.\"\"\"
            return text

        raise RuntimeError("boom after registering")
    """)
    _write(folder, "e_invalid.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def no_docstring(text: str) -> str:
            return text
    """)
    _write(folder, "f_good.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def good_two(text: str) -> str:
            \"\"\"Another good tool.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert [p.name for p in runtime.loaded] == ["a_good", "f_good"]
    assert "good_one" in REGISTRY and "good_two" in REGISTRY
    assert "d_registered" not in REGISTRY  # rolled back with the plugin that raised
    warnings = "\n".join(runtime.warnings)
    assert len(runtime.warnings) == 4
    for name, error in [("b_syntax", "SyntaxError"), ("c_import", "ModuleNotFoundError"),
                        ("d_raises", "RuntimeError: boom after registering"), ("e_invalid", "docstring")]:
        line = next(w for w in runtime.warnings if f"plugin '{name}'" in w)
        assert error in line, line
    assert "Warning:" in warnings


def test_a_plugin_that_fails_leaves_no_half_loaded_module_behind(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "bad.py", "raise ValueError('nope')\n")

    load_plugins(_settings(folder, _workspace(tmp_path)))

    assert "wildcat_code_agent_plugins.bad" not in sys.modules


def test_warning_text_from_a_plugin_is_escaped(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "noisy.py", 'raise RuntimeError("bad\\x1b[2Jtext")\n')

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    (line,) = runtime.warnings
    assert "\x1b" not in line
    assert "bad\\x1b[2Jtext" in line


def test_files_that_are_not_plugins_are_ignored(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "_helper.py", "raise RuntimeError('must not be imported')\n")
    _write(folder, "notes.txt", "not python\n")
    (folder / "sub").mkdir()
    _write(folder / "sub", "inner.py", "raise RuntimeError('must not be imported either')\n")

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert runtime.loaded == [] and runtime.warnings == []


def test_a_missing_plugin_folder_is_not_an_error_and_is_not_created(tmp_path):
    missing = tmp_path / "does-not-exist"

    runtime = load_plugins(_settings(missing, _workspace(tmp_path)))

    assert runtime.loaded == [] and runtime.warnings == []
    assert not missing.exists()


# --- name clashes ---------------------------------------------------------------------------------------

def test_a_plugin_cannot_replace_a_built_in_tool(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "clash.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def read_file(path: str) -> str:
            \"\"\"A plugin that tries to replace the built-in read_file.\"\"\"
            return "replaced"

        @tool(risk=Risk.READ)
        def clash_extra(text: str) -> str:
            \"\"\"Another tool in the same plugin.\"\"\"
            return text
    """)
    builtin = REGISTRY["read_file"]

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert REGISTRY["read_file"] is builtin  # intact
    assert "clash_extra" in REGISTRY  # the rest of the plugin still loads
    assert runtime.loaded[0].tools == ("clash_extra",)
    assert "plugin 'clash' tool 'read_file' was skipped" in runtime.warnings[0]


def test_two_plugins_with_the_same_tool_name_keep_the_first(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "a_first.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def shared_tool(text: str) -> str:
            \"\"\"The first plugin's version.\"\"\"
            return "first"
    """)
    _write(folder, "b_second.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def shared_tool(text: str) -> str:
            \"\"\"The second plugin's version.\"\"\"
            return "second"

        @tool(risk=Risk.READ)
        def b_own(text: str) -> str:
            \"\"\"Only in the second plugin.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert REGISTRY["shared_tool"].fn.__module__ == "wildcat_code_agent_plugins.a_first"
    assert REGISTRY["shared_tool"].call({"text": "x"}) == "first"
    assert "b_own" in REGISTRY
    assert [p.name for p in runtime.loaded] == ["a_first", "b_second"]
    assert runtime.loaded[1].tools == ("b_own",)
    assert any("plugin 'b_second' tool 'shared_tool' was skipped" in w for w in runtime.warnings)


def test_plugins_and_built_ins_keep_their_order_of_loading(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    before = list(REGISTRY)

    load_plugins(_settings(folder, _workspace(tmp_path)))

    assert list(REGISTRY)[:len(before)] == before  # built-ins keep their place
    assert list(REGISTRY)[len(before):] == ["sample_count_words", "sample_write_note"]


# --- switches ------------------------------------------------------------------------------------------

def test_plugins_enabled_false_loads_nothing(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(folder, _workspace(tmp_path), plugins_enabled=False))

    assert runtime.loaded == [] and runtime.warnings == []
    assert "sample_count_words" not in REGISTRY and "entry_echo" not in REGISTRY


def test_disabled_plugins_are_skipped_by_file_stem_and_entry_point_name(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(folder, _workspace(tmp_path),
                                     disabled_plugins=("sample_plugin", "entry_sample")))

    assert runtime.loaded == []
    assert "sample_count_words" not in REGISTRY and "entry_echo" not in REGISTRY


def test_only_the_disabled_plugin_is_skipped(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _write(folder, "other.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def other_tool(text: str) -> str:
            \"\"\"Another plugin.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path), disabled_plugins=("sample_plugin",)))

    assert [p.name for p in runtime.loaded] == ["other"]
    assert "sample_count_words" not in REGISTRY and "other_tool" in REGISTRY


def test_no_plugins_flag_turns_plugins_off_in_the_settings(tmp_path):
    empty = tmp_path / "config.toml"  # an explicit file, so the real per-user config is never read
    empty.write_text("", encoding="utf-8")
    parser = cli.build_parser()

    off = cli.settings_from_args(parser.parse_args(["--config", str(empty), "--no-plugins"]), env={})
    on = cli.settings_from_args(parser.parse_args(["--config", str(empty)]), env={})

    assert off.plugins_enabled is False
    assert on.plugins_enabled is True


def test_no_plugins_flag_overrides_the_config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("plugins_enabled = true\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["--config", str(path), "--no-plugins"])

    assert cli.settings_from_args(args, env={}).plugins_enabled is False


def test_the_repl_loads_plugins_and_says_so_once(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    answers = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    cli.run(settings=_settings(folder, _workspace(tmp_path)), client=FakeOllama([]))

    out = capsys.readouterr().out
    assert "[plugins] loaded: sample_plugin (2 tools)" in out
    assert out.index("[plugins]") < out.index("Wildcat Code Agent ready")
    assert "sample_count_words" not in REGISTRY  # removed when the REPL exits


def test_the_repl_prints_no_plugin_line_when_there_are_none(tmp_path, monkeypatch, capsys):
    answers = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    cli.run(settings=_settings(_folder(tmp_path), _workspace(tmp_path)), client=FakeOllama([]))

    assert "[plugins]" not in capsys.readouterr().out


def test_the_repl_shows_plugin_warnings_and_starts_anyway(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path)
    _write(folder, "broken.py", "raise RuntimeError('kaput')\n")
    answers = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    cli.run(settings=_settings(folder, _workspace(tmp_path)), client=FakeOllama([]))

    out = capsys.readouterr().out
    assert "Warning: plugin 'broken' was not loaded (RuntimeError: kaput)" in out
    assert "Wildcat Code Agent ready" in out
    assert "[plugins] loaded" not in out


# --- configuration -------------------------------------------------------------------------------------

def test_plugin_settings_default_to_on_and_the_per_user_folder(tmp_path):
    empty = tmp_path / "config.toml"
    empty.write_text("", encoding="utf-8")
    settings = load_settings(config_file=empty, env={})

    assert settings.plugins_enabled is True
    assert settings.disabled_plugins == ()
    assert config.plugin_dir_of(settings) == config.default_plugin_dir()


def test_plugin_settings_come_from_the_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('plugins_enabled = false\ndisabled_plugins = ["a", "b"]\nplugin_dir = "my-tools"\n',
                    encoding="utf-8")

    settings = load_settings(config_file=path, env={})

    assert settings.plugins_enabled is False
    assert settings.disabled_plugins == ("a", "b")
    assert settings.plugin_dir == (tmp_path / "my-tools").resolve()  # relative to the config file
    assert config.plugin_dir_of(settings) == (tmp_path / "my-tools").resolve()


def test_a_plugin_folder_need_not_exist(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'plugin_dir = "{(tmp_path / "not-yet").as_posix()}"\n', encoding="utf-8")

    settings = load_settings(config_file=path, env={})

    assert not settings.plugin_dir.exists()


@pytest.mark.parametrize("text, fragment", [
    ('plugins_enabled = "yes"', "plugins_enabled must be true or false"),
    ('disabled_plugins = "sample"', "disabled_plugins must be a list of plugin names"),
    ('disabled_plugins = [1]', "disabled_plugins must be a list of plugin names"),
    ('disabled_plugins = [""]', "disabled_plugins must be a list of plugin names"),
    ("plugin_dir = 5", "plugin_dir must be a folder path"),
    ('plugin_dir = "  "', "plugin_dir must be a folder path"),
])
def test_bad_plugin_settings_are_config_errors_that_name_the_key(tmp_path, text, fragment):
    path = tmp_path / "config.toml"
    path.write_text(text + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=fragment):
        load_settings(config_file=path, env={})


# --- risk ---------------------------------------------------------------------------------------------

def test_a_plugin_tool_without_a_risk_is_not_loaded(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "norisk.py", """
        from wildcat_code_agent.plugin import tool

        @tool()
        def no_risk(text: str) -> str:
            \"\"\"Forgot its risk.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert "no_risk" not in REGISTRY
    assert "plugin 'norisk' was not loaded" in runtime.warnings[0]
    assert "risk" in runtime.warnings[0]


def test_a_plugin_tool_with_an_unknown_risk_is_not_loaded(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "badrisk.py", """
        from wildcat_code_agent.plugin import tool

        @tool(risk="admin")
        def bad_risk(text: str) -> str:
            \"\"\"Unknown tier.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert "bad_risk" not in REGISTRY and runtime.loaded == []


def test_a_plugin_exec_tool_always_asks(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "runner.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.EXEC)
        def run_thing(text: str) -> str:
            \"\"\"Pretend to run something.\"\"\"
            return "ran " + text
    """)
    settings = _settings(folder, _workspace(tmp_path))
    load_plugins(settings)
    approver = Approver(Decision.REJECT_ONCE)

    messages, _ = _turn(settings, [tool_reply(("run_thing", {"text": "x"})), text_reply("x")], approver)

    assert len(approver.requests) == 1
    assert _tool_messages(messages) == [REGISTRY["run_thing"].denied_message]


# --- the self-modification guard -----------------------------------------------------------------------

def test_a_write_session_allowance_does_not_cover_the_plugin_folder(tmp_path):
    ws = _workspace(tmp_path)
    folder = ws / "tools"  # inside the workspace, so only the guard (not the outside-workspace check) applies
    folder.mkdir()
    settings = _settings(folder, ws)
    session = SessionApprovals()
    session.allow("write_file", ws)  # the user once said "allow for this session" to write_file here
    approver = Approver(Decision.ALLOW_ONCE)

    _turn(settings, [tool_reply(("write_file", {"path": "tools/evil.py", "content": "import os\n"})),
                     tool_reply(("write_file", {"path": "tools/evil2.py", "content": "x = 1\n"})),
                     text_reply("x")], approver, session)

    assert [r.tool_name for r in approver.requests] == ["write_file", "write_file"]  # asked every time
    for request in approver.requests:
        assert request.options == ("allow_once", "reject_once")  # no "allow always" offered
    assert (folder / "evil.py").exists()  # allowed once, so it ran


def test_an_always_answer_for_the_plugin_folder_is_not_kept(tmp_path):
    ws = _workspace(tmp_path)
    folder = ws / "tools"
    folder.mkdir()
    settings = _settings(folder, ws)
    session = SessionApprovals()
    approver = Approver(Decision.ALLOW_ALWAYS)

    _turn(settings, [tool_reply(("write_file", {"path": "tools/a.py", "content": "a\n"})),
                     tool_reply(("write_file", {"path": "tools/b.py", "content": "b\n"})),
                     text_reply("x")], approver, session)

    assert len(approver.requests) == 2  # "always" was not kept: the second write asked too
    assert not session.allows("write_file", ws)


def test_an_edit_in_the_plugin_folder_asks_every_time(tmp_path):
    ws = _workspace(tmp_path)
    folder = ws / "tools"
    folder.mkdir()
    (folder / "mine.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    settings = _settings(folder, ws)
    session = SessionApprovals()
    session.allow("edit_file", ws)
    approver = Approver(Decision.ALLOW_ONCE)

    _turn(settings, [tool_reply(("edit_file", {"path": "tools/mine.py", "old_text": "x = 1",
                                              "new_text": "x = 3"})),
                     tool_reply(("edit_file", {"path": "tools/mine.py", "old_text": "y = 2",
                                              "new_text": "y = 4"})),
                     text_reply("x")], approver, session)

    assert [r.tool_name for r in approver.requests] == ["edit_file", "edit_file"]
    assert all("allow_always" not in r.options for r in approver.requests)
    assert (folder / "mine.py").read_text(encoding="utf-8") == "x = 3\ny = 4\n"


def test_a_write_outside_the_plugin_folder_still_uses_the_session_allowance(tmp_path):
    ws = _workspace(tmp_path)
    folder = ws / "tools"
    folder.mkdir()
    settings = _settings(folder, ws)
    session = SessionApprovals()
    session.allow("write_file", ws)
    approver = Approver(Decision.REJECT_ONCE)

    messages, _ = _turn(settings, [tool_reply(("write_file", {"path": "notes.txt", "content": "n\n"})),
                                   text_reply("x")], approver, session)

    assert approver.requests == []  # the allowance still works outside the plugin folder
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "n\n"


def test_the_guard_checks_the_resolved_path_not_the_spelling(tmp_path):
    ws = _workspace(tmp_path)
    folder = ws / "tools"
    folder.mkdir()
    settings = _settings(folder, ws)
    session = SessionApprovals()
    session.allow("write_file", ws)
    approver = Approver(Decision.ALLOW_ONCE)

    _turn(settings, [tool_reply(("write_file", {"path": "sub/../tools/sneaky.py", "content": "s\n"})),
                     text_reply("x")], approver, session)

    assert len(approver.requests) == 1  # "sub/../tools" resolves into the plugin folder, so it asks
    assert (folder / "sneaky.py").exists()


def test_the_plugin_folder_guard_is_a_path_check_not_a_prefix_check(tmp_path):
    from wildcat_code_agent import sandbox

    ws = _workspace(tmp_path)
    folder = ws / "tools"
    sibling = ws / "tools-extra" / "x.py"
    assert sandbox.session_allowance_ok(str(sibling), plugin_dir=folder) is True
    assert sandbox.session_allowance_ok(str(folder / "x.py"), plugin_dir=folder) is False
    assert sandbox.session_allowance_ok(str(folder / "x.py")) is True  # no folder given: as before


# --- reloading in one process ---------------------------------------------------------------------------

def test_a_file_plugin_registers_its_tools_again_after_close(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    settings = _settings(folder, _workspace(tmp_path))

    first = load_plugins(settings)
    assert first.loaded[0].tools == ("sample_count_words", "sample_write_note")
    first.close()
    second = load_plugins(settings)

    assert second.warnings == []
    assert second.loaded[0].tools == ("sample_count_words", "sample_write_note")
    assert REGISTRY["sample_count_words"].call({"text": "a b"}) == "2 words"
    second.close()
    assert "sample_count_words" not in REGISTRY
    assert "wildcat_code_agent_plugins.sample_plugin" not in sys.modules


def test_an_entry_point_module_registers_its_tools_again_after_close(monkeypatch, tmp_path):
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))
    settings = _settings(_folder(tmp_path), _workspace(tmp_path))

    first = load_plugins(settings)
    first.close()
    second = load_plugins(settings)

    assert second.warnings == []
    assert [(p.name, p.tools) for p in second.loaded] == [("entry_sample", ("entry_echo",))]
    second.close()
    assert "entry_echo" not in REGISTRY


def test_a_failed_plugin_loads_again_once_it_is_fixed(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "fixme.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def fixme_tool(text: str) -> str:
            \"\"\"Registered before the failure.\"\"\"
            return text

        raise RuntimeError("not yet")
    """)
    settings = _settings(folder, _workspace(tmp_path))

    broken = load_plugins(settings)
    assert "fixme_tool" not in REGISTRY and "wildcat_code_agent_plugins.fixme" not in sys.modules
    assert "not yet" in broken.warnings[0]

    _write(folder, "fixme.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def fixme_tool(text: str) -> str:
            \"\"\"Works now.\"\"\"
            return text
    """)
    fixed = load_plugins(settings)

    assert fixed.warnings == []
    assert "fixme_tool" in REGISTRY
    fixed.close()


# --- plugins that register nothing -------------------------------------------------------------------

def test_a_plugin_with_no_tools_warns_and_is_left_out_of_the_loaded_line(tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _write(folder, "empty.py", "VALUE = 1\n")

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert runtime.warnings == ["Warning: plugin 'empty' registered no tools."]
    assert [p.name for p in runtime.loaded] == ["sample_plugin"]
    assert describe_loaded(runtime) == "[plugins] loaded: sample_plugin (2 tools)"
    runtime.close()
    assert "wildcat_code_agent_plugins.empty" not in sys.modules


def test_only_a_plugin_with_no_tools_gives_no_loaded_line(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "empty.py", "VALUE = 1\n")

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert runtime.loaded == []
    assert describe_loaded(runtime) is None
    assert runtime.warnings == ["Warning: plugin 'empty' registered no tools."]


# --- helper files and the wildcat_code_agent_plugins stub ------------------------------------------------------

HELPER_PLUGIN = """
    from wildcat_code_agent.plugin import Risk, tool
    from . import _helper

    @tool(risk=Risk.READ)
    def shout_text(text: str) -> str:
        \"\"\"Upper-case a piece of text.\"\"\"
        return _helper.shout(text)
"""


def test_a_plugin_can_import_a_helper_file_with_a_relative_import(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "_helper.py", "def shout(text):\n    return text.upper() + '!'\n")
    _write(folder, "uses_helper.py", HELPER_PLUGIN)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert runtime.warnings == []
    assert [p.name for p in runtime.loaded] == ["uses_helper"]  # the helper is not listed as a plugin
    assert REGISTRY["shout_text"].call({"text": "hi"}) == "HI!"
    assert not list(folder.glob("__pycache__"))  # no bytecode written into the user's folder
    runtime.close()
    assert "shout_text" not in REGISTRY
    assert "wildcat_code_agent_plugins" not in sys.modules
    assert "wildcat_code_agent_plugins._helper" not in sys.modules


def test_two_plugin_folders_in_one_process_do_not_share_helpers(tmp_path):
    ws = _workspace(tmp_path)
    for name, label in [("first", "A"), ("second", "B")]:
        folder = tmp_path / name
        folder.mkdir()
        _write(folder, "_helper.py", f"def shout(text):\n    return {label!r} + ' ' + text\n")
        _write(folder, "uses_helper.py", HELPER_PLUGIN)

        runtime = load_plugins(_settings(folder, ws))
        assert REGISTRY["shout_text"].call({"text": "hi"}) == f"{label} hi"
        runtime.close()
        assert "shout_text" not in REGISTRY


def test_a_helper_with_a_syntax_error_fails_only_the_plugin_that_imports_it(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "_helper.py", "def shout(:\n")
    _write(folder, "uses_helper.py", HELPER_PLUGIN)
    _write(folder, "plain.py", """
        from wildcat_code_agent.plugin import Risk, tool

        @tool(risk=Risk.READ)
        def plain_tool(text: str) -> str:
            \"\"\"Loads fine.\"\"\"
            return text
    """)

    runtime = load_plugins(_settings(folder, _workspace(tmp_path)))

    assert [p.name for p in runtime.loaded] == ["plain"]
    assert "shout_text" not in REGISTRY
    assert "plugin 'uses_helper' was not loaded" in runtime.warnings[0]
    runtime.close()


# --- disabled_plugins is case-insensitive ---------------------------------------------------------------

def test_disabled_plugins_match_case_insensitively(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    _fake_entry_points(monkeypatch, importlib.metadata.EntryPoint(
        name="entry_sample", value=ENTRY_SAMPLE, group=ENTRY_POINT_GROUP))

    runtime = load_plugins(_settings(folder, _workspace(tmp_path),
                                     disabled_plugins=("SAMPLE_Plugin", "Entry_SAMPLE")))

    assert runtime.loaded == [] and runtime.warnings == []
    assert "sample_count_words" not in REGISTRY and "entry_echo" not in REGISTRY


# --- Ctrl+C and the REPL's cleanup -----------------------------------------------------------------------

def _no_prompt(prompt=""):
    raise AssertionError("the REPL should not start")


def test_ctrl_c_while_plugins_load_returns_quietly_and_undoes_the_loaded_ones(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path)
    (folder / "a_sample.py").write_bytes(SAMPLE.read_bytes())
    _write(folder, "b_interrupt.py", "raise KeyboardInterrupt\n")
    monkeypatch.setattr("builtins.input", _no_prompt)

    cli.run(settings=_settings(folder, _workspace(tmp_path)), client=FakeOllama([]))  # must return, not raise

    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert "Wildcat Code Agent ready" not in out
    assert "sample_count_words" not in REGISTRY
    assert "wildcat_code_agent_plugins.a_sample" not in sys.modules


def test_plugins_are_closed_when_starting_the_mcp_servers_raises(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    (folder / "sample_plugin.py").write_bytes(SAMPLE.read_bytes())
    monkeypatch.setattr(mcp_tools, "start_mcp_servers", _raise_runtime_error)
    monkeypatch.setattr("builtins.input", _no_prompt)

    with pytest.raises(RuntimeError, match="mcp failed"):
        cli.run(settings=_settings(folder, _workspace(tmp_path)), client=FakeOllama([]))

    assert "sample_count_words" not in REGISTRY
    assert "wildcat_code_agent_plugins" not in sys.modules


def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("mcp failed")
