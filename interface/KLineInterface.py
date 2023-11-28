import serial, os, time

class KLineInterface:
	socket = False
	interbyte_delay = 30 # ms

	def __init__ (self, iface, baudrate):
		print('    [K] K-line init. Iface: {} baudrate: {}'.format(iface, baudrate))
		self.socket = serial.serial_for_url(iface, baudrate, rtscts=False, dsrdtr=False, do_not_open=True)
		self.socket.dtr = 0
		self.socket.rts = 0
		self.socket.open()
		try:
			os.remove('kline.log')
		except OSError:
			pass

	def execute (self, kwp_command):
		# first byte 8 + length 
		# second byte always 0x11
		# third byte always 0xF1
		# 4th byte command 
		# 4+x bytes data 
		# last byte unknown

		data = [kwp_command.command] + kwp_command.data
		length = len(data)-1 #FIXME!!

		payload = bytes([0x80+length, 0x11, 0xF1] + data) # figure out the last byte!

		self._write(payload)

		print(self.socket.read(20))#len(payload))) # avoid echo, dont log this (we already did)

		response_length = int.from_bytes(self._read(1), "big")-0x80
		print('    [K] K-line received one byte (response length). It is {}'.format(response_length))
		response = self._read(response_length)

		return kwp_command.prepare_output(response)

	def _write (self, message):
		print('    [K] K-Line sending: {}'.format(message))
		for byte in message:
			self.socket.write(byte)
			time.sleep(5/1000)
		self.log(message)

	def _read (self, length):
		print('    [K] K-Line trying to read {} bytes'.format(length))
		message = self.socket.read(length) 
		print('    [K] Success! Received: {}'.format(message))
		self.log(message)
		return message

	def log (self, message):
		with open('kline.log', 'ab') as file:
			file.write(message)