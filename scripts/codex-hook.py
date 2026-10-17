"""Stable entry point for Codex hooks when running from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from agent_deck.hook import main

main()
