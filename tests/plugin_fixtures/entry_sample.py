"""A sample entry-point plugin for the tests: importing this module registers its tool."""

from local_agent.plugin import Risk, tool


@tool(risk=Risk.READ)
def entry_echo(text: str) -> str:
    """Return the text unchanged."""
    return text
