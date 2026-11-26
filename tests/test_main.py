"""The `local-agent` subcommands (T11): chat (the default), serve --stdio, serve --web and gui (T18)."""

import pytest

from local_agent import main
from local_agent.frontends import cli, server


def _empty_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")  # an empty file: no settings, and the user's config is never read
    return path


def test_gui_opens_the_window_with_the_shared_flags(monkeypatch, tmp_path):
    from local_agent.frontends import gui

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
    from local_agent.frontends import gui

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
    for expected in ("chat", "serve --stdio", "gui", "put the subcommand first", "local-agent serve --stdio --cwd"):
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
    from local_agent.frontends import web

    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(web, "serve_web", lambda settings, *, port=0, **kwargs: seen.update(port=port, model=settings.model))
    main(["serve", "--web", "--port", "8123", "--model", "some-model", "--config", str(_empty_config(tmp_path))])
    assert seen == {"port": 8123, "model": "some-model"}


def test_serve_web_without_a_port_takes_a_free_one(monkeypatch, tmp_path):
    from local_agent.frontends import web

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
