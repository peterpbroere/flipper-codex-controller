from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent_deck.control_channel import CommandChannel, ControlClient, ControlPublisher


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)

    def test_commands_are_claimed_once_and_scoped_to_bridge_instance(self):
        channel = CommandChannel(self.path)
        self.addCleanup(channel.close)
        old = channel.enqueue('old', 'agent', 'session', 'stop', '1234abcd')
        current = channel.enqueue('new', 'agent', 'session', 'approve', '1234abcd')
        command = channel.take('new')
        self.assertEqual(current, command['id'])
        self.assertEqual(('failed', 'Request expired; review again'), channel.result(old))
        self.assertIsNone(channel.take('new'))
        channel.finish(current)
        self.assertEqual(('done', ''), channel.result(current))

    def test_expired_action_never_executes(self):
        channel = CommandChannel(self.path)
        self.addCleanup(channel.close)
        key = channel.enqueue('instance', 'agent', 'session', 'stop', '1234abcd')
        with channel.db:
            channel.db.execute('UPDATE commands SET created=?', (time.time()-10,))
        self.assertIsNone(channel.take('instance'))
        self.assertEqual('failed', channel.result(key)[0])

    def test_snapshot_requires_matching_live_instance_revision_and_session(self):
        publisher = ControlPublisher(self.path, 'instance')
        row = {'id': 'agent', 'session_id': 'session', 'actions': ['approve'], 'control_token': '1234abcd', 'review': 'exact request'}
        publisher.update([row])
        client = ControlClient(Mock(state_dir=self.path))
        health = {'running': True, 'instance': 'instance', 'control_revision': publisher.revision}
        self.assertEqual(['approve'], client.decorate(health, [dict(row)])[0]['actions'])
        for change in ({'running': False}, {'instance': 'old'}, {'control_revision': 'old'}):
            self.assertEqual([], client.decorate({**health, **change}, [dict(row)])[0]['actions'])
        self.assertEqual([], client.decorate(health, [{'id': 'agent', 'session_id': 'other'}])[0]['actions'])

    def test_two_windows_share_identical_request_token(self):
        publisher = ControlPublisher(self.path, 'instance')
        publisher.update([{'id': 'agent', 'session_id': 'session', 'actions': ['approve'], 'control_token': '1234abcd'}])
        health = {'running': True, 'instance': 'instance', 'control_revision': publisher.revision}
        clients = [ControlClient(Mock(state_dir=self.path)) for _ in range(2)]
        rows = [c.decorate(health, [{'id': 'agent', 'session_id': 'session'}])[0] for c in clients]
        self.assertEqual(rows[0], rows[1])

    def test_client_waits_for_service_confirmation_without_owning_codex_connection(self):
        publisher = ControlPublisher(self.path, 'instance')
        publisher.update([{'id': 'agent', 'session_id': 'session', 'actions': ['stop'], 'control_token': '1234abcd'}])
        manager = Mock(state_dir=self.path)
        manager.health.return_value = {'running': True, 'instance': 'instance', 'control_revision': publisher.revision}
        ready = threading.Event()
        received = []
        def service():
            channel = CommandChannel(self.path)
            ready.set()
            try:
                until = time.monotonic()+3
                while time.monotonic() < until:
                    command = channel.take('instance')
                    if command:
                        received.append(command)
                        channel.finish(command['id'])
                        return
                    time.sleep(.01)
            finally:
                channel.close()
        worker = threading.Thread(target=service)
        worker.start()
        self.assertTrue(ready.wait(timeout=2))
        ControlClient(manager).execute('agent', 'session', 'stop', '1234abcd')
        worker.join(timeout=3)
        self.assertEqual(('agent', 'session', 'stop', '1234abcd'), tuple(received[0][k] for k in ('agent', 'session', 'verb', 'token')))
