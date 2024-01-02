import sys
from PyQt5 import QtWidgets, uic
from PyQt5.QtCore import QThreadPool
from pyftdi import ftdi, usbtools
import gkbus, yaml
from gkbus.kwp.commands import *
from gkbus.kwp.enums import *
from gkbus.kwp import KWPNegativeResponseException
from flasher.ecu import enable_security_access, fetch_ecu_identification, identify_ecu
from flasher.memory import read_memory


class Progress(object):
	def __init__ (self, progress_bar, max_value: int):
		self.progress_bar = progress_bar
		self.progress_bar.setMaximum(max_value)

	def __call__ (self, value: int):
		self.progress_bar.setValue(self.progress_bar.value()+value)

	def title (self, title: str):
		pass

class Ui(QtWidgets.QMainWindow):
	def __init__(self):
		super(Ui, self).__init__()
		self.load_ui()

	def load_ui(self):
		uic.loadUi('flasher/gkflasher.ui', self)
		self.thread_manager = QThreadPool()
		self.show()
		
		self.detect_interfaces()
		self.add_listeners()

	def add_listeners (self):
		self.readCalibrationZone.clicked.connect(self.handler_read_calibration_zone)
		self.displayECUID.clicked.connect(self.handler_display_ecu_identification)

	def log (self, text):
		self.logOutput.append(text)

	def detect_interfaces(self):
		ftdi_ins = ftdi.Ftdi()
		devices = ftdi_ins.list_devices()
		for device_str in usbtools.UsbTools.build_dev_strings('ftdi', ftdi_ins.VENDOR_IDS, ftdi_ins.PRODUCT_IDS, devices):
			self.interfacesBox.addItem(' '.join(device_str), device_str[0])

	def get_interface_url (self):
		return self.interfacesBox.currentData()

	def progress_callback (self, value):
		self.progressBar.setValue(value)

	def initialize_ecu (self, interface_url: str):
		self.log('[*] Initializing interface ' + self.get_interface_url())
		config = yaml.safe_load(open('gkflasher.yml'))
		del config['kline']['interface']
		bus = gkbus.Bus('kline', interface=interface_url, **config['kline'])

		try:
			bus.execute(StopDiagnosticSession())
			bus.execute(StopCommunication())
		except (KWPNegativeResponseException, gkbus.GKBusTimeoutException):
			pass

		bus.init(StartCommunication())

		self.log('[*] Trying to start diagnostic session')
		bus.execute(StartDiagnosticSession(DiagnosticSession.FLASH_REPROGRAMMING))
		bus.set_timeout(12)

		self.log('[*] Set timing parameters to maximum')
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
			self.log('[!] Not supported on this ECU!')

		self.log('[*] Security Access')
		enable_security_access(bus)

		self.log('[*] Trying to identify ECU automatically.. ')
	
		try:
			ecu = identify_ecu(bus)
		except ECUIdentificationException:
			ecu = ECU(**self.gui_choose_ecu()['ecu'])
			ecu.set_bus(bus)

		self.log('[*] Found! {}'.format(ecu.get_name()))
		return ecu

	def gui_read_eeprom (self, ecu, eeprom_size, address_start=0x000000, address_stop=None, output_filename=None):
		if (address_stop == None):
			address_stop = eeprom_size

		self.log('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

		requested_size = address_stop-address_start
		eeprom = [0xFF]*eeprom_size

		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=Progress(self.progressBar, requested_size+1))

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

		self.log('[*] saved to {}'.format(output_filename))

	def handler_read_calibration_zone (self):
		self.thread_manager.start(self.read_calibration_zone)

	def read_calibration_zone (self):
		ecu = self.initialize_ecu(self.get_interface_url())
		eeprom_size = ecu.get_eeprom_size_bytes()

		self.gui_read_eeprom(ecu, eeprom_size, address_start=0x090000, address_stop=0x090000+ecu.get_calibration_size_bytes())

	def handler_display_ecu_identification (self):
		self.thread_manager.start(self.display_ecu_identification)

	def display_ecu_identification (self):
		ecu = self.initialize_ecu(self.get_interface_url())


		for parameter_key, parameter in fetch_ecu_identification(ecu.bus).items():
			value_hex = ' '.join([hex(x) for x in parameter['value']])
			value_ascii = ''.join([chr(x) for x in parameter['value']])

			self.log('')
			self.log('    [*] [{}] {}:'.format(hex(parameter_key), parameter['name']))
			self.log('            [HEX]: {}'.format(value_hex))
			self.log('            [ASCII]: {}'.format(value_ascii))


if __name__ == "__main__":
	app = QtWidgets.QApplication(sys.argv)
	window = Ui()
	sys.exit(app.exec_())
