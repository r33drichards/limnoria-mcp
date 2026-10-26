import ast
import importlib.util
import json
from pathlib import Path
import struct
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('tcl_broker', Path(__file__).with_name('tcl_broker.py'))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class Connection:
    def __init__(self, data, uid=42):
        self.data, self.uid, self.response = data, uid, None
        self.ready = False
    def getsockopt(self, *args):
        return struct.pack('3i', 1, self.uid, 42)
    def settimeout(self, timeout):
        assert timeout > 0
    def recv(self, size):
        assert self.ready
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk
    def sendall(self, data):
        if data == b'READY\n':
            self.ready = True
        else:
            self.response = json.loads(data)


class TclBrokerTests(unittest.TestCase):
    def test_peer_rejection(self):
        execute = Mock()
        conn = Connection(b'{}\n', uid=99)
        broker.serve_connection(conn, 42, execute)
        execute.assert_not_called()
        self.assertIsNone(conn.response)
        self.assertFalse(conn.ready)

    def test_valid_frame(self):
        request = {'script': 'reply hello', 'state': {}, 'context': {}}
        conn = Connection(json.dumps(request).encode() + b'\n')
        execute = Mock(return_value={'replies': ['hello'], 'state': {}})
        broker.serve_connection(conn, 42, execute)
        execute.assert_called_once_with(request)
        self.assertEqual(conn.response['replies'], ['hello'])

    def test_late_final_chunk_never_executes(self):
        execute = Mock()
        conn = Connection(b'{}\n')
        with patch.object(broker.time, 'monotonic', side_effect=[10, 10, 14]):
            broker.serve_connection(conn, 42, execute)
        execute.assert_not_called()
        self.assertIn('error', conn.response)

    def test_bad_framing(self):
        for data in [b'{}', b'{}\n{}\n', b'\xff\n', b'x' * (broker.MAX_REQUEST + 1)]:
            execute = Mock()
            conn = Connection(data)
            broker.serve_connection(conn, 42, execute)
            execute.assert_not_called()
            self.assertIn('error', conn.response)

    def test_subprocess_protocol_literals_are_real_newlines(self):
        tree = ast.parse(Path(__file__).read_text())
        assignment = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'client_code'
                                  for target in node.targets))
        constants = [node.value for node in ast.walk(ast.parse(ast.literal_eval(assignment.value)))
                     if isinstance(node, ast.Constant) and isinstance(node.value, bytes)]
        self.assertIn(bytes([82, 69, 65, 68, 89, 10]), constants)
        self.assertIn(bytes([10]), constants)

    def test_independent_restarted_client_cannot_queue_payload(self):
        # Real Unix sockets and independent processes: no runtime process-local
        # lock can make this pass. The second client represents a restarted bot.
        client_code = r"""
import json, socket, sys
with socket.socket(socket.AF_UNIX) as conn:
    conn.settimeout(float(sys.argv[3]))
    conn.connect(sys.argv[1])
    ready = b''
    try:
        while len(ready) < 6:
            chunk = conn.recv(6 - len(ready))
            if not chunk:
                raise RuntimeError('admission closed')
            ready += chunk
    except TimeoutError:
        assert sys.argv[2] == 'new-bot'
        print('NO-PAYLOAD-SENT')
        raise SystemExit(0)
    assert ready == b'READY\n'
    assert sys.argv[2] == 'old-bot', 'queued client admitted unexpectedly'
    conn.sendall(json.dumps({'script': sys.argv[2]}).encode() + b'\n')
    assert b'replies' in conn.recv(4096)
"""
        entered, release = threading.Event(), threading.Event()
        calls, failures = [], []
        def execute(request):
            calls.append(request['script'])
            entered.set()
            assert release.wait(5)
            return {'replies': [], 'state': {}, 'effects': []}
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/worker.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen(2)
                listener.settimeout(5)
                def serve():
                    try:
                        for _ in range(2):
                            conn, _ = listener.accept()
                            with conn:
                                try:
                                    broker.serve_connection(conn, os.getuid(), execute)
                                except (BrokenPipeError, ConnectionResetError):
                                    pass
                    except Exception as exc:
                        failures.append(exc)
                server = threading.Thread(target=serve)
                server.start()
                old = subprocess.Popen([sys.executable, '-I', '-c', client_code, path, 'old-bot', '4'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.assertTrue(entered.wait(3))
                    new = subprocess.run([sys.executable, '-I', '-c', client_code, path, 'new-bot', '.15'],
                                         capture_output=True, timeout=3)
                    self.assertEqual(new.returncode, 0, new.stderr)
                    self.assertIn(b'NO-PAYLOAD-SENT', new.stdout)
                finally:
                    release.set()
                    stdout, stderr = old.communicate(timeout=5)
                    server.join(timeout=5)
                self.assertEqual(old.returncode, 0, stderr)
                self.assertFalse(server.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(calls, ['old-bot'])

    def test_failure_preserves_trusted_effect_receipts(self):
        error = RuntimeError('secret body must not be returned')
        error.effects = [{'operation_id': 'operation-1', 'outcome': 'unknown'}]
        conn = Connection(b'{}\n')
        broker.serve_connection(conn, 42, Mock(side_effect=error))
        self.assertEqual(conn.response['effects'], error.effects)
        self.assertNotIn('secret', json.dumps(conn.response))

    def test_failure_and_output_limit(self):
        for execute in [Mock(side_effect=ValueError('private details')),
                        Mock(return_value={'replies': ['x' * broker.MAX_RESPONSE]})]:
            conn = Connection(b'{}\n')
            broker.serve_connection(conn, 42, execute)
            self.assertEqual(conn.response, {'error': 'script execution rejected or failed', 'effects': []})


if __name__ == '__main__':
    unittest.main()
