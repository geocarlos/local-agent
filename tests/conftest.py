"""Shared fixtures."""

import pytest

from fakes import FakeOllama


@pytest.fixture
def fake_ollama():
    """Factory: fake_ollama([text_reply("hi")]) -> FakeOllama."""
    return FakeOllama


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A temp folder used as both the working folder and the workspace.

    `Settings()` takes the cwd as its workspace, so changing into the folder makes
    files under it inside the workspace. monkeypatch restores the cwd afterwards.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path
