from kwp.KWPCommand import KWPCommand

class StartDiagnosticSession(KWPCommand):
	command = 0x10

	def __init__ (self):
		self.data = [0x85, 0x19]