import os, time, logging
from interface.kline.KLineSerial import KLineSerial
from kwp.KWPResponse import KWPResponse

logger = logging.getLogger(__name__)

class KLineInterface:
	socket = False

	def __init__ (self, iface, baudrate, rx_id, tx_id):
		print('    [K] K-line init. Iface: {} baudrate: {}'.format(iface, baudrate))
		self.rx_id = rx_id
		self.tx_id = tx_id
		self.socket = KLineSerial(iface, baudrate=baudrate)
		try:
			os.remove('kline.log')
		except OSError:
			pass

	def calculate_checksum (self, payload):
		checksum = 0x0
		for byte in payload:
			checksum += byte
		return checksum & 0xFF

	def build_payload (self, data):
		data_length = len(data)

		if (data_length < 127):
			counter = 0x80 + data_length
		else:
			counter = 0x80
			data = [data_length] + data

		tx_id_b1 = (self.tx_id >> 8) & 0xFF
		tx_id_b2 = (self.tx_id & 0xFF)

		payload = [counter, tx_id_b1, tx_id_b2] + data
		payload += [self.calculate_checksum(payload)]
		return bytes(payload)

	def fetch_response (self):
		counter = self._read(1)

		if (len(counter) == 0):
			return False

		rx_id_b1 = int.from_bytes(self._read(1), "big")
		rx_id_b2 = int.from_bytes(self._read(1), "big")
		rx_id = (rx_id_b1 << 8) | rx_id_b2

		if (counter == b'\x80'): # more than 127 bytes incoming, counter overflowed. counter is gonna come after IDs
			counter = int.from_bytes(self._read(1), "big")
		else:
			counter = int.from_bytes(counter, "big")-0x80

		status = self._read(1)

		data = self._read(counter-1)

		checksum = self._read(1)

		#if (self.calculate_checksum()) todo

		return KWPResponse().set_status(status).set_data(list(data))


	def execute (self, kwp_command):
		# first byte 8 + length 
		# byte 2,3 = tx id
		# 4th byte command 
		# 4+x bytes data 
		# last byte checksum

		self._write(self.build_payload([kwp_command.command] + kwp_command.data))
		response = self.fetch_response()

		if (response == False):
			logger.warning('Timeout! returning []')
			return KWPResponse().set_data([])


		return response

	def _write (self, message):
		logger.debug('K-Line sending: {}'.format(' '.join([hex(x) for x in message])))
		self.socket.write(message)
		self.log(message)

	def _read (self, length):
		logger.debug('K-Line trying to read {} bytes'.format(length))
		message = self.socket.read(length)
		logger.debug('Success! Received: {}'.format(' '.join([hex(x) for x in message])))
		self.log(message)
		return message

	def log (self, message):
		with open('kline.log', 'ab') as file:
			file.write(message)