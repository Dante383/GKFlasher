import time, sys, logging
from pyftdi.ftdi import Ftdi
import pyftdi.serialext
import serial

logger = logging.getLogger(__name__)

class KLineSerial:
	socket = False

	HIGH = bytes([0x01])
	LOW = bytes([0x0])

	TIMEOUT_IDLE_BUS_BEFORE_INIT = 3000/1000
	TIMEOUT_POST_INIT = 50/1000
	TIMEOUT_WAIT_FOR_ECHO = 5/1000 # ms/1000
	TIMEOUT_ECHO_PER_BYTE = 3/1000
	TIMEOUT_AFTER_REQUEST = (30 + 20)/1000

	def __init__ (self, iface, baudrate):
		self.init(iface, baudrate)

	def init (self, iface, baudrate):
		try:
			Ftdi().get_device(iface)
		except pyftdi.usbtools.UsbToolsError:
			print('[!] Device {} not found!'.format(iface))
			Ftdi().show_devices()
			sys.exit(1)

		logger.info('fast init..')
		self.fast_init(iface, baudrate)
		logger.info('fast init done. closing serial opening serialext instance')

		self.socket = pyftdi.serialext.serial_for_url(iface, baudrate=baudrate, timeout=0.2)

		logger.info('wait for startCommunication response')
		time.sleep(0.04)

		logger.info('received {} bytes.'.format(len(self.read(7))))
		self.socket.timeout = 5
		
	def fast_init (self, iface, baudrate):
		self.socket = Ftdi()
		self.socket.open_from_url(iface)
		self.socket.purge_buffers()
		self.socket.set_baudrate(baudrate)
		self.socket.set_line_property(8, 1, 'N')
		self.socket.set_bitmode(0x01, Ftdi.BitMode(0x01))
		#time.sleep(self.TIMEOUT_IDLE_BUS_BEFORE_INIT)
		self.socket.write_data(self.HIGH)

		start = time.monotonic()
		while (time.monotonic() <= start + 0.35):
			time.sleep(0.01)

		self.socket.write_data(self.LOW)

		start = time.monotonic()
		while (time.monotonic() <= start + 0.0245):
			time.sleep(0.00025)

		self.socket.write_data(self.HIGH)

		start = time.monotonic()
		while (time.monotonic() <= start + 0.0245):
			time.sleep(0.00025)

		self.socket.set_bitmode(0x00, Ftdi.BitMode(0x00))
		#time.sleep(self.TIMEOUT_POST_INIT)

		self.socket.write_data(bytes([0x81, 0x11, 0xF1, 0x81, 0x04]))

	def write (self, payload):
		time.sleep(self.TIMEOUT_AFTER_REQUEST*2)
		self.socket.write(payload)

		while self.socket.out_waiting > 0:
			time.sleep(0.01)

		time.sleep(self.TIMEOUT_WAIT_FOR_ECHO + (self.TIMEOUT_ECHO_PER_BYTE * len(payload)))
		self.read(len(payload))
		time.sleep(self.TIMEOUT_AFTER_REQUEST)

	def read (self, length):
		message = self.socket.read(length)

		return message