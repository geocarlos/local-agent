"""Shared fixtures."""

import importlib.metadata
import sys
from pathlib import Path

import pytest

from fakes import FakeOllama
from wildcat_code_agent import config
from wildcat_code_agent.tools.plugins import ENTRY_POINT_GROUP
from wildcat_code_agent.tools.registry import REGISTRY

_real_entry_points = importlib.metadata.entry_points


@pytest.fixture(autouse=True)
def isolated_user_folders(tmp_path_factory, monkeypatch):
    """Keep every test away from the real per-user folders (the per-user app folder): the config file and the
    data folder (saved sessions) point at temp folders. Subprocesses inherit the environment variable."""
    base = tmp_path_factory.mktemp("user-folders")
    monkeypatch.setattr(config, "config_path", lambda: base / "config" / "config.toml")
    monkeypatch.setenv("WILDCAT_CODE_AGENT_DATA_DIR", str(base / "data"))


@pytest.fixture(autouse=True)
def isolated_plugins(tmp_path_factory, monkeypatch):
    """Keep every test away from the real user config folder and from installed plugins.

    The per-user tools folder points at a path that does not exist, and the wildcat_code_agent.tools entry
    points are empty unless a test installs its own. Afterwards the tool registry and the imported
    plugin modules (and the wildcat_code_agent_plugins stub package) are removed, so a plugin tool never leaks
    into another test.
    """
    missing = tmp_path_factory.mktemp("no-user-config") / "tools"
    monkeypatch.setattr(config, "default_plugin_dir", lambda: Path(missing))

    def entry_points(**kwargs):
        if kwargs.get("group") == ENTRY_POINT_GROUP:
            return []
        return _real_entry_points(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    registry_before = dict(REGISTRY)
    yield
    REGISTRY.clear()
    REGISTRY.update(registry_before)
    for name in [n for n in sys.modules
                 if n == "wildcat_code_agent_plugins" or n.startswith(("wildcat_code_agent_plugins.", "plugin_fixtures."))]:
        del sys.modules[name]


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
