import sys
from PyQt5 import QtWidgets, uic
from PyQt5.QtWidgets import QFileDialog
from PyQt5.QtCore import QThreadPool, QObject, pyqtSignal, QRunnable, pyqtSlot
from pyftdi import ftdi, usbtools
import gkbus, yaml, traceback
from gkbus.kwp.commands import *
from gkbus.kwp.enums import *
from gkbus.kwp import KWPNegativeResponseException
from flasher.ecu import enable_security_access, fetch_ecu_identification, identify_ecu, ECUIdentificationException, ECU
from flasher.memory import read_memory, write_memory
from flasher.checksum import *
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES

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
        '''
        Initialise the runner function with passed args, kwargs.
        '''

        # Retrieve args/kwargs here; and fire processing using them
        try:
            result = self.fn(*self.args, **self.kwargs)
        except:
            traceback.print_exc()
            exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((exctype, value, traceback.format_exc()))
        else:
            self.signals.result.emit(result)  # Return the result of the processing
        finally:
            self.signals.finished.emit()  # Done

class Ui(QtWidgets.QMainWindow):
	def __init__(self):
		super(Ui, self).__init__()
		self.load_ui()
		self.previous_baudrate = False

	def load_ui(self):
		uic.loadUi('flasher/gkflasher.ui', self)
		self.thread_manager = QThreadPool()
		self.show()
		
		self.detect_interfaces()
		self.load_ecus()
		self.load_baudrates()
		self.add_listeners()

	def add_listeners (self):
		self.readCalibrationZone.clicked.connect(lambda: self.click_handler(self.read_calibration_zone))
		self.readProgramZone.clicked.connect(lambda: self.click_handler(self.read_program_zone))
		self.readFull.clicked.connect(lambda: self.click_handler(self.full_read))

		self.displayECUID.clicked.connect(lambda: self.click_handler(self.display_ecu_identification))

		self.checksumCorrectBtn.clicked.connect(lambda: self.click_handler(self.correct_checksum))
		
		self.flashingCalibrationBtn.clicked.connect(lambda: self.click_handler(self.flash_calibration))
		self.flashingProgramBtn.clicked.connect(lambda: self.click_handler(self.flash_program))
		self.flashingFullBtn.clicked.connect(lambda: self.click_handler(self.flash_full))
		self.flashingClearAVBtn.clicked.connect(lambda: self.click_handler(self.clear_adaptive_values))

		self.readingFileBtn.clicked.connect(self.handler_select_file_reading)
		self.flashingFileBtn.clicked.connect(self.handler_select_file_flashing)
		self.checksumFileBtn.clicked.connect(self.handler_select_file_checksum)

	def click_handler (self, callback):
		worker = Worker(callback)
		worker.signals.log.connect(self.log)
		worker.signals.progress.connect(self.progress_callback)
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

	def log (self, text):
		self.logOutput.append(text)

	def detect_interfaces(self):
		ftdi_ins = ftdi.Ftdi()
		devices = ftdi_ins.list_devices()
		for device_str in usbtools.UsbTools.build_dev_strings('ftdi', ftdi_ins.VENDOR_IDS, ftdi_ins.PRODUCT_IDS, devices):
			self.interfacesBox.addItem(' '.join(device_str), device_str[0])

	def load_ecus (self):
		self.ecusBox.addItem('ECU (autodetect)', -1)
		for index, ecu in enumerate(ECU_IDENTIFICATION_TABLE):
			self.ecusBox.addItem('    [{}] {}'.format(index, ecu['ecu']['name']), index)

	def load_baudrates (self):
		self.baudratesBox.addItem('Desired baudrate (default)', -1)
		for index, baudrate in BAUDRATES.items():
			self.baudratesBox.addItem('{} baud'.format(baudrate), index)

	def get_interface_url (self):
		return self.interfacesBox.currentData()

	def progress_callback (self, value):
		if (len(value) > 1):
			self.progressBar.setMaximum(value[0])
			self.progressBar.setValue(0)
		else:
			self.progressBar.setValue(self.progressBar.value()+value[0])

	def initialize_ecu (self, interface_url: str, log_callback):
		log_callback.emit('[*] Initializing interface ' + self.get_interface_url())
		config = yaml.safe_load(open('gkflasher.yml'))
		del config['kline']['interface']
		bus = gkbus.Bus('kline', interface=interface_url, **config['kline'])

		try:
			bus.execute(StopDiagnosticSession())
			bus.execute(StopCommunication())
		except (KWPNegativeResponseException, gkbus.GKBusTimeoutException):
			pass

		try:
			bus.init(StartCommunication())
		except gkbus.GKBusTimeoutException:
			pass

		if self.baudratesBox.currentData() == -1:
			log_callback.emit('[*] Trying to start diagnostic session')
			bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING))
		else:
			desired_baudrate = self.baudratesBox.currentData()
			log_callback.emit('[*] Trying to start diagnostic session with baudrate {}'.format(BAUDRATES[desired_baudrate]))
			try:
				bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate))
			except gkbus.GKBusTimeoutException:
				bus.socket.socket.baudrate = BAUDRATES[desired_baudrate]
				bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate))
			bus.socket.socket.baudrate = BAUDRATES[desired_baudrate]

		bus.set_timeout(12)

		log_callback.emit('[*] Set timing parameters to maximum')
		try:
			available_timing = bus.execute(
				AccessTimingParameters(
					TimingParameterIdentifier.READ_LIMITS_OF_POSSIBLE_TIMING_PARAMETERS
				)
			).get_data()

			bus.execute(
				AccessTimingParameters(
					TimingParameterIdentifier.SET_TIMING_PARAMETERS_TO_GIVEN_VALUES, 
					*available_timing[1:]
				)
			)
		except KWPNegativeResponseException:
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
		log_callback.emit('[*] Found! {}'.format(ecu.get_name()))
		
		return ecu

	def disconnect_ecu (self, ecu):
		try:
			ecu.bus.execute(StopCommunication())
		except (KWPNegativeResponseException, gkbus.GKBusTimeoutException):
			pass

	def gui_read_eeprom (self, ecu, eeprom_size, address_start=0x000000, address_stop=None, output_filename=None, log_callback=None, progress_callback=None):
		if (address_stop == None):
			address_stop = eeprom_size

		log_callback.emit('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

		requested_size = address_stop-address_start
		eeprom = [0xFF]*eeprom_size

		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=Progress(progress_callback, requested_size-0x468))

		eeprom_start = ecu.calculate_bin_offset(address_start)
		eeprom_end = eeprom_start + len(fetched)
		eeprom[eeprom_start:eeprom_end] = fetched

		if (output_filename == None):
			try:
				calibration = ecu.get_calibration()
				description = ecu.get_calibration_description()
				hw_rev_c = ''.join([chr(x) for x in ecu.bus.execute(ReadEcuIdentification(0x8c)).get_data()[1:]])
				hw_rev_d = ''.join([chr(x) for x in ecu.bus.execute(ReadEcuIdentification(0x8d)).get_data()[1:]])
				output_filename = "{}_{}_{}_{}_{}.bin".format(description, calibration, hw_rev_c, hw_rev_d, date.today())
			except: # dirty
				output_filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))
		
		with open(output_filename, "wb") as file:
			file.write(bytes(eeprom))

		log_callback.emit('[*] saved to {}'.format(output_filename))
		self.disconnect_ecu(ecu)

	def gui_flash_eeprom (self, ecu, input_filename, flash_calibration=True, flash_program=True, log_callback=None, progress_callback=None):
		log_callback.emit('[*] Loading up {}'.format(input_filename))

		with open(input_filename, 'rb') as file:
			eeprom = file.read()

		log_callback.emit('[*] Loaded {} bytes'.format(len(eeprom)))

		if flash_program:
			log_callback.emit('[*] start routine 0x00 (erase program code section)')
			ecu.bus.execute(StartRoutineByLocalIdentifier(0x00))

			flash_start = 0x8A0010
			flash_size = 0x05FFF0
			payload_start = 0x020010
			payload_stop = payload_start+flash_size
			payload = eeprom[payload_start:payload_stop]

			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))

		if flash_calibration:
			log_callback.emit('[*] start routine 0x01 (erase calibration section)')
			ecu.bus.execute(StartRoutineByLocalIdentifier(0x01))

			flash_start = ecu.calculate_memory_write_offset(0x090000)
			flash_size = ecu.get_calibration_size_bytes()
			payload_start = ecu.calculate_bin_offset(0x090000)
			payload_stop = payload_start + flash_size
			payload = eeprom[payload_start:payload_stop]

			write_memory(ecu, payload, flash_start, flash_size, progress_callback=Progress(progress_callback, flash_size))

		ecu.bus.set_timeout(300)
		log_callback.emit('[*] start routine 0x02')
		ecu.bus.execute(StartRoutineByLocalIdentifier(0x02)).get_data()
		ecu.bus.set_timeout(12)

		log_callback.emit('[*] ecu reset')
		ecu.bus.execute(ECUReset(ResetMode.POWER_ON_RESET)).get_data()
		self.disconnect_ecu(ecu)

	def read_calibration_zone (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		eeprom_size = ecu.get_eeprom_size_bytes()
		if (self.readingFileInput.text() == ''):
			output_filename = None
		else:
			output_filename = self.readingFileInput.text()

		self.gui_read_eeprom(ecu, eeprom_size, address_start=0x090000, address_stop=0x090000+ecu.get_calibration_size_bytes(), output_filename=output_filename, log_callback=log_callback, progress_callback=progress_callback)

	def read_program_zone (self):
		pass

	def full_read (self):
		pass

	def display_ecu_identification (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		for parameter_key, parameter in fetch_ecu_identification(ecu.bus).items():
			value_hex = ' '.join([hex(x) for x in parameter['value']])
			value_ascii = ''.join([chr(x) for x in parameter['value']])

			log_callback.emit('')
			log_callback.emit('    [*] [{}] {}:'.format(hex(parameter_key), parameter['name']))
			log_callback.emit('            [HEX]: {}'.format(value_hex))
			log_callback.emit('            [ASCII]: {}'.format(value_ascii))
		self.disconnect_ecu(ecu)

	def correct_checksum (self, progress_callback, log_callback):
		filename = self.checksumFileInput.text()
		log_callback.emit('[*] Reading {}'.format(filename))

		with open(filename, 'rb') as file:
			payload = file.read()

		log_callback.emit('Trying to detect type.. ')
		name, flag_address, offset_address, init_address, cks_address, bin_offset = detect_offsets(payload)
		log_callback.emit(name)

		log_callback.emit('[*] Trying to find addresses of Zone1.. ')
		zone1_start_offset = cks_address+0x04
		zone1_start = concat_3_bytes(read_and_reverse(payload, zone1_start_offset, 3)) + bin_offset

		zone1_stop_offset = cks_address+0x08
		zone1_stop = concat_3_bytes(read_and_reverse(payload, zone1_stop_offset, 3)) + bin_offset + 1
		log_callback.emit('{} - {}'.format(hex(zone1_start), hex(zone1_stop)))

		log_callback.emit('[*] Trying to find initial value.. ')
		initial_value_bytes = read_and_reverse(payload, init_address, 2)
		initial_value = (initial_value_bytes[0]<< 8) | initial_value_bytes[1]
		log_callback.emit(hex(initial_value))

		log_callback.emit('[*] checksum of zone1: ')
		zone1_cks = checksum(payload, zone1_start, zone1_stop, initial_value)
		log_callback.emit(hex(zone1_cks))

		log_callback.emit('[*] Trying to find addresses of Zone2.. ')
		zone2_start_offset = cks_address+0xC
		zone2_start = concat_3_bytes(read_and_reverse(payload, zone2_start_offset, 3)) + bin_offset

		zone2_stop_offset = cks_address+0x10
		zone2_stop = concat_3_bytes(read_and_reverse(payload, zone2_stop_offset, 3)) + bin_offset + 1
		log_callback.emit('{} - {}'.format(hex(zone2_start), hex(zone2_stop)))

		log_callback.emit('[*] checksum of zone2: ')
		zone2_cks = checksum(payload, zone2_start, zone2_stop, zone1_cks)
		log_callback.emit(hex(zone2_cks))

		checksum_b1 = (zone2_cks >> 8) & 0xFF
		checksum_b2 = (zone2_cks & 0xFF)
		checksum_reversed = (checksum_b2 << 8) | checksum_b1

		current_checksum = int.from_bytes(payload[cks_address:cks_address+2], "big")

		log_callback.emit('[*] OK! Current checksum: {}, new checksum: {}'.format(hex(current_checksum), hex(checksum_reversed)))

		log_callback.emit('[*] Saving to {}'.format(filename))
		with open(filename, 'rb+') as file:
			file.seek(cks_address)
			file.write(checksum_reversed.to_bytes(2, "big"))
		log_callback.emit('[*] Done!')

	def flash_calibration (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=False, log_callback=log_callback, progress_callback=progress_callback)

	def flash_program (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=False, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)

	def flash_full (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		filename = self.flashingFileInput.text()
		self.gui_flash_eeprom(ecu, input_filename=filename, flash_calibration=True, flash_program=True, log_callback=log_callback, progress_callback=progress_callback)

	def clear_adaptive_values (self, progress_callback, log_callback):
		try:
			ecu = self.initialize_ecu(self.get_interface_url(), log_callback)
		except gkbus.GKBusTimeoutException:
			log_callback.emit('[*] Timeout! Try again. Maybe the ECU isn\'t connected properly?')
			return
		if (ecu == False):
			return

		log_callback.emit('[*] Clearing adaptive values.. ')
		ecu.clear_adaptive_values()
		log_callback.emit('Done!')
		self.disconnect_ecu(ecu)

if __name__ == "__main__":
	app = QtWidgets.QApplication(sys.argv)
	window = Ui()
	sys.exit(app.exec_())
