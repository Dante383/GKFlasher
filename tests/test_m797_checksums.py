"""Offline checksum detection, correction and CLI file workflow tests."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flasher import checksum as checksum_tool

ROOT = Path(__file__).resolve().parents[1]


def m797_checksum_image(calibration_only=False):
    image = bytearray(bytes(range(256)) * (0x80000 // 256))
    for index in range(32):
        if index == 0:
            start, size = 0x800000, 0x10000
        elif index < 28:
            start, size = 0x820000 + (index - 1) * 0x2000, 0x2000
        else:
            start, size = 0x810000 + (index - 28) * 0x4000, 0x4000
        struct.pack_into('<IIII', image, 0x1FC20 + index * 16,
                         start, start + size - 1, 0, 0xFFFFFFFF)
    if calibration_only:
        image[:0x10000] = b'\xFF' * 0x10000
        image[0x20000:] = b'\xFF' * 0x60000
    return bytes(image)


def reference_correction(image, calibration_only=False):
    result = bytearray(image)
    selected = []
    for index in range(32):
        offset = 0x1FC20 + index * 16
        start, end, checksum, inverse = struct.unpack_from('<IIII', image, offset)
        if not calibration_only or 0x810000 <= start <= end < 0x820000:
            selected.append((offset, start - 0x800000, end - 0x800000 + 1))
            struct.pack_into('<I', result, offset + 12, checksum ^ 0xFFFFFFFF)
    if not calibration_only:
        header = sum(struct.unpack_from('<H', result, offset)[0] for offset in (0x8000, 0xFFFE))
        boundary = sum(
            struct.unpack_from('<H', result, offset)[0]
            for start, stop in ((0, 0x10000), (0x20000, 0x80000))
            for block in range(start, stop, 0x2000)
            for offset in (block, block + 0x1FFE)
        )
        struct.pack_into('<II', result, 0x8038, header, boundary)
    for offset, start, stop in selected:
        checksum = sum(value for (value,) in struct.iter_unpack('<H', result[start:stop])) & 0xFFFFFFFF
        struct.pack_into('<II', result, offset + 8, checksum, checksum ^ 0xFFFFFFFF)
    return bytes(result)


def simk_checksum_image():
    image = bytearray(0x80000)
    image[0x17EFE:0x17F00] = b'OK'
    image[0x10000:0x10004] = b'ABCD'
    struct.pack_into('<H', image, 0x1000C, 0x1234)
    image[0x17EE2] = 1
    image[0x17EE4:0x17EE7] = (0x90000).to_bytes(3, 'little')
    image[0x17EE8:0x17EEB] = (0x90003).to_bytes(3, 'little')
    return bytes(image)


class M797ChecksumTests(unittest.TestCase):
    def test_detection_requires_valid_descriptor_table_not_just_size(self):
        self.assertEqual(checksum_tool.detect_offsets(m797_checksum_image())['algorithm'], 'm797')
        self.assertEqual(checksum_tool.detect_offsets(m797_checksum_image(True))['algorithm'], 'm797')
        self.assertIsNone(checksum_tool.detect_offsets(bytes(0x80000)))
        self.assertIsNone(checksum_tool.detect_offsets(b'\xFF' * 0x80000))
        invalid = bytearray(m797_checksum_image())
        struct.pack_into('<I', invalid, 0x1FC20, 0x900000)
        self.assertIsNone(checksum_tool.detect_offsets(invalid))

    def test_full_image_matches_independent_reference_and_is_idempotent(self):
        source = m797_checksum_image()
        corrected, changes = checksum_tool.prepare_m797_checksum_correction(source, lambda message: None)
        self.assertEqual(corrected, reference_correction(source))
        self.assertTrue(changes)
        allowed = {offset for change in changes for offset in range(change['offset'], change['offset'] + 4)}
        self.assertTrue(all(index in allowed for index, (old, new) in enumerate(zip(source, corrected)) if old != new))
        repeated, changes = checksum_tool.prepare_m797_checksum_correction(corrected, lambda message: None)
        self.assertEqual(repeated, corrected)
        self.assertEqual(changes, [])

    def test_calibration_dump_preserves_missing_data_and_unread_region_entries(self):
        source = m797_checksum_image(True)
        corrected, changes = checksum_tool.prepare_m797_checksum_correction(source, lambda message: None)
        self.assertEqual(corrected, reference_correction(source, calibration_only=True))
        self.assertEqual(corrected[:0x10000], source[:0x10000])
        self.assertEqual(corrected[0x20000:], source[0x20000:])
        self.assertEqual(corrected[0x1FC20:0x1FC20 + 28 * 16], source[0x1FC20:0x1FC20 + 28 * 16])
        self.assertTrue(all(0x1FC20 + 28 * 16 <= change['offset'] < 0x1FE20 for change in changes))

    def test_console_reports_unchanged_checksums_for_each_available_region(self):
        for calibration_only in (False, True):
            with self.subTest(calibration_only=calibration_only):
                source = reference_correction(m797_checksum_image(calibration_only), calibration_only)
                messages = []
                corrected, changes = checksum_tool.prepare_m797_checksum_correction(source, messages.append)
                self.assertEqual(corrected, source)
                self.assertEqual(changes, [])
                results = [message for message in messages if message.startswith('[*] OK! Current ')]
                self.assertEqual(len(results), 4 if calibration_only else 34)
                for message in results:
                    self.assertRegex(message, r'checksum: (0x[0-9a-f]+), new checksum: \1$')

    def test_friendly_region_names_and_console_format_match_simk(self):
        for calibration_only in (False, True):
            with self.subTest(calibration_only=calibration_only):
                source = reference_correction(m797_checksum_image(calibration_only), calibration_only)
                messages = []
                checksum_tool.prepare_m797_checksum_correction(
                    source, messages.append
                )
                names = [message.split('Current ', 1)[1].split(' checksum:', 1)[0]
                         for message in messages if message.startswith('[*] OK! Current ')]
                self.assertIn('Calibration 1', names)
                self.assertIn('Calibration 4', names)
                self.assertTrue(all('range 0x' not in name for name in names))
                self.assertEqual(
                    [message for message in messages if message.startswith('[*] Calculating checksum for region ')],
                    ['[*] Calculating checksum for region Boot',
                     '[*] Calculating checksum for region Calibration',
                     '[*] Calculating checksum for region Program'],
                )
                self.assertEqual(
                    [message for message in messages if message.startswith('[*] Amount of zones:')],
                    ['[*] Amount of zones: {}'.format(count)
                     for count in ((0, 4, 0) if calibration_only else (1, 4, 27))],
                )
                self.assertTrue(any(message.startswith('[*] checksum of zone #4:') for message in messages))
                self.assertTrue(any(message.startswith('[*] OK! Current Calibration 1 checksum:')
                                    for message in messages))
                if not calibration_only:
                    self.assertIn('Boot', names)
                    self.assertIn('Boot Header', names)
                    self.assertIn('Boot Boundary', names)
                    self.assertIn('Program 1', names)
                    self.assertIn('Program 27', names)

    def test_damaged_self_covered_checksum_and_complement_can_be_repaired(self):
        for calibration_only in (False, True):
            with self.subTest(calibration_only=calibration_only):
                source = bytearray(m797_checksum_image(calibration_only))
                struct.pack_into('<II', source, 0x1FC20 + 31 * 16 + 8, 0xDEADBEEF, 0x11111111)
                original = bytes(source)
                corrected, changes = checksum_tool.prepare_m797_checksum_correction(source, lambda message: None)
                self.assertEqual(corrected, reference_correction(source, calibration_only))
                self.assertEqual(source, original)
                self.assertTrue(changes)

    def test_incomplete_image_and_calibration_boundary_crossing_are_rejected(self):
        incomplete = bytearray(m797_checksum_image())
        incomplete[:0x10000] = b'\xFF' * 0x10000
        with self.assertRaisesRegex(ValueError, 'missing boot/program'):
            checksum_tool.prepare_m797_checksum_correction(incomplete)
        crossing = bytearray(m797_checksum_image(True))
        struct.pack_into('<II', crossing, 0x1FC20, 0x80FFFE, 0x810001)
        with self.assertRaisesRegex(ValueError, 'crosses calibration boundary'):
            checksum_tool.prepare_m797_checksum_correction(crossing, lambda message: None)
        with self.assertRaisesRegex(ValueError, '512 KiB'):
            checksum_tool.prepare_m797_checksum_correction(bytes(0x10000))

    def test_cli_preview_decline_and_save(self):
        for answer in ('n', 'y'):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                filename = Path(directory) / 'calibration.bin'
                source = m797_checksum_image(True)
                filename.write_bytes(source)
                output = StringIO()
                with patch('builtins.input', return_value=answer), redirect_stdout(output), \
                     self.assertRaises(SystemExit) as stopped:
                    checksum_tool.correct_checksum(str(filename))
                self.assertEqual(stopped.exception.code, 0)
                self.assertEqual(filename.read_bytes(), reference_correction(source, True) if answer == 'y' else source)
                self.assertIn('[*] OK! Current Calibration 1 checksum:', output.getvalue())
                self.assertIn('[*] Calculating checksum for region Calibration', output.getvalue())

    def test_cli_already_correct_file_uses_simk_save_prompt(self):
        for answer in ('n', 'y'):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                filename = Path(directory) / 'correct.bin'
                source = reference_correction(m797_checksum_image())
                filename.write_bytes(source)
                output = StringIO()
                with patch('builtins.input', return_value=answer) as prompt, \
                     redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                    checksum_tool.correct_checksum(str(filename))
                self.assertEqual(stopped.exception.code, 0)
                prompt.assert_called_once_with('[?] Save to {}? [y/n]: '.format(filename))
                self.assertEqual(filename.read_bytes(), source)
                self.assertNotIn('Checking M7.9.7', output.getvalue())
                self.assertEqual(output.getvalue().splitlines().count(
                    '[+] Image checksums already correct.'
                ), 1)
                self.assertEqual('[*] Done!' in output.getvalue(), answer == 'y')

    def test_real_cli_command_corrects_offline_without_initializing_an_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'full.bin'
            source = m797_checksum_image()
            filename.write_bytes(source)
            result = subprocess.run(
                [sys.executable, '-B', str(ROOT / 'gkflasher.py'), '--protocol', 'kline',
                 '--interface', 'NO_INTERFACE', '--correct-checksum', str(filename)],
                input='y\n', capture_output=True, text=True, cwd=ROOT, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(filename.read_bytes(), reference_correction(source))
            self.assertNotIn('Initializing..', result.stdout)

    def test_simk_detection_and_cli_correction(self):
        source = simk_checksum_image()
        detected = checksum_tool.detect_offsets(source)
        self.assertEqual(detected['name'], '4mbit')
        self.assertNotIn('algorithm', detected)
        expected = bytearray(source)
        value = checksum_tool.checksum(source, 0x10000, 0x10004, 0x1234)
        expected[0x17EE0:0x17EE2] = value.to_bytes(2, 'little')
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'simk.bin'
            filename.write_bytes(source)
            with patch('builtins.input', return_value='y'), redirect_stdout(StringIO()), \
                 self.assertRaises(SystemExit) as stopped:
                checksum_tool.correct_checksum(str(filename))
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(filename.read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
