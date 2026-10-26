import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('controller_ready', Path(__file__).with_name('controller_ready.py'))
ready = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ready)


class Response:
    status = 200
    def __init__(self, document):
        self.data = json.dumps(document).encode()
    def read(self, maximum):
        return self.data[:maximum]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class ReadinessTests(unittest.TestCase):
    def test_actual_mcp_result_required(self):
        opener = Mock()
        opener.open.return_value = Response({'id': 1, 'result': {
            'protocolVersion': '2025-03-26', 'serverInfo': {'name': 'fixture'}}})
        with patch.object(ready.sys, 'argv', ['ready', '8767']), \
             patch.object(ready.urllib.request, 'build_opener', return_value=opener):
            ready.main()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8767/mcp')
        self.assertEqual(json.loads(request.data)['method'], 'initialize')

    def test_http_200_mcp_error_is_not_ready(self):
        opener = Mock()
        opener.open.return_value = Response({'id': 1, 'error': {'code': -1}})
        with patch.object(ready.sys, 'argv', ['ready', '8767']), \
             patch.object(ready.urllib.request, 'build_opener', return_value=opener), \
             patch.object(ready.time, 'monotonic', side_effect=[0, 0, 21]), \
             patch.object(ready.time, 'sleep'):
            with self.assertRaises(SystemExit):
                ready.main()

    def test_port_only_input(self):
        for port in ['0', '65536', 'localhost', '8767;id']:
            with patch.object(ready.sys, 'argv', ['ready', port]):
                with self.assertRaises(SystemExit):
                    ready.main()
