import os, sys
import time
from datetime import datetime
from functools import partial
from concurrent.futures import Future

import plyer
from PyQt5 import QtWidgets, uic
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QFileDialog, QMessageBox
from PyQt5.QtCore import Qt, QThreadPool, QObject, pyqtSignal, QRunnable, pyqtSlot, QTimer, QThread
from plyer import notification
import gkbus, yaml, traceback, logging
from gkbus.device import Bus, DeviceABC, discover
from gkbus.exceptions import GkbusException, TimeoutException
from gkbus.transport import Kwp2000OverKLineTransport, Kwp2000OverCanTransport, RawPacket, PacketDirection
from gkbus.protocol import kwp2000
from gkbus.protocol.kwp2000.commands import *
from gkbus.protocol.kwp2000.enums import *
from flasher.ecu import KWP_RESPONSE_TIMEOUT, enable_security_access, configure_serial_kline, fetch_ecu_identification, identify_ecu, negotiate_timing, end_diagnostic_session, ECUIdentificationException, ECU, DesiredBaudrate
from flasher.baudrate import discover_baudrate
from flasher.memory import read_memory, write_memory, prepare_flash_payloads, prepare_write_memory
from flasher.checksum import *
from flasher.immo import immo_status
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES, Routine, AccessLevel
from gkflasher import strip
from _version import __version__
from flasher.lineswap import generate_sie, generate_bin, set_gui_log_handler as set_lineswap_log_handler
from flasher.smartra import calculate_smartra_pin
from flasher.rsw.handler import rsw_handler
from flasher.mtos.handler import mtos_handler, MTOS_MODES, WRITE_MODES
from flasher import bsl
from flasher.kernel import KERNEL_TIMING, available_filename
from flasher.progress import Progress as TransferProgress, transfer_summary

BUS_NAMES = {Bus.KLINE: 'K-Line', Bus.CANBUS: 'CAN bus'}


def configure_appearance(app):
	"""Keep native Windows rendering and load bundled fonts on other platforms."""
	root = os.path.dirname(os.path.abspath(__file__))
	if os.name != 'nt':
		for filename in ('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf', 'DejaVuSansMono.ttf'):
			path = os.path.join(root, 'assets', 'fonts', filename)
			if QFontDatabase.addApplicationFont(path) == -1:
				raise RuntimeError('Could not load bundled GUI font: {}'.format(path))
		app.setStyle('Fusion')
		font = QFont('DejaVu Sans')
		font.setPixelSize(11)
		font.setHintingPreference(QFont.PreferFullHinting)
		app.setFont(font)
	app.setAttribute(Qt.AA_DontUseNativeDialogs, os.name != 'nt')
	with open(os.path.join(root, 'flasher', 'gkflasher.qss'), encoding='utf-8') as stylesheet:
		style = stylesheet.read()
	if os.name == 'nt':
		style = style.replace('"DejaVu Sans"', '"MS Shell Dlg 2"').replace('"DejaVu Sans Mono"', '"Consolas"')
	else:
		# DejaVu's wider glyphs need slightly less padding to fit all tab headings.
		style += '\nQTabBar::tab { padding: 6px 7px; }\n'
	style = style.replace('url("assets/', 'url("{}/assets/'.format(root.replace('\\', '/')))
	app.setStyleSheet(style)

#
# @TODO: ... man, I don't even know. Start by separating this mess into controllers and views?
# Ideally, in gkflasher.py break up most of methods that are duplicated here so that 
# they can be mostly reused. Intercept stdout for rest of them? 
#

# Set user friendly working directory variable
if os.name == 'nt':
		import ctypes.wintypes
		CSIDL_PERSONAL = 5       # My Documents
		SHGFP_TYPE_CURRENT = 0   # Get current directory, not the default value.
		winhome = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
		ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, winhome)
		home = os.sep.join([winhome.value,"GKFlasher Files"])
else: #nix
	home = os.path.expanduser(os.sep.join(["~","Documents","GKFlasher Files"]))

os.makedirs(home, exist_ok=True)

try:
	log_path = home + '/gkflasher_debug.log'
	with open(log_path, 'a+') as f:
		f.write('\n\nGKFlasher GUI launched, {}\n'.format(__version__))
	logging.basicConfig(level=4, filename=log_path, datefmt='%Y-%m-%d %H:%M:%S.%f')
except KeyboardInterrupt:
	pass
except Exception as e:
	print('[!] Couldn\'t create or set the log file: {}'.format(str(e)))

Progress = partial(TransferProgress, incremental=True)

class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object)
    progress = pyqtSignal(tuple)
    log = pyqtSignal(str)

class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()

        # Store constructor arguments (re-used for processing)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

        # Add the callback to our kwargs
        self.kwargs['progress_callback'] = self.signals.progress
        self.kwargs['log_callback'] = self.signals.log

    @pyqtSlot()
    def run(self):
        # Retrieve args/kwargs here; and fire processing using them
        try:
            result = self.fn(*self.args, **self.kwargs)
        except KeyboardInterrupt:
            self.signals.log.emit('[!] Interrupted')
        except Exception as e:
            #traceback.print_exc()
            #exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((e, traceback.format_exc()))
        else:
            self.signals.result.emit(result)  # Return the result of the processing
        finally:
            self.signals.finished.emit()  # Done

class InterfaceDiscoverySignals(QObject):
    result = pyqtSignal(object)

class InterfaceDiscoveryWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = InterfaceDiscoverySignals()

    @pyqtSlot()
    def run(self):
        self.signals.result.emit(discover({Bus.KLINE, Bus.CANBUS}))

