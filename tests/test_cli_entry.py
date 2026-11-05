"""CLI exit status, cleanup and offline operation without physical adapters."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_m797_checksums import simk_checksum_image


ROOT = Path(__file__).resolve().parents[1]


class CliEntryTests(unittest.TestCase):
    def test_operation_error_returns_failure_and_closes_device(self):
        script = '''
import runpy, sys
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_m797_gkbus_api import MemoryKLine
channel = MemoryKLine()
device = SimpleNamespace(kline=lambda: channel, description=lambda: 'test',
                         close=lambda: print('DEVICE CLOSED'))
sys.argv = ['gkflasher.py', '--protocol', 'kline', '--interface', 'test',
            '--ecu-family', 'm797', '--flash-program', 'unused.bin']
with patch('gkbus.device.find_device', return_value=device):
    runpy.run_path('gkflasher.py', run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('--flash-program', result.stdout)
        self.assertIn('DEVICE CLOSED', result.stdout)

    def test_simk_checksum_correction_never_opens_an_adapter(self):
        script = '''
import runpy
from unittest.mock import patch
with patch('gkbus.device.find_device', side_effect=AssertionError('adapter opened')):
    runpy.run_path('gkflasher.py', run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'simk.bin'
            filename.write_bytes(simk_checksum_image())
            result = subprocess.run([sys.executable, '-B', '-c', script,
                                     '--correct-checksum', str(filename)], cwd=ROOT,
                                    input='n\n', text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Initializing', result.stdout)
        self.assertNotIn('adapter opened', result.stderr)


if __name__ == '__main__':
    unittest.main()
