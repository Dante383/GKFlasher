from kwp.KWPCommand import KWPCommand

class ReadMemoryByAddress(KWPCommand):
	command = 0x23
	offset = 0x000000
	size = 0xFE

	def __init__ (self, offset=0x000000, size=0xFE):
		self.offset = offset
		self.size = size
		byte1 = (self.offset >> 16) & 0xFF
		byte2 = (self.offset >> 8) & 0xFF
		byte3 = self.offset & 0xFF

		self.data = [byte1, byte2, byte3, self.size]

	def prepare_output (self, output):
		return output[1:]