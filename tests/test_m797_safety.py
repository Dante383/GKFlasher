"""Routing, RAM restoration and packet regressions without an attached ECU."""

from contextlib import ExitStack, contextmanager
from itertools import product
import hashlib
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import gkflasher
from ecu_definitions import ECU_FAMILY_M797, ECU_FAMILY_SIMK, ECU_FAMILY_UNKNOWN
from flasher import checksum, ecu, m797
from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000
from gkbus.transport import Kwp2000OverKLineTransport
from tests.test_m797_checksums import m797_checksum_image
from tests.test_m797_gkbus_api import MemoryKLine


def arguments(**overrides):
    values = dict(
        ecu_family='auto', desired_baudrate=None, id=False, read_dtcs=False,
        read=False, read_calibration=False, read_program=False,
        flash=None, flash_calibration=None, flash_program=None,
        output='test.bin', address_start=None, address_stop=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class RoutingTests(unittest.TestCase):
    def test_initialization_failure_releases_the_device(self):
        device = Mock()
        device.kline.return_value.open.side_effect = TimeoutException
        with patch.object(gkflasher, 'select_device', return_value=device), patch('builtins.print'):
            with self.assertRaises(TimeoutException):
                gkflasher.initialize_bus('kline', {'interface': 'test', 'tx_id': 0x11, 'rx_id': 0xF1})
        device.close.assert_called_once()

    def test_locked_simk_is_detected_from_versions_before_memory_unlock(self):
        # COM69 capture: memory signatures are unavailable until security access.
        records = {0x8C: b'KR77025241--', 0x8D: b'KR77025160--', 0x8E: b'G7TCMP6C----'}
        bus = Mock()
        def respond(command):
            self.assertEqual(command.get_service_identifier(), 0x1A)
            identifier = command.get_data()[0]
            return SimpleNamespace(get_data=lambda: bytes((identifier,)) + records.get(identifier, b'\xFF' * 12))
        bus.execute.side_effect = respond
        with patch.object(ecu, 'identify_ecu', side_effect=ecu.ECUIdentificationException) as memory:
            self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_SIMK)
        memory.assert_not_called()

    def test_one_simk_version_response_does_not_establish_family(self):
        bus = Mock()
        def respond(command):
            identifier = command.get_data()[0]
            data = b'KR77025241--' if identifier == 0x8C else b'\xFF' * 12
            return SimpleNamespace(get_data=lambda: bytes((identifier,)) + data)
        bus.execute.side_effect = respond
        with patch.object(ecu, 'identify_ecu', side_effect=ecu.ECUIdentificationException):
            self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_UNKNOWN)

    def test_disconnected_bus_is_unknown(self):
        bus = Mock()
        bus.execute.side_effect = TimeoutException
        self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_UNKNOWN)

    def test_erased_identifiers_are_not_kefico(self):
        bus = Mock()
        bus.execute.return_value.get_data.return_value = b'\xFF' * 16
        with patch.object(ecu, 'identify_ecu', side_effect=ecu.ECUIdentificationException):
            self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_UNKNOWN)

    def test_modified_calibration_name_does_not_affect_family_detection(self):
        bus = Mock()
        records = {0x90: b'DMG_v69', 0x91: b'KEFICO123', 0x92: b'3910621400'}
        bus.execute.side_effect = lambda command: SimpleNamespace(
            get_data=lambda: bytes(command.get_data()) + records.get(command.get_data()[0], b'\xFF' * 12))
        self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_M797)

    def test_v6_hardware_identifiers_do_not_override_simk_versions(self):
        # COM69 V6: 0x91/0x92 are valid on this Siemens ECU as well as Kefico.
        records = {0x8C: b'KR77035201--', 0x8D: b'KR77035110--',
                   0x8E: b'G4N7TS0A----', 0x91: b'M39110-37100', 0x92: b'KR77037601A-'}
        bus = Mock()
        bus.execute.side_effect = lambda command: SimpleNamespace(
            get_data=lambda: bytes(command.get_data()) + records.get(command.get_data()[0], b'\xFF' * 12))
        self.assertEqual(ecu.detect_ecu_family(bus), ECU_FAMILY_SIMK)

    def test_identification_accepts_payload_with_or_without_echo(self):
        bus = Mock()
        for value in (b'\x90DMG_v69\x00 ', b'DMG_v69\x00 '):
            bus.execute.return_value.get_data.return_value = value
            self.assertEqual(ecu.get_m797_calibration_id(bus), 'DMG_v69')

    def test_program_only_flashing_still_detects_the_family(self):
        bus = Mock()
        bus.transport = Kwp2000OverKLineTransport(MemoryKLine(), 0x11, 0xF1)
        with patch.object(gkflasher, 'detect_ecu_family', return_value=ECU_FAMILY_M797) as detect, \
             patch.object(gkflasher, 'enable_security_access') as security, patch('builtins.print'):
            with self.assertRaisesRegex(ValueError, '--flash-program'):
                gkflasher.main(bus, arguments(flash_program='program.bin'))
        detect.assert_called_once_with(bus)
        security.assert_not_called()

    def test_unsupported_operations_are_rejected_before_flash_or_identification(self):
        for option in ('immo', 'mtos', 'rsw_full', 'flash_program', 'logger'):
            with self.subTest(option=option), \
                 patch.object(gkflasher, 'm797_flash') as flash, \
                 patch.object(gkflasher, 'cli_m797_id') as identify:
                with self.assertRaisesRegex(ValueError, 'Not yet supported'):
                    gkflasher.handle_m797(Mock(), arguments(id=True, flash='full.bin', **{option: True}))
                flash.assert_not_called()
                identify.assert_not_called()

    def test_custom_read_addresses_reach_the_reader(self):
        with patch.object(gkflasher, 'get_m797_calibration_id', return_value='DMG_v69'), \
             patch.object(gkflasher, 'get_m797_engine_id', return_value='Alpha'), \
             patch.object(gkflasher, 'm797_read') as read, patch('builtins.print'):
            gkflasher.handle_m797(Mock(), arguments(read=True, address_start=0x810000, address_stop=0x810020))
        self.assertEqual(read.call_args.kwargs['address_start'], 0x810000)
        self.assertEqual(read.call_args.kwargs['size'], 0x20)
        self.assertEqual(read.call_args.kwargs['read_profile'], 'standard')

    def test_invalid_read_addresses_never_send_commands(self):
        bus = Mock()
        with self.assertRaisesRegex(ValueError, 'outside'):
            gkflasher.handle_m797(bus, arguments(read=True, address_start=0x7FFFFF))
        bus.execute.assert_not_called()

    def test_explicit_kefico_selection_requires_kline(self):
        bus = Mock()
        with self.assertRaisesRegex(ValueError, 'K-Line'):
            gkflasher.main(bus, arguments(ecu_family=ECU_FAMILY_M797))
        bus.init.assert_not_called()

    def test_custom_flash_addresses_are_not_silently_ignored(self):
        with patch.object(gkflasher, 'm797_flash') as flash:
            with self.assertRaisesRegex(ValueError, 'Custom flash addresses'):
                gkflasher.handle_m797(Mock(), arguments(flash='image.bin', address_start=0x820000))
        flash.assert_not_called()

    def test_config_filename_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'other.yml'
            filename.write_text('protocol: kline\n', encoding='utf-8')
            self.assertEqual(gkflasher.load_config(filename), {'protocol': 'kline'})


