from kwp.KWPCommand import KWPCommand

class StopDiagnosticSession(KWPCommand):
	command = 0x20

	def __init__ (self):
		self.data = []