"""The `local-agent` command: subcommands `chat` (the default), `serve --stdio`, `serve --web` and `gui`."""

import argparse
import os
import sys

from local_agent.config import ConfigError
from local_agent.frontends import cli, server

SUBCOMMANDS = ("chat", "serve", "gui")
GUI_MESSAGE = ("The GUI arrives in a later version of local-agent. "
               "Use `local-agent chat` in a terminal, or `local-agent serve --stdio` from an editor or another program.")
GUI_EXIT_CODE = 2


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0 to 65535: {value}")
    return value


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent serve",
        description="Run the agent as an Agent Client Protocol (ACP) server. Choose one transport: "
                    "--stdio (JSON lines on stdin and stdout) or --web (WebSocket on 127.0.0.1).",
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--stdio", action="store_true",
                           help="speak JSON-RPC 2.0 (one message per line) on stdin and stdout; see docs/protocol.md")
    transport.add_argument("--web", action="store_true",
                           help="serve the same messages over a WebSocket at ws://127.0.0.1:PORT/ws, "
                                "with a token printed at start; see docs/protocol.md")
    parser.add_argument("--port", type=_port, default=None, metavar="N",
                        help="with --web: the port to listen on (default 0: a free port chosen by the system)")
    cli.add_settings_flags(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    command = "chat"  # with no subcommand word, every argument is a chat flag
    if argv and argv[0] in SUBCOMMANDS:
        command, argv = argv[0], argv[1:]

    if command == "gui":
        print(GUI_MESSAGE, file=sys.stderr)
        raise SystemExit(GUI_EXIT_CODE)

    if command == "serve":
        parser = _serve_parser()
        args = parser.parse_args(argv)
        if not (args.stdio or args.web):
            parser.error("choose a transport: --stdio or --web")
        if args.port is not None and not args.web:
            parser.error("--port applies to --web only")
    else:
        parser = cli.build_parser()
        args, extra = parser.parse_known_args(argv)
        if extra:
            # A subcommand word after the flags (`local-agent --cwd X serve --stdio`) is the common mistake.
            word = next((token for token in extra if token in SUBCOMMANDS), None)
            if word is not None:
                parser.error(f"the subcommand must come first: write `local-agent {word} ...` with its flags after it, "
                             f"for example `local-agent {word} --stdio --cwd X`")
            parser.error("unrecognized arguments: " + " ".join(extra))

    try:
        settings = cli.settings_from_args(args)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    # Tool paths resolve against settings.workspace, not the cwd; the chdir only keeps the REPL's own
    # relative paths (and the default for run_command) in the same folder.
    os.chdir(settings.workspace)
    if command == "serve":
        if args.web:
            from local_agent.frontends import web  # imported here: the web stack is not needed for chat or stdio

            web.serve_web(settings, port=args.port or 0)
        else:
            server.serve_stdio(settings)
    else:
        cli.run(settings=settings)
