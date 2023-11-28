from kwp.KWPCommand import KWPCommand

class StartCommunication(KWPCommand):
	command = 0x81

	def __init__ (self):
		self.data = [0x04]