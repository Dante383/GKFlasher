from scapy.config import conf
conf.contribs['CANSocket'] = {'use-python-can': False}
conf.contribs['ISOTP'] = {'use-can-isotp-kernel-module': True}
from scapy.contrib.cansocket import *
from scapy.contrib.isotp import *

class CanInterface:
	socket = False

	def __init__ (self, rx_id, tx_id, iface='can0'):
		self.socket = ISOTPNativeSocket(iface=iface, tx_id=tx_id, rx_id=rx_id, padding=True)
		
	def execute (self, kwp_command):
		data = [kwp_command.command] + kwp_command.data
 
		self.socket.send(bytes(data))
		
		response = self.socket.recv()
		return kwp_command.prepare_output(list(response.data))
		
	def shutdown (self):
		self.bus.shutdown()