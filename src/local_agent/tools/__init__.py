"""Tool table and dispatch. T02 replaces the plain dict with a @tool registry."""

from local_agent.tools.builtin import read_file, run_command, write_file

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
}


def execute_tool(fn_name: str, fn_args: dict) -> str:
    """Run one tool call; any failure comes back as text so the model can recover."""
    if fn_name not in TOOLS:
        return f"Unknown tool: {fn_name}"
    try:
        return str(TOOLS[fn_name](**fn_args))
    except Exception as e:
        return f"Error calling {fn_name} with arguments {fn_args}: {type(e).__name__}: {e}"
