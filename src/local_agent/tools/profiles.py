"""Tool profiles: which registered tools the model is offered, and which the loop will run.

A profile is a set of risk tiers:
- minimal: read tools only.
- coding (the default): read, write and exec tools.
- all: every tier, including network.

Network-tier tools (web_fetch) are also gated by `network_enabled` in the config, so
they are offered only when both the profile and the switch allow them. A call to a
tool that is not offered is refused without a question (see `blocked_message`).
"""

from collections.abc import Mapping

from local_agent.config import DEFAULT_TOOL_PROFILE, TOOL_PROFILES, Settings
from local_agent.permissions import Risk
from local_agent.tools.registry import Tool

PROFILE_RISKS: dict[str, frozenset[Risk]] = {
    "minimal": frozenset({Risk.READ}),
    "coding": frozenset({Risk.READ, Risk.WRITE, Risk.EXEC}),
    "all": frozenset(Risk),
}

assert set(PROFILE_RISKS) == set(TOOL_PROFILES) and DEFAULT_TOOL_PROFILE in PROFILE_RISKS


def is_offered(tool: Tool, settings: Settings) -> bool:
    """True if the profile includes this tool's risk tier, and network tools are switched on."""
    if tool.risk not in PROFILE_RISKS[settings.tool_profile]:
        return False
    return tool.risk is not Risk.NETWORK or settings.network_enabled


def offered_tools(registry: Mapping[str, Tool], settings: Settings) -> dict[str, Tool]:
    """The tools sent to the model and allowed to run for these settings, by name."""
    return {name: tool for name, tool in registry.items() if is_offered(tool, settings)}


def blocked_message(name: str, tool: Tool | None, offered: Mapping[str, Tool], settings: Settings) -> str | None:
    """The error text for a call to a registered tool that is not offered, or None if it may run.

    An unknown name returns None too: the loop answers it as an unknown tool.
    """
    if tool is None or name in offered:
        return None
    if tool.risk is Risk.NETWORK and not settings.network_enabled:
        return (f"Error: {name} is disabled; enable it in config "
                "(network_enabled = true in config.toml).")
    return (f"Error: {name} is not in the '{settings.tool_profile}' tool profile. "
            "Use another profile (tool_profile in config.toml, or --profile).")
