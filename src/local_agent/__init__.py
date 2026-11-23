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
    os.chdir(settings.workspace)  # tools use relative paths and run commands here
    cli.run(settings=settings)
