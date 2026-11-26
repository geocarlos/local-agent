"""The `wildcat-code-agent` subcommands (T11): chat (the default), serve --stdio, serve --web and gui (T18)."""

import sys

import pytest

from wildcat_code_agent import main
from wildcat_code_agent.frontends import cli, server


def _empty_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")  # an empty file: no settings, and the user's config is never read
    return path


def test_gui_opens_the_window_with_the_shared_flags(monkeypatch, tmp_path):
    from wildcat_code_agent.frontends import gui

    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    seen = {}

    def fake_run(settings, *, config_file=None, **kwargs):
        seen.update(settings=settings, config_file=config_file)
        return 0

    monkeypatch.setattr(gui, "run", fake_run)
    config = _empty_config(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["gui", "--model", "some-model", "--cwd", str(folder), "--num-ctx", "4096", "--no-plugins",
              "--config", str(config)])

    assert exit_info.value.code == 0
    settings = seen["settings"]
    assert settings.model == "some-model"
    assert settings.num_ctx == 4096
    assert settings.workspace == folder.resolve()
    assert settings.plugins_enabled is False
    assert seen["config_file"] == config


def test_gui_exit_code_comes_from_the_window_command(monkeypatch, tmp_path):
    from wildcat_code_agent.frontends import gui

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gui, "run", lambda settings, **kwargs: 1)
    with pytest.raises(SystemExit) as exit_info:
        main(["gui", "--config", str(_empty_config(tmp_path))])
    assert exit_info.value.code == 1


def test_gui_with_a_bad_config_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["gui", "--config", str(tmp_path / "missing.toml")])
    assert exit_info.value.code == 2
    assert "config file not found" in capsys.readouterr().err


def test_serve_without_a_transport_says_to_choose_stdio(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["serve"])
    assert exit_info.value.code == 2
    assert "--stdio" in capsys.readouterr().err


def test_serve_stdio_takes_the_same_flags_as_chat(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    seen = {}
    monkeypatch.setattr(server, "serve_stdio", lambda settings, **kwargs: seen.setdefault("settings", settings))

    main(["serve", "--stdio", "--model", "some-model", "--cwd", str(folder), "--num-ctx", "4096",
          "--config", str(_empty_config(tmp_path))])

    settings = seen["settings"]
    assert settings.model == "some-model"
    assert settings.num_ctx == 4096
    assert settings.workspace == folder.resolve()


@pytest.mark.parametrize("argv", [["--model", "some-model"], ["chat", "--model", "some-model"]])
def test_chat_runs_the_repl_with_the_flags_with_or_without_the_word(monkeypatch, tmp_path, argv):
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(cli, "run", lambda settings=None, client=None: seen.setdefault("settings", settings))

    main([*argv, "--config", str(_empty_config(tmp_path))])

    assert seen["settings"].model == "some-model"


def test_a_bad_config_file_exits_2_for_chat_and_serve(tmp_path, capsys):
    missing = str(tmp_path / "missing.toml")
    for argv in (["--config", missing], ["chat", "--config", missing], ["serve", "--stdio", "--config", missing]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 2
    assert "config file not found" in capsys.readouterr().err


def test_an_unknown_profile_exits_2_for_serve(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--stdio", "--profile", "nope", "--config", str(_empty_config(tmp_path))])
    assert exit_info.value.code == 2
    assert "--profile must be one of" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--cwd", "X", "serve", "--stdio"], ["--model", "m", "gui"],
                                  ["--num-ctx", "8192", "chat"]])
def test_a_subcommand_after_a_flag_says_to_put_it_first(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
    assert "the subcommand must come first" in capsys.readouterr().err


def test_help_lists_the_subcommands_and_says_to_put_one_first(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for expected in ("chat", "serve --stdio", "gui", "put the subcommand first", "wildcat-code-agent serve --stdio --cwd"):
        assert expected in out
    with pytest.raises(SystemExit):
        main(["chat", "--help"])
    assert "serve --stdio" in capsys.readouterr().out


def test_an_unknown_flag_without_a_subcommand_is_still_an_ordinary_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--bogus"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --bogus" in err
    assert "subcommand" not in err


def test_serve_web_takes_the_flags_and_the_port(monkeypatch, tmp_path):
    from wildcat_code_agent.frontends import web

    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(web, "serve_web", lambda settings, *, port=0, **kwargs: seen.update(port=port, model=settings.model))
    main(["serve", "--web", "--port", "8123", "--model", "some-model", "--config", str(_empty_config(tmp_path))])
    assert seen == {"port": 8123, "model": "some-model"}


def test_serve_web_without_a_port_takes_a_free_one(monkeypatch, tmp_path):
    from wildcat_code_agent.frontends import web

    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(web, "serve_web", lambda settings, *, port=0, **kwargs: seen.setdefault("port", port))
    main(["serve", "--web", "--config", str(_empty_config(tmp_path))])
    assert seen["port"] == 0


@pytest.mark.parametrize("argv, message", [
    (["serve", "--web", "--stdio"], "not allowed with"),
    (["serve", "--stdio", "--port", "8000"], "--port applies to --web only"),
    (["serve", "--web", "--port", "70000"], "port must be 0 to 65535"),
    (["serve", "--web", "--port", "abc"], "not a port number"),
])
def test_serve_web_flag_mistakes_exit_2(argv, message, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


# --- the name the user typed (T24: wca is a second name for wildcat-code-agent) ---------------


@pytest.mark.parametrize("argv0, expected", [
    ("wca", "wca"),
    (r"C:\Program Files\wildcat-code-agent\wca.exe", "wca"),
    ("WCA.EXE", "wca"),
    ("wildcat-code-agent", "wildcat-code-agent"),
    (r"C:\Program Files\wildcat-code-agent\wildcat-code-agent.exe", "wildcat-code-agent"),
    ("/usr/local/bin/wca", "wca"),
    (r"C:\x\__main__.py", "wildcat-code-agent"),  # python -m wildcat_code_agent
    (r"C:\x\pytest.exe", "wildcat-code-agent"),
    ("wca-tools.exe", "wildcat-code-agent"),
    ("", "wildcat-code-agent"),
])
def test_command_name_is_the_typed_name_or_the_long_name(monkeypatch, argv0, expected):
    from wildcat_code_agent.frontends import cli

    monkeypatch.setattr(sys, "argv", [argv0, "--help"])
    assert cli.command_name() == expected


def test_help_usage_line_names_the_command_typed(monkeypatch, capsys):
    for argv0, name in (("wca", "wca"), ("wildcat-code-agent.exe", "wildcat-code-agent")):
        monkeypatch.setattr(sys, "argv", [argv0])
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith(f"usage: {name} ")
        assert f"example: {name} serve --stdio --cwd" in out


def test_serve_help_names_the_command_typed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["wca.exe"])
    with pytest.raises(SystemExit):
        main(["serve", "--help"])
    assert capsys.readouterr().out.startswith("usage: wca serve ")


def test_subcommand_order_hint_names_the_command_typed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["wca"])
    with pytest.raises(SystemExit):
        main(["--cwd", "X", "serve", "--stdio"])
    err = capsys.readouterr().err
    assert "`wca serve ...`" in err
    assert "`wca serve --stdio --cwd X`" in err
    assert "wildcat-code-agent" not in err


def test_python_dash_m_falls_back_to_the_long_name(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [r"C:\x\wildcat_code_agent\__main__.py"])
    with pytest.raises(SystemExit):
        main(["--help"])
    assert capsys.readouterr().out.startswith("usage: wildcat-code-agent ")
