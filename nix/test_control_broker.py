import importlib.util
import json
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('broker', Path(__file__).with_name('control_broker.py'))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class Connection:
    def __init__(self, request, uid=1001):
        self.request, self.uid, self.response = request, uid, None
    def getsockopt(self, *args):
        return struct.pack('3i', 42, self.uid, 1001)
    def settimeout(self, timeout):
        pass
    def recv(self, size):
        result, self.request = self.request[:size], self.request[size:]
        return result
    def sendall(self, data):
        self.response = json.loads(data)


class BrokerTests(unittest.TestCase):
    def test_fixed_actions(self):
        for action in broker.ACTIONS:
            cmd = broker.command(action)
            self.assertIn('supybot.service', ' '.join(cmd))
            self.assertNotIn('sh', cmd)
        self.assertEqual(broker.command('restart'),
                         ['@systemctl@', '--no-ask-password', 'restart', 'supybot.service'])
        self.assertIn('--lines=100', broker.command('logs'))

    def test_bad_actions(self):
        for action in ['restart sshd', 'daemon-reload', 'enable', '/bin/sh', 'logs --unit=sshd', 'start\nstop']:
            with self.assertRaises(ValueError):
                broker.command(action)

    def test_peer_uid(self):
        c = Connection(b'restart\n', uid=1002)
        with patch.object(broker, 'execute') as run:
            broker.serve_connection(c, 1001)
        run.assert_not_called()
        self.assertIsNone(c.response)

    def test_protocol_rejects_trailing_payload(self):
        for request in [b'restart\nstop\n', b'logs --unit=sshd\n', b'\xff', b'x' * 64]:
            c = Connection(request)
            with patch.object(broker, 'execute') as run:
                broker.serve_connection(c, 1001)
            run.assert_not_called()
            self.assertIn('error', c.response)

    def test_allowed_action(self):
        c = Connection(b'status\n')
        with patch.object(broker, 'execute', return_value={'action': 'status'}) as run:
            broker.serve_connection(c, 1001)
        run.assert_called_once_with('status')
        self.assertEqual(c.response, {'action': 'status'})

    def test_clean_fixed_argv_and_output_bound(self):
        def fake_run(argv, **kwargs):
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['cwd'], '/')
            self.assertEqual(kwargs['timeout'], 30)
            self.assertNotIn('PATH', kwargs['env'])
            kwargs['stdout'].write(b'x' * (broker.MAX_OUTPUT + 10))
            return type('Result', (), {'returncode': 0})()
        with patch.object(broker.subprocess, 'run', side_effect=fake_run):
            result = broker.execute('logs')
        self.assertEqual(len(result['stdout']), broker.MAX_OUTPUT)
        self.assertLess(len(json.dumps(result)), 128 * 1024)


if __name__ == '__main__':
    unittest.main()
