import importlib.util
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('tcl_broker', Path(__file__).with_name('tcl_broker.py'))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class Connection:
    def __init__(self, data, uid=42):
        self.data, self.uid, self.response = data, uid, None
    def getsockopt(self, *args):
        return struct.pack('3i', 1, self.uid, 42)
    def settimeout(self, timeout):
        assert timeout > 0
    def recv(self, size):
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk
    def sendall(self, data):
        self.response = json.loads(data)


class TclBrokerTests(unittest.TestCase):
    def test_peer_rejection(self):
        execute = Mock()
        conn = Connection(b'{}\n', uid=99)
        broker.serve_connection(conn, 42, execute)
        execute.assert_not_called()
        self.assertIsNone(conn.response)

    def test_valid_frame(self):
        request = {'script': 'reply hello', 'state': {}, 'context': {}}
        conn = Connection(json.dumps(request).encode() + b'\n')
        execute = Mock(return_value={'replies': ['hello'], 'state': {}})
        broker.serve_connection(conn, 42, execute)
        execute.assert_called_once_with(request)
        self.assertEqual(conn.response['replies'], ['hello'])

    def test_bad_framing(self):
        for data in [b'{}', b'{}\n{}\n', b'\xff\n', b'x' * (broker.MAX_REQUEST + 1)]:
            execute = Mock()
            conn = Connection(data)
            broker.serve_connection(conn, 42, execute)
            execute.assert_not_called()
            self.assertIn('error', conn.response)

    def test_failure_and_output_limit(self):
        for execute in [Mock(side_effect=ValueError('private details')),
                        Mock(return_value={'replies': ['x' * broker.MAX_RESPONSE]})]:
            conn = Connection(b'{}\n')
            broker.serve_connection(conn, 42, execute)
            self.assertEqual(conn.response, {'error': 'script execution rejected or failed'})


if __name__ == '__main__':
    unittest.main()
