import os
import sys

from local_agent.config import ConfigError
from local_agent.frontends import cli


def main(argv: list[str] | None = None) -> None:
    args = cli.build_parser().parse_args(argv)
    try:
        settings = cli.settings_from_args(args)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    # Tool paths resolve against settings.workspace, not the cwd; the chdir only keeps the REPL's own
    # relative paths (and the default for run_command) in the same folder.
    os.chdir(settings.workspace)
    cli.run(settings=settings)
