"""Shared frozen entry point for console CLI/bridge and the windowed GUI."""
import sys
from pathlib import Path

from agent_deck.cli import main


if __name__ == "__main__":
    if Path(sys.executable).name.lower() in {"agentdeckcompanion.exe", "agentdeckcompanion"} and len(sys.argv) == 1:
        sys.argv.append("gui")
    main()
