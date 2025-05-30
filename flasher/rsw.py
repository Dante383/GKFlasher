from .ecu import ECU, fetch_ecu_identification
from gkbus.protocol import kwp2000

RSW_FILE = '../dev_fw_relocated_rsw.bin'
RSW_ADDRESS = 0xC000
RSW_LENGTH = 0x20 #0x10AC

def strip (string):
	return ''.join(x for x in string if x.isalnum())

def rsw_handler (ecu: ECU) -> None:
	print('[*] Loading RSW from {}'.format(RSW_FILE))

	try:
		with open(RSW_FILE, 'rb') as f:
			f.seek(RSW_ADDRESS)
			rsw_bytes = f.read(RSW_LENGTH)
	except (FileNotFoundError, IOError) as e:
		print(e)
		return

	print('[*] {} bytes loaded, checksum: {}'.format(len(rsw_bytes), rsw_bytes[:2]))

	print('[*] Trying to start extended diagnostic session')
	ecu.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT))

	#print('[*] reading data by local identifier')
	#print(ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(0x00).set_data(b'\x1F\xF6')).get_data())

	print('[*] Reading RSW checksum from RAM')
	print(hex(int.from_bytes(ecu.bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=RSW_ADDRESS, size=2)).get_data(), 'big')))
	
	bytes_written = 0
	while len(rsw_bytes) > 0:
		len_bytes_to_write = min(16, len(rsw_bytes))
		bytes_to_write = rsw_bytes[:len_bytes_to_write]

		# @todo: fix gkbus
		write_memory_address = (RSW_ADDRESS+bytes_written).to_bytes(3, 'big')
		write_memory_data = write_memory_address + len_bytes_to_write.to_bytes(1, 'little') + bytes_to_write
		ecu.bus.execute(kwp2000.commands.WriteMemoryByAddress(offset=0x0, data_to_write=[0xFF, 0xFF]).set_data(write_memory_data))

		#print(rsw_bytes[bytes_written:bytes_written+len_bytes_to_write])
		bytes_written += len_bytes_to_write
		rsw_bytes = rsw_bytes[len_bytes_to_write:]

	print('[*] RSW writen to RAM!')
	print(bytes_written)

	print('[*] starting MTOS routine: 0xFA')
	ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(0xFA))