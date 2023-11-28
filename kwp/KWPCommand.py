class KWPCommand:
	COMMAND_READ_ECU_IDENTIFICATION = 0x1A
	COMMAND_READ_MEMORY_BY_ADDRESS = 0x23
	COMMAND_WRITE_MEMORY_BY_ADDRESS = 0x3D

	command = 0x0
	data = []

	def set_data (self, data):
		self.data = data
		return self

	def set_command (self, command):
		self.command = command
		return self

	def prepare_output (self, output):
		return output