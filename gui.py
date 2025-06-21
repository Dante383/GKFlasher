import os, sys
from datetime import datetime
from PyQt5 import QtWidgets, uic
from PyQt5.QtWidgets import QFileDialog, QMessageBox
from PyQt5.QtCore import QThreadPool, QObject, pyqtSignal, QRunnable, pyqtSlot
import gkbus, yaml, traceback, bsl
from gkbus.hardware import KLineHardware, CanHardware, OpeningPortException, TimeoutException
from gkbus.transport import Kwp2000OverKLineTransport, Kwp2000OverCanTransport, RawPacket, PacketDirection
from gkbus.protocol import kwp2000
from gkbus.protocol.kwp2000.commands import *
from gkbus.protocol.kwp2000.enums import *
from flasher.ecu import enable_security_access, fetch_ecu_identification, identify_ecu, ECUIdentificationException, ECU, DesiredBaudrate
from flasher.memory import read_memory, write_memory, dynamic_find_end
from flasher.checksum import *
from flasher.immo import immo_status
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES, Routine, AccessLevel
from gkflasher import strip
from flasher.lineswap import generate_sie, generate_bin
from flasher.smartra import calculate_smartra_pin

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

class Progress(object):
	def __init__ (self, progress_callback, max_value: int):
		self.progress_callback = progress_callback
		self.progress_callback.emit((max_value, 0))
		self.progress_callback.emit((0,))

	def __call__ (self, value: int):
		self.progress_callback.emit((value,))

	def title (self, title: str):
		pass

class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object)
    progress = pyqtSignal(tuple)
    log = pyqtSignal(str)
    log2 = pyqtSignal(str)

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
        # Add log_callback2 only if the function supports it (bsl only)
        if 'log_callback2' in fn.__code__.co_varnames:
            self.kwargs['log_callback2'] = self.signals.log2

    @pyqtSlot()
    def run(self):
        # Retrieve args/kwargs here; and fire processing using them
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            #traceback.print_exc()
            #exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((e, traceback.format_exc()))
        else:
            self.signals.result.emit(result)  # Return the result of the processing
        finally:
            self.signals.finished.emit()  # Done

