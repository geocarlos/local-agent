"""Startup capability probe (client.show) and the Ollama error messages."""

import httpx
import ollama
import pytest
from fakes import FakeOllama, text_reply

from local_agent.config import Settings
from local_agent.core.model import describe_error, probe
from local_agent.frontends import cli


def test_probe_warns_when_tools_is_missing(fake_ollama):
    client = FakeOllama([], capabilities=("completion", "vision"))

    warnings = probe(client, Settings(model="qwen2.5-coder:7b"))

    assert len(warnings) == 1
    assert "'qwen2.5-coder:7b' does not report the 'tools' capability" in warnings[0]
    assert "completion, vision" in warnings[0]
    assert client.show_calls == ["qwen2.5-coder:7b"]


def test_probe_is_silent_when_tools_is_present(fake_ollama):
    client = FakeOllama([], capabilities=("completion", "tools", "thinking"))

    assert probe(client, Settings()) == []


def test_probe_is_silent_when_the_server_reports_no_capabilities(fake_ollama):
    # An older server may not send a capability list at all: nothing is known, so nothing is warned.
    client = FakeOllama([], capabilities=None)

    assert probe(client, Settings()) == []


def test_probe_raises_when_ollama_is_down():
    client = FakeOllama([], show_error=ConnectionError("Failed to connect to Ollama."))

    with pytest.raises(ConnectionError):
        probe(client, Settings())


def test_describe_error_for_a_missing_model_names_the_configured_model():
    message = describe_error(ollama.ResponseError('model "x:1b" not found', 404), Settings(model="x:1b"))

    assert message == "Model 'x:1b' is not installed in Ollama. Run `ollama pull x:1b`, then try again."


def test_describe_error_for_an_unreachable_host_names_the_configured_host():
    message = describe_error(httpx.ConnectError("refused"), Settings(host="http://10.0.0.5:11434"))

    assert message.startswith("Cannot reach Ollama at http://10.0.0.5:11434.")
    assert "ollama serve" in message


def test_startup_shows_the_capability_warning_and_still_runs_the_prompt(monkeypatch, capsys):
    inputs = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = FakeOllama([], capabilities=("completion",))

    cli.run(settings=Settings(), client=client)

    out = capsys.readouterr().out
    assert "does not report the 'tools' capability" in out
    assert out.index("Local Agent ready") < out.index("does not report")


def test_startup_with_no_tools_capability_sends_no_warning_when_tools_present(monkeypatch, capsys):
    inputs = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    cli.run(settings=Settings(), client=FakeOllama([]))

    assert "tools" not in capsys.readouterr().out


def test_startup_with_ollama_down_shows_the_error_and_keeps_the_prompt(monkeypatch, capsys):
    inputs = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = FakeOllama([], show_error=ConnectionError("Failed to connect to Ollama."))

    cli.run(settings=Settings(host="http://127.0.0.1:1"), client=client)

    out = capsys.readouterr().out
    assert "Error: Cannot reach Ollama at http://127.0.0.1:1" in out
    assert list(inputs) == []  # the prompt was read: the REPL kept going


def test_startup_with_the_model_missing_suggests_ollama_pull(monkeypatch, capsys):
    inputs = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = FakeOllama([], show_error=ollama.ResponseError("model not found", 404))

    cli.run(settings=Settings(model="gone:1b"), client=client)

    assert "Run `ollama pull gone:1b`" in capsys.readouterr().out


def test_probe_result_does_not_add_a_model_call(fake_ollama):
    client = FakeOllama([text_reply("hi")])

    probe(client, Settings())

    assert client.calls == []