class ReadSafetyTests(unittest.TestCase):
    def test_read_progress_covers_setup_transfer_and_cleanup(self):
        for fail in (False, True):
            with self.subTest(fail=fail), ExitStack() as stack:
                events = []
                baseline = b'original'

                @contextmanager
                def progress(total, title, callback):
                    self.assertEqual(total, 126)
                    events.append('start')
                    try:
                        yield lambda size: events.append(size)
                    finally:
                        events.append('finish')

                def setup(*args, **kwargs):
                    events.append('setup')
                    self.channel.set_baudrate(108800)
                    if fail:
                        raise TimeoutException

                stack.enter_context(patch.object(m797, '_m797_progress', side_effect=progress))
                stack.enter_context(patch.object(m797, '_m797_switch_read_baud', side_effect=setup))
                stack.enter_context(patch.object(m797, '_m797_restore_startup_baud',
                                                side_effect=lambda *a, **kw: events.append('cleanup')))
                stack.enter_context(patch.object(m797, '_m797_detect_dynamic_address_mode', return_value=('linear', baseline)))
                stack.enter_context(patch.object(m797, '_m797_patchless_read_preflight', return_value=(None, None)))
                stack.enter_context(patch.object(m797, '_m797_define_dynamic_lid'))
                stack.enter_context(patch.object(m797, '_m797_clear_dynamic_lid'))
                stack.enter_context(patch.object(m797, '_m797_read_dynamic_lid',
                                                side_effect=lambda bus, lid, size, **kw: baseline if size == 8 else bytes(size)))
                if fail:
                    with self.assertRaises(TimeoutException):
                        m797.m797_read(self.bus, 'UNKNOWN', None, read_profile='fast', size=126)
                else:
                    self.assertEqual(m797.m797_read(self.bus, 'UNKNOWN', None, read_profile='fast', size=126), bytes(126))
                self.assertEqual(events, ['start', 'setup'] + ([] if fail else [126]) + ['cleanup', 'finish'])

    def setUp(self):
        self.channel = MemoryKLine()
        self.channel.set_baudrate(10400)
        self.bus = Mock()
        self.bus.transport = Kwp2000OverKLineTransport(self.channel, 0x11, 0xF1)
        self.print_patch = patch('builtins.print')
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)

    def test_page_clipping_depends_on_the_architecture(self):
        for clip, expected_first in ((True, 0x10), (False, 0xF0)):
            chunks = [definition for batch in m797._m797_read_batches(
                0x803FF0, 0x200, (0xF0, 0xF1), clip) for definition in batch]
            self.assertEqual(chunks[0][2], expected_first)
            self.assertEqual(sum(size for _, _, size in chunks), 0x200)
            for (_, address, size), (_, next_address, _) in zip(chunks, chunks[1:]):
                self.assertEqual(address + size, next_address)

    def test_shared_dynamic_reader_rejects_malformed_lengths_and_identifiers(self):
        for payload in (bytes(15), bytes(18), b'\xF1' + bytes(16)):
            with self.subTest(payload=payload):
                self.bus.execute.return_value.get_data.return_value = payload
                with self.assertRaisesRegex(RuntimeError, 'Unexpected dynamic LID'):
                    m797._m797_read_dynamic_lid(self.bus, 0xF0, 16)

    def test_native_gate_uses_only_live_relocation_and_restores_on_rejection(self):
        baseline = bytes.fromhex('7e 1b 06 02 7e 1b 06 02')
        spoof = bytes.fromhex('64 1c 06 02 64 1c 06 02')
        rejection = kwp2000.Kwp2000NegativeResponseException(SimpleNamespace(identifier=0x12))
        for rejected in (False, True):
            with self.subTest(rejected=rejected), ExitStack() as stack:
                write = stack.enter_context(patch.object(m797, '_m797_write_ram'))
                stack.enter_context(patch.object(m797, '_m797_read_descriptor_pair', return_value=baseline))
                define = stack.enter_context(patch.object(m797, '_m797_define_dynamic_lid',
                                                         side_effect=[rejection, rejection if rejected else Mock()]))
                read = stack.enter_context(patch.object(m797, '_m797_read_dynamic_lid', return_value=bytes(16)))
                if rejected:
                    with self.assertRaises(kwp2000.Kwp2000NegativeResponseException):
                        m797._m797_patchless_read_preflight(self.bus, baseline, 'linear')
                    read.assert_not_called()
                else:
                    self.assertEqual(m797._m797_patchless_read_preflight(self.bus, baseline, 'linear'), (spoof, 0x1C64))
                self.assertEqual(define.call_count, 2)
                self.assertEqual([c.args[2] for c in write.call_args_list], [baseline, spoof, baseline])

    def test_previously_profiled_calibrations_use_the_ordered_generic_probes(self):
        for calibration in ('ADE1I52', 'FCE2N64', 'LCR0N53'):
            with self.subTest(calibration=calibration), ExitStack() as stack:
                stack.enter_context(patch.object(m797, '_m797_switch_read_baud'))
                stack.enter_context(patch.object(m797, '_m797_detect_dynamic_address_mode', side_effect=RuntimeError))
                lcr = stack.enter_context(patch.object(m797, '_m797_lcr_generic_preflight', side_effect=RuntimeError))
                candidates = m797.M797_C166_GENERIC_CANDIDATES
                probe = stack.enter_context(patch.object(m797, '_m797_c166_generic_preflight',
                                                        side_effect=[RuntimeError(), candidates[1]]))
                stack.enter_context(patch.object(m797, '_m797_read_flash_c166_direct',
                                                side_effect=lambda bus, result, *a, **kw: result.extend(bytes(16))))
                self.assertEqual(m797.m797_read(self.bus, calibration, None, Mock(), 'standard', size=16), bytes(16))
                lcr.assert_called_once()
                self.assertEqual([c.args[1] for c in probe.call_args_list], list(candidates[:2]))

    def test_gate_is_restored_when_activation_acknowledgement_is_lost(self):
        baseline, spoof = b'original', b'new gate'
        with patch.object(m797, '_m797_write_ram', side_effect=[TimeoutException, b'']) as write:
            with self.assertRaises(TimeoutException):
                with m797._m797_descriptor_gate(self.bus, baseline, spoof):
                    self.fail('An unacknowledged gate must not be used')
        self.assertEqual(write.call_args_list, [
            call(self.bus, m797.M797_READ_DESCRIPTOR_ADDRESS, spoof),
            call(self.bus, m797.M797_READ_DESCRIPTOR_ADDRESS, baseline),
        ])

    def test_gate_restoration_failure_is_explicit(self):
        with patch.object(m797, '_m797_write_ram', side_effect=[b'', TimeoutException]):
            with self.assertRaisesRegex(m797.M797ReadCleanupError, 'Power-cycle'):
                with m797._m797_descriptor_gate(self.bus, b'original', b'new gate'):
                    pass

    def test_generic_probe_restoration_failure_stops_discovery(self):
        for probe in ('_m797_lcr_generic_preflight', '_m797_c166_generic_preflight'):
            with self.subTest(probe=probe), ExitStack() as stack:
                stack.enter_context(patch.object(m797, '_m797_write_ram', side_effect=[b'', b'', b'', TimeoutException]))
                stack.enter_context(patch.object(m797, '_m797_read_dynamic_lid', return_value=b'\x01' * 16))
                with self.assertRaises(m797.M797ReadCleanupError):
                    if 'c166' in probe:
                        getattr(m797, probe)(self.bus, m797.M797_C166_GENERIC_CANDIDATES[0])
                    else:
                        getattr(m797, probe)(self.bus)

    def test_cleanup_failure_does_not_try_another_architecture_or_save_a_dump(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            filename = Path(directory) / 'dump.bin'
            stack.enter_context(patch.object(m797, '_m797_switch_read_baud'))
            stack.enter_context(patch.object(m797, '_m797_detect_dynamic_address_mode', side_effect=RuntimeError('unavailable')))
            stack.enter_context(patch.object(m797, '_m797_lcr_generic_preflight', side_effect=m797.M797ReadCleanupError('cleanup failed')))
            other_probe = stack.enter_context(patch.object(m797, '_m797_c166_generic_preflight'))
            with self.assertRaises(m797.M797ReadCleanupError):
                m797.m797_read(self.bus, 'UNKNOWN', str(filename), Mock(), 'standard', size=32)
            other_probe.assert_not_called()
            self.assertFalse(filename.exists())

    def test_direct_window_is_cleaned_after_an_install_timeout(self):
        cleanup = Mock()
        with self.assertRaises(TimeoutException):
            m797._m797_read_direct(
                self.bus, bytearray(), Mock(), 0x800000, 16, (0xF0,),
                Mock(return_value=b'window'), Mock(side_effect=TimeoutException), cleanup, True,
            )
        cleanup.assert_called_once_with()

    def test_failed_speed_handoff_restores_host_timing(self):
        original = self.channel.get_timing()

        def failed_handoff(*args, **kwargs):
            m797._m797_set_write_delay(self.bus, 0)
            self.channel.set_baudrate(108800)
            raise TimeoutException

        with patch.object(m797, '_m797_switch_read_baud', side_effect=failed_handoff), \
             patch.object(m797, '_m797_restore_startup_baud') as restore:
            with self.assertRaises(TimeoutException):
                m797.m797_read(self.bus, 'UNKNOWN', read_profile='fast', progress_callback=Mock())
        restore.assert_called_once_with(self.bus, 'fast', log_callback=None)
        self.assertEqual(self.channel.get_timing(), original)

    def test_native_read_restores_the_gate_before_fetching_data_and_pads_the_file(self):
        baseline, spoof = b'original', b'new gate'
        events = []

        def read(live_bus, lid, size, log_callback=None):
            if lid == m797.M797_READ_STATE_LID:
                return baseline
            self.assertEqual(events[-1], baseline)
            return b'\xA5' * size

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            filename = Path(directory) / 'partial.bin'
            stack.enter_context(patch.object(m797, '_m797_switch_read_baud'))
            stack.enter_context(patch.object(m797, '_m797_detect_dynamic_address_mode', return_value=('linear', baseline)))
            stack.enter_context(patch.object(m797, '_m797_patchless_read_preflight', return_value=(spoof, 0x1A8A)))
            stack.enter_context(patch.object(m797, '_m797_write_ram', side_effect=lambda bus, address, data: events.append(data)))
            stack.enter_context(patch.object(m797, '_m797_define_dynamic_lid'))
            stack.enter_context(patch.object(m797, '_m797_clear_dynamic_lid'))
            stack.enter_context(patch.object(m797, '_m797_read_dynamic_lid', side_effect=read))
            result = m797.m797_read(self.bus, 'UNKNOWN', str(filename), Mock(), 'standard', 0x810000, 32)
            self.assertEqual(result, b'\xA5' * 32)
            saved = filename.read_bytes()
            self.assertEqual(len(saved), 0x80000)
            self.assertEqual(saved[:0x10000], b'\xFF' * 0x10000)
            self.assertEqual(saved[0x10000:0x10020], result)
            self.assertEqual(saved[0x10020:], b'\xFF' * (0x80000 - 0x10020))
            with patch.object(m797.Path, 'write_bytes') as save:
                memory = m797.m797_read(
                    self.bus, 'UNKNOWN', None, Mock(), 'standard', 0x810000, 32)
                self.assertEqual(memory, result)
                save.assert_not_called()
        self.assertEqual(events, [spoof, baseline, spoof, baseline])

    def test_malformed_zero_seed_is_rejected(self):
        self.bus.execute.return_value.get_data.return_value = b'\x81\x00'
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            m797.m797_security_access(self.bus)
        self.assertEqual(self.bus.execute.call_count, 1)

    def test_security_key_vectors(self):
        for seed, normal, recovery in (
            ('00000001', '24DE49D0', '088C9338'),
            ('A18AC523', '4F4388D0', 'DD535460'),
            ('FFFFFFFF', '0CB9A6A0', '4667EECC'),
        ):
            with self.subTest(seed=seed):
                self.assertEqual(m797.m797_calculate_key(bytes.fromhex(seed)), bytes.fromhex(normal))
                self.assertEqual(m797.m797_calculate_recovery_key(bytes.fromhex(seed)), bytes.fromhex(recovery))

    def test_recovery_key_is_only_attempted_after_invalid_key_response(self):
        for status in (0x35, 0x36):
            with self.subTest(status=status):
                self.bus.reset_mock()
                response = Mock()
                response.get_data.return_value = bytes.fromhex('81 A1 8A C5 23')
                rejection = kwp2000.Kwp2000NegativeResponseException(SimpleNamespace(identifier=status))
                self.bus.execute.side_effect = [response, rejection, Mock()]
                if status == 0x35:
                    m797.m797_security_access(self.bus)
                    commands = [entry.args[0].get_data() for entry in self.bus.execute.call_args_list]
                    self.assertEqual(commands, [
                        b'\x81\x05SECURITY', bytes.fromhex('82 4F 43 88 D0'), bytes.fromhex('82 DD 53 54 60'),
                    ])
                else:
                    with self.assertRaises(kwp2000.Kwp2000NegativeResponseException):
                        m797.m797_security_access(self.bus)
                    self.assertEqual(self.bus.execute.call_count, 2)

    def test_security_delay_has_a_deadline(self):
        self.bus.execute.side_effect = kwp2000.Kwp2000NegativeResponseException(SimpleNamespace(identifier=0x37))
        with patch.object(m797.time, 'monotonic', side_effect=[0, 7]):
            with self.assertRaisesRegex(TimeoutError, 'security delay'):
                m797.m797_security_access(self.bus)

    def test_routine_polling_has_a_deadline(self):
        error = kwp2000.Kwp2000NegativeResponseException(SimpleNamespace(identifier=0x23))
        self.bus.execute.side_effect = error
        with patch.object(m797.time, 'monotonic', side_effect=[0, 300]):
            with self.assertRaisesRegex(TimeoutError, 'routine 0x02 timed out'):
                m797.m797_wait_for_routine(self.bus, 2)


class PacketRegressionTests(unittest.TestCase):
    def test_blank_range_planner_preserves_gap_threshold_and_partial_blocks(self):
        block = b'\x00' * 128
        for gap, expected in (
            (0xF80, [(0, 0x1080)]),
            (0x1000, [(0, 128), (0x1080, 0x1100)]),
        ):
            with self.subTest(gap=gap):
                self.assertEqual(m797.m797_find_nonblank_ranges(block + b'\xFF' * gap + block), expected)
        self.assertEqual(m797.m797_find_nonblank_ranges(b'\xFF' * 257), [])
        self.assertEqual(m797.m797_find_nonblank_ranges(b'\xFF' * 128 + b'\x01'), [(128, 129)])

    def test_calibration_flash_preserves_live_firmware_checksums_for_full_and_padded_bins(self):
        live = checksum.m797_correct_image_checksums(m797_checksum_image(), verbose=False)
        source = bytearray(live)
        source[0x20000] ^= 1  # The supplied BIN has different program data.
        source[0x12000] ^= 1  # Include a calibration change to upload.
        source = checksum.m797_correct_image_checksums(source, verbose=False)
        table = live[0x1FC20:0x1FE20]
        for padded in (False, True):
            with self.subTest(padded=padded):
                image = bytearray(source)
                if padded:
                    image[:0x10000] = b'\xFF' * 0x10000
                    image[0x20000:] = b'\xFF' * 0x60000
                corrected = checksum.m797_correct_calibration_checksums(
                    image, 0x810000, 0x10000, current_table=table,
                )
                combined = bytearray(live)
                combined[0x10000:0x20000] = corrected[0x10000:0x20000]
                for descriptor in checksum.m797_checksum_descriptors(combined):
                    stored = struct.unpack_from('<I', combined, descriptor['offset'] + 8)[0]
                    calculated = sum(value[0] for value in struct.iter_unpack(
                        '<H', combined[descriptor['start'] - 0x800000:descriptor['end'] - 0x800000 + 1],
                    )) & 0xFFFFFFFF
                    self.assertEqual(stored, calculated, descriptor)

    def test_calibration_flash_reads_live_table_before_entering_programming(self):
        image = m797_checksum_image()
        events = []
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(image)
            stack.enter_context(patch('builtins.print'))
            stack.enter_context(patch.object(m797, 'prepare_reboot', return_value=None))
            stack.enter_context(patch.object(m797, 'get_m797_calibration_id', return_value='TEST'))
            read = stack.enter_context(patch.object(m797, 'm797_read',
                side_effect=lambda *args, **kwargs: events.append('read') or image[0x1FC20:0x1FE20]))
            prepare = stack.enter_context(patch.object(m797, 'm797_prepare_flash_session',
                side_effect=lambda *args, **kwargs: events.append('programming')))
            stack.enter_context(patch.object(m797, 'm797_security_access'))
            stack.enter_context(patch.object(m797, 'm797_flash_region',
                side_effect=lambda *args, **kwargs: events.append('erase/write')))
            bus = Mock()
            bus.transport.channel.read.return_value = b'\x01\xC2\xC3'
            m797.m797_flash(bus, str(filename), 'standard', False, Mock(), True)
            self.assertEqual(events, ['read', 'programming', 'erase/write'])
            self.assertIsNone(read.call_args.kwargs['output_filename'])
            self.assertEqual(read.call_args.kwargs['address_start'], 0x81FC20)
            self.assertEqual(read.call_args.kwargs['size'], 512)
            prepare.assert_called_once()

    def test_unreadable_or_incompatible_live_table_never_enters_programming(self):
        image = m797_checksum_image()
        mismatched = bytearray(image[0x1FC20:0x1FE20])
        mismatched[0] ^= 2
        invalid_inverse = bytearray(image[0x1FC20:0x1FE20])
        invalid_inverse[12] ^= 1
        for live_result in (b'', bytes(mismatched), bytes(invalid_inverse), TimeoutException('read failed')):
            with self.subTest(result=type(live_result).__name__), \
                 tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                filename = Path(directory) / 'image.bin'
                filename.write_bytes(image)
                stack.enter_context(patch('builtins.print'))
                stack.enter_context(patch.object(m797, 'get_m797_calibration_id', return_value='TEST'))
                read = stack.enter_context(patch.object(m797, 'm797_read'))
                if isinstance(live_result, Exception):
                    read.side_effect = live_result
                else:
                    read.return_value = live_result
                prepare = stack.enter_context(patch.object(m797, 'm797_prepare_flash_session'))
                with self.assertRaises((ValueError, TimeoutException)):
                    m797.m797_flash(Mock(), str(filename), 'standard', False, Mock(), True)
                prepare.assert_not_called()

    def test_direct_read_windows_match_pre_cleanup_layouts(self):
        definitions = [(0xF0 + index, 0x800000 + index * 0xF0, 0xF0) for index in range(5)]
        # Original serialized windows, including all five active table/descriptor slots.
        expected = {
            0x380044: 'db7c3d22343223a392d9072441377ff086f9c7c3cbe8b60226d66b93bef041a0',
            0x38003A: 'c9f7daacbc0f5a347f0c5db871b4f8eeb89abf0015f053a6ecdde66622a8d464',
            0x380036: 'fe0fdeeab278c0b2620f4715d536787f705a625431b4e2e34045a14fd6b49695',
        }
        for base, digest in expected.items():
            with self.subTest(base=hex(base)):
                window = m797._m797_build_c166_direct_window(definitions, base)
                self.assertEqual(hashlib.sha256(window).hexdigest(), digest)
        window = m797._m797_build_lcr_direct_window(definitions)
        self.assertEqual(hashlib.sha256(window).hexdigest(),
                         '6db4222ee3bd813e6b61740d6d82186fe1be66eef00f37f7a3e65f2b63f2fd47')

    def test_full_flash_rejects_a_padded_calibration_read_before_ecu_access(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'calibration.bin'
            filename.write_bytes(m797_checksum_image(calibration_only=True))
            bus = Mock()
            with patch('builtins.print'), self.assertRaisesRegex(ValueError, 'calibration read'):
                m797.m797_flash(bus, str(filename), 'standard', False)
            bus.execute.assert_not_called()

    def test_flash_packets_match_the_pre_cleanup_implementation(self):
        # Programming packets are unchanged; checksum/reboot preflights are tested separately.
        expected = {
            ('standard', False): '41edeee41baf6fbd7878fe9e1005b67f8d749b58e8a0b3edc186d413de5bcff2',
            ('standard', True): '25dab1b255dc610b49fe69a9186c2a7e9c326c33ecc7e4105a8ca72b2800d2f0',
            ('fast', False): '56c55a45e180e2bb7a399ad9266a4d9b10814fd33b0aa0a116cebc8e6438ef04',
            ('fast', True): '3911aff95c4cf8c4b39c9b5f7fca6cb2665996b1985d76285343fa855e882b66',
        }
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'full.bin'
            filename.write_bytes(m797_checksum_image())
            for ((profile, calibration), digest), log_callback in product(expected.items(), (None, Mock())):
                with self.subTest(profile=profile, calibration=calibration, gui=log_callback is not None):
                    channel = MemoryKLine()
                    bus = kwp2000.Kwp2000Protocol(Kwp2000OverKLineTransport(channel, 0x11, 0xF1, timeout=0.25))
                    try:
                        bus.init(kwp2000.commands.StartCommunication())
                        with patch('builtins.print'), \
                             patch.object(m797, 'prepare_reboot', return_value=None), \
                             patch.object(m797, 'get_m797_calibration_id', return_value='UNKNOWN'), \
                             patch.object(m797, 'm797_read', return_value=filename.read_bytes()[0x1FC20:0x1FE20]):
                            m797.m797_flash(bus, str(filename), profile, False, Mock(), calibration,
                                            log_callback=log_callback)
                        self.assertEqual(hashlib.sha256(b''.join(channel.frames)).hexdigest(), digest)
                    finally:
                        bus.close()


class ChecksumSaveTests(unittest.TestCase):
    def test_a_file_changed_after_preview_is_never_overwritten(self):
        source = m797_checksum_image()
        corrected, changes = checksum.prepare_m797_checksum_correction(source, lambda message: None)
        changed = bytearray(source)
        changed[0x12000] ^= 1
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, 'changed after checksum preview'):
                checksum.save_m797_checksum_correction(filename, corrected, changes)
            self.assertEqual(filename.read_bytes(), changed)

    def test_failed_atomic_replace_preserves_the_original_and_removes_temporary_files(self):
        source = m797_checksum_image()
        corrected, changes = checksum.prepare_m797_checksum_correction(source, lambda message: None)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(source)
            with patch.object(checksum.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaisesRegex(OSError, 'replace failed'):
                    checksum.save_m797_checksum_correction(filename, corrected, changes)
            self.assertEqual(filename.read_bytes(), source)
            self.assertEqual(list(Path(directory).iterdir()), [filename])
