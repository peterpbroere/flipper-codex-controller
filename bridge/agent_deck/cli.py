from __future__ import annotations

import argparse
import json
import logging
import secrets
import time
import sqlite3

from .install import install_hooks
from .model import Deck
from .names import rename_session
from .service import drain, remove_dead, run
from .store import EventStore
from .transport import flipper_ports


def main():
    parser = argparse.ArgumentParser(description="Flipper Agent Deck companion")
    parser.add_argument("--verbose", action="store_true", help="Log device acknowledgements for diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "demo"):
        cmd = sub.add_parser(name, help="Run the live Flipper bridge" if name == "run" else "Send simulated agents to the Flipper")
        cmd.add_argument("--port", default="auto", help="Application's second USB serial port, or auto")
        cmd.add_argument("--transport", choices=["usb", "bluetooth"], default="usb")
        cmd.add_argument("--address", default="", help="Paired Bluetooth address (use bluetooth scan)")
        cmd.add_argument("--duration", type=float, help="Stop after this many seconds")
        cmd.add_argument("--store", help="Event database path")
        cmd.add_argument("--focus-script", help="Optional PowerShell terminal focus helper")
        cmd.add_argument("--status-file", help=argparse.SUPPRESS)
        cmd.add_argument("--instance", default="", help=argparse.SUPPRESS)
    cmd = sub.add_parser("gui", help="Open the PC companion window")
    cmd.add_argument("--store")
    cmd = sub.add_parser("background", help="Manage the background bridge")
    cmd.add_argument("action", choices=["start", "stop", "restart"])
    cmd.add_argument("--port", default=None)
    cmd.add_argument("--transport", choices=["usb", "bluetooth"])
    cmd.add_argument("--address", default=None)
    cmd.add_argument("--demo", action="store_true")
    cmd.add_argument("--store")
    sub.add_parser("ports", help="List connected Flipper USB ports")
    cmd = sub.add_parser("bluetooth", help="Bluetooth diagnostic probe for the separate BLE test app")
    bt = cmd.add_subparsers(dest="bluetooth_command", required=True)
    cmd = bt.add_parser("scan", help="Find advertising Agent Deck BLE diagnostic devices")
    cmd.add_argument("--timeout", type=float, default=8)
    cmd = bt.add_parser("probe", help="Probe the separate test app with read-only snapshots and a harmless button test")
    cmd.add_argument("--address", required=True, help="Explicit device address from bluetooth scan")
    cmd.add_argument("--duration", type=float, default=30, help="Seconds per connection after pairing")
    cmd.add_argument("--cycles", type=int, default=1, help="Disconnect/reconnect cycles to test")
    cmd.add_argument("--demo", action="store_true", help="Use simulated agents instead of the event journal")
    cmd.add_argument("--store")
    for name in ("status", "list"):
        cmd = sub.add_parser(name, help="Show tracked sessions as JSON" if name == "status" else "List session IDs and names")
        cmd.add_argument("--store")
    cmd = sub.add_parser("rename", help="Set a persistent session display name")
    cmd.add_argument("session", help="ID from list (unique prefix of at least 4 characters), or full Codex session ID")
    cmd.add_argument("name", nargs="?", help="Display name, 1-32 characters; quote names containing spaces")
    cmd.add_argument("--reset", action="store_true", help="Return to the automatic project name")
    cmd.add_argument("--store")
    cmd = sub.add_parser("install-hooks", help="Preview or install Codex hook configuration")
    cmd.add_argument("--config", help="Hook configuration path; defaults to ~/.codex/hooks.json")
    cmd.add_argument("--apply", action="store_true", help="Back up existing configuration and save the merge")
    cmd = sub.add_parser("notify-test", help="Emit a temporary test notification; executes no agent command")
    cmd.add_argument("--store")
    cmd.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.command in {"run", "demo"}:
            if args.duration is not None and args.duration <= 0:
                parser.error("--duration must be positive")
            from .runtime import connection_settings
            settings = connection_settings(args.transport, args.port, args.address)
            run(args.port, args.store, args.command == "demo", args.duration, args.focus_script, args.status_file, args.instance,
                settings["transport"], settings["address"])
        elif args.command == "gui":
            from .gui import launch
            launch(args.store)
        elif args.command == "background":
            from .runtime import BridgeManager
            manager = BridgeManager()
            if args.action == "stop":
                manager.stop()
                print("Background bridge stopped")
            else:
                pid = manager.restart() if args.action == "restart" else manager.start(args.port, args.demo, args.store, args.transport, args.address)
                print(f"Background bridge running (PID {pid})")
        elif args.command == "ports":
            ports = flipper_ports()
            for port in ports:
                print(f"{port.device}: {port.description} ({port.hwid})")
            if not ports:
                print("No Flipper USB ports found. Use a data cable and turn the device on.")
        elif args.command == "bluetooth":
            if (getattr(args, "duration", 1) <= 0 or getattr(args, "timeout", 1) <= 0 or
                    getattr(args, "cycles", 1) <= 0):
                parser.error("Bluetooth duration, timeout, and cycles must be positive")
            from .bluetooth import main as bluetooth_main
            bluetooth_main(args)
        elif args.command == "install-hooks":
            install_hooks(args.config, args.apply)
        elif args.command in {"status", "list", "rename"}:
            store = EventStore(args.store)
            try:
                deck = Deck()
                drain(store, deck, 0)
                remove_dead(deck)
                if args.command == "status":
                    print(json.dumps({"agents": deck.view(limit=None), "overflow": deck.overflow}, indent=2))
                elif args.command == "list":
                    print(f"{'ID':16}  {'NAME':32}  {'STATE':8}  PROJECT")
                    for row in deck.view(limit=None):
                        clean = lambda value: "".join(c if c.isprintable() else " " for c in value)
                        print(f"{row['id']}  {clean(row['label']):32}  {row['state']:8}  {clean(row['project'])}")
                else:
                    if args.reset == (args.name is not None):
                        parser.error("Supply a name or --reset, but not both")
                    row = rename_session(store, deck, args.session, None if args.reset else args.name)
                    print(f"{row['id']}: {'automatic project name restored' if args.reset else 'name saved'}")
            finally:
                store.close()
        elif args.command == "notify-test":
            if not 0 < args.seconds <= 300:
                parser.error("--seconds must be between 0 and 300")
            store = EventStore(args.store)
            session = "test-" + secrets.token_hex(8)
            try:
                store.append({"session_id": session, "label": "TEST only", "kind": "PermissionRequest", "detail": "Test alert - no command runs"})
                print("Test notification sent. It will clear automatically.", flush=True)
                time.sleep(args.seconds)
            except KeyboardInterrupt:
                pass
            finally:
                store.append({"session_id": session, "kind": "SessionEnd"})
                store.close()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"Agent Deck: {error}\n")


if __name__ == "__main__":
    main()
