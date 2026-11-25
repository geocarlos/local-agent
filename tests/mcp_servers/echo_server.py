"""A tiny MCP server over stdio, used by the MCP client tests.

Run as `python echo_server.py`. Tools:
- echo(text): returns the text.
- add(a, b): returns the sum (an int argument, so the schema has integer types).
- fail(): raises, so the result comes back with isError set.
- slow(seconds): sleeps, for the timeout test.
- die(): ends the process at once, as a crashed server would.
- picture(): returns a text line and a small PNG image.
- pid(): returns this process's id, so tests can check that it is gone after shutdown.
- getenv(name): returns this process's value of an environment variable, or "(unset)".
"""

import base64
import os
import time

from mcp.server.mcpserver import MCPServer

# A 1x1 transparent PNG.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

server = MCPServer("echo-test")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two whole numbers."""
    return a + b


@server.tool()
def fail() -> str:
    """Always fail, with an error message."""
    raise ValueError("this tool always fails")


@server.tool()
def slow(seconds: float) -> str:
    """Sleep for the given number of seconds, then answer."""
    time.sleep(seconds)
    return "done"


@server.tool()
def die() -> str:
    """End the server process at once, without answering."""
    os._exit(3)


@server.tool()
def picture() -> list:
    """Return a line of text and a small image."""
    from mcp.server.mcpserver.utilities.types import Image

    return ["a picture", Image(data=PNG_BYTES, format="png")]


@server.tool()
def pid() -> int:
    """Return this server's process id."""
    return os.getpid()


@server.tool()
def getenv(name: str) -> str:
    """Return the value of an environment variable in this server, or (unset)."""
    return os.environ.get(name, "(unset)")


if __name__ == "__main__":
    server.run("stdio")
