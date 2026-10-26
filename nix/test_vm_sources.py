"""VM Python lives outside Nix strings; validate it without starting a VM."""
import ast
from pathlib import Path
import re
import unittest


class VmSourceTests(unittest.TestCase):
    def test_all_python_fixtures_parse(self):
        directory = Path(__file__).with_name('vm')
        self.assertEqual(len(list(directory.glob('*.py'))), 7)
        for source in directory.glob('*.py'):
            with self.subTest(source=source.name):
                ast.parse(source.read_text(), filename=str(source))

    def test_driver_uses_only_declared_immutable_path_markers(self):
        directory = Path(__file__).parent
        driver = (directory / 'vm/driver.py').read_text()
        markers = set(re.findall(r'@[A-Za-z]+@', driver))
        self.assertEqual(markers, {'@plainPython@', '@packagePython@', '@controlClient@',
                                   '@fileProbe@', '@publishFixture@', '@bundleProbe@',
                                   '@workerProbe@', '@invalidProbe@'})
        nix = (directory / 'test-vm.nix').read_text()
        for marker in markers:
            self.assertIn('"' + marker + '"', nix)
            driver = driver.replace(marker, '/nix/store/test-immutable-path')
        self.assertNotIn('${', driver)
        ast.parse(driver)
