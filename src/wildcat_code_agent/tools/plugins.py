"""Plugin loading: tools from the per-user plugin folder and from Python entry points.

Loaded once at startup, after the built-in tools and before the MCP tools (frontends/cli.py).

Two sources, in this order:
- The plugin folder (`plugin_dir_of(settings)`, by default `<config folder>\\tools`). Each `*.py` file
  directly in it (not recursive, not starting with `_`) is one plugin, named by its file stem. It runs
  as a module named `wildcat_code_agent_plugins.<stem>`, so it cannot shadow a real package. Files whose name
  starts with `_` are not plugins, but a plugin can import them with a relative import
  (`from . import _helper`): the loader installs a stub package `wildcat_code_agent_plugins` whose path is the
  plugin folder. The folder is never created by the app.
- Entry points in the group `wildcat_code_agent.tools`, sorted by name. The entry point's value must name a
  module (`package.module`); importing it registers the tools. A `module:attr` value is refused before
  anything is imported.

A plugin registers tools with `@tool` (see wildcat_code_agent.plugin). The risk must be given
(`@tool(risk=...)`), as for the built-in tools: a tool without it fails to load.

Modules the loader imported (plugin modules, helpers, and the new modules of an entry point's package)
are removed again from `sys.modules` by `PluginRuntime.close()` and by a rollback, so a later load in
the same process runs them again and registers their tools again.

Failure handling, per plugin (all or nothing):
- If the plugin raises (syntax error, import error, an invalid tool definition, or an exception
  after it registered some tools), every tool it registered is removed, and a warning names the
  plugin and the error. The other plugins still load.
- A plugin tool whose name is already registered (a built-in tool, or another plugin's tool) is
  skipped with a warning. The plugin's other tools still load.
- A plugin that loads but registers no tools gets a warning and is left out of the loaded line.

Settings: `plugins_enabled = false` (or `--no-plugins`) loads nothing; `disabled_plugins` lists
plugins to skip. A name is matched case-insensitively against both file stems and entry point names.
"""

import importlib
import importlib.metadata
import importlib.util
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wildcat_code_agent.config import Settings, plugin_dir_of
from wildcat_code_agent.tools.output import printable
from wildcat_code_agent.tools.registry import REGISTRY, Tool, skip_name_collisions

ENTRY_POINT_GROUP = "wildcat_code_agent.tools"
MODULE_PREFIX = "wildcat_code_agent_plugins"


@dataclass(frozen=True)
class LoadedPlugin:
    """A plugin that loaded, with the names of the tools it registered."""

    name: str
    source: str  # "file" or "entry point"
    tools: tuple[str, ...]


@dataclass
class PluginRuntime:
    """What loading produced: the loaded plugins, the warning lines, and the modules the loader imported.

    `close()` removes the plugins' tools, the modules and the stub package, so a later load runs them again.
    Only registry key additions are undone: overwrites, deletions, changed Tool objects and monkeypatches
    are not. Plugins are trusted code, so this guards against accidents only.
    """

    registry: dict[str, Tool] = field(default_factory=lambda: REGISTRY)
    loaded: list[LoadedPlugin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)  # modules this loader imported, removed on close
    stub: types.ModuleType | None = None  # the `wildcat_code_agent_plugins` parent package this load installed

    def close(self) -> None:
        for plugin in self.loaded:
            for name in plugin.tools:
                self.registry.pop(name, None)
        self.loaded.clear()
        _forget_modules(self.modules)
        self.modules.clear()
        if self.stub is not None and sys.modules.get(MODULE_PREFIX) is self.stub:
            del sys.modules[MODULE_PREFIX]
        self.stub = None


