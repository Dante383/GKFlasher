from kwp.KWPCommand import KWPCommand

class StopCommunication(KWPCommand):
	command = 0x82

	def __init__ (self):
		self.data = [0x05]