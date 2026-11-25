"""A hand-written MCP server that speaks only the older handshake (initialize), over stdio.

It answers server/discover with "method not found", as a server from before the 2026 protocol
would. The client must fall back to initialize. One tool: shout(text), which upper-cases the text.
"""

import json
import sys

TOOLS = [{
    "name": "shout",
    "description": "Upper-case the text.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def refuse(request_id, method: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:  # a notification, such as notifications/initialized
        continue
    request_id, method = message["id"], message.get("method")
    if method == "initialize":
        reply(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "legacy-test", "version": "1"}})
    elif method == "tools/list":
        reply(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        reply(request_id, {"content": [{"type": "text", "text": text.upper()}], "isError": False})
    else:
        refuse(request_id, method)
