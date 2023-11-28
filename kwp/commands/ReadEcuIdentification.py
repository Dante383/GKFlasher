from kwp.KWPCommand import KWPCommand

class ReadEcuIdentification(KWPCommand):
	command = 0x1A
	identifier = 0x0

	def __init__ (self, identifier):
		self.identifier = identifier
		self.data = [self.identifier]

	def prepare_output (self, output):
		return output[2:]