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
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import ollama

from wildcat_code_agent.config import DEFAULT_HOST, Settings, load_settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import (
    ApprovalRequest,
    Decision,
    Error,
    Event,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    ToolStarted,
    TurnDone,
    Usage,
)
from wildcat_code_agent.core.model import OLLAMA_ERRORS, describe_error, probe
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools import mcp_tools, plugins

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
        try:
            answer = input(prompt).strip().lower()
        except EOFError:  # input ended (piped stdin): nothing approved, the tool is refused
            print()
            return Decision.REJECT_ONCE
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
        if isinstance(event, ToolStarted):  # the server frontend reports this; the terminal does not need it
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


SUBCOMMANDS_HELP = """\
subcommands (put the subcommand first, before any flag):
  chat             the interactive chat; the default when no subcommand is given
  serve --stdio    the Agent Client Protocol server over stdin and stdout (see docs/protocol.md)
  gui              a placeholder until a later version

example: {prog} serve --stdio --cwd C:\\work\\my-project
"""

LONG_NAME = "wildcat-code-agent"
SHORT_NAME = "wca"  # the same program under a short name (T24): the wca entry point and wca.exe


def command_name() -> str:
    """The command the user typed: `wca` or `wildcat-code-agent`, the basename of sys.argv[0] without `.exe`.

    Anything else (for example `python -m`, where sys.argv[0] ends in `__main__.py`) gets the long name.
    """
    stem = os.path.basename(sys.argv[0]) if sys.argv else ""
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    stem = stem.lower()
    return stem if stem in (SHORT_NAME, LONG_NAME) else LONG_NAME


def build_parser() -> argparse.ArgumentParser:
    """The parser for `wildcat-code-agent` (or `wca`) and `... chat` (the REPL)."""
    prog = command_name()
    parser = argparse.ArgumentParser(prog=prog, description="Local coding assistant on Ollama.",
                                     epilog=SUBCOMMANDS_HELP.format(prog=prog),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_settings_flags(parser)
    return parser


def add_settings_flags(parser: argparse.ArgumentParser) -> None:
    """The flags every subcommand that runs the agent takes (chat, serve)."""
    parser.add_argument("--model", help="Ollama model name (default: config file, else qwen3-vl:8b)")
    parser.add_argument("--host", help=f"Ollama server URL (default: OLLAMA_HOST, config file, else {DEFAULT_HOST})")
    parser.add_argument("--num-ctx", type=int, help="context window in tokens, sent to Ollama with every request")
    parser.add_argument("--cwd", type=Path, help="workspace folder to start in; tools run there (default: current folder)")
    parser.add_argument("--profile", help="tool profile: minimal (read tools), coding (read, write and exec; the default), "
                                          "or all (adds web_fetch when enabled in config)")
    parser.add_argument("--config", type=Path, help="config file to read instead of the per-user config.toml")
    parser.add_argument("--no-plugins", action="store_true",
                        help="do not load plugins (the tools folder and entry points); see README")


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
            "plugins_enabled": False if getattr(args, "no_plugins", False) else None,
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

    Plugins (tools/plugins.py) load first, after the built-in tools; then the MCP servers in
    `settings.mcp_servers` start. Both are removed when the REPL exits.
    """
    settings = Settings() if settings is None else settings
    if client is None:
        client = ollama.Client(host=settings.host)
    messages = loop.new_messages()
    approver = TerminalApprover()
    session = SessionApprovals()  # "allow always" answers last until this REPL exits
    plugin_runtime = None
    mcp = None
    try:
        try:
            plugin_runtime = plugins.load_plugins(settings)
            mcp = mcp_tools.start_mcp_servers(settings.mcp_servers, timeout_s=settings.command_timeout_s)
        except KeyboardInterrupt:  # Ctrl+C while plugins or servers start: nothing is left running
            print()
            return
        for line in plugin_runtime.warnings:
            print(line)
        summary = plugins.describe_loaded(plugin_runtime)
        if summary is not None:
            print(summary)
        for line in mcp.warnings:
            print(line)
        print(f"Wildcat Code Agent ready ({settings.model}). Type 'exit' to quit.")
        _startup_checks(client, settings)
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (KeyboardInterrupt, EOFError):  # Ctrl+C, or input ended (piped stdin): same as exit
                print()
                break
            if user_input.lower() in {"exit", "quit"}:
                break
            messages.append({"role": "user", "content": user_input})
            # Ctrl+C during the turn is handled by the core: the turn ends as cancelled and the prompt returns.
            # If no reply was recorded (an Ollama error, or Ctrl+C before one), the core removes the user message.
            loop.run_turn(messages, client=client, settings=settings,
                          emit=Renderer(settings.num_ctx), approver=approver, session=session)
    finally:
        if mcp is not None:
            mcp.close()
        if plugin_runtime is not None:
            plugin_runtime.close()


if __name__ == "__main__":
    run()
