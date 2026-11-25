"""Entry script for the PyInstaller build. It calls the same `main` as the `local-agent` console script."""

from local_agent import main

if __name__ == "__main__":
    main()
