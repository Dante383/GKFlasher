from kwp.KWPCommand import KWPCommand

class WriteMemoryByAddress(KWPCommand):
	command = 0x3D
	offset = 0x0
	size = 0
	data_to_write = []

	def __init__ (self, offset, data_to_write):
		self.offset = offset
		self.data_to_write = data_to_write
		self.size = len(self.data_to_write)

		byte1 = (self.offset >> 16) & 0xFF
		byte2 = (self.offset >> 8) & 0xFF
		byte3 = self.offset & 0xFF

		self.data = [byte1, byte2, byte3, self.size] + self.data_to_write