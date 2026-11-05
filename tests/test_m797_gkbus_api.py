"""Exercise Kefico against the current gkbus protocol/transport without hardware."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flasher import m797
from gkbus.channel import KLineChannel, KLineTiming
from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000
from gkbus.transport import Kwp2000OverKLineTransport


class MemoryKLine(KLineChannel):
    def __init__(self):
        super().__init__(timing=KLineTiming(p1_max=25, p3_min=60, p4_min=1))
        self.opened = False
        self.pending = bytearray()
        self.frames = []
        self.resets = 0
        self.fail_zero_delay = False
        self.fail_stop = False
        self.read_timeouts = []

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def is_open(self):
        return self.opened

    def set_baudrate(self, baudrate):
        self.baudrate = baudrate

    def reset_buffers(self):
        self.pending.clear()
        self.resets += 1

    def fast_init(self, payload):
        return b''

    def write(self, data):
        assert isinstance(data, bytes)
        self.frames.append(data)
        if data == b'\x01\x82\x83':
            if not self.fail_stop:
                self.pending += b'\x01\xC2\xC3'
            return len(data)
        assert sum(data[:-1]) & 0xFF == data[-1]
        pdu = data[3:-1] if data[0] != 0x80 else data[4:-1]
        if self.fail_zero_delay and self.timing.p3_min == 0 and pdu == b'\x3E\x01':
            self.fail_zero_delay = False
            return len(data)
        response = (b'\x67\x81\x00\x00\x00\x00' if pdu[:2] == b'\x27\x81'
                    else bytes((pdu[0] + 0x40,)) + pdu[1:2])
        frame = bytes((0x80 + len(response), 0xF1, 0x11)) + response
        self.pending += frame + bytes((sum(frame) & 0xFF,))
        return len(data)

    def read(self, size, timeout):
        self.read_timeouts.append(timeout)
        if len(self.pending) < size:
            self.pending.clear()
            raise TimeoutException
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data


class CurrentGkbusTests(unittest.TestCase):
    def setUp(self):
        self.channel = MemoryKLine()
        self.transport = Kwp2000OverKLineTransport(self.channel, 0x11, 0xF1, timeout=0.25)
        self.bus = kwp2000.Kwp2000Protocol(self.transport)
        self.bus.init(kwp2000.commands.StartCommunication())
        self.addCleanup(self.bus.close)

    def test_delay_conversion_preserves_other_timing_parameters(self):
        for delay, milliseconds in ((0.020, 20), (0.002, 2), (0, 0)):
            m797._m797_set_write_delay(self.bus, delay)
            self.assertEqual(self.channel.get_timing(), KLineTiming(25, milliseconds, 1))

    def test_programming_session_restores_timing_on_error(self):
        original = self.channel.get_timing()
        with patch('builtins.print'), self.assertRaisesRegex(RuntimeError, 'test failure'):
            with m797._m797_programming_session(self.bus, 'fast'):
                self.assertEqual(self.channel.baudrate, 108800)
                self.assertEqual(self.channel.timing.p3_min, 2)
                self.assertEqual(self.transport.timeout, m797.M797_RESPONSE_TIMEOUT)
                raise RuntimeError('test failure')
        self.assertEqual(self.channel.timing, original)

    def test_read_speed_handoff_and_buffer_reset(self):
        with patch.object(m797.time, 'sleep'), patch('builtins.print'):
            m797._m797_switch_read_baud(self.bus, 'fast')
            self.assertEqual(self.channel.baudrate, 108800)
            self.assertEqual(self.channel.timing.p3_min, 0)
            m797._m797_restore_startup_baud(self.bus, 'fast')
        self.assertEqual(self.channel.baudrate, 10400)
        self.assertEqual(self.channel.resets, 2)

    def test_read_delay_fallback(self):
        self.channel.fail_zero_delay = True
        with patch.object(m797.time, 'sleep'), patch('builtins.print'):
            m797._m797_switch_read_baud(self.bus, 'standard')
        self.assertEqual(self.channel.baudrate, 57600)
        self.assertEqual(self.channel.timing.p3_min, 20)

    def test_calibration_flow_uses_byte_channel_and_explicit_timeout(self):
        original = self.channel.timing
        with tempfile.TemporaryDirectory() as directory:
            image = bytes(m797.M797_IMAGE_SIZE)
            filename = Path(directory) / 'calibration.bin'
            filename.write_bytes(image)
            progress = Mock()
            with patch.object(m797, 'm797_correct_calibration_checksums', return_value=image), \
                 patch.object(m797, 'get_m797_calibration_id', return_value='UNKNOWN'), \
                 patch.object(m797, 'm797_read', return_value=bytes(512)), \
                 patch('builtins.print'):
                m797.m797_flash(self.bus, str(filename), 'fast', False, progress, True)
        self.assertEqual(self.channel.frames[-1], b'\x01\x82\x83')
        self.assertEqual(self.channel.read_timeouts[-1], m797.M797_RESPONSE_TIMEOUT)
        self.assertEqual(self.channel.baudrate, 108800)
        self.assertEqual(self.channel.timing, original)
        self.assertEqual(sum(call.args[0] for call in progress.call_args_list), 0x10000)

    def test_stop_timeout_does_not_discard_successful_verification(self):
        self.channel.fail_stop = True
        with tempfile.TemporaryDirectory() as directory:
            image = bytes(m797.M797_IMAGE_SIZE)
            filename = Path(directory) / 'calibration.bin'
            filename.write_bytes(image)
            with patch.object(m797, 'm797_correct_calibration_checksums', return_value=image), \
                 patch.object(m797, 'get_m797_calibration_id', return_value='UNKNOWN'), \
                 patch.object(m797, 'm797_read', return_value=bytes(512)), \
                 patch('builtins.print') as output:
                m797.m797_flash(self.bus, str(filename), 'standard', False, Mock(), True)
        self.assertTrue(any('Power-cycle' in str(call) for call in output.call_args_list))


if __name__ == '__main__':
    unittest.main()
