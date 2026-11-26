"""Tool registry and the built-in tools.

Importing this package registers the built-in tools into TOOLS. To add a tool,
write one decorated function in a module imported here (see README).
"""

from wildcat_code_agent.tools import builtin, edit_tools, git_write, read_tools, web_tools  # noqa: F401  (registers the built-in tools)
from wildcat_code_agent.tools.registry import REGISTRY as TOOLS
from wildcat_code_agent.tools.registry import Location, Tool, tool

__all__ = ["TOOLS", "Location", "Tool", "tool"]
