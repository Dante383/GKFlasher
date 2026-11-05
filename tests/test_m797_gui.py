"""Headless Qt coverage for Kefico GUI routing, flashing and progress."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import ctypes
import importlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5 import QtCore, QtWidgets

from tests.test_m797_gkbus_api import MemoryKLine
from tests.test_m797_checksums import m797_checksum_image, reference_correction, simk_checksum_image

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    global gui, app, test_home
    test_home = tempfile.TemporaryDirectory(prefix='m797-gui-test-', dir=ROOT)
    # Keep gui.py's existing Documents/log setup inside the test workspace.
    with ExitStack() as stack:
        stack.enter_context(patch('logging.basicConfig'))
        if os.name == 'nt':
            def documents(*args):
                args[-1].value = test_home.name
                return 0
            stack.enter_context(patch.object(ctypes.windll.shell32, 'SHGetFolderPathW',
                                             side_effect=documents))
        else:
            stack.enter_context(patch('os.path.expanduser', return_value=test_home.name))
        gui = importlib.import_module('gui')
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def tearDownModule():
    test_home.cleanup()


class M797GuiTests(unittest.TestCase):
    def setUp(self):
        self.ui = gui.Ui.__new__(gui.Ui)
        QtWidgets.QMainWindow.__init__(self.ui)
        self.addCleanup(self.ui.close)
        for name in ('ecusBox', 'baudratesBox', 'interfacesBox'):
            setattr(self.ui, name, QtWidgets.QComboBox(self.ui))
        for name in ('readingFileInput', 'flashingFileInput', 'checksumFileInput'):
            setattr(self.ui, name, QtWidgets.QLineEdit(self.ui))
        self.ui.load_ecus()
        self.ui.load_baudrates()
        self.ui._close_bus = Mock()
        self.ui.send_notification = Mock()
        self.log = Mock()
        self.progress = Mock()
        self.bus = Mock()
        self.ui.bus = self.bus
        self.ecu = gui.M797ECU().set_bus(self.bus)
        self.ecu.set_desired_baudrate(gui.DesiredBaudrate(index=0x64, baudrate=57600))

    def test_real_ui_loads_with_manual_kefico_and_labelled_speeds(self):
        previous_directory = os.getcwd()
        try:
            with patch.object(gui, 'discover', return_value=[]), \
                 patch.object(gui, 'set_lineswap_log_handler'):
                window = gui.Ui()
                try:
                    window.thread_manager.waitForDone(2000)
                    app.processEvents()
                    self.assertGreaterEqual(window.ecusBox.findData(gui.ECU_FAMILY_M797), 0)
                    for selector in (0x64, 0x82):
                        index = window.baudratesBox.findData(selector)
                        self.assertIn('M7.9.7', window.baudratesBox.itemText(index))
                finally:
                    window.close()
        finally:
            os.chdir(previous_directory)

    def initialize(self, family='m797', supported=True):
        channel = MemoryKLine()
        device = Mock()
        device.kline.return_value = channel
        self.ui.get_selected_interface = Mock(return_value=(device, gui.Bus.KLINE))
        with patch.object(gui, 'detect_ecu_family', return_value=family) as detector, \
             patch.object(gui, 'enable_security_access') as simk_security:
            ecu = self.ui.initialize_ecu(self.log, m797_supported=supported)
        self.detector = detector
        self.addCleanup(self.ui.bus.close)
        simk_security.assert_not_called()
        self.assertEqual(channel.frames, [])  # No SIMK session/security/erase requests.
        return ecu

    def test_detection_routes_before_simk_setup_with_standard_default(self):
        ecu = self.initialize()
        self.detector.assert_called_once_with(self.ui.bus)
        self.assertIsInstance(ecu, gui.M797ECU)
        self.assertEqual(ecu.get_desired_baudrate().index, 0x64)
        self.assertEqual(self.ui.bus.transport.timeout, 12)

    def test_manual_selection_bypasses_detection_for_recovery(self):
        self.ui.ecusBox.setCurrentIndex(self.ui.ecusBox.findData(gui.ECU_FAMILY_M797))
        ecu = self.initialize()
        self.detector.assert_not_called()
        self.assertIsInstance(ecu, gui.M797ECU)

    def test_unknown_or_unsupported_ecu_does_not_enter_simk_programming(self):
        self.assertFalse(self.initialize(family=gui.ECU_FAMILY_UNKNOWN))
        self.assertNotIn('Found!', str(self.log.emit.call_args_list))
        self.assertFalse(self.initialize(supported=False))
        self.assertIn('not supported', str(self.log.emit.call_args_list))

    def test_simk_manual_selection_keeps_existing_security_and_layout(self):
        self.ui.ecusBox.setCurrentIndex(1)
        channel = MemoryKLine()
        device = Mock()
        device.kline.return_value = channel
        self.ui.get_selected_interface = Mock(return_value=(device, gui.Bus.KLINE))
        bus = Mock()
        bus.execute.return_value.get_data.return_value = bytes(6)
        with patch.object(gui.kwp2000, 'Kwp2000Protocol', return_value=bus), \
             patch.object(gui, 'detect_ecu_family') as detect, \
             patch.object(gui, 'enable_security_access') as security:
            ecu = self.ui.initialize_ecu(self.log, m797_supported=True)
        security.assert_called_once_with(bus)
        detect.assert_not_called()
        self.assertNotIsInstance(ecu, gui.M797ECU)
        self.assertEqual(ecu.get_name(), gui.ECU_IDENTIFICATION_TABLE[0]['ecu']['name'])

    def test_invalid_kefico_speed_and_can_selection_are_rejected(self):
        self.ui.baudratesBox.setCurrentIndex(self.ui.baudratesBox.findData(0x05))
        self.assertFalse(self.initialize())
        self.assertIn('Use 0x64 (57600) or 0x82 (108800)', str(self.log.emit.call_args_list))
        self.ui.ecusBox.setCurrentIndex(self.ui.ecusBox.findData(gui.ECU_FAMILY_M797))
        device = Mock()
        self.ui.get_selected_interface = Mock(return_value=(device, gui.Bus.CANBUS))
        self.assertFalse(self.ui.initialize_ecu(self.log, m797_supported=True))
        device.canbus.assert_not_called()

    def test_read_buttons_dispatch_exact_ranges_and_progress(self):
        self.ui.readingFileInput.setText('chosen.bin')

        def read(bus, **kwargs):
            kwargs['log_callback'].emit('Kefico read complete')
            kwargs['progress_callback'](kwargs['size'])

        for method, address, size in (
            ('read_calibration_zone', 0x810000, 0x10000),
            ('read_program_zone', 0x820000, 0x60000),
            ('full_read', 0x800000, 0x80000),
        ):
            with self.subTest(method=method), \
                 patch.object(self.ui, 'initialize_ecu', return_value=self.ecu), \
                 patch.object(gui, 'get_m797_calibration_id', return_value='DMG_v69'), \
                 patch.object(gui, 'get_m797_engine_id', return_value='Alpha1.3S'), \
                 patch.object(gui, 'm797_read', side_effect=read) as core, \
                 patch.object(gui, 'read_memory', side_effect=AssertionError('SIMK read')):
                getattr(self.ui, method)(self.progress, self.log)
                kwargs = core.call_args.kwargs
                self.assertEqual((kwargs['address_start'], kwargs['size']), (address, size))
                self.assertEqual(kwargs['output_filename'], 'chosen.bin')
                self.assertEqual(kwargs['read_profile'], 'standard')
                self.assertEqual(kwargs['calibration_id'], 'DMG_v69')
                self.progress.emit.assert_called_with((100,))
        self.assertIn('Kefico read complete', str(self.log.emit.call_args_list))
        self.ui._close_bus.assert_called()

    def test_read_failure_closes_connection_and_reports_in_gui(self):
        with patch.object(gui, 'get_m797_calibration_id', return_value='DMG_v69'), \
             patch.object(gui, 'get_m797_engine_id', return_value='Alpha1.3S'), \
             patch.object(gui, 'm797_read', side_effect=RuntimeError('read failed')):
            with self.assertRaisesRegex(RuntimeError, 'read failed'):
                self.ui.gui_m797_read(self.ecu, 0x810000, 0x820000, False,
                                      'chosen.bin', self.log, self.progress)
        self.ui._close_bus.assert_called_once_with(self.log)
        self.ui.send_notification.assert_not_called()
        self.assertIn('read failed', str(self.log.emit.call_args_list))

    def test_busy_worker_prevents_overlapping_ecu_operations(self):
        self.ui.thread_manager = QtCore.QThreadPool()
        self.ui._operation_running = False
        self.ui.log = Mock()
        entered, release = threading.Event(), threading.Event()
        second = Mock()
        def first(progress_callback, log_callback):
            entered.set()
            release.wait(2)
        try:
            self.ui.click_handler(first)
            self.assertTrue(entered.wait(1))
            self.ui.click_handler(second)
            second.assert_not_called()
            self.ui.log.assert_called_once_with('[!] An ECU operation is already in progress.')
        finally:
            release.set()
            self.ui.thread_manager.waitForDone(2000)
            app.processEvents()
        self.assertFalse(self.ui._operation_running)

    def test_simk_disconnect_closes_device_even_after_stop_timeout(self):
        ecu = Mock(spec=gui.ECU)
        ecu.bus = Mock()
        ecu.bus.execute.side_effect = gui.TimeoutException
        self.ui.disconnect_ecu(ecu)
        self.ui._close_bus.assert_called_once_with(self.ui.log_signal)

    def test_flash_buttons_reuse_core_without_dialog_or_console_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(bytes(0x80000))
            self.ui.flashingFileInput.setText(str(filename))
            for method, calibration_only in (('flash_calibration', True), ('flash_full', False)):
                with self.subTest(method=method), \
                     patch.object(self.ui, 'initialize_ecu', return_value=self.ecu), \
                     patch.object(gui.QMessageBox, 'question', side_effect=AssertionError('unexpected dialog')), \
                     patch.object(gui, 'm797_flash', return_value=False) as core, \
                     patch('builtins.input', side_effect=AssertionError('console prompt')):
                    getattr(self.ui, method)(self.progress, self.log)
                    self.assertEqual(core.call_args.kwargs['calibration_only'], calibration_only)
                    self.assertFalse(core.call_args.kwargs['confirm'])
                    factory = core.call_args.kwargs['progress_factory']
                    callback = factory(512)
                    self.assertIsInstance(callback, gui.Progress)
                    callback(240)
                    self.progress.emit.assert_called_with((46,))
                    callback(272)
                    self.progress.emit.assert_called_with((100,))
                    total = 0x10000 if calibration_only else 0x80000
                    callback = factory(total)
                    self.progress.emit.assert_called_with((0,))
                    callback(total // 2)
                    self.progress.emit.assert_called_with((50,))
                    callback(total // 2)
                    self.progress.emit.assert_called_with((100,))
            self.assertIn('Power-cycle', str(self.ui.send_notification.call_args_list))

    def test_confirmed_reboot_notification_does_not_request_power_cycle(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gui, 'm797_flash', return_value=True):
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(bytes(0x80000))
            self.ui.gui_m797_flash(self.ecu, str(filename), True, False, self.log, self.progress)
            message = self.ui.send_notification.call_args.args[1]
            self.assertIn('ECU rebooted', message)
            self.assertNotIn('Power-cycle', message)

    def test_invalid_flash_never_calls_programming_core(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gui, 'm797_flash') as core, \
             patch.object(gui.QMessageBox, 'question', side_effect=AssertionError('unexpected dialog')) as dialog:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(b'invalid')
            with self.assertRaisesRegex(ValueError, '512 KiB'):
                self.ui.gui_m797_flash(self.ecu, str(filename), True, False, self.log, self.progress)
            dialog.assert_not_called()
            core.assert_not_called()

    def test_worker_flash_finishes_without_confirmation(self):
        finished = []
        errors = []

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gui.QMessageBox, 'question', side_effect=AssertionError('unexpected dialog')), \
             patch.object(gui, 'm797_flash') as core:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(bytes(0x80000))
            worker = gui.Worker(self.ui.gui_m797_flash, self.ecu, str(filename), True, False)
            worker.signals.finished.connect(lambda: finished.append(True))
            worker.signals.error.connect(errors.append)
            pool = QtCore.QThreadPool()
            pool.start(worker)
            deadline = time.monotonic() + 5
            while not finished and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            self.assertTrue(finished, 'flash worker did not finish')
            self.assertEqual(errors, [])
            core.assert_called_once()
            self.assertFalse(core.call_args.kwargs['confirm'])
            pool.waitForDone(1000)

    def test_identification_and_dtcs_skip_simk_session_and_immo_requests(self):
        with patch.object(self.ui, 'initialize_ecu', return_value=self.ecu), \
             patch.object(gui, 'display_m797_identification') as identification, \
             patch.object(gui, 'display_m797_dtcs') as dtcs:
            self.ui.display_ecu_identification(self.progress, self.log)
            self.ui.read_dtcs(self.progress, self.log)
        identification.assert_called_once_with(self.bus, log_callback=self.log)
        dtcs.assert_called_once_with(self.bus, log_callback=self.log)
        self.bus.execute.assert_not_called()

    def test_overlapping_worker_callbacks_stay_separate_without_redirecting_stdout(self):
        first_ready = threading.Event()
        second_ready = threading.Event()
        first, second = Mock(), Mock()
        original = StringIO()
        from flasher import ecu as ecu_module
        first_bus, second_bus = Mock(), Mock()

        def worker_one():
            first_ready.set()
            second_ready.wait(2)
            gui.display_m797_identification(first_bus, log_callback=first)

        def worker_two():
            first_ready.wait(2)
            second_ready.set()
            gui.display_m797_identification(second_bus, log_callback=second)

        def identification(bus, parameters):
            return {0x90: {'name': 'Calibration', 'value': b'FIRST' if bus is first_bus else b'SECOND'}}

        with redirect_stdout(original), patch.object(ecu_module, 'fetch_ecu_identification', side_effect=identification):
            threads = [threading.Thread(target=target) for target in (worker_one, worker_two)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            print('outside')
        self.assertIn('[ASCII]: FIRST', str(first.emit.call_args_list))
        self.assertNotIn('SECOND', str(first.emit.call_args_list))
        self.assertIn('[ASCII]: SECOND', str(second.emit.call_args_list))
        self.assertNotIn('FIRST', str(second.emit.call_args_list))
        self.assertEqual(original.getvalue(), 'outside\n')

    def test_log_callback_failure_never_changes_stdout(self):
        original = StringIO()
        log = Mock()
        log.emit.side_effect = RuntimeError('closed log widget')
        with redirect_stdout(original):
            with self.assertRaisesRegex(RuntimeError, 'closed log widget'):
                gui.display_m797_dtcs(self.bus, log_callback=log)
            print('restored')
        self.assertEqual(original.getvalue(), 'restored\n')

    def test_windowed_launch_without_console_uses_log_callback(self):
        self.bus.execute.return_value.get_data.return_value = b''
        with patch('sys.stdout', None):
            gui.display_m797_dtcs(self.bus, log_callback=self.log)
            self.assertIsNone(gui.sys.stdout)
        self.assertIn('Reading Kefico/Bosch M7.9.7 diagnostic trouble codes', str(self.log.emit.call_args_list))

    def test_checksum_tool_previews_and_saves_full_and_calibration_images(self):
        self.ui.log = Mock()
        for calibration_only in (False, True):
            for answer in (gui.QMessageBox.Yes, gui.QMessageBox.No):
                with self.subTest(calibration_only=calibration_only, answer=answer), \
                     tempfile.TemporaryDirectory() as directory, \
                     patch.object(gui.QMessageBox, 'question', return_value=answer) as dialog:
                    filename = Path(directory) / 'image.bin'
                    source = m797_checksum_image(calibration_only)
                    filename.write_bytes(source)
                    self.ui.checksumFileInput.setText(str(filename))
                    self.ui.correct_checksum()
                    expected = reference_correction(source, calibration_only) if answer == gui.QMessageBox.Yes else source
                    self.assertEqual(filename.read_bytes(), expected)
                    dialog.assert_called_once()
                    self.assertIn('Save checksum to file?', dialog.call_args.args)
                    message = dialog.call_args.args[2]
                    self.assertIn('Current Calibration 1 checksum: 0x0, new checksum:', message)
                    self.assertIn('Current Calibration 4 checksum:', message)
                    self.assertNotIn('range 0x', message)
                    self.assertEqual(message.count('Current '), 8 if calibration_only else 66)
                    self.assertIn('Current Calibration 1 complement checksum:', message)
                    if not calibration_only:
                        self.assertIn('Current Boot Header checksum:', message)
                        self.assertIn('Current Boot Boundary checksum:', message)
                        self.assertIn('Current Program 1 checksum:', message)
        self.assertIn('[*] OK! Current Calibration 1 checksum:', str(self.ui.log.call_args_list))
        self.bus.execute.assert_not_called()

    def test_checksum_save_failure_is_reported_without_leaving_the_qt_slot(self):
        self.ui.log = Mock()
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'full.bin'
            source = m797_checksum_image()
            filename.write_bytes(source)
            self.ui.checksumFileInput.setText(str(filename))
            with patch.object(gui.QMessageBox, 'question', return_value=gui.QMessageBox.Yes), \
                 patch.object(gui, 'save_m797_checksum_correction', side_effect=OSError('read-only file')):
                self.ui.correct_checksum()
            self.assertEqual(filename.read_bytes(), source)
            self.ui.log.assert_any_call('[!] Could not save checksum correction: read-only file')

    def test_checksum_tool_reports_invalid_and_already_correct_files_without_save(self):
        self.ui.log = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gui.QMessageBox, 'question', return_value=gui.QMessageBox.Yes) as dialog, \
             patch.object(gui, 'save_m797_checksum_correction') as save:
            filename = Path(directory) / 'image.bin'
            filename.write_bytes(b'invalid')
            self.ui.checksumFileInput.setText(str(filename))
            self.ui.correct_checksum()
            dialog.assert_not_called()
            for calibration_only in (False, True):
                source = reference_correction(m797_checksum_image(calibration_only), calibration_only)
                filename.write_bytes(source)
                dialog.reset_mock()
                self.ui.correct_checksum()
                self.assertEqual(filename.read_bytes(), source)
                dialog.assert_called_once()
                self.assertIn('Image checksums already correct.', dialog.call_args.args[2])
                self.ui.log.assert_any_call('[+] Image checksums already correct.')
                self.assertEqual(dialog.call_args.args[2].count('Current '), 0)
            save.assert_not_called()
        self.assertIn('already correct', str(self.ui.log.call_args_list))

    def test_checksum_tool_keeps_simk_save_workflow(self):
        self.ui.log = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(gui.QMessageBox, 'question', return_value=gui.QMessageBox.Yes) as dialog:
            filename = Path(directory) / 'simk.bin'
            source = simk_checksum_image()
            filename.write_bytes(source)
            self.ui.checksumFileInput.setText(str(filename))
            self.ui.correct_checksum()
            expected = bytearray(source)
            value = gui.checksum(source, 0x10000, 0x10004, 0x1234)
            expected[0x17EE0:0x17EE2] = value.to_bytes(2, 'little')
            self.assertEqual(filename.read_bytes(), expected)
            dialog.assert_called_once()


if __name__ == '__main__':
    unittest.main()