def load_plugins(settings: Settings, *, registry: dict[str, Tool] = REGISTRY) -> PluginRuntime:
    """Load the plugin folder, then the entry points. Never raises for a plugin's own failure.

    If a KeyboardInterrupt (or anything else unexpected) arrives mid-way, what was loaded so far is
    removed again before the exception goes on.
    """
    runtime = PluginRuntime(registry=registry)
    if not settings.plugins_enabled:
        return runtime
    disabled = {name.casefold() for name in settings.disabled_plugins}
    try:
        _load_file_plugins(runtime, plugin_dir_of(settings), disabled)
        for entry_point in sorted(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
            if entry_point.name.casefold() not in disabled:
                prefix = entry_point.module.partition(".")[0]
                _load(runtime, entry_point.name, "entry point", prefix, _import_entry_point, entry_point)
    except BaseException:
        runtime.close()
        raise
    return runtime


def describe_loaded(runtime: PluginRuntime) -> str | None:
    """The one-line startup summary, or None when no plugin loaded."""
    if not runtime.loaded:
        return None
    parts = [f"{printable(p.name, single_line=True)} ({_count(len(p.tools))})" for p in runtime.loaded]
    return "[plugins] loaded: " + ", ".join(parts)


def _count(n: int) -> str:
    return f"{n} tool" if n == 1 else f"{n} tools"


def _plugin_files(folder: Path) -> list[Path]:
    """The plugin files in `folder`, by name. A missing folder has none."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.py") if p.is_file() and not p.name.startswith("_"))


def _load_file_plugins(runtime: PluginRuntime, folder: Path, disabled: set[str]) -> None:
    """Load the plugin folder's files, with the stub package that lets them share `_helper` files.

    A new stub is installed for this load. Any `wildcat_code_agent_plugins` modules left by an earlier load that was
    not closed are dropped first, so a helper of another folder can never be picked up.
    """
    files = [p for p in _plugin_files(folder) if p.stem.casefold() not in disabled]
    if not files:
        return
    _drop_prefix_modules()
    stub = types.ModuleType(MODULE_PREFIX)
    stub.__path__ = [str(folder)]
    stub.__package__ = MODULE_PREFIX
    sys.modules[MODULE_PREFIX] = stub
    runtime.stub = stub
    # Helpers are imported through the normal finder; keep the user's folder free of __pycache__.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        for path in files:
            _load(runtime, path.stem, "file", MODULE_PREFIX, _import_file, path)
    finally:
        sys.dont_write_bytecode = previous


def _drop_prefix_modules() -> None:
    _forget_modules([n for n in sys.modules if n == MODULE_PREFIX or n.startswith(MODULE_PREFIX + ".")])


def _forget_modules(names: list[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)


def _new_modules(before: set[str], prefix: str) -> list[str]:
    """Modules imported since `before` that belong to the package `prefix` (the plugin's own package)."""
    return [n for n in sys.modules if n not in before and (n == prefix or n.startswith(prefix + "."))]


def _load(runtime: PluginRuntime, name: str, source: str, prefix: str,
          action: Callable[..., None], *args: Any) -> None:
    """Run one plugin's import. On failure, roll back its tools and modules and warn. Records the plugin on success."""
    table = runtime.registry
    before = set(table)
    before_modules = set(sys.modules)
    skipped: list[str] = []
    shown = _quote(name)
    try:
        with skip_name_collisions(skipped):
            action(*args)
    except BaseException as e:
        for added in set(table) - before:
            table.pop(added, None)
        _forget_modules(_new_modules(before_modules, prefix))
        if isinstance(e, KeyboardInterrupt):
            raise
        runtime.warnings.append(f"Warning: plugin {shown} was not loaded ({_describe(e)}). "
                                "The agent continues without it.")
        return
    runtime.modules.extend(_new_modules(before_modules, prefix))
    for tool_name in skipped:
        runtime.warnings.append(
            f"Warning: plugin {shown} tool {_quote(tool_name)} was skipped: "
            "the name is already used by a built-in tool or another plugin's tool.")
    added_names = tuple(n for n in table if n not in before)
    if added_names:
        runtime.loaded.append(LoadedPlugin(name=name, source=source, tools=added_names))
    else:
        runtime.warnings.append(f"Warning: plugin {shown} registered no tools.")


def _quote(text: str) -> str:
    """A name in single quotes, with control characters escaped (names come from file names and config)."""
    return f"'{printable(text, single_line=True)}'"


def _describe(error: BaseException) -> str:
    text = str(error)
    message = f"{type(error).__name__}: {text}" if text else type(error).__name__
    return printable(message, single_line=True)


def _import_file(path: Path) -> None:
    """Run a plugin file as module `wildcat_code_agent_plugins.<stem>`. Its @tool functions register as they are defined.

    The source is compiled and run directly (no .pyc is written into the plugin folder), so an edited
    file is always the one that runs.
    """
    module_name = f"{MODULE_PREFIX}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise ImportError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = MODULE_PREFIX  # so that `from . import _helper` finds the sibling helper
    code = compile(path.read_bytes(), str(path), "exec")
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)  # a plugin is code the user put in this folder on purpose
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise


def _import_entry_point(entry_point: importlib.metadata.EntryPoint) -> None:
    """Import the entry point's module. A `module:attr` value is refused before anything is imported,
    so a refused entry point has no side effects."""
    if entry_point.attr or ":" in entry_point.value:
        raise TypeError(f"the entry point {entry_point.value!r} must name a module, not an attribute "
                        "(its import registers the tools); it was not imported")
    importlib.import_module(entry_point.module)