class Ui(QtWidgets.QMainWindow):
	VALIDATION_INTERVAL = 180 # Validation routine animation tick, in milliseconds.
	ERASE_INTERVAL = 20 # Erase routine animation duration.

	request_pin_signal1 = pyqtSignal(object, object)  # Signal to request PIN from the main thread. # Pass `log_callback` and `ecu`  
	request_pin_signal2 = pyqtSignal(object, object)  # Signal to request PIN from the main thread. # Pass `log_callback` and `ecu`  
	request_pin_signal3 = pyqtSignal(object, object)  # For requesting the current password
	request_pin_signal4 = pyqtSignal(object, object)  # For requesting the PIN
	request_pin_signal5 = pyqtSignal(object, object)  # For requesting the PIN
	request_new_password_signal = pyqtSignal(object, object, str)  # For requesting the new password. Three arguments: log_callback, ecu, current_password
	request_vin_signal = pyqtSignal(object, object)  # For requesting the Write VIN Input
	request_vin_to_pin_signal = pyqtSignal(object)  # For requesting the VIN to PIN Input
	mtos_warning_requested = pyqtSignal(str, object)
	automatic_baudrate_selected = pyqtSignal(int)
	log_signal = pyqtSignal(str)  # Define a signal for logging

	def __init__(self):
		super(Ui, self).__init__()
		self._operation_running = False
		self.load_ui()
		self.previous_baudrate = False
		self.interfacesBox.currentIndexChanged.connect(self.update_baudrate_state)
		self.update_baudrate_state()
		self.mtos_warning_requested.connect(self._show_mtos_warning)
		self.automatic_baudrate_selected.connect(self.show_automatic_baudrate)
		self.log_signal.connect(self.log)  # Connect the signal to the `log` method

		# bin <-> sie conversion messages
		set_lineswap_log_handler(self.log_signal.emit)

		# Connect the signals
		self.request_pin_signal1.connect(self.request_pin_from_user1)
		self.request_pin_signal2.connect(self.request_pin_from_user2)
		self.request_pin_signal3.connect(self.request_pin_from_user3)
		self.request_pin_signal4.connect(self.request_pin_from_user4)
		self.request_pin_signal5.connect(self.request_pin_from_user5)
		self.request_new_password_signal.connect(self.request_new_password_from_user)
		self.request_vin_signal.connect(self.request_vin_from_user)
		self.request_vin_to_pin_signal.connect(self.request_vin_to_pin_from_user)

		# Change the working directory
		os.chdir (home)

	def handle_exception (self, data):
		exception, traceback_str = data
		print('\n\n[!] Exception in main thread!')
		print(traceback_str)
		print('[*] Dumping buffer:\n')
		try:
			print('\n'.join([packet2hex(packet) for packet in self.bus.transport.buffer_dump()]))
		except:
			print('[!] Buffer not found!')
		print('\n[!] For exception details, see above.')
		self._close_bus(self.log_signal)

	def load_ui(self):
		uic.loadUi(os.path.dirname(os.path.abspath(__file__)) + '/flasher/gkflasher.ui', self)
		# Keep tab headings clear of the baudrate selector on every platform.
		self.tabWidget.tabBar().setMaximumWidth(self.ecusBox.x() - self.tabWidget.x() - 10)
		# Fit checkbox text to the active font, with a small consistent gap.
		x = self.mtosSkipBackupCheck.x()
		for checkbox in (self.mtosSkipBackupCheck, self.mtosForceWriteCheck,
					 self.mtosSkipVerifyCheck, self.mtosOverrideSafetyCheck):
			checkbox.setFixedWidth(checkbox.sizeHint().width())
			checkbox.move(x, checkbox.y())
			x += checkbox.width() + 4
		self.thread_manager = QThreadPool()
		self.setWindowTitle('GKFlasher [{}]'.format(__version__))
		self.show()
		
		# Seperate thread to run the interface discovery process.
		self.interfacesBox.addItem("Discovering interfaces...")
		self.interface_discovery_worker = InterfaceDiscoveryWorker()
		self.interface_discovery_worker.signals.result.connect(self.populate_interfaces)
		self.thread_manager.start(self.interface_discovery_worker)

		# Timer animation that updates the progress bar every 200 milliseconds.
		self.validation_timer = QTimer(self)
		self.validation_timer.setInterval(200)
		self.validation_progress = 0
		self.validation_timer.timeout.connect(self.update_validation_progress)

		self.load_ecus()
		self.load_baudrates()
		self.mtosModeBox.clear()
		self.mtosModeBox.addItems(MTOS_MODES)
		self.add_listeners()

	def add_listeners (self):
		self.readCalibrationZone.clicked.connect(lambda: self.click_handler(self.read_calibration_zone))
		self.readProgramZone.clicked.connect(lambda: self.click_handler(self.read_program_zone))
		self.readFull.clicked.connect(lambda: self.click_handler(self.full_read))
		self.readDtcs.clicked.connect(lambda: self.click_handler(self.read_dtcs))

		self.displayECUID.clicked.connect(lambda: self.click_handler(self.display_ecu_identification))

		self.checksumCorrectBtn.clicked.connect(self.correct_checksum)
		self.binToSieBtn.clicked.connect(self.bin_to_sie_conversion)
		self.sieToBinBtn.clicked.connect(self.sie_to_bin_conversion)

		self.flashingCalibrationBtn.clicked.connect(lambda: self.click_handler(self.flash_calibration))
		self.flashingProgramBtn.clicked.connect(lambda: self.click_handler(self.flash_program))
		self.flashingFullBtn.clicked.connect(lambda: self.click_handler(self.flash_full))
		self.flashingClearAVBtn.clicked.connect(lambda: self.click_handler(self.clear_adaptive_values))

		self.readingFileBtn.clicked.connect(self.handler_select_file_reading)
		self.flashingFileBtn.clicked.connect(self.handler_select_file_flashing)
		self.checksumFileBtn.clicked.connect(self.handler_select_file_checksum)
		self.bslFileBtn.clicked.connect(self.handler_select_bsl_file)
		self.rswFileBtn.clicked.connect(self.select_rsw_file)

		self.immoInfoBtn.clicked.connect(lambda: self.click_handler(self.display_immo_information))
		self.limpHomeModeBtn.clicked.connect(lambda: self.click_handler(self.limp_home))
		self.limpHomePasswordChangeBtn.clicked.connect(lambda: self.click_handler(self.limp_home_teach))
		self.immoResetBtn.clicked.connect(lambda: self.click_handler(self.immo_reset))
		self.smartraNeturalizeBtn.clicked.connect(lambda: self.click_handler(self.smartra_neutralize))
		self.teachKeysBtn.clicked.connect(lambda: self.click_handler(self.teach_keys))
		self.readVinBtn.clicked.connect(lambda: self.click_handler(self.read_vin))
		self.writeVinBtn.clicked.connect(lambda: self.click_handler(self.write_vin))
		self.vinToPinBtn.clicked.connect(lambda: self.click_handler(self.vin_to_pin))

		self.bslHwInfoBtn.clicked.connect(lambda: self.click_handler(partial(self.run_bsl, 'hwinfo')))
		self.bslReadIntRomBtn.clicked.connect(lambda: self.click_handler(partial(self.run_bsl, 'readint')))
		self.bslReadExtFlashBtn.clicked.connect(lambda: self.click_handler(partial(self.run_bsl, 'read')))
		self.bslWriteExtFlashBtn.clicked.connect(self.handler_bsl_write)

		self.rswBSWBtn.clicked.connect(lambda: self.click_handler(self.rsw_flash_bsw))
		self.rswASWBtn.clicked.connect(lambda: self.click_handler(self.rsw_flash_asw))
		self.rswCALBtn.clicked.connect(lambda: self.click_handler(self.rsw_flash_cal))
		self.rswFullBtn.clicked.connect(lambda: self.click_handler(self.rsw_flash_full))
		self.rswVirginizeBtn.clicked.connect(lambda: self.click_handler(self.rsw_virginize))

		self.mtosFileBtn.clicked.connect(self.handler_select_mtos_file)
		self.mtosRunBtn.clicked.connect(self.handler_run_mtos)

	def click_handler (self, callback):
		if self._operation_running:
			self.log_signal.emit('[!] An operation is already running. Wait for it to finish before starting another.')
			return
		self._operation_running = True
		worker = Worker(callback)
		worker.signals.log.connect(self.log)
		worker.signals.progress.connect(self.progress_callback)
		worker.signals.error.connect(self.handle_exception)
		worker.signals.finished.connect(self._operation_finished)
		self.thread_manager.start(worker)

	@pyqtSlot()
	def _operation_finished (self):
		self._operation_running = False

	def send_notification (self, title: str, body: str) -> None:
		try:
			notification.notify(title=title, message=body)
		except Exception as e:
			print('[.] Non-critical: Exception while trying to send a notification: {}'.format(str(e)))

	def handler_select_file_reading (self):
		filename = QFileDialog().getSaveFileName()[0]
		self.readingFileInput.setText(os.path.normpath(filename) if filename else '')

	def handler_select_file_flashing (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.flashingFileInput.setText(os.path.normpath(filename) if filename else '')

	def handler_select_file_checksum (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.checksumFileInput.setText(os.path.normpath(filename) if filename else '')

	def handler_select_bsl_file (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.bslFileInput.setText(os.path.normpath(filename) if filename else '')

	def log (self, text):
		self.logOutput.append(text)
		self.logOutput.setReadOnly(True)









	def populate_interfaces(self, devices):
		self.interfacesBox.clear()

		if len(devices) == 0:
			self.interfacesBox.addItem('[!] No interfaces found. Connect an adapter and restart.')
			return

		# one entry per bus a device supports - not every ECU speaks CAN, so on a device
		# supporting both the user picks which one to use
		for bus_type in (Bus.KLINE, Bus.CANBUS):
			for device in devices:
				if bus_type in device.buses:
					self.interfacesBox.addItem(
						'{} ({})'.format(device.description(), BUS_NAMES[bus_type]), (device, bus_type)
					)

		self.update_baudrate_state()





	def load_ecus (self):
		self.ecusBox.addItem('ECU (autodetect)', -1)
		for index, ecu in enumerate(ECU_IDENTIFICATION_TABLE):
			self.ecusBox.addItem('    [{}] {}'.format(index, ecu['ecu']['name']), index)

	def load_baudrates (self):
		self.bslBaudrateBox.insertItem(0, 'Automatic')
		self.bslBaudrateBox.setCurrentIndex(0)
		self.baudratesBox.addItem('Desired Baudrate (automatic)', -1)
		self.baudratesBox.setItemData(0,
			'Discover the fastest verified rate for normal KWP or the selected RAM loader.', Qt.ToolTipRole)
		self._baudrates = {**BAUDRATES, 0x06: 750000}
		for index, baudrate in self._baudrates.items():
			self.baudratesBox.addItem('{} baud'.format(baudrate), index)
		self.baudratesBox.setItemData(self.baudratesBox.count() - 1,
			'Normal KWP requires compatible OpenGK firmware. MTOS selects this rate in its RAM payload.', Qt.ToolTipRole)
		self.baudratesBox.setCurrentIndex(0)

	def update_baudrate_state(self):
		selected = self.interfacesBox.currentData()

		is_can = bool(selected) and selected[1] == Bus.CANBUS

		was_enabled = self.baudratesBox.isEnabled()
		self.baudratesBox.setItemText(0, 'Desired Baudrate (500 Kbps)' if is_can
			else 'Desired Baudrate (automatic)')
		self.baudratesBox.setEnabled(not is_can)

		if is_can:
			self.baudratesBox.setCurrentIndex(0)
		elif not was_enabled:
			self.baudratesBox.setCurrentIndex(0)

	@pyqtSlot(int)
	def show_automatic_baudrate(self, baudrate):
		if self.baudratesBox.isEnabled() and self.baudratesBox.currentData() == -1:
			self.baudratesBox.setItemText(0, 'Desired Baudrate (auto {:g}k)'.format(baudrate / 1000))

	def get_selected_interface (self) -> tuple[DeviceABC, Bus]:
		selected = self.interfacesBox.currentData()
		if not selected:
			raise IndexError
		return selected

	# @todo: not ideal, but its a bridge towards moving this completely to the ECU class. it was a mess
	def get_desired_baudrate (self) -> DesiredBaudrate:
		baudrate_index = self.baudratesBox.currentData()
		if self.baudratesBox.isEnabled() and baudrate_index == -1:
			return DesiredBaudrate(index=-1, baudrate=None)
		if (not self.baudratesBox.isEnabled()
		or baudrate_index == -1
		or self._baudrates.get(baudrate_index) == 10400):
			return DesiredBaudrate(index=None, baudrate=10400)
		return DesiredBaudrate(index=baudrate_index, baudrate=self._baudrates[baudrate_index])

	def progress_callback(self, value):
		if len(value) > 2:
			if value[2] == 'baudrate':
				self.show_automatic_baudrate(value[0])
				if self.bslBaudrateBox.currentIndex() == 0:
					self.bslBaudrateBox.setItemText(0, 'Automatic ({:g}k)'.format(value[0] / 1000))
			elif value[2] == 'bytes':
				self.progressBar.setValue(value[0])
				self.progressBar.setFormat('%p% | {:,.0f} B/s'.format(value[1]))
			elif value[2] == 1:
				self.validation_timer.stop()
				self.validation_progress = 0
				self.progressBar.setValue(0)
				if len(value) > 3 and value[3] == 'erase':
					self.progressBar.setRange(0, 100)
					self.progressBar.setFormat('%p%')
					self.validation_timer.setInterval(self.ERASE_INTERVAL)
					self.validation_timer.start()
				else:
					self.progressBar.setRange(0, 100)
					self.progressBar.setFormat('Waiting for ECU...')
					self.validation_timer.setInterval(self.VALIDATION_INTERVAL)
					self.validation_timer.start()

			elif value[2] == 0:
				self.validation_timer.stop()
				self.progressBar.setRange(0, 100)
				self.progressBar.setFormat('%p%')
				self.progressBar.setValue(100)

		elif len(value) > 1:
			self.progressBar.setFormat('%p%')
			self.progressBar.setMaximum(value[0])
			self.progressBar.setValue(0)
		else:
			self.progressBar.setValue(value[0])

	def update_validation_progress(self):
		if self.validation_progress < 95:
			self.validation_progress += 1
			self.progressBar.setValue(self.validation_progress)

	def _close_bus (self, log_callback) -> None:
		# the device too, it closes the channel opened in _initialize_ecu()
		for name in ('bus', 'device'):
			closeable = getattr(self, name, None)
			setattr(self, name, None)
			if closeable is None:
				continue
			try:
				closeable.close()
			except GkbusException as e:
				log_callback.emit('[!] Failed to close the port. This shouldn\'t affect operation. Please report to OpenGK: {}'.format(str(e)))

	def _initialize_ecu (self, log_callback, desired_baudrate=None, p4_min=0.001) -> ECU:
		self._close_bus(log_callback)

		try:
			device, bus_type = self.get_selected_interface()
		except IndexError:
			log_callback.emit('[*] Interface not found!')
			return False

		log_callback.emit('[*] Initializing interface {} ({})'.format(device.description(), BUS_NAMES[bus_type]))

		config = yaml.safe_load(open(os.path.dirname(os.path.abspath(__file__)) + '/gkflasher.yml'))

		self.device = device
		if bus_type == Bus.CANBUS:
			channel = device.canbus(config['canbus']['bitrate'])
			transport = Kwp2000OverCanTransport(channel, tx_id=config['canbus']['tx_id'], rx_id=config['canbus']['rx_id'])
		else:
			channel = device.kline()
			transport = Kwp2000OverKLineTransport(channel, tx_id=config['kline']['tx_id'], rx_id=config['kline']['rx_id'])

		# opened here rather than by the transport, so the channel outlives the KWP2000 session
		# (RSW switches to CCP on it). Closed by _close_bus() along with the device
		channel.open()
		configure_serial_kline(channel, p4_min=p4_min)

		self.bus = kwp2000.Kwp2000Protocol(transport)
		self.bus.transport.set_timeout(KWP_RESPONSE_TIMEOUT)
		self.bus.init(StartCommunication(), keepalive_command=TesterPresent(ResponseType.REQUIRED), keepalive_delay=2)

		if desired_baudrate is None:
			desired_baudrate = self.get_desired_baudrate()
		automatic_baudrate = bus_type == Bus.KLINE and desired_baudrate.index == -1
		if automatic_baudrate:
			desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)
		if not desired_baudrate.index:
			desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)
			log_callback.emit('[*] Trying to start diagnostic session')
			self.bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING))
		else:
			log_callback.emit('[*] Trying to start diagnostic session with baudrate {}'.format(desired_baudrate.baudrate))
			try:
				self.bus.execute(desired_baudrate.session_command(DiagnosticSession.FLASH_REPROGRAMMING))
				self.bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
			except TimeoutException:
				# it's possible that the ecu is already running at the desired baudrate - let's check
				self.bus.transport.channel.reset_buffers()
				self.bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
				self.bus.execute(desired_baudrate.session_command(DiagnosticSession.FLASH_REPROGRAMMING))

		if not automatic_baudrate:
			log_callback.emit('[*] Set timing parameters to maximum')
			if not negotiate_timing(self.bus):
				log_callback.emit('[!] Not supported on this ECU!')

		log_callback.emit('[*] Security Access')
		enable_security_access(self.bus)

		log_callback.emit('[*] Trying to identify ECU.. ')
		if self.ecusBox.currentData() == -1:
			ecu = identify_ecu(self.bus)
		else:
			ecu = ECU(**ECU_IDENTIFICATION_TABLE[self.ecusBox.currentData()]['ecu'])
			ecu.set_bus(self.bus)
		
		if automatic_baudrate:
			desired_baudrate = discover_baudrate(ecu, log_callback.emit)
			log_callback.emit('[*] Set timing parameters to maximum')
			if not negotiate_timing(self.bus):
				log_callback.emit('[!] Not supported on this ECU!')
			self.automatic_baudrate_selected.emit(desired_baudrate.baudrate)
		ecu.set_desired_baudrate(desired_baudrate)
		ecu.diagnostic_session_type = DiagnosticSession.FLASH_REPROGRAMMING
		ecu.access_level = AccessLevel.HYUNDAI_0x1

		log_callback.emit('[*] Found! {}'.format(ecu.get_name()))
		
		return ecu

	def initialize_ecu (self, log_callback, desired_baudrate=None, p4_min=0.001) -> ECU:
		try:
			ecu = self._initialize_ecu(log_callback, desired_baudrate=desired_baudrate, p4_min=p4_min)
			if ecu:
				return ecu
		except TimeoutException:
			message = '[!] Timeout! Check the ECU connection and selected baudrate.'
		except ECUIdentificationException:
			message = '[!] Failed to identify ECU! Please select it from the dropdown and try again.'
		except Exception as exc:
			message = '[!] Exception occurred: {}'.format(exc)
		else:
			message = None
		# Finish teardown before announcing that another attempt can start.
		bus = getattr(self, 'bus', None)
		if bus is not None:
			bus.transport.set_timeout(0.5)
			bus._stop_keepalive()
			try:
				if isinstance(bus.transport, Kwp2000OverKLineTransport):
					bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT))
					bus.transport.channel.set_baudrate(10400)
			except (GkbusException, kwp2000.Kwp2000Exception, AttributeError):
				pass
		self._close_bus(log_callback)
		if message:
			log_callback.emit(message)
		return False

	def disconnect_ecu (self, ecu: ECU) -> None:
		try:
			end_diagnostic_session(ecu.bus)
		finally:
			self._close_bus(self.log_signal)

	def gui_read_eeprom (self, ecu: ECU, address_start: int = 0x000000, address_stop: int = None, escalate_privileges: bool = False, output_filename: str = None, log_callback=None, progress_callback=None):
		eeprom_size = ecu.get_eeprom_size_bytes()

		if escalate_privileges:
			log_callback.emit('[*] Attempting privilege escalation with the IOCLID patch')
			if (ecu.security_access(AccessLevel.SIEMENS_0xFD)):
				log_callback.emit('[*] Success!')
				address_start = 0
				address_stop = eeprom_size
			else:
				log_callback.emit('[!] Patch likely not present, failed to escalate privileges (this is normal).')
				log_callback.emit('    Read will only include the calibration and program zones.')
				log_callback.emit('    If you\'re running ca663056, feel encouraged to apply the IOCLID patch.')
				log_callback.emit('    It will allow you to read the whole memory over OBD2, just like BSL.')
				log_callback.emit('    You can find it at https://github.com/OpenGK-org/opengk-simk')

		if (address_stop == None):
			address_stop = eeprom_size

		log_callback.emit('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

		requested_size = address_stop-address_start
		eeprom = bytearray([0xFF]*eeprom_size)

		progress = Progress(progress_callback, requested_size)
		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=progress)

		eeprom_start = ecu.calculate_bin_offset(address_start)
		eeprom_end = eeprom_start + len(fetched)
		eeprom[eeprom_start:eeprom_end] = fetched

		if (output_filename == None):
			try:
				calibration = ecu.get_calibration()
				description = ecu.get_calibration_description()
				hw_rev_c = strip(''.join([chr(x) for x in list(ecu.bus.execute(ReadEcuIdentification(0x8c)).get_data())[1:]]))
				hw_rev_d = strip(''.join([chr(x) for x in list(ecu.bus.execute(ReadEcuIdentification(0x8d)).get_data())[1:]]))
				output_filename = "{}_{}_{}_{}_{}.bin".format(description, calibration, hw_rev_c, hw_rev_d, datetime.now().strftime('%Y-%m-%d_%H%M'))
			except: # dirty
				output_filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))
		
		with open(output_filename, "wb") as file:
			file.write(bytes(eeprom))

		log_callback.emit(transfer_summary('Read', len(fetched), progress.started_at))
		log_callback.emit('[*] Saved to {}'.format(os.path.abspath(output_filename)))

		log_callback.emit('[*] Done!')
		self.send_notification('Reading finished', 'Saved to {}'.format(os.path.abspath(output_filename)))

	def gui_flash_eeprom (self, ecu: ECU, input_filename: str, flash_calibration: bool = True, flash_program: bool = True, log_callback=None, progress_callback=None):
		log_callback.emit('[*] Loading up {}'.format(input_filename))

		try:
			with open(input_filename, 'rb') as file:
				eeprom = file.read()
			log_callback.emit('[*] Loaded {} bytes'.format(len(eeprom)))
		except FileNotFoundError:
			log_callback.emit('[!] Error: File not found.')
			return self.disconnect_ecu(ecu)

		try:
			payloads = prepare_flash_payloads(ecu, eeprom, flash_calibration, flash_program)
		except ValueError as exc:
			log_callback.emit('[!] Error: {}'.format(exc))
			return

		prepare_write_memory(ecu)
		ecu.bus._stop_keepalive()
		ecu.bus.transport.set_timeout(12)  # Erase timeout; verification has its own longer wait.
		started_at = time.monotonic()
		written_bytes = 0

		if flash_program:
			log_callback.emit('[*] Start routine 0x00 (erase program code section)')
			# Start the estimated erase progress animation.
			progress_callback.emit((0, 0, 1, 'erase'))
			try:
				ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ERASE_PROGRAM.value))
			finally:
				# Stop the animation when the erase routine finishes.
				progress_callback.emit((0, 0, 0))

			payload = payloads['program']

			flash_start = ecu.get_region('program').write.address + 16
			flash_size = len(payload)

			log_callback.emit('[*] Uploading data to the ECU')
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))
			written_bytes += len(payload)

		if flash_calibration:
			log_callback.emit('[*] Start routine 0x01 (erase calibration section)')
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ERASE_CALIBRATION.value))

			payload = payloads['calibration']

			flash_start = ecu.calculate_memory_write_offset(ecu.get_region('calibration').write.address)
			flash_size = len(payload)

			log_callback.emit('[*] Uploading data to the ECU')
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))
			written_bytes += len(payload)

		flash_summary = transfer_summary('Flashed', written_bytes, started_at)
		ecu.bus.transport.set_timeout(300)
		log_callback.emit('[*] Start routine 0x02 (verify blocks and mark as ready to execute)')

		# Show activity until the ECU reports validation complete.
		progress_callback.emit((0, 0, 1))

		try:
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.VERIFY_BLOCKS.value))
		except kwp2000.Kwp2000NegativeResponseException as e:
			print(str(e))
			log_callback.emit(str(e))
			log_callback.emit('[*] Verifying blocks failed! Did you forget to correct the checksum?')
			
			log_callback.emit('[!] Your ECU is now soft-bricked. There\'s no need to panic, all you need to do is flash a valid file.')
			progress_callback.emit((0, 0, 0))
			return

		progress_callback.emit((0, 0, 0))

		log_callback.emit(flash_summary)
		log_callback.emit('[*] ECU Reset')
		log_callback.emit('[*] Done!')
		self.send_notification('Flashing finished', 'Turn off your ignition for 10 seconds')
		
		ecu.bus.transport.set_timeout(0.5)
		try:
			ecu.bus.execute(ECUReset(ResetMode.POWER_ON_RESET))
		except TimeoutException: 
			pass # response is not guaranteed

	def read_calibration_zone (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		self.gui_read_eeprom(
			ecu,
			address_start=ecu.get_region('calibration').read.address,
			address_stop=ecu.get_region('calibration').read.address+ecu.get_region('calibration').read.size,
			output_filename=output_filename, 
			log_callback=log_callback, 
			progress_callback=progress_callback
		)
		self.disconnect_ecu(ecu)

	def read_program_zone (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		address_start = ecu.get_region('program').read.address
		address_stop = address_start + ecu.get_region('program').read.size

		self.gui_read_eeprom(ecu, address_start=address_start, address_stop=address_stop, output_filename=output_filename, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def full_read (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		eeprom_size = ecu.get_eeprom_size_bytes()
		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		address_start = ecu.get_region('calibration').read.address
		address_stop = ecu.get_region('program').read.address+ecu.get_region('program').read.size

		self.gui_read_eeprom(ecu, address_start=address_start, address_stop=address_stop, escalate_privileges=True, output_filename=output_filename, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def read_dtcs(self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)

		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Reading diagnostic trouble codes')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(kwp2000.enums.DiagnosticSession.DEFAULT))

		dtcs_raw = ecu.bus.execute(
			kwp2000.commands.ReadDTCsByStatus(
				kwp2000.enums.DtcStatus.REQUEST_IDENTIFIED_DTC_AND_STATUS,
				kwp2000.enums.DtcGroup.POWERTRAIN
			)
		).get_data()

		dtc_amount = dtcs_raw[0]
		log_callback.emit('[*] Amount of DTCs: {}'.format(dtc_amount))
		dtcs = {}
		for x in range(dtc_amount):
			dtc = int.from_bytes(dtcs_raw[1:][(x*3):(x*3) + 2])
			dtc_status = dtcs_raw[1:][(x*3) + 2]
			dtcs[dtc] = dtc_status

		for dtc, status in dtcs.items():
			log_callback.emit('[*] DTC: P{} ({})'.format(f"{dtc:04x}", bin(status)))

		self.disconnect_ecu(ecu)

	def display_ecu_identification (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Querying additional parameters,  this might take a few seconds..')

		for parameter_key, parameter in fetch_ecu_identification(ecu.bus).items():
			value_dec = list(parameter['value'])
			value_hex = ' '.join([hex(x) for x in value_dec])
			value_ascii = strip(''.join([chr(x) for x in value_dec]))

			log_callback.emit('')
			log_callback.emit('    [*] [{}] {}:'.format(hex(parameter_key), parameter['name']))
			log_callback.emit('            [HEX]: {}'.format(value_hex))
			log_callback.emit('            [ASCII]: {}'.format(value_ascii))
			log_callback.emit('')

		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))
		try:
			immo_data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.QUERY_IMMO_INFO.value)).get_data()
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[*] Immo seems to be disabled')
			return self.disconnect_ecu(ecu)

		log_callback.emit('[*] Immo keys learnt: {}'.format(immo_data[1]))
		ecu_status = immo_status[immo_data[2]]
		key_status = immo_status[immo_data[3]]
	
		log_callback.emit('[*] Immo ECU status: {}'.format(ecu_status))
		log_callback.emit('[*] Immo key status: {}'.format(key_status))
		if (len(immo_data) > 4):
			log_callback.emit('[*] Smartra status: {}'.format(immo_status[immo_data[4]]))
		self.disconnect_ecu(ecu)

	def correct_checksum (self):
		filename = self.checksumFileInput.text()
		self.log('[*] Reading {}'.format(filename))

		try:
			with open(filename, 'rb') as file:
				payload = file.read()
		except FileNotFoundError:
			self.log('[!] Error: File not found.')
			return
		self.log('Trying to detect type.. ')
		cks_type = detect_offsets(payload)
		
		if cks_type == None:
			self.log('[!] Error: Calibration zone not detected.')
			return
		self.log(cks_type['name'])

		for region in cks_type['regions']:
			self.log('[*] Calculating checksum for region {}'.format(region['name']))
			flag_address, init_address, cks_address, bin_offset = region['flag_address'], region['init_address'], region['cks_address'], region['bin_offset']

			amount_of_zones = int.from_bytes(payload[cks_address+2:cks_address+3], "big")
			self.log('[*] Amount of zones: {}'.format(amount_of_zones))

			if (amount_of_zones == 0 or amount_of_zones == 0xFF):
				self.log('[*] Skipping region {}'.format(region['name']))
				continue

			checksums = []

			zone_address = cks_address
			for zone_index in range(amount_of_zones):

				self.log('[*] Trying to find addresses of zone #{}.. '.format(zone_index+1))
				zone_start_offset = zone_address+0x04
				zone_start = concat_3_bytes(read_and_reverse(payload, zone_start_offset, 3)) + bin_offset

				zone_stop_offset = zone_address+0x08
				zone_stop = concat_3_bytes(read_and_reverse(payload, zone_stop_offset, 3)) + bin_offset + 1
				self.log('{} - {}'.format(hex(zone_start), hex(zone_stop)))

				self.log('[*] Trying to find initial value.. ')
				if (zone_index == 0):
					initial_value_bytes = read_and_reverse(payload, init_address, 2)
					initial_value = (initial_value_bytes[0]<< 8) | initial_value_bytes[1]
				else:
					initial_value = checksums[zone_index-1]
				self.log(hex(initial_value))

				self.log('[*] checksum of zone #{}: '.format(zone_index+1))
				zone_cks = checksum(payload, zone_start, zone_stop, initial_value)
				self.log(hex(zone_cks))
				checksums.append(zone_cks)
				zone_address += 0x08

			checksum_b1 = (checksums[-1] >> 8) & 0xFF
			checksum_b2 = (checksums[-1] & 0xFF)
			checksum_reversed = (checksum_b2 << 8) | checksum_b1

			current_checksum = int.from_bytes(payload[cks_address:cks_address+2], "big")
			region['current_checksum'] = current_checksum
			region['checksum'] = checksum_reversed

			self.log('[*] OK! Current {} checksum: {}, new checksum: {}'.format(region['name'], hex(current_checksum), hex(checksum_reversed)))

		dialog_message = ''
		for region in cks_type['regions']:
			try:
				dialog_message += 'Current {} checksum: {}, new checksum: {}\n'.format(region['name'], hex(region['current_checksum']), hex(region['checksum']))
			except KeyError:
				continue
		dialog_message += 'Save?'

		if QMessageBox.question(
				self, 
				'Save checksum to file?', 
				dialog_message,
				QMessageBox.Yes | QMessageBox.No, 
				QMessageBox.No
			) == QMessageBox.Yes:

			self.log('[*] Saving to {}'.format(filename))
			with open(filename, 'rb+') as file:
				for region in cks_type['regions']:
					file.seek(region['cks_address'])
					try:
						file.write(region['checksum'].to_bytes(2, "big"))
					except KeyError:
						continue

		self.log('[*] Done!')

	def flash_calibration (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=False, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def flash_program (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=False, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def flash_full (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def clear_adaptive_values (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Clearing adaptive values.. ')
		ecu.clear_adaptive_values()
		log_callback.emit('Done! Turn off ignition for 10 seconds to apply changes.')
		self.disconnect_ecu(ecu)

	def display_immo_information (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return
		
		log_callback.emit('[*] Querying additional parameters,  this might take a few seconds..')

		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))
		
		try:
			immo_data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.QUERY_IMMO_INFO.value)).get_data()
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[*] Immo seems to be disabled')
			return self.disconnect_ecu(ecu)

		log_callback.emit('[*] Immo keys learnt: {}'.format(immo_data[1]))
		ecu_status = immo_status[immo_data[2]]
		key_status = immo_status[immo_data[3]]
	
		log_callback.emit('[*] Immo ECU status: {}'.format(ecu_status))
		log_callback.emit('[*] Immo key status: {}'.format(key_status))
		if (len(immo_data) > 4):
			log_callback.emit('[*] Smartra status: {}'.format(immo_status[immo_data[4]]))
		self.disconnect_ecu(ecu)


	def immo_reset(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		log_callback.emit('[*] Checking Immobilizer status...')
		try:
			data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.BEFORE_IMMO_RESET.value)).get_data()
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Unable to validate immobilizer status. The immobilizer is either disabled or disconnected.')
			return self.disconnect_ecu(ecu)

		# Check if the system is locked
		if len(data) > 1 and data[1] == 4:
			log_callback.emit('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return self.disconnect_ecu(ecu)

		# Request the PIN asynchronously
		self.request_pin_signal1.emit(log_callback, ecu)

	def request_pin_from_user1(self, log_callback, ecu: ECU):
		# Open the PIN dialog on the main thread
		pin, ok = QtWidgets.QInputDialog.getText(self, 'Enter PIN', 'Enter 6-digit immobilizer PIN:')
		if ok and pin.isdigit() and len(pin) == 6:
			self.continue_immo_reset(pin, log_callback, ecu)  # Continue with the reset process
		else:
			self.log('[!] Invalid PIN or operation cancelled.')
		return self.disconnect_ecu(ecu)	

	def continue_immo_reset(self, pin: str, log_callback, ecu: ECU) -> None:
		pin = int('0x' + pin, 0)  # Treat the input as a hexadecimal string
		pin_a, pin_b, pin_c = (pin >> 16) & 0xFF, (pin >> 8) & 0xFF, pin & 0xFF
		
		self.log('[*] Sending PIN to the ECU...')
		try:
			ecu.bus.execute(
				StartRoutineByLocalIdentifier(
					Routine.IMMO_INPUT_PASSWORD.value, pin_a, pin_b, pin_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
				)
			)
			#log_callback.emit(f'[*] IMMO_INPUT_PASSWORD response: {" ".join(hex(x) for x in response)}')
		except kwp2000.Kwp2000NegativeResponseException as e:
			self.log('[!] Invalid PIN. Immobilizer reset failed.')
			return

		# Confirm the immobilizer reset
		if QMessageBox.question(self, 'Confirm Reset', 'Do you want to proceed with the immobilizer reset?', QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
			try:
				response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.IMMO_RESET_CONFIRM.value, 0x01)).get_data()
				self.log('[*] Immobilizer reset successful. Turn off ignition for 10 seconds to apply changes.')
			except kwp2000.Kwp2000NegativeResponseException as e:
				self.log('[!] Immobilizer reset confirmation failed.')

		else:
			self.log('[*] Immobilizer reset cancelled by user.')


	def limp_home(self, progress_callback=None, log_callback=None) -> None:
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		# Check the ECU status
		try:
			data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.BEFORE_LIMP_HOME.value)).get_data()
			if len(data) > 1 and data[1] == 4:
				log_callback.emit('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
				return self.disconnect_ecu(ecu)
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Error: Immo is inactive or limp home pin is not set.')
			return self.disconnect_ecu(ecu)

		# Save ECU context and proceed
		self._ecu = ecu
		self._log_callback = log_callback

		# Request the password asynchronously
		self.request_pin_signal4.emit(log_callback, ecu)

	def request_pin_from_user4(self, log_callback, ecu: ECU):
		# Open a dialog for the user to enter the PIN
		pin, ok = QtWidgets.QInputDialog.getText(self, 'Enter Password', 'Enter 4-digit password:')
		if ok and pin.isdigit() and len(pin) == 4:
			self.continue_limp_home(pin, log_callback, ecu)  # Proceed to limp home process
		else:
			self.log('[!] Invalid PIN or operation cancelled.')
		return self.disconnect_ecu(ecu)

	def continue_limp_home(self, pin: str, log_callback, ecu: ECU):
		# Parse and split the PIN
		try:
			pin = int('0x' + pin, 0)
			pin_a = (pin >> 8)
			pin_b = (pin & 0xFF)
		except ValueError:
			self.log('[!] Invalid PIN format.')
			return

		try:
			self.log('[*] Activating limp home mode...')
			response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ACTIVATE_LIMP_HOME.value, pin_a, pin_b)).get_data()
			self.log(f'[*] Response: {" ".join(hex(x) for x in response)}')
			
			if len(response) > 1 and response[1] == 1:
				self.log('[*] Limp home mode activated successfully.')
				self.log('[*] You need to start the engine within 5-10 seconds, without unplugging the adapter. After starting, adapter may be unplugged')
			else:
				self.log('[!] Activation failed. Ensure the PIN is correct.')
		except kwp2000.Kwp2000NegativeResponseException as e:
			self.log('[!] Invalid PIN. Activation failed.')

	def smartra_neutralize(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		log_callback.emit('[*] Starting SMARTRA neutralization...')
		# Check the ECU status with BEFORE_SMARTRA_NEUTRALIZE
		try:
			data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.BEFORE_SMARTRA_NEUTRALIZE.value)).get_data()
			log_callback.emit(f'[*] BEFORE_SMARTRA_NEUTRALIZE response: {" ".join(hex(x) for x in list(data))}')
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Error: Unable to perform BEFORE_SMARTRA_NEUTRALIZE routine. Usually this means that connection with the transponder inside of your key couldn\'t be estabilished. If you intended to turn immo off, use the \'Immo reset\' feature - SMARTRA Neutralize wipes the key transponder so that it can be paired with another ECU.')
			return self.disconnect_ecu(ecu)

		# Check if the system is locked
		if len(data) > 1 and data[1] == 4:
			log_callback.emit('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return self.disconnect_ecu(ecu)

		# Save ECU context and proceed
		self._ecu = ecu
		self._log_callback = log_callback

		# Request the password asynchronously
		self.request_pin_signal5.emit(log_callback, ecu)

	def request_pin_from_user5(self, log_callback, ecu: ECU):
		# Open a dialog for the user to enter the PIN
		pin, ok = QtWidgets.QInputDialog.getText(self, 'Enter Password', 'Enter 6-digit SMARTRA password:')
		if ok and pin.isdigit() and len(pin) == 6:
			self.continue_smartra_neutralize(pin, log_callback, ecu)  # Proceed with SMARTRA neutralization
		else:
			self.log('[!] Invalid PIN or operation cancelled.')	
		return self.disconnect_ecu(ecu)	

	def continue_smartra_neutralize(self, pin: str, log_callback, ecu: ECU):
		# Parse and split the PIN
		try:
			pin = int('0x' + pin, 0)
			pin_a = (pin >> 16) & 0xFF
			pin_b = (pin >> 8) & 0xFF
			pin_c = (pin & 0xFF)
		except ValueError:
			self.log('[!] Invalid PIN format.')
			return

		try:
			# Send the PIN
			self.log('[*] Sending PIN to the ECU...')
			response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.IMMO_INPUT_PASSWORD.value, pin_a, pin_b, pin_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)).get_data()
			self.log(f'[*] IMMO_INPUT_PASSWORD response: {" ".join(hex(x) for x in list(response))}')

			# Perform SMARTRA neutralization
			self.log('[*] Neutralizing SMARTRA...')
			response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.NEUTRALIZE_SMARTRA.value, 0x01)).get_data()
			self.log(f'[*] NEUTRALIZE_SMARTRA response: {" ".join(hex(x) for x in list(response))}')

			if len(response) > 1 and response[1] == 1:
				self.log('[*] SMARTRA neutralization completed successfully.')
			else:
				self.log('[!] Neutralization failed. Ensure the PIN is correct.')
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Neutralization failed. Ensure the PIN is correct.')
		self.disconnect_ecu(ecu)

	def teach_keys(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		log_callback.emit('[*] Teaching immobilizer keys...')
		#log_callback.emit('[*] starting routine 0x14')
		try:
			data = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.BEFORE_IMMO_KEY_TEACHING.value)).get_data()
			#log_callback.emit(f'[*] BEFORE_IMMO_KEY_TEACHING response: {" ".join(hex(x) for x in data)}')
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Error starting IMMO_KEY_TEACHING routine.')

		# Request the PIN asynchronously
		self.request_pin_signal2.emit(log_callback, ecu)

	def request_pin_from_user2(self, log_callback, ecu: ECU):
		# Open the PIN dialog on the main thread
		pin, ok = QtWidgets.QInputDialog.getText(self, 'Enter PIN', 'Enter 6-digit immobilizer PIN:')
		if ok and pin.isdigit() and len(pin) == 6:
			self.continue_teach_keys(pin, log_callback, ecu)  # Continue with the reset process
		else:
			self.log('[!] Invalid PIN or operation cancelled.')
		return self.disconnect_ecu(ecu)	

	def continue_teach_keys(self, pin: str, log_callback, ecu: ECU):
		if not pin or not pin.isdigit() or len(pin) != 6:
			self.log('[!] Invalid PIN or operation cancelled.')
			return

		pin = int('0x' + pin, 0)  # Treat the input as a hexadecimal string
		pin_a, pin_b, pin_c = (pin >> 16) & 0xFF, (pin >> 8) & 0xFF, pin & 0xFF

		try:
			# Start the routine
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.IMMO_INPUT_PASSWORD.value, pin_a, pin_b, pin_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF))

			all_keys_successful = True  # Flag to track if all keys were successfully taught

			for i in range(4):
				if QMessageBox.question(self, f'Teach Key {i+1}', f'Teach immobilizer key {i+1}?', QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
					try:
						# Attempt to execute the teaching routine
						ecu.bus.execute(StartRoutineByLocalIdentifier(0x1B + i, 0x01))
					except kwp2000.Kwp2000NegativeResponseException as e:
						# Handle known exception for this specific teaching routine
						self.log(f'[!] Failed to teach Key {i+1}: {str(e.status)}')
						QMessageBox.warning(self, 'Key Teaching Error', f'Failed to teach Key {i+1}. Reason: {str(e.status)}')
						all_keys_successful = False
						break  # Stop on failure
					except Exception as e:
						# Handle unexpected exceptions
						self.log(f'[!] Unexpected error during Key {i+1} teaching: {str(e)}')
						QMessageBox.critical(self, 'Critical Error', f'Unexpected error occurred while teaching Key {i+1}. Reason: {str(e)}')
						all_keys_successful = False
						break  # Stop on failure
				else:
					if i == 0:  # Cancelation at the first key
						self.log('[!] Key teaching cancelled.')
						QMessageBox.information(self, 'Cancelled', 'Teach keying process has been cancelled.')
						all_keys_successful = False
					break

			# Show completion message only if all keys were successfully taught
			if all_keys_successful:
				self.log('[*] Key teaching completed. Turn off ignition for 10 seconds to apply changes.')
				QMessageBox.information(self, 'Success', 'Key teaching completed successfully. Turn off ignition for 10 seconds to apply changes.')

		except kwp2000.Kwp2000NegativeResponseException as e:
			# Handle known exception for the overall routine
			self.log(f'[!] Key teaching failed: {str(e)}')
			QMessageBox.critical(self, 'Routine Error', f'Key teaching initialization failed. Reason: {str(e.status)}')

		except Exception as e:
			# Handle unexpected exceptions for the overall routine
			self.log(f'[!] Unexpected error during key teaching: {str(e)}')
			QMessageBox.critical(self, 'Critical Error', f'Unexpected error during key teaching. Reason: {str(e)}')

	def limp_home_teach(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Starting limp home password teaching...')
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		# Check ECU status
		try:
			status = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.BEFORE_LIMP_HOME_TEACHING.value)).get_data()[1]
			log_callback.emit(f'[*] Current ECU status: {immo_status.get(status, status)}')
		except kwp2000.Kwp2000NegativeResponseException:
			log_callback.emit('[!] Failed to check ECU status.')
			return self.disconnect_ecu(ecu)	

		# Store context for continuation
		self._ecu = ecu
		self._log_callback = log_callback

		# Request the current password asynchronously if needed
		if status == 1:  # Learnt
			self.request_pin_signal3.emit(log_callback, ecu)
		else:
			self.request_new_password_signal.emit(log_callback, ecu, "")

	def request_pin_from_user3(self, log_callback, ecu: ECU):
		# Prompt user for the current password
		password, ok = QtWidgets.QInputDialog.getText(self, 'Enter Current Password', 'Enter 4-digit current password:')
		if ok and password.isdigit() and len(password) == 4:
			self.request_new_password_signal.emit(log_callback, ecu, password)
		else:
			self.log('[!] Invalid current password or operation cancelled.')
			return self.disconnect_ecu(ecu)	

	def request_new_password_from_user(self, log_callback, ecu: ECU, current_password: str = ''):
		# Prompt user for the new password
		password, ok = QtWidgets.QInputDialog.getText(self, 'Enter New Password', 'Enter 4-digit new password:')
		if ok and password.isdigit() and len(password) == 4:
			self.continue_limp_home_teach(current_password, password, log_callback, ecu)
		else:
			self.log('[!] Invalid new password or operation cancelled.')
			return self.disconnect_ecu(ecu)

	def continue_limp_home_teach(self, current_password: str, new_password: str, log_callback, ecu: ECU):
		try:
			# Validate and split passwords
			if current_password:
				current_password = int('0x' + current_password, 0)
				current_password_a = (current_password >> 8)
				current_password_b = (current_password & 0xFF)

				# Send the current password
				self.log('[*] Sending current password to the ECU...')
				ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ACTIVATE_LIMP_HOME.value, current_password_a, current_password_b))

			new_password = int('0x' + new_password, 0)
			new_password_a = (new_password >> 8)
			new_password_b = (new_password & 0xFF)

			# Send the new password
			self.log('[*] Sending new password to the ECU...')
			response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.LIMP_HOME_INPUT_NEW_PASSWORD.value, new_password_a, new_password_b)).get_data()
			self.log(f'[*] Response: {" ".join(hex(x) for x in response)}')

			# Confirm the new password
			if QMessageBox.question(self, 'Confirm Password', 'Are you sure you want to set this password?', QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
				response = ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.LIMP_HOME_CONFIRM_NEW_PASSWORD.value, 0x01)).get_data()
				self.log(f'[*] Response: {" ".join(hex(x) for x in list(response))}')
				self.log('[*] Limp home password teaching completed successfully.')
				return self.disconnect_ecu(ecu)
			else:
				self.log('[*] Limp home password teaching cancelled.')
				return self.disconnect_ecu(ecu)

		except kwp2000.Kwp2000NegativeResponseException as e:
			self.log('[!] Password teaching failed. Ensure the passwords are correct.')
			return self.disconnect_ecu(ecu)
		except ValueError:
			self.log('[!] Invalid password format.')
		finally:
			# Clean up temporary state
			self._ecu = None
			self._log_callback = None


	def read_vin(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		try:
			cmd = kwp2000.Kwp2000Command()
			cmd.set_service_identifier(0x09).set_data(b'\x02') # OBD2 service

			vin_data = ecu.bus.execute(cmd).get_data()
			vin = ''.join(chr(x) for x in list(vin_data))
			log_callback.emit(f'[*] Vehicle Identification Number (VIN): {vin}')
			return self.disconnect_ecu(ecu)
		except kwp2000.Kwp2000NegativeResponseException as e:
			log_callback.emit('[!] Reading VIN failed. Not supported on this ECU.')
		
		self.disconnect_ecu(ecu)

	def write_vin(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self._close_bus(log_callback)
			return
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(ecu.get_desired_baudrate().session_command(DiagnosticSession.DEFAULT))

		log_callback.emit('[*] Starting VIN writing process...')

		# Save ECU context and proceed
		self._ecu = ecu
		self._log_callback = log_callback

		# Request the VIN asynchronously
		self.request_vin_signal.emit(log_callback, ecu)

	def request_vin_from_user(self, log_callback, ecu):
		# Open a dialog for the user to enter the VIN
		vin, ok = QtWidgets.QInputDialog.getText(self, 'Enter VIN', 'Enter the VIN (up to 17 characters):')
		if ok and len(vin.strip()) > 0 and len(vin) <= 17:
			self.continue_write_vin(vin, log_callback, ecu)  # Proceed with VIN writing
		else:
			self.log('[!] Invalid VIN or operation cancelled.')
			return self.disconnect_ecu(ecu)

	def continue_write_vin(self, vin, log_callback, ecu):
		if not vin or len(vin) > 17 or len(vin.strip()) == 0:
			self.log('[!] Invalid VIN. It must be up to 17 characters long and cannot be empty.')
			return self.disconnect_ecu(ecu)
		# Pad VIN to 17 characters if necessary
		vin_padded = vin.ljust(17)
		try:
			self.log(f'[*] Writing VIN: {vin_padded}')
			ecu.bus.execute(WriteDataByLocalIdentifier(0x90, vin_padded.encode('ascii')))
			self.log('[*] VIN written successfully.')
		except kwp2000.Kwp2000NegativeResponseException as e:
			self.log('[!] Writing VIN failed. Ensure the ECU is writable.')
		except Exception as e:
			self.log(f'[!] Unexpected error while writing VIN: {e}')

		self.disconnect_ecu(ecu)

	def vin_to_pin(self, progress_callback=None, log_callback=None):
		log_callback.emit('[*] SMARTRA VIN to PIN calculator')
		log_callback.emit('[*] This calculator should apply for all Hyundai and KIA models equipped with SMARTRA2')
		log_callback.emit('[*] From 2007 or so, some models started using SMARTRA3 and a different algorithm - beware.')

		# Save ECU context and proceed
		self._log_callback = log_callback

		# Request the VIN asynchronously
		self.request_vin_to_pin_signal.emit(log_callback)

	def request_vin_to_pin_from_user(self, log_callback):
		# Open a dialog for the user to enter the VIN
		vin, ok = QtWidgets.QInputDialog.getText(self, 'Enter VIN', 'Enter your VIN (or last 6 digits):')
		if ok and len(vin.strip()) >= 6 and len(vin) <= 17:
			vin = vin.strip()

			# Extract the last 6 digits
			try:
				last_6_digits = int(vin[-6:])  # Extract the last 6 digits
				self.continue_vin_to_pin(last_6_digits, log_callback)  # Proceed with the last 6 digits
			except ValueError:
				self.log('[!] Error: The last 6 characters of the VIN must be numeric.')
				return
		else:
			self.log('[!] Invalid VIN or operation cancelled.')
			return

	def continue_vin_to_pin(self, last_6_digits, log_callback):
		while True:
			try:
				calculated_pin = calculate_smartra_pin(last_6_digits)
				self.log(f'[*] All good! Your immo pin is: {calculated_pin}')
				break
			except ValueError:
				print('[!] Something went wrong. Try again')

	def handler_bsl_write (self):
		if not self.bslFileInput.text().strip():
			self.log('[!] Select the whole flash image to write')
			return
		skip_backup = self.bslSkipBackupCheck.isChecked()
		force = self.bslForceWriteCheck.isChecked()
		skip_verify = self.bslSkipVerifyCheck.isChecked()
		details = ('No backup file will be saved. ' if skip_backup else
			'The flash is read and saved first (GKFlasher Files folder). ')
		details += ('Every sector is erased and rewritten, even if it already matches. ' if force else
			'Only sectors that differ from the image are erased and rewritten. ')
		details += ('Read-back verification is disabled.' if skip_verify else
			'Written sectors and the whole flash are verified.')
		confirmed = QMessageBox.warning(
			self,
			'BSL write',
			'This rewrites the whole external flash, the boot sectors included.\n\n'
			+ details + '\n\nIf it fails, run the write again: BSL doesn\'t depend on what\'s in the flash.\n\nContinue?',
			QMessageBox.Yes | QMessageBox.No,
			QMessageBox.No
		) == QMessageBox.Yes
		if confirmed:
			self.click_handler(partial(self.run_bsl, 'write', confirmed=True,
				skip_backup=skip_backup, force=force, skip_verify=skip_verify))

	def run_bsl (self, mode, progress_callback=None, log_callback=None, confirmed=False,
	             skip_backup=False, force=False, skip_verify=False):
		try:
			device, bus_type = self.get_selected_interface()
		except IndexError:
			log_callback.emit('[!] Interface not found!')
			return
		if bus_type != Bus.KLINE:
			log_callback.emit('[!] BSL runs on the K-line: select a K-line interface')
			return

		filename = self.bslFileInput.text().strip() or None
		if mode == 'write' and not (filename and confirmed):
			return
		stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
		if mode == 'write':
			output_filename = os.path.join(home, 'bsl_backup_{}.bin'.format(stamp))
		elif mode == 'read':
			output_filename = filename or os.path.join(home, 'bsl_read_{}.bin'.format(stamp))
		elif mode == 'readint':
			output_filename = filename or os.path.join(home, 'bsl_irom_{}.bin'.format(stamp))
		else:
			output_filename = None

		def confirm (message):
			log_callback.emit('[*] {} Confirmed before starting'.format(message))
			return True

		driver = self.bslDriverBox.currentText().lower()
		baudrate = None if self.bslBaudrateBox.currentIndex() == 0 else int(self.bslBaudrateBox.currentText())

		self._close_bus(log_callback)
		self.device = device
		log_callback.emit('[*] Interface {} ({})'.format(device.description(), BUS_NAMES[bus_type]))
		channel = None
		try:
			channel = device.kline(baudrate=baudrate or 57600, timing=KERNEL_TIMING)
			channel.open()
			bsl.bsl_handler(channel, mode, image_filename=filename if mode == 'write' else None,
				output_filename=output_filename, driver=driver, confirm=confirm,
				skip_verify=skip_verify, skip_backup=skip_backup, force=force, auto_baudrate=baudrate is None,
				log_callback=log_callback, progress_callback=progress_callback)
		except Exception as e:
			log_callback.emit('[!] BSL failed: {}'.format(str(e)))
		finally:
			if channel is not None:
				try:
					channel.close()
				except GkbusException:
					pass
			self._close_bus(log_callback)

	def bin_to_sie_conversion (self):
		filename = self.checksumFileInput.text()
		generate_sie(filename=filename)

	def sie_to_bin_conversion(self):
		filename = self.checksumFileInput.text()
		generate_bin(filename=filename)

	def get_rsw_logger(self, log_callback=None):

		if log_callback:
			return log_callback

		class Logger:
			def emit(_, msg):
				self.log(msg)

		return Logger()

	def select_rsw_file(self):

		filename, _ = QFileDialog.getOpenFileName(
			self,
			"Select BIN File",
			"",
			"BIN Files (*.bin)"
		)

		if filename:
			self.rswFileInput.setText(os.path.normpath(filename) if filename else '')

	# ==========================================================
	# GENERIC RSW HANDLER
	# ==========================================================

	def run_rsw(self, mode, log_callback=None, progress_callback=None):

		log_callback = self.get_rsw_logger(log_callback)

		try:
			_, bus_type = self.get_selected_interface()
			kline = bus_type == Bus.KLINE
			if kline and mode == 'virginize':
				raise ValueError('K-Line virginize is not supported by the native RSW sector table')
			key = int(self.rswKeyInput.text().strip(), 0) if kline and self.rswKeyInput.text().strip() else None
			if kline and (key is None or not 0 <= key <= 0xFFFFFFFF):
				raise ValueError('K-Line RSW requires a four-byte Siemens key')
			baudrate = self.get_desired_baudrate().baudrate
		except ValueError as exc:
			log_callback.emit('[!] {}'.format(exc))
			return
		ecu = self.initialize_ecu(log_callback,
			desired_baudrate=DesiredBaudrate(index=None, baudrate=10400) if kline else None,
			p4_min=0 if kline else 0.001)

		if ecu == False:
			self._close_bus(log_callback)
			return

		filename = self.rswFileInput.text().strip()

		if mode != 'virginize':
			if not filename:

				log_callback.emit('[!] No BIN file selected')

				self.disconnect_ecu(ecu)

				return

		try:
			rsw_handler(ecu, mode=mode, bin_file=filename, key=key, baudrate=baudrate, log_callback=log_callback, progress_callback=progress_callback)

		except Exception as e:

			log_callback.emit(f'[!] RSW flash failed: {str(e)}')
		finally:
			# Only pre-handoff failures can leave the original KWP transport open.
			if ecu.bus.transport.is_open():
				end_diagnostic_session(ecu.bus)
			ecu.bus.close()

	# ==========================================================
	# BUTTON HANDLERS
	# ==========================================================
	def rsw_flash_bsw(self, progress_callback=None, log_callback=None):
		self.run_rsw('bsw', log_callback=log_callback, progress_callback=progress_callback)

	def rsw_flash_asw(self, progress_callback=None, log_callback=None):
		self.run_rsw('asw', log_callback=log_callback, progress_callback=progress_callback)

	def rsw_flash_cal(self, progress_callback=None, log_callback=None):
		self.run_rsw('cal', log_callback=log_callback, progress_callback=progress_callback)

	def rsw_flash_full(self, progress_callback=None, log_callback=None):
		self.run_rsw('full', log_callback=log_callback, progress_callback=progress_callback)

	def rsw_virginize(self, progress_callback=None, log_callback=None):
		self.run_rsw('virginize', log_callback=log_callback, progress_callback=progress_callback)

	# ==========================================================
	# MTOS
	# ==========================================================

	def handler_select_mtos_file (self):
		if self.mtosModeBox.currentText() in ('read', 'read-calibration'):
			filename, _ = QFileDialog.getSaveFileName(self, "Save the flash read as", "", "BIN Files (*.bin);;All Files (*)")
		else:
			filename, _ = QFileDialog.getOpenFileName(
				self,
				"Select MTOS payload BIN or full flash image",
				"",
				"BIN Files (*.bin);;All Files (*)"
			)

		if filename:
			self.mtosFileInput.setText(os.path.normpath(filename) if filename else '')

	def confirm_mtos_warning(self, message):
		answer = Future()
		if QThread.currentThread() == self.thread():
			self._show_mtos_warning(message, answer)
		else:
			self.mtos_warning_requested.emit(message, answer)
		return answer.result()

	@pyqtSlot(str, object)
	def _show_mtos_warning(self, message, answer):
		try:
			result = QMessageBox.warning(self, 'MTOS Safety Check', message.removesuffix(' Continue?') +
				'\n\nOnly continue if this image is intended for this ECU.\n\nContinue?',
				QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
			answer.set_result(result)
		except Exception as exc:
			answer.set_exception(exc)

	def _mtos_options(self):
		mode = self.mtosModeBox.currentText()
		filename = self.mtosFileInput.text().strip()
		filename = os.path.normpath(filename) if filename else ''
		if mode in ('read', 'read-calibration'):
			if not filename:
				filename = os.path.join(home, 'mtos_{}_{}.bin'.format(
					mode, datetime.now().strftime('%Y%m%d_%H%M%S')))
			filename = os.path.abspath(os.path.expanduser(filename))
			if not os.path.isdir(filename):
				filename = available_filename(filename)
			self.mtosFileInput.setText(os.path.normpath(filename) if filename else '')
		return dict(mode=mode, filename=filename, key_text=self.mtosKeyInput.text().strip(),
			baudrate=self.get_desired_baudrate().baudrate,
			address_text=self.mtosAddressInput.text(), length_text=self.mtosLengthInput.text(),
			skip_verify=mode in WRITE_MODES and self.mtosSkipVerifyCheck.isChecked(),
			skip_backup=mode in WRITE_MODES and self.mtosSkipBackupCheck.isChecked(),
			force=mode in WRITE_MODES and self.mtosForceWriteCheck.isChecked(),
			override_safety_checks=mode in WRITE_MODES and self.mtosOverrideSafetyCheck.isChecked())

	def handler_run_mtos (self):
		confirmed = False
		options = self._mtos_options()
		mode = options['mode']
		if mode in WRITE_MODES:
			if not options['filename']:
				self.log('[!] {} mode requires a full 4/8 Mbit BIN image'.format(mode))
				return
			details = ('Only the calibration region is rewritten; boot and program are preserved. '
				if mode == 'flash-calibration' else
				'The whole flash is rewritten, including the bootloader. ')
			confirmed = QMessageBox.warning(
				self, 'MTOS Safety Check',
				details + ('No backup file will be saved. ' if options['skip_backup'] else 'The flash is backed up first. ') +
				('All selected sectors will be rewritten. ' if options['force'] else 'Only changed sectors will be rewritten. ') +
				('Read-back verification is disabled. ' if options['skip_verify'] else 'Writes will be verified. ') +
				'Keep the ECU powered until completion.\n\nContinue?',
				QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
			) == QMessageBox.Yes
			if not confirmed:
				return
		self.click_handler(lambda progress_callback, log_callback: self.run_mtos(
			progress_callback=progress_callback, log_callback=log_callback, confirmed=confirmed, options=options))

	def run_mtos (self, progress_callback=None, log_callback=None, confirmed=False, options=None):

		log_callback = self.get_rsw_logger(log_callback)
		if options is None:
			options = self._mtos_options()

		def parse_hex (text, default):
			text = text.strip()
			if not text:
				return default
			try:
				return int(text, 0)
			except ValueError:
				log_callback.emit('[!] Invalid number {!r}, falling back to 0x{:X}'.format(text, default))
				return default

		mode = options['mode']

		key_text = options['key_text']
		if not key_text:
			log_callback.emit('[!] MTOS key is required (e.g. 0x2d381c78)')
			return

		try:
			mtos_key = int(key_text, 0)
		except ValueError:
			log_callback.emit('[!] Invalid MTOS key: {}'.format(key_text))
			return

		filename = options['filename']
		if mode in ('payload', 'loader', *WRITE_MODES) and not filename:
			log_callback.emit('[!] {} mode requires a {}'.format(
				mode, 'full 4/8 Mbit BIN image' if mode in WRITE_MODES else 'payload file'))
			return
		if mode in WRITE_MODES and not confirmed:
			return
		if mode in ('read', 'read-calibration'):
			if os.path.isdir(filename) or not os.path.isdir(os.path.dirname(filename)):
				log_callback.emit('[!] Select an output BIN file in an existing directory.')
				return

		def confirm (message):
			log_callback.emit('[*] {} Confirmed before starting'.format(message))
			return True

		def warn_check (message):
			result = self.confirm_mtos_warning(message)
			if result:
				log_callback.emit('[*] User confirmed dangerous operation')
			else:
				log_callback.emit('[!] User aborted dangerous operation')
			return result


		address = parse_hex(options['address_text'], 0x000000)
		length = parse_hex(options['length_text'], 0x40)

		ecu = self.initialize_ecu(log_callback, desired_baudrate=DesiredBaudrate(index=None, baudrate=10400), p4_min=0)

		if ecu == False:
			self._close_bus(log_callback)
			return

		attach_hint = 'python gkflasher.py -p {} -i <this adapter> --mtos --mtos-mode {} --mtos-attach{}'.format(
			'canbus' if isinstance(ecu.bus.transport, Kwp2000OverCanTransport) else 'kline', mode,
			' --mtos-payload "{}"'.format(filename) if mode in WRITE_MODES else '')
		if isinstance(ecu.bus.transport, Kwp2000OverKLineTransport):
			attach_hint += ' --desired-baudrate 0x06 --session-baudrate {}'.format(options['baudrate'])


		try:
			mtos_handler(
				ecu, mtos_key, mode=mode, payload_filename=filename or None,
				address=address, length=length,
				output_filename=(filename or None) if mode in ('read', 'read-calibration') else None,
				confirm_callback=confirm, warn_check=warn_check, attach_hint=attach_hint,
				skip_verify=options['skip_verify'], skip_backup=options['skip_backup'], force=options['force'],
				override_safety_checks=options['override_safety_checks'],
				log_callback=log_callback, progress_callback=progress_callback, baudrate=options['baudrate']
			)
		except Exception as e:
			log_callback.emit('[!] MTOS failed: {}'.format(str(e) or type(e).__name__))
		finally:
			# the 0xFA routine tears the diagnostic session down, so just drop the bus
			self._close_bus(log_callback)

def packet2hex (packet: RawPacket) -> str:
	direction = 'Incoming' if packet.direction == PacketDirection.INCOMING else 'Outgoing'
	data = ' '.join([hex(x)[2:].zfill(2) for x in packet.data])
	parsed = 'RawPacket({}, ts={}, data={})'.format(direction, packet.timestamp, data)
	return parsed

if __name__ == "__main__":
	# Render at the display's resolution instead of scaling a low-resolution window.
	QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
	QtWidgets.QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
	app = QtWidgets.QApplication(sys.argv)
	configure_appearance(app)
	window = Ui()

	status = app.exec_()
	sys.exit(status)
