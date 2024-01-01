from gkbus.kwp.commands import *
from gkbus.kwp.enums import *

def logger(ecu):
	ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT))