class Ui(QtWidgets.QMainWindow):
	request_pin_signal1 = pyqtSignal(object, object)  # Signal to request PIN from the main thread. # Pass `log_callback` and `ecu`  
	request_pin_signal2 = pyqtSignal(object, object)  # Signal to request PIN from the main thread. # Pass `log_callback` and `ecu`  
	request_pin_signal3 = pyqtSignal(object, object)  # For requesting the current password
	request_pin_signal4 = pyqtSignal(object, object)  # For requesting the PIN
	request_pin_signal5 = pyqtSignal(object, object)  # For requesting the PIN
	request_new_password_signal = pyqtSignal(object, object, str)  # For requesting the new password. Three arguments: log_callback, ecu, current_password
	request_vin_signal = pyqtSignal(object, object)  # For requesting the Write VIN Input
	request_vin_to_pin_signal = pyqtSignal(object)  # For requesting the VIN to PIN Input
	log_signal = pyqtSignal(str)  # Define a signal for logging

	def __init__(self):
		super(Ui, self).__init__()
		self.load_ui()
		self.previous_baudrate = False
		self.log_signal.connect(self.log)  # Connect the signal to the `log` method

		# Configure bsl logging for GUI
		bsl.set_gui_log_handler(self.log) # Pass the `self.log` method from the GUI class

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
		if not os.path.exists(home):
			os.makedirs(home)
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
		self.bus.close()

	def load_ui(self):
		uic.loadUi(os.path.dirname(os.path.abspath(__file__)) + '/flasher/gkflasher.ui', self)
		self.thread_manager = QThreadPool()
		self.show()
		
		try:
			self.detect_interfaces()
		except ValueError:
			print('[!] No serial interfaces found! If you plug in an interface now, you\'ll have to restart GKFlasher')

		self.load_ecus()
		self.load_baudrates()
		self.add_listeners()

	def add_listeners (self):
		self.readCalibrationZone.clicked.connect(lambda: self.click_handler(self.read_calibration_zone))
		self.readProgramZone.clicked.connect(lambda: self.click_handler(self.read_program_zone))
		self.readFull.clicked.connect(lambda: self.click_handler(self.full_read))

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

		self.immoInfoBtn.clicked.connect(lambda: self.click_handler(self.display_immo_information))
		self.limpHomeModeBtn.clicked.connect(lambda: self.click_handler(self.limp_home))
		self.limpHomePasswordChangeBtn.clicked.connect(lambda: self.click_handler(self.limp_home_teach))
		self.immoResetBtn.clicked.connect(lambda: self.click_handler(self.immo_reset))
		self.smartraNeturalizeBtn.clicked.connect(lambda: self.click_handler(self.smartra_neutralize))
		self.teachKeysBtn.clicked.connect(lambda: self.click_handler(self.teach_keys))
		self.readVinBtn.clicked.connect(lambda: self.click_handler(self.read_vin))
		self.writeVinBtn.clicked.connect(lambda: self.click_handler(self.write_vin))
		self.vinToPinBtn.clicked.connect(lambda: self.click_handler(self.vin_to_pin))

		self.bslHwInfoBtn.clicked.connect(lambda: self.click_handler(self.bslHwInfo))
		self.bslReadIntRomBtn.clicked.connect(lambda: self.click_handler(self.bslReadIntRom))
		self.bslReadExtFlashBtn.clicked.connect(lambda: self.click_handler(self.bslReadExtFlash))
		self.bslWriteExtFlashBtn.clicked.connect(lambda: self.click_handler(self.bslWriteExtFlash))

	def click_handler (self, callback):
		worker = Worker(callback)
		worker.signals.log.connect(self.log)
		worker.signals.log2.connect(self.log2)
		worker.signals.progress.connect(self.progress_callback)
		worker.signals.error.connect(self.handle_exception)
		self.thread_manager.start(worker)

	def handler_select_file_reading (self):
		filename = QFileDialog().getSaveFileName()[0]
		self.readingFileInput.setText(filename)

	def handler_select_file_flashing (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.flashingFileInput.setText(filename)

	def handler_select_file_checksum (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.checksumFileInput.setText(filename)

	def handler_select_bsl_file (self):
		filename = QFileDialog().getOpenFileName()[0]
		self.bslFileInput.setText(filename)

	def log (self, text):
		self.logOutput.append(text)
		self.logOutput.setReadOnly(True)

	def log2 (self, text):
		self.bslOutput.setPlainText(text)
		self.bslOutput.setReadOnly(True)

	def detect_interfaces(self):
		devices = KLineHardware.available_ports()
		if (len(devices) == 0):
			raise ValueError
		for device in devices:
			self.interfacesBox.addItem(device.description(), device.port)

	def load_ecus (self):
		self.ecusBox.addItem('ECU (autodetect)', -1)
		for index, ecu in enumerate(ECU_IDENTIFICATION_TABLE):
			self.ecusBox.addItem('    [{}] {}'.format(index, ecu['ecu']['name']), index)

	def load_baudrates (self):
		self.baudratesBox.addItem('Desired baudrate (default)', -1)
		for index, baudrate in BAUDRATES.items():
			self.baudratesBox.addItem('{} baud'.format(baudrate), index)

	def get_interface_url (self):
		url = self.interfacesBox.currentData()
		if not url:
			raise IndexError
		return url

	def get_desired_baudrate (self) -> DesiredBaudrate:
		baudrate_index = self.baudratesBox.currentData()
		if baudrate_index == -1:
			# @todo: not ideal, but its a bridge towards moving this completely to the ECU class. it was a mess
			return DesiredBaudrate(index=None, baudrate=10400)
		return DesiredBaudrate(index=baudrate_index, baudrate=BAUDRATES[baudrate_index])

	def progress_callback (self, value):
		if (len(value) > 1):
			self.progressBar.setMaximum(value[0])
			self.progressBar.setValue(0)
		else:
			self.progressBar.setValue(self.progressBar.value()+value[0])

	def _initialize_ecu (self, log_callback) -> ECU:
		if hasattr(self, 'bus'):
			self.bus.close()

		try:
			log_callback.emit('[*] Initializing interface ' + self.get_interface_url())
		except IndexError:
			log_callback.emit('[*] Interface not found!')
			return False

		config = yaml.safe_load(open(os.path.dirname(os.path.abspath(__file__)) + '/gkflasher.yml'))
		del config['kline']['interface']

		hardware = KLineHardware(self.get_interface_url())
		transport = Kwp2000OverKLineTransport(hardware, tx_id=config['kline']['tx_id'], rx_id=config['kline']['rx_id'])

		bus = kwp2000.Kwp2000Protocol(transport)
		bus.init(StartCommunication(), keepalive_command=TesterPresent(ResponseType.REQUIRED), keepalive_delay=2)
		self.bus = bus

		if not self.get_desired_baudrate().index:
			desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)
			log_callback.emit('[*] Trying to start diagnostic session')
			bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING))
		else:
			desired_baudrate = self.get_desired_baudrate()
			log_callback.emit('[*] Trying to start diagnostic session with baudrate {}'.format(desired_baudrate.baudrate))
			try:
				bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate.index))
				bus.transport.hardware.set_baudrate(desired_baudrate.baudrate)
			except TimeoutException:
				# it's possible that the ecu is already running at the desired baudrate - let's check
				bus.transport.hardware.socket.reset_input_buffer() # @todo: expose this in public gkbus api
				bus.transport.hardware.socket.reset_output_buffer()
				bus.transport.hardware.set_baudrate(desired_baudrate.baudrate)
				bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate.index))

		bus.transport.hardware.set_timeout(12)

		log_callback.emit('[*] Set timing parameters to maximum')
		try:
			available_timing = bus.execute(
				kwp2000.commands.AccessTimingParameters().read_limits_of_possible_timing_parameters()
			).get_data()

			bus.execute(
				kwp2000.commands.AccessTimingParameters().set_timing_parameters_to_given_values(
					*available_timing[1:]
				)
			)
		except kwp2000.Kwp2000NegativeResponseException:
			log_callback.emit('[!] Not supported on this ECU!')

		log_callback.emit('[*] Security Access')
		enable_security_access(bus)

		log_callback.emit('[*] Trying to identify ECU.. ')
		if self.ecusBox.currentData() == -1:
			try:
				ecu = identify_ecu(bus)
			except ECUIdentificationException:
				log_callback.emit('[*] Failed to identify ECU! Please select it from the dropdown and try again.')
				return False
		else:
			ecu = ECU(**ECU_IDENTIFICATION_TABLE[self.ecusBox.currentData()]['ecu'])
			ecu.set_bus(bus)
		
		ecu.set_desired_baudrate(desired_baudrate)
		ecu.diagnostic_session_type = DiagnosticSession.FLASH_REPROGRAMMING
		ecu.access_level = AccessLevel.HYUNDAI_0x1

		log_callback.emit('[*] Found! {}'.format(ecu.get_name()))
		
		return ecu

	def initialize_ecu (self, log_callback) -> ECU:
		try:
			return self._initialize_ecu(log_callback)
		except TimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
		except Exception as e:
			log_callback.emit('[*] Exception occurred: {}'.format(str(e)))

		return False

	def disconnect_ecu (self, ecu: ECU) -> None:
		try:
			ecu.bus.execute(StopCommunication())
		except (kwp2000.Kwp2000NegativeResponseException, TimeoutException, AttributeError):
			pass
		ecu.bus.close()

	def gui_read_eeprom (self, ecu: ECU, address_start: int = 0x000000, address_stop: int = None, escalate_privileges: bool = False, output_filename: str = None, log_callback=None, progress_callback=None):
		eeprom_size = ecu.get_eeprom_size_bytes()

		if escalate_privileges:
			log_callback.emit('[*] Attempting privilege escalation with the IOCLID patch')
			if (ecu.security_access(AccessLevel.SIEMENS_0xFD)):
				log_callback.emit('[*] Success!')
				address_start = 0
				address_stop = eeprom_size
			else:
				log_callback.emit('[!] Patch likely not present, failed to escalate privileges.')
				log_callback.emit('    Read will only include the calibration and program zones.')
				log_callback.emit('    If you\'re running ca663056, feel encouraged to apply the IOCLID patch.')
				log_callback.emit('    It will allow you to read the whole memory over OBD2, just like BSL.')
				log_callback.emit('    You can find it at https://github.com/OpenGK-org/opengk-simk')

		if (address_stop == None):
			address_stop = eeprom_size

		log_callback.emit('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

		requested_size = address_stop-address_start
		eeprom = bytearray([0xFF]*eeprom_size)

		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=Progress(progress_callback, requested_size))

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

		# Display user friendly path based on OS
		if os.name == 'nt':
			log_callback.emit('[*] saved to {}'.format(home + "\\" + output_filename))
		else: # nix
			log_callback.emit('[*] saved to {}'.format(home + "/" + output_filename))

		log_callback.emit('[*] Done!')

	def gui_flash_eeprom (self, ecu: ECU, input_filename: str, flash_calibration: bool = True, flash_program: bool = True, log_callback=None, progress_callback=None):
		log_callback.emit('[*] Loading up {}'.format(input_filename))

		try:
			with open(input_filename, 'rb') as file:
				eeprom = file.read()
			log_callback.emit('[*] Loaded {} bytes'.format(len(eeprom)))
		except FileNotFoundError:
			log_callback.emit('[!] Error: File not found.')
			return self.disconnect_ecu(ecu)

		if flash_program:
			log_callback.emit('[*] start routine 0x00 (erase program code section)')
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ERASE_PROGRAM.value))

			# we need to start 16 bytes later as the program section starts with a flag that we can't write
			payload_start = ecu.calculate_bin_offset(ecu.get_program_section_address()) + 16
			payload_stop = payload_start + dynamic_find_end(eeprom[payload_start:(payload_start+ecu.get_program_section_size()-16)])
			payload = eeprom[payload_start:payload_stop]

			flash_start = ecu.get_program_section_address() + 16
			flash_size = payload_stop-payload_start

			log_callback.emit('[*] Uploading data to the ECU')
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))

		if flash_calibration:
			log_callback.emit('[*] start routine 0x01 (erase calibration section)')
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.ERASE_CALIBRATION.value))

			payload_start = ecu.calculate_bin_offset(ecu.get_calibration_section_address())
			# we need to shave 16 bytes off the top as this is where a flag that we can't write is located
			payload_stop = payload_start + dynamic_find_end(eeprom[payload_start:(payload_start+ecu.get_calibration_size_bytes()-16)])
			payload = eeprom[payload_start:payload_stop]

			flash_start = ecu.calculate_memory_write_offset(ecu.get_calibration_section_address())
			flash_size = payload_stop-payload_start

			log_callback.emit('[*] Uploading data to the ECU')
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))

		ecu.bus.transport.hardware.set_timeout(300)
		log_callback.emit('[*] start routine 0x02 (verify blocks and mark as ready to execute)')
		try:
			ecu.bus.execute(StartRoutineByLocalIdentifier(Routine.VERIFY_BLOCKS.value))
		except kwp2000.Kwp2000NegativeResponseException as e:
			print(str(e))
			log_callback.emit(str(e))
			log_callback.emit('[*] Verifying blocks failed! Did you forget to correct the checksum?')
			
			log_callback.emit('[!] Your ECU is now soft-bricked. There\'s no need to panic, all you need to do is flash a valid file.')
			return

		log_callback.emit('[*] ecu reset')
		log_callback.emit('[*] Done!')
		
		ecu.bus.transport.hardware.set_timeout(0.5)
		try:
			ecu.bus.execute(ECUReset(ResetMode.POWER_ON_RESET))
		except TimeoutException: 
			pass # response is not guaranteed

	def read_calibration_zone (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		eeprom_size = ecu.get_eeprom_size_bytes()
		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		self.gui_read_eeprom(
			ecu, 
			eeprom_size, 
			address_start=ecu.get_calibration_section_address(), 
			address_stop=ecu.get_calibration_section_address()+ecu.get_calibration_size_bytes(), 
			output_filename=output_filename, 
			log_callback=log_callback, 
			progress_callback=progress_callback
		)
		self.disconnect_ecu(ecu)

	def read_program_zone (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		eeprom_size = ecu.get_eeprom_size_bytes()
		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		address_start = ecu.get_program_section_address()
		address_stop = address_start + ecu.get_program_section_size()

		self.gui_read_eeprom(ecu, eeprom_size, address_start=address_start, address_stop=address_stop, output_filename=output_filename, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def full_read (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		eeprom_size = ecu.get_eeprom_size_bytes()
		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		address_start = ecu.get_calibration_section_address()
		address_stop = ecu.get_program_section_address()+ecu.get_program_section_size()

		self.gui_read_eeprom(ecu, address_start=address_start, address_stop=address_stop, escalate_privileges=True, output_filename=output_filename, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def display_ecu_identification (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
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

		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))
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
			self.bus.close()
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=False, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def flash_program (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=False, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def flash_full (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)
		self.disconnect_ecu(ecu)

	def clear_adaptive_values (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		log_callback.emit('[*] Clearing adaptive values.. ')
		ecu.clear_adaptive_values()
		log_callback.emit('Done! Turn off ignition for 10 seconds to apply changes.')
		self.disconnect_ecu(ecu)

	def display_immo_information (self, progress_callback, log_callback):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return
		
		log_callback.emit('[*] Querying additional parameters,  this might take a few seconds..')

		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))
		
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
			self.bus.close()
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			self.bus.close()
			return
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			else:
				self.log('[!] Activation failed. Ensure the PIN is correct.')
		except kwp2000.Kwp2000NegativeResponseException as e:
			self.log('[!] Invalid PIN. Activation failed.')

	def smartra_neutralize(self, progress_callback=None, log_callback=None):
		ecu = self.initialize_ecu(log_callback)
		
		if (ecu == False):
			self.bus.close()
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			self.bus.close()
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			self.bus.close()
			return

		log_callback.emit('[*] Starting limp home password teaching...')
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			self.bus.close()
			return

		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
			self.bus.close()
			return
		
		log_callback.emit('[*] Starting default diagnostic session...')
		ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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

	def get_or_generate_file_path(self) -> str:
		# Retrieve the file path from the QLineEdit
		user_file_path = self.bslFileInput.text().strip()

		if not user_file_path:
			# Placeholder parameters for file naming
			calibration = "calibration"  # Replace with actual logic
			description = "description"  # Replace with actual logic
			hw_rev_c = "revC"            # Replace with actual logic
			hw_rev_d = "revD"            # Replace with actual logic

			# Generate the default file name using parameters and current date/time
			output_filename = "{}_{}_{}_{}_{}.bin".format(
				description, calibration, hw_rev_c, hw_rev_d,
				datetime.now().strftime('%Y-%m-%d_%H%M')
			)

			# Construct the full file path based on the platform
			if os.name == 'nt':
				user_file_path = os.path.join(home, output_filename)
			else:  # For non-Windows platforms
				user_file_path = os.path.join(home, output_filename)

			# Update the QLineEdit for user visibility, looks janky in the gui, missing chars prob due to signalling.
			#self.bslFileInput.setText(user_file_path)

		return user_file_path

	def bslHwInfo(self, progress_callback=None, log_callback=None, log_callback2=None):
		try:		
			# BSL arguments
			args = [
				"57600",      # Baud rate
				"-hwinfo"
			]
			# Pass the Progress object
			bsl.execute_bsl(args, progress_callback=Progress(progress_callback, 100),log_callback2=log_callback2)

		except Exception as e:
			log_callback.emit(f"An error occurred: {str(e)}")

	def bslReadIntRom(self, progress_callback=None, log_callback=None, log_callback2=None):
		try:
			# Retrieve the file path from the QLineEdit
			user_file_path = self.get_or_generate_file_path()
			
			# BSL arguments
			args = [
				"57600",      # Baud rate
				"-readint",   # Command
				"0x8000",     # Size in hex
				#user_file_path  # Use the dynamically retrieved/generated file path
			]

			#log_callback.emit(f"Calling BSL with arguments: {args}")
			
			# Pass the Progress object
			bsl.execute_bsl(args, progress_callback=Progress(progress_callback, 100),log_callback2=log_callback2)

		except Exception as e:
			log_callback.emit(f"An error occurred: {str(e)}")

	def bslReadExtFlash(self, progress_callback=None, log_callback=None, log_callback2=None):
		try:
			# Retrieve the file path from the QLineEdit
			user_file_path = self.get_or_generate_file_path()
			
			# BSL arguments
			args = [
				"57600",     # Baud rate
				"-readextflash",  # Command
				"0x80000",    # Size in hex
				#user_file_path  # Use the dynamically retrieved/generated file path
			]
			
			# Pass the Progress object
			bsl.execute_bsl(args, progress_callback=Progress(progress_callback, 100),log_callback2=log_callback2)

		except Exception as e:
			log_callback.emit(f"An error occurred: {str(e)}")

	def bslWriteExtFlash(self, progress_callback=None, log_callback=None, log_callback2=None):
		try:
			# Retrieve the file path from the QLineEdit
			user_file_path = self.get_or_generate_file_path()
			
			# BSL arguments
			args = [
				"57600",     # Baud rate
				"-writeextflash",  # Command
				user_file_path  # Use the dynamically retrieved/generated file path
			]
			
			# Pass the Progress object
			bsl.execute_bsl(args, progress_callback=Progress(progress_callback, 100),log_callback2=log_callback2)

		except Exception as e:
			log_callback.emit(f"An error occurred: {str(e)}")


	def bin_to_sie_conversion (self):
		filename = self.checksumFileInput.text()
		generate_sie(filename=filename)

	def sie_to_bin_conversion(self):
		filename = self.checksumFileInput.text()
		generate_bin(filename=filename)

def packet2hex (packet: RawPacket) -> str:
	direction = 'Incoming' if packet.direction == PacketDirection.INCOMING else 'Outgoing'
	data = ' '.join([hex(x)[2:].zfill(2) for x in packet.data])
	parsed = 'RawPacket({}, ts={}, data={})'.format(direction, packet.timestamp, data)
	return parsed

if __name__ == "__main__":
	app = QtWidgets.QApplication(sys.argv)
	stylesheet_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flasher', 'gkflasher.qss')
	if os.path.exists(stylesheet_path):
		with open(stylesheet_path, "r") as stylesheet:
			app.setStyleSheet(stylesheet.read())
	else:
		print(f"Stylesheet not found at {stylesheet_path}")
	window = Ui()

	status = app.exec_()
	sys.exit(status)
