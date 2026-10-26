"""VM Python lives outside Nix strings; validate it without starting a VM."""
import ast
from pathlib import Path
import re
import unittest


class VmSourceTests(unittest.TestCase):
    def test_all_python_fixtures_parse(self):
        directory = Path(__file__).with_name('vm')
        self.assertEqual(len(list(directory.glob('*.py'))), 10)
        for source in directory.glob('*.py'):
            with self.subTest(source=source.name):
                ast.parse(source.read_text(), filename=str(source))

    def test_driver_uses_only_declared_immutable_path_markers(self):
        directory = Path(__file__).parent
        driver = (directory / 'vm/driver.py').read_text()
        markers = set(re.findall(r'@[A-Za-z]+@', driver))
        self.assertEqual(markers, {'@plainPython@', '@packagePython@', '@controlClient@',
                                   '@fileProbe@', '@publishFixture@', '@bundleProbe@',
                                   '@workerProbe@', '@invalidProbe@', '@httpControllerProbe@', '@publicHttpProbe@'})
        nix = (directory / 'test-vm.nix').read_text()
        for marker in markers:
            self.assertIn('"' + marker + '"', nix)
            driver = driver.replace(marker, '/nix/store/test-immutable-path')
        self.assertNotIn('${', driver)
        ast.parse(driver)

    def test_http_probe_requires_admission_not_just_error_classification(self):
        import json
        import runpy
        import socket
        from unittest.mock import MagicMock, patch
        probe = Path(__file__).with_name('vm') / 'public_http_probe.py'
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [b'READY\n', json.dumps({
            'error': 'denied public destination',
            'operation_id': 'vm-blocked-local-request',
            'outcome': 'failed_before_send'}).encode(), b'']
        with patch.object(socket, 'socket', return_value=connection):
            runpy.run_path(str(probe))
        connection.sendall.assert_called_once()
        denied = MagicMock()
        denied.__enter__.return_value = denied
        denied.connect.side_effect = PermissionError('wrong Unix UID')
        with patch.object(socket, 'socket', return_value=denied):
            with self.assertRaises(PermissionError):
                runpy.run_path(str(probe))
        denied.sendall.assert_not_called()
