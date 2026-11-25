"""The `local-agent` command: subcommands `chat` (the default), `serve --stdio` and `gui`."""

import argparse
import os
import sys

from local_agent.config import ConfigError
from local_agent.frontends import cli, server

SUBCOMMANDS = ("chat", "serve", "gui")
GUI_MESSAGE = ("The GUI arrives in a later version of local-agent. "
               "Use `local-agent chat` in a terminal, or `local-agent serve --stdio` from an editor or another program.")
GUI_EXIT_CODE = 2


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent serve",
        description="Run the agent as an Agent Client Protocol (ACP) server over stdin and stdout.",
    )
    parser.add_argument("--stdio", action="store_true",
                        help="speak JSON-RPC 2.0 (one message per line) on stdin and stdout; see docs/protocol.md")
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
        args = _serve_parser().parse_args(argv)
        if not args.stdio:
            _serve_parser().error("choose a transport: --stdio is the only one for now")
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
        server.serve_stdio(settings)
    else:
        cli.run(settings=settings)
