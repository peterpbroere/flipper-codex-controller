from __future__ import annotations

import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import serial

from .focus import FocusRunner
from .controls import Controls
from .control_channel import CommandChannel, ControlPublisher
from .model import Deck, demo_deck
from .processes import classify_owner, is_alive
from .protocol import parse_action, snapshot
from .store import EventStore
from .runtime import StatusWriter
from .transport import SerialLink

log = logging.getLogger(__name__)


def drain(store, deck, cursor):
    while True:
        events = store.read(cursor)
        for index, event in events:
            deck.apply(index, event)
            cursor = index
        if len(events) < 1000:
            return cursor


def remove_dead(deck):
    for key, agent in list(deck.agents.items()):
        alive = is_alive(agent.owner_pid, agent.owner_created)
        if alive is False:
            del deck.agents[key]
            continue
        if agent.owner_kind == "unknown" and alive is True:
            agent.owner_kind = classify_owner(agent.owner_pid, agent.owner_created)
        # Inactivity says nothing about whether a session is still open.
        # Preserve its reported state until a hook or confirmed exit changes it.


def run(port="auto", store_path=None, demo=False, duration=None, focus_script=None, status_file=None, instance=""):
    store = None if demo else EventStore(store_path)
    deck = demo_deck() if demo else Deck()
    focus = FocusRunner(focus_script)
    link = SerialLink(port)
    cursor = 0
    next_frame = 0
    next_cleanup = 0
    pending_focus = None
    controls = Controls()
    control_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-deck-controls")
    syncing = None
    pending_action = None
    pending_pc = None
    channel = CommandChannel(Path(status_file).parent) if status_file and instance else None
    publisher = ControlPublisher(Path(status_file).parent, instance) if channel else None
    deadline = time.monotonic() + duration if duration else float("inf")
    signature = None
    status = StatusWriter(status_file, instance)
    last_error = ""

    def publish(running=True):
        nonlocal last_error
        try:
            if publisher and time.monotonic() >= publisher.next_update:
                views = controls.decorate(deck.view(limit=None)) if running else []
                if not focus.enabled:
                    for row in views:
                        row["actions"] = [verb for verb in row["actions"] if verb != "focus"]
                publisher.update(views)
                publisher.next_update = time.monotonic() + .5
            status.update(force=not running, running=running,
                          connected=running and link.port is not None and time.monotonic() - link.last_ack <= 3,
                          port=link.port.port if link.port is not None else "",
                          error=last_error, mode="demo" if demo else "live",
                          count=len(deck.agents), attention=sum(a.state == "waiting" for a in deck.agents.values()),
                          control_revision=publisher.revision if publisher else "",
                          control_connected=controls.connected, control_error=controls.error)
        except OSError as error:
            log.warning("Could not update companion status: %s", error)
    try:
        log.info("%s mode. Launch Agent Deck on the Flipper; Ctrl+C stops the bridge.", "Demo" if demo else "Live")
        while time.monotonic() < deadline:
            if store:
                cursor = drain(store, deck, cursor)
            if time.monotonic() >= next_cleanup:
                remove_dead(deck)
                next_cleanup = time.monotonic() + 2
            rows = deck.view()
            if pending_pc and pending_pc[0].done():
                future, key = pending_pc
                pending_pc = None
                try:
                    future.result()
                    channel.finish(key)
                except Exception as error:
                    channel.finish(key, str(error))
                    log.warning("PC agent action failed: %s", error)
            if channel and not (pending_pc or pending_action or pending_focus):
                command = channel.take(instance)
                if command:
                    agent = deck.agents.get(command["agent"])
                    if not agent or agent.session_id != command["session"]:
                        channel.finish(command["id"], "Session no longer available")
                    elif command["verb"] == "focus":
                        try:
                            pending_pc = (focus.open_session(agent.session_id, agent.tab, agent.project), command["id"])
                        except Exception as error:
                            channel.finish(command["id"], str(error))
                    else:
                        pending_pc = (control_worker.submit(controls.execute, agent.session_id,
                            command["verb"], command["token"]), command["id"])
            if not demo and pending_action is None and pending_pc is None and (syncing is None or syncing.done()):
                syncing = control_worker.submit(controls.sync, deck.view(limit=None))
            publish()
            current_signature = [(r["id"], r["state"], r["event"]) for r in rows]
            if signature != current_signature:
                log.info("%d agents, %d need attention%s", len(rows), sum(r["state"] == "waiting" for r in rows),
                         f"; {deck.overflow} additional sessions exceed the device capacity" if deck.overflow else "")
                signature = current_signature
            was_connected = link.port is not None
            if not link.connect():
                time.sleep(0.1)
                continue
            if not was_connected:
                deck.new_connection()
                next_frame = 0
                last_error = ""
            try:
                if time.monotonic() >= next_frame:
                    session, revision, rows = deck.wire_view()
                    if not focus.enabled:
                        for row in rows:
                            row["tab"] = ""
                    controls.decorate(rows, device=True)
                    if not focus.enabled:
                        for row in rows:
                            row["actions"] = [verb for verb in row["actions"] if verb != "focus"]
                    link.send(snapshot(session, revision, rows))
                    next_frame = time.monotonic() + 0.5
                for raw in link.receive():
                    if raw.startswith(b"ACK\t"):
                        fields = raw.decode("ascii").strip().split("\t")
                        if len(fields) == 8 and fields[1] == deck.session:
                            link.last_ack = time.monotonic()
                            log.debug("Device ACK count=%s vibration=%s alerts=%s buzzes=%s selected=%s", *fields[3:])
                    elif raw.startswith(b"ACT\t"):
                        try:
                            action = parse_action(raw)
                            agent = deck.accept(action)
                            if pending_focus or pending_action or pending_pc:
                                raise ValueError("An action is already running")
                            if action.verb == "focus":
                                if not agent.tab and "focus" not in controls.decorate([dict(session_id=agent.session_id)])[0]["actions"]:
                                    raise ValueError("Session is not on the shared local server")
                                pending_focus = (focus.open_session(agent.session_id, agent.tab, agent.project), deck.session)
                            else:
                                pending_action = (control_worker.submit(controls.execute, agent.session_id,
                                    action.verb, action.token, True), deck.session, action.sequence, action.verb)
                        except (ValueError, KeyError) as error:
                            log.warning("Action ignored: %s", error)
                            link.send(b"RESULT\tERROR\n")
                if pending_action and pending_action[0].done():
                    future, connection, sequence, verb = pending_action
                    pending_action = None
                    try:
                        future.result()
                        log.info("Agent action confirmed: %s", verb)
                        if connection == deck.session:
                            link.send(f"RESULT\t{sequence}\tOK\t{verb}\n".encode("ascii"))
                    except Exception as error:
                        log.warning("Agent action failed: %s", error)
                        if connection == deck.session:
                            link.send(f"RESULT\t{sequence}\tERROR\t{verb}\n".encode("ascii"))
                if pending_focus and pending_focus[0].done():
                    try:
                        pending_focus[0].result()
                        if pending_focus[1] == deck.session:
                            link.send(b"RESULT\tOK\n")
                        log.info("Terminal focused")
                    except Exception as error:
                        log.warning("Terminal focus failed: %s", error)
                        link.send(b"RESULT\tERROR\n")
                    pending_focus = None
                if time.monotonic() - link.last_ack > 3:
                    raise serial.SerialException("Device stopped acknowledging snapshots")
            except (serial.SerialException, OSError) as error:
                log.warning("USB disconnected: %s", error)
                last_error = str(error)
                link.close()
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        publish(running=False)
        focus.close()
        control_worker.shutdown(wait=True)
        controls.close()
        if channel:
            channel.close()
        if store:
            store.close()
