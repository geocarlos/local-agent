"""Terminal frontend: the interactive REPL and the command-line flags.

Renders core events as text and answers approval requests with y/n.
The text matches the pre-T02 output: "[Tool Call]", "[PROPOSED CONTENT]",
"[APPROVE]" and "Agent:" lines. A path outside the workspace first shows an
"[OUTSIDE WORKSPACE]" line, then its own [APPROVE] question with the resolved
path. Model text is printed as it streams, after one "Agent: " prefix per reply. A "[thinking...]" line marks reasoning, and a
"[tokens: ...]" line follows each turn that Ollama reported counts for.

Ctrl+C during a turn cancels the turn and returns to the "You:" prompt. Ctrl+C
at the prompt exits.
"""

import argparse
from collections.abc import Mapping
from pathlib import Path

import ollama

from local_agent.config import DEFAULT_HOST, Settings, load_settings
from local_agent.core import loop
from local_agent.core.events import (
    ApprovalRequest,
    Decision,
    Error,
    Event,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnDone,
    Usage,
)
from local_agent.core.model import OLLAMA_ERRORS, describe_error, probe
from local_agent.permissions import SessionApprovals
from local_agent.tools import mcp_tools

CONTEXT_WARNING_FRACTION = 0.9  # warn when the prompt uses this share of num_ctx


class TerminalApprover:
    """Asks on the terminal. 'y' allows once; 'a' allows for this session when the request offers it.

    Anything else is a refusal. A request that does not offer "allow always" (a path outside the
    workspace) asks y/n only, and 'a' there is refused.
    """

    def decide(self, request: ApprovalRequest) -> Decision:
        offers_always = "allow_always" in request.options
        if offers_always:
            prompt = f"\n[APPROVE] {request.title}? [y]es / [a]lways for this session / [n]o: "
        else:
            prompt = f"\n[APPROVE] {request.title}? (y/n): "
        answer = input(prompt).strip().lower()
        if answer == "y":
            return Decision.ALLOW_ONCE
        if answer in ("a", "always") and offers_always:
            return Decision.ALLOW_ALWAYS
        return Decision.REJECT_ONCE


class Renderer:
    """Prints events. Streamed text stays on one "Agent: " line until another event arrives.

    `num_ctx` (optional) lets the token line show the context size and warn when it is nearly full.
    """

    def __init__(self, num_ctx: int | None = None) -> None:
        self._text_open = False
        self._thinking_shown = False
        self._num_ctx = num_ctx

    def __call__(self, event: Event) -> None:
        if isinstance(event, ThinkingDelta):
            if not self._thinking_shown:
                print("\n[thinking...]", end="", flush=True)
                self._thinking_shown = True
            return
        if isinstance(event, TextDelta):
            if not self._text_open:
                print("\nAgent: ", end="", flush=True)
                self._text_open = True
            print(event.text, end="", flush=True)
            return
        self._end_text()
        if isinstance(event, ToolCall):
            print(f"\n[Tool Call] {event.name}({event.arguments})")
        elif isinstance(event, ToolResult):
            self._thinking_shown = False  # the next model request starts a new round of reasoning
        elif isinstance(event, ApprovalRequest):
            if event.scope == "outside_workspace":
                print(f"\n[OUTSIDE WORKSPACE] {event.tool_name} reaches past the folder the agent works in. "
                      "Answer n unless you expect it.")
            preview = event.preview
            if preview is not None and preview.kind == "text" and preview.path is not None:
                print(f"\n[PROPOSED CONTENT for '{preview.path}']\n{preview.text}\n[END PROPOSED CONTENT]")
            elif preview is not None and preview.kind == "text":  # no path: for example an MCP call's arguments
                print(f"\n[PREVIEW]\n{preview.text}\n[END PREVIEW]")
            elif preview is not None and preview.kind == "diff":
                label = f" for '{preview.path}'" if preview.path is not None else ""
                print(f"\n[PROPOSED CHANGES{label}]\n{preview.text}\n[END PROPOSED CHANGES]")
        elif isinstance(event, TurnDone):
            if event.stop_reason == "max_turn_requests":
                print(f"\nAgent: stopped after {event.rounds} tool rounds without a final answer. "
                      "Ask again or narrow the request.")
            elif event.stop_reason == "cancelled":
                print("\n[Turn cancelled]")
            if event.usage is not None:
                self._print_usage(event.usage)
        elif isinstance(event, Error):
            print(f"\nError: {event.message}")

    def _print_usage(self, usage: Usage) -> None:
        of_context = f" of {self._num_ctx}" if self._num_ctx else ""
        print(f"\n[tokens: {usage.prompt_tokens}{of_context} prompt, {usage.completion_tokens} generated]")
        if self._num_ctx and usage.prompt_tokens >= CONTEXT_WARNING_FRACTION * self._num_ctx:
            print(f"Warning: the conversation fills {usage.prompt_tokens} of {self._num_ctx} context tokens. "
                  "Ollama drops the oldest messages past the context size: start a new chat or raise --num-ctx.")

    def _end_text(self) -> None:
        if self._text_open:
            print()
            self._text_open = False


