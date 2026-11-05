"""Firmware guards, RAM cleanup and post-verification reset sequencing."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flasher import m797, m797_reboot as reset
from tests.test_m797_checksums import m797_checksum_image


class LayoutTests(unittest.TestCase):
    def test_table_selected_handler_is_accepted_but_modified_code_or_rom_is_not(self):
        # Captured routine bodies, independent of calibration strings or BIN addresses.
        handler = bytes.fromhex(
            '88908860f09cf3f8cae149828d2cf2f4cee1f4a4010049a13d1df2f474e09af41090'
            'e6f5000874f574e0e11ce7f85100b989e148f7f8e6e1e7fa2900f7faf5e10d1a'
            'e6fc1100e6fd2200f0e9da002447f1c80d11e6fc1100e6fd1200f0e9da002447f1c8'
            '0d08e6fc1100e6fd1000f0e9da002447f1c8f18c98609890db00')
        table = bytearray(156)
        for index, address in ((0,0x3B66),(2,0x3266),(3,0x32AC),(5,0x36B2),
                               (9,0x381E),(13,0x395C),(14,0x3ABC),(15,0x3B1C),(16,0x844522)):
            table[index * 4:index * 4 + 4] = address.to_bytes(4, 'little')
        read = Mock(return_value=handler)
        self.assertEqual(reset._read_reset_handler(read, table), handler)
        read.assert_called_once_with(0x844522, 126)
        read.return_value = handler[:2] + bytes([handler[2] ^ 1]) + handler[3:]
        self.assertIsNone(reset._read_reset_handler(read, table))
        read.reset_mock()
        table[0] ^= 1
        self.assertIsNone(reset._read_reset_handler(read, table))
        read.assert_not_called()
        table[0] ^= 1
        for address in (0, 0x810000, 0x837709, 0x87FF84):
            table[64:68] = address.to_bytes(4, 'little')
            self.assertIsNone(reset._read_reset_handler(read, table))
            read.assert_not_called()

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.bus = Mock()
        self.bus.transport.send_read_pdu.return_value = b'\x5A\x94ST3IS00E   '
        self.bus.execute.return_value.frame.status = 0x51
        mapping = bytearray(128)
        mapping[0x11], mapping[0x3E] = 16, 9
        table = bytearray(156)
        table[64:68] = (0x83763E).to_bytes(4, 'little')
        self.table = bytes(table)
        self.read_flash = Mock(side_effect=[self.table + bytes(mapping), bytes(126)])
        self.read_ram = Mock(side_effect=lambda bus, lid, address, size:
                             bytes.fromhex('3a 1c 06 02 d6 1c 06 02') if address == 0xE228 else bytes(size))
        self.write_ram = Mock()
        self.clear = Mock()
        self.get_id = Mock(return_value='TBE4I30')

    def prepare(self):
        return reset.prepare_reboot(self.bus, self.read_flash, self.read_ram,
                                    self.write_ram, self.clear, self.get_id)

    def matching_fingerprints(self, stack):
        stack.enter_context(patch.object(reset, '_read_reset_handler',
                                        side_effect=lambda read, table: read(0x83763E, 126)))
        stack.enter_context(patch.object(reset.time, 'sleep'))
        stack.enter_context(patch('builtins.print'))

    def test_unavailable_application_uses_stock_path_without_ram_access(self):
        self.get_id.side_effect = RuntimeError('no application ID')
        self.assertIsNone(self.prepare())
        self.bus.execute.assert_not_called()
        self.read_flash.assert_not_called()
        self.write_ram.assert_not_called()

    def test_modified_code_never_installs_monitor(self):
        with patch('builtins.print'):
            self.assertIsNone(self.prepare())
        self.write_ram.assert_not_called()
        self.assertEqual(self.read_ram.call_count, 1)  # Read dispatcher pointers only.

    def test_matching_layout_does_not_require_a_calibration_allowlist(self):
        self.get_id.return_value = 'GKE2I60'
        blob = reset._build_monitor(bytes(126), self.table)
        # Discovery happens before scratch writes; initial scratch must be zero.
        reads = [bytes.fromhex('3a 1c 06 02 d6 1c 06 02')] + [bytes(128)] * 20
        reads += [blob[offset:offset + 128] for offset in range(0, len(blob), 128)]
        self.read_ram.side_effect = reads
        self.bus.transport.send_read_pdu.side_effect = [b'\x5A\x94QG6IS00E', b'\x5A\x94\x32\x05']
        with ExitStack() as stack:
            self.matching_fingerprints(stack)
            self.assertEqual(self.prepare(), b'\x5A\x94QG6IS00E')
        self.assertEqual(self.write_ram.call_count, 11)
        self.assertEqual([call.kwargs['size'] for call in self.read_flash.call_args_list], [284, 126])

    def test_malformed_live_pointer_never_reads_flash_or_writes_ram(self):
        self.read_ram.side_effect = None
        self.read_ram.return_value = b'\xFF' * 8
        with patch('builtins.print'):
            self.assertIsNone(self.prepare())
        self.read_flash.assert_not_called()
        self.write_ram.assert_not_called()

    def test_occupied_ram_is_not_overwritten(self):
        original_read = self.read_ram.side_effect
        self.read_ram.side_effect = lambda *args: (original_read(*args) if args[2] == 0xE228 else b'\x01' * args[3])
        with ExitStack() as stack:
            self.matching_fingerprints(stack)
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                self.prepare()
        self.write_ram.assert_not_called()

    def test_failed_upload_restores_scratch_ram_before_activation(self):
        # Two complete snapshots, followed by a wrong readback.
        self.read_ram.side_effect = [bytes.fromhex('3a 1c 06 02 d6 1c 06 02')] + [bytes(128)] * 31
        with ExitStack() as stack:
            self.matching_fingerprints(stack)
            with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
                self.prepare()
        restored = self.write_ram.call_args_list[1:]
        self.assertEqual(len(restored), 10)
        self.assertTrue(all(call.args[2] == bytes(128) for call in restored))
        self.assertFalse(any(call.args[1] == 0xE228 for call in self.write_ram.call_args_list))

    def test_uncertain_dispatch_write_does_not_erase_active_monitor(self):
        handler, table = bytes(126), self.table
        blob = reset._build_monitor(handler, table)
        self.read_ram.side_effect = ([bytes.fromhex('3a 1c 06 02 d6 1c 06 02')] + [bytes(128)] * 20
                                    + [blob[offset:offset + 128] for offset in range(0, len(blob), 128)])
        self.write_ram.side_effect = lambda bus, address, data: (
            (_ for _ in ()).throw(RuntimeError('lost response')) if address == 0xE228 else None)
        with ExitStack() as stack:
            self.matching_fingerprints(stack)
            with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
                self.prepare()
        self.assertEqual(len(self.write_ram.call_args_list), 11)
        self.bus.transport.send_pdu.assert_not_called()

    def test_failed_rollback_readback_aborts_before_activation(self):
        self.read_ram.side_effect = ([bytes.fromhex('3a 1c 06 02 d6 1c 06 02')]
                                    + [bytes(128)] * 21 + [b'\xFF' * 128])
        with ExitStack() as stack:
            self.matching_fingerprints(stack)
            with self.assertRaisesRegex(RuntimeError, 'RAM restoration failed'):
                self.prepare()
        self.assertFalse(any(c.args[1] == 0xE228 for c in self.write_ram.call_args_list))
        self.bus.transport.send_pdu.assert_not_called()

    def test_monitor_layout_and_pointer_address_spaces(self):
        handler = bytes(range(126))
        table = bytes(range(0x9C))
        blob = reset._build_monitor(handler, table)
        self.assertEqual(len(blob), 0x500)
        self.assertEqual(blob[0x80:0x82], b'\x00\xA8')  # DPP2 near pointer
        self.assertEqual(blob[0x23C:0x240], (0x382C80).to_bytes(4, 'little'))
        self.assertEqual(blob[0x251], 15)  # SID 0x11
        self.assertEqual(blob[0x324:0x328], (0x382980).to_bytes(4, 'little'))
        self.assertEqual(blob[0x480:0x4FE], handler)
        self.assertEqual(blob[0x180:0x188], bytes.fromhex('da 38 40 28 da 38 9c 2b'))
        self.assertEqual(blob[0x40:0x62], reset._SERIAL_SHUTDOWN)
        self.assertEqual(blob[0x300:0x39C], table[:36] + (0x382980).to_bytes(4, 'little') + table[40:])
        self.assertEqual(hashlib.sha256(blob[0x39C:0x47E]).hexdigest(),
                         '6917bf1edb80c3ca636877ccc1b2c01cd22033317eb9134a6e80de8a69b1843f')
        self.assertEqual(reset._paged_pointer(0x382B00), bytes.fromhex('00 2b e0 00'))

    def test_reset_acknowledgement_alone_is_not_success(self):
        self.bus.transport.send_read_pdu.return_value = b'\x5A\x94\x32\x05'
        with patch.object(reset.time, 'sleep'), patch('builtins.print'), \
             self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            reset.reboot(self.bus, b'\x5A\x94ST3IS00E   ')

    def test_full_flash_accepts_changed_application_id_but_rejects_rom(self):
        for software, accepted in ((b'QG6IS00E', True), (b'\x32\x05', False)):
            self.bus.transport.send_read_pdu.side_effect = [b'\x5A\x94' + software, b'\x5A\x90GKE2I61']
            with patch.object(reset.time, 'sleep'), patch('builtins.print'):
                if accepted:
                    reset.reboot(self.bus, None)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                        reset.reboot(self.bus, None)


class FlashResetTests(unittest.TestCase):
    def test_preflight_reads_and_writing_have_independent_progress_ranges(self):
        image = m797_checksum_image()
        stages = []
        def factory(total):
            increments = []
            stages.append((total, increments))
            return increments.append

        def read(bus, calibration, **options):
            options['progress_callback'](options['size'])
            return image[0x1FC20:0x1FE20] if options['size'] == 512 else bytes(options['size'])

        def prepare(bus, read_flash, *args, **options):
            for size in (284, 126):
                read_flash(bus, 'TEST', address_start=0x819000, size=size)
            return b'expected'

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source = Path(directory) / 'image.bin'
            source.write_bytes(image)
            stack.enter_context(patch('builtins.print'))
            stack.enter_context(patch.object(m797, 'get_m797_calibration_id', return_value='TEST'))
            stack.enter_context(patch.object(m797, 'm797_read', side_effect=read))
            stack.enter_context(patch.object(m797, 'prepare_reboot', side_effect=prepare))
            stack.enter_context(patch.object(m797, 'm797_prepare_flash_session'))
            stack.enter_context(patch.object(m797, 'm797_security_access'))
            stack.enter_context(patch.object(m797, 'reboot'))
            stack.enter_context(patch.object(m797, 'm797_flash_region',
                side_effect=lambda bus, address, data, ranges, progress_callback, **kwargs: progress_callback(len(data))))
            m797.m797_flash(Mock(), str(source), 'fast', False, calibration_only=True, progress_factory=factory)
        self.assertEqual(stages, [(512, [512]), (284, [284]), (126, [126]), (65536, [65536])])

    def test_monitor_preparation_failure_aborts_before_programming(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source = Path(directory) / 'image.bin'
            source.write_bytes(m797_checksum_image())
            stack.enter_context(patch('builtins.print'))
            stack.enter_context(patch.object(m797, 'prepare_reboot', side_effect=RuntimeError('RAM failure')))
            prepare = stack.enter_context(patch.object(m797, 'm797_prepare_flash_session'))
            flash = stack.enter_context(patch.object(m797, 'm797_flash_region'))
            with self.assertRaisesRegex(RuntimeError, 'RAM failure'):
                m797.m797_flash(Mock(), str(source), 'fast', False, Mock())
            prepare.assert_not_called()
            flash.assert_not_called()

    def test_unconfirmed_reset_preserves_verified_flash_and_requests_power_cycle(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source = Path(directory) / 'image.bin'
            source.write_bytes(m797_checksum_image())
            output = stack.enter_context(patch('builtins.print'))
            stack.enter_context(patch.object(m797, 'prepare_reboot', return_value=b'expected'))
            stack.enter_context(patch.object(m797, 'm797_prepare_flash_session'))
            stack.enter_context(patch.object(m797, 'm797_security_access'))
            flash = stack.enter_context(patch.object(m797, 'm797_flash_region'))
            stack.enter_context(patch.object(m797, 'reboot', side_effect=RuntimeError('lost response')))
            m797.m797_flash(Mock(), str(source), 'fast', False, Mock())
            self.assertEqual(flash.call_count, 3)
            messages = '\n'.join(str(call.args[0]) for call in output.call_args_list)
            self.assertIn('Flash verified; automatic reboot not confirmed', messages)
            self.assertIn('Power-cycle', messages)

    def test_reset_follows_all_verified_regions_and_is_skipped_on_failure(self):
        for fail in (False, True):
            events = []
            def flash(*args, **kwargs):
                events.append('verified' if not fail else 'failed')
                if fail:
                    raise RuntimeError('verification failed')

            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                source = Path(directory) / 'image.bin'
                source.write_bytes(m797_checksum_image())
                stack.enter_context(patch('builtins.print'))
                stack.enter_context(patch.object(m797, 'prepare_reboot', return_value=b'expected'))
                stack.enter_context(patch.object(m797, 'm797_prepare_flash_session'))
                stack.enter_context(patch.object(m797, 'm797_security_access'))
                stack.enter_context(patch.object(m797, 'm797_flash_region', side_effect=flash))
                reboot = stack.enter_context(patch.object(m797, 'reboot', side_effect=lambda *a, **kw: events.append('reset')))
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                        m797.m797_flash(Mock(), str(source), 'fast', False, Mock())
                    reboot.assert_not_called()
                else:
                    m797.m797_flash(Mock(), str(source), 'fast', False, Mock())
                    self.assertEqual(events, ['verified', 'verified', 'verified', 'reset'])


if __name__ == '__main__':
    unittest.main()
