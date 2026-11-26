"""The public API for plugin authors. Import from here, not from the internal modules.

    from wildcat_code_agent.plugin import Risk, Preview, ToolContext, ToolCancelled, tool

    @tool(risk=Risk.READ)
    def count_lines(path: str) -> str:
        \"\"\"Count the lines in a text file.\"\"\"
        ...

Names exported here are the stable surface. Everything else in wildcat_code_agent is internal and may change.
See README.md ("Plugins") for where plugin files go and how they are loaded.
"""

from wildcat_code_agent.core.events import Preview
from wildcat_code_agent.permissions import Risk
from wildcat_code_agent.tools.registry import ToolCancelled, ToolContext, tool

__all__ = ["Preview", "Risk", "ToolCancelled", "ToolContext", "tool"]
