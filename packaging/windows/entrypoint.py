"""Shared frozen entry point for console CLI/bridge and the windowed GUI."""
import sys

from agent_deck.cli import main


if __name__ == "__main__":
    if sys.executable.lower().endswith("agentdeckcompanion.exe") and len(sys.argv) == 1:
        sys.argv.append("gui")
    main()
