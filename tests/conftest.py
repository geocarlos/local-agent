"""Shared fixtures."""

import pytest

from fakes import FakeOllama


@pytest.fixture
def fake_ollama():
    """Factory: fake_ollama([text_reply("hi")]) -> FakeOllama."""
    return FakeOllama
