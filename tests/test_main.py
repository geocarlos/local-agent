"""The `local-agent` subcommands (T11): chat (the default), serve --stdio and the gui placeholder."""

import pytest

from local_agent import main
from local_agent.frontends import cli, server


def _empty_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")  # an empty file: no settings, and the user's config is never read
    return path


def test_gui_is_a_placeholder_that_exits_with_code_2(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["gui"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "later version" in err
    assert "serve --stdio" in err


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
    monkeypatch.setattr(server, "serve_stdio", lambda settings: seen.setdefault("settings", settings))

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
