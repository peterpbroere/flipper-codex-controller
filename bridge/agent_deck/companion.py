"""Window-independent companion operations, serialized by the UI worker."""
from .model import Deck
from .names import rename_session
from .runtime import BridgeManager, tail
from .service import drain, remove_dead
from .store import EventReader, EventStore
from .control_channel import ControlClient


class Companion:
    def __init__(self, store_path=None, manager=None):
        self.reader = EventReader(store_path)
        self.manager = manager or BridgeManager()
        self.deck = Deck()
        self.cursor = 0
        self.controls = ControlClient(self.manager)

    def refresh(self):
        errors = []
        try:
            self.cursor = drain(self.reader, self.deck, self.cursor)
            remove_dead(self.deck)
        except Exception as error:
            errors.append("Could not read sessions: " + str(error))
        try:
            health = self.manager.health()
        except Exception as error:
            health = {"running": None, "connected": False, "message": "Bridge status unavailable"}
            errors.append(str(error))
        try:
            logs = tail(self.manager.log_path)
        except OSError as error:
            logs = ""
            errors.append("Could not read activity: " + str(error))
        if health.get("error"):
            errors.append(health["error"])
        try:
            connection = self.manager.connection()
        except Exception as error:
            connection = dict(transport="usb", port="auto", address="")
            errors.append("Could not read connection settings: " + str(error))
        rows = self.deck.view(limit=None)
        self.controls.decorate(health, rows)
        return {"rows": rows, "health": health, "logs": logs, "connection_settings": connection,
                "error": "\n".join(errors)}

    def act(self, selector, verb, token):
        self.refresh()
        agent = self.deck.agents.get(selector)
        if not agent:
            raise ValueError("Session is no longer available")
        self.controls.execute(selector, agent.session_id, verb, token)
        return self.refresh()

    def close(self):
        pass  # Windows do not own a Codex connection or the background bridge.

    def rename(self, selector, name=None):
        self.refresh()
        store = EventStore(self.reader.path)
        try:
            rename_session(store, self.deck, selector, name)
        finally:
            store.close()
        return self.refresh()

    def control(self, action):
        if action == "start":
            self.manager.start(store_path=self.reader.path)
        elif action == "stop":
            self.manager.stop()
        elif action == "restart":
            self.manager.restart()
        else:
            raise ValueError("Unknown bridge action")
        return self.refresh()

    def scan_bluetooth(self):
        import asyncio
        from .bluetooth import scan
        devices = asyncio.run(scan())
        result = self.refresh()
        result["bluetooth_devices"] = devices
        return result

    def connect(self, transport, port="auto", address=""):
        from .runtime import connection_settings
        settings = connection_settings(transport, port, address)
        if transport == "bluetooth":
            from .bluetooth import backend
            backend()  # Do not stop a working bridge if the dependency is absent.
        self.manager.stop()
        self.manager.configure(**settings)
        self.manager.start(store_path=self.reader.path)
        return self.refresh()
