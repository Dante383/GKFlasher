from kwp.KWPCommand import KWPCommand

class SecurityAccess(KWPCommand):
	command = 0x27

	def __init__ (self):
		self.data = [0x01]