"""Talking to Ollama about the model: error messages and the startup capability probe.

The loop uses describe_error for failed chat requests. The CLI runs probe once
at startup: it reports whether the model is reachable and installed, and
warns when the model does not report the `tools` capability.
"""

from typing import Any

import httpx
import ollama

from wildcat_code_agent.config import Settings

# Failures that end a turn (or the startup check) with a message, not a traceback.
# ConnectionError: the non-streaming client. httpx.TransportError: the streaming client.
OLLAMA_ERRORS = (ConnectionError, httpx.TransportError, ollama.ResponseError)


def describe_error(error: BaseException, settings: Settings) -> str:
    """A message for the user about a failed Ollama request."""
    if isinstance(error, (ConnectionError, httpx.ConnectError)):
        return (f"Cannot reach Ollama at {settings.host}. "
                "Start it with `ollama serve` (or check OLLAMA_HOST), then try again.")
    if isinstance(error, ollama.ResponseError):
        text = str(error.error) if error.error else str(error)
        # 404 is "model not found" on the chat endpoint. -1 is an error object inside the stream, which has no status.
        missing = error.status_code == 404 or (error.status_code == -1 and "not found" in text.lower())
        if missing:
            return (f"Model '{settings.model}' is not installed in Ollama. "
                    f"Run `ollama pull {settings.model}`, then try again.")
        return f"Ollama returned an error: {text}"
    return f"Lost the connection to Ollama at {settings.host}: {error}"


def probe(client: Any, settings: Settings) -> list[str]:
    """Check the configured model at startup. Returns warning lines (empty when all is well).

    Raises the Ollama errors (OLLAMA_ERRORS) when Ollama is unreachable or the
    model is not installed; the caller shows them with describe_error. A server
    that reports no capability list gets no warning, since nothing is known.
    """
    info = client.show(settings.model)
    capabilities = list(info.capabilities or [])
    if capabilities and "tools" not in capabilities:
        warning = (
            f"Warning: model '{settings.model}' does not report the 'tools' capability "
            f"(it reports: {', '.join(capabilities)}). Tool calls may not work. "
            "Use a model with tool support, for example qwen3-vl:8b."
        )
        return [warning]
    return []
