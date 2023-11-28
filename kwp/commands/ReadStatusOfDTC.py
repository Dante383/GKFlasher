from kwp.KWPCommand import KWPCommand

class ReadStatusOfDTC(KWPCommand):
	command = 0x01
	dtc = 0x0

	def __init__ (self, dtc):
		self.dtc = dtc
		self.data = [self.dtc]

	def prepare_output (self, output):
		return output[2:]