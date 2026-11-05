"""CLI output and GUI signals share messages without redirecting stdout."""
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import Mock, call, patch

from flasher import m797, mtos, rsw
from flasher.checksum import m797_correct_image_checksums
from flasher.rsw import log
from gkbus.exceptions import TimeoutException
from tests.test_m797_checksums import m797_checksum_image


class LoggingTests(unittest.TestCase):
    def test_existing_frontends_share_cli_fallback_and_gui_signal_behavior(self):
        output, signal = StringIO(), Mock()
        with redirect_stdout(output):
            rsw.log('RSW message', signal)
            mtos.log('MTOS message', signal)
            log('CLI message')
        self.assertEqual(output.getvalue(), 'CLI message\n')
        self.assertEqual(signal.emit.call_args_list, [call('RSW message'), call('MTOS message')])

    def test_nested_read_retry_uses_callback_without_console_output(self):
        output, signal, bus = StringIO(), Mock(), Mock()
        response = Mock()
        response.get_data.return_value = b'\xF0\xAA\xBB'
        bus.execute.side_effect = [TimeoutException(), response]
        with redirect_stdout(output), patch.object(m797.time, 'sleep'):
            result = m797._m797_read_dynamic_lid(bus, 0xF0, 2, log_callback=signal)
        self.assertEqual(result, b'\xAA\xBB')
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(signal.emit.call_count, 1)
        self.assertIn('attempt 1/3 failed', signal.emit.call_args.args[0])

    def test_checksum_console_and_callback_messages_match(self):
        image, console, signal = m797_checksum_image(), StringIO(), Mock()
        with redirect_stdout(console):
            expected = m797_correct_image_checksums(image)
        callback_output = StringIO()
        with redirect_stdout(callback_output):
            actual = m797_correct_image_checksums(image, log_callback=signal)
        self.assertEqual(actual, expected)
        self.assertEqual(callback_output.getvalue(), '')
        self.assertEqual(console.getvalue(), ''.join(message.args[0] + '\n' for message in signal.emit.call_args_list))


if __name__ == '__main__':
    unittest.main()
