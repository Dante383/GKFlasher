class KWPCommand:
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