def render(event: Event) -> None:
    """Print one event with a fresh renderer (for events that are not part of a streamed reply)."""
    Renderer()(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-agent", description="Local coding assistant on Ollama.")
    parser.add_argument("--model", help="Ollama model name (default: config file, else qwen3-vl:8b)")
    parser.add_argument("--host", help=f"Ollama server URL (default: OLLAMA_HOST, config file, else {DEFAULT_HOST})")
    parser.add_argument("--num-ctx", type=int, help="context window in tokens, sent to Ollama with every request")
    parser.add_argument("--cwd", type=Path, help="workspace folder to start in; tools run there (default: current folder)")
    parser.add_argument("--profile", help="tool profile: minimal (read tools), coding (read, write and exec; the default), "
                                          "or all (adds web_fetch when enabled in config)")
    parser.add_argument("--config", type=Path, help="config file to read instead of the per-user config.toml")
    return parser


def settings_from_args(args: argparse.Namespace, *, env: Mapping[str, str] | None = None) -> Settings:
    """Settings for parsed flags: flags over OLLAMA_HOST over the config file over defaults.

    Raises ConfigError for an invalid value or a bad config file.
    """
    return load_settings(
        config_file=args.config,
        env=env,
        overrides={
            "model": args.model,
            "host": args.host,
            "num_ctx": args.num_ctx,
            "workspace": str(args.cwd) if args.cwd is not None else None,
            "tool_profile": args.profile,
        },
    )


def _startup_checks(client, settings: Settings) -> None:
    """Probe the model once. Problems are shown, and the REPL still starts, as with a failed turn."""
    try:
        warnings = probe(client, settings)
    except OLLAMA_ERRORS as e:
        print(f"\nError: {describe_error(e, settings)}")
        return
    for line in warnings:
        print(f"\n{line}")


def run(settings: Settings | None = None, client: loop.ChatClient | None = None) -> None:
    """Interactive REPL. `settings` defaults to Settings() (no file or environment is read).

    The MCP servers in `settings.mcp_servers` are started first and stopped when the REPL exits.
    """
    settings = Settings() if settings is None else settings
    if client is None:
        client = ollama.Client(host=settings.host)
    messages = loop.new_messages()
    approver = TerminalApprover()
    session = SessionApprovals()  # "allow always" answers last until this REPL exits
    try:
        mcp = mcp_tools.start_mcp_servers(settings.mcp_servers, timeout_s=settings.command_timeout_s)
    except KeyboardInterrupt:  # Ctrl+C while the servers start: nothing is running yet
        print()
        return
    try:
        for line in mcp.warnings:
            print(line)
        print(f"Local Agent ready ({settings.model}). Type 'exit' to quit.")
        _startup_checks(client, settings)
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except KeyboardInterrupt:
                print()
                break
            if user_input.lower() in {"exit", "quit"}:
                break
            start = len(messages)
            messages.append({"role": "user", "content": user_input})
            # Ctrl+C during the turn is handled by the core: the turn ends as cancelled and the prompt returns.
            loop.run_turn(messages, client=client, settings=settings,
                          emit=Renderer(settings.num_ctx), approver=approver, session=session)
            if len(messages) == start + 1:
                # No reply was recorded (an Ollama error, or Ctrl+C before one): drop the message
                # so that sending it again does not put it into the history twice.
                del messages[start]
    finally:
        mcp.close()


if __name__ == "__main__":
    run()
