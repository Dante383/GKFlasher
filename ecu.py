from gkbus.kwp.commands.ReadEcuIdentification import ReadEcuIdentification
from gkbus.kwp.commands.SecurityAccess import SecurityAccess

ecu_identification_parameters = [
	{'value': 0x86, 'name': 'DCS ECU Identification'},
	{'value': 0x87, 'name': 'DCX/MMC ECU Identification'},
	{'value': 0x88, 'name': 'VIN (original)'},
	{'value': 0x89, 'name': 'Diagnostic Variant Code'},
	{'value': 0x90, 'name': 'VIN (current)'},
	{'value': 0x96, 'name': 'Calibration identification'},
	{'value': 0x97, 'name': 'Calibration Verification Number'},
	{'value': 0x9A, 'name': 'ECU Code Fingerprint'},
	{'value': 0x9B, 'name': 'ECU Data Fingerprint'},
	{'value': 0x9C, 'name': 'ECU Code Software Identification'},
	{'value': 0x9D, 'name': 'ECU Data Software Identification'},
	{'value': 0x9E, 'name': 'ECU Boot Software Identification'},
	{'value': 0x9F, 'name': 'ECU Boot Fingerprint'},
	{'value': 0x8A, 'name': 'System supplier specific'},
	{'value': 0x8B, 'name': 'System supplier specific'},
	{'value': 0x8C, 'name': 'System supplier specific'},
	{'value': 0x8D, 'name': 'System supplier specific'},
	{'value': 0x8E, 'name': 'System supplier specific'},
	{'value': 0x8F, 'name': 'System supplier specific'},
]

def print_ecu_identification (bus):
	print('[*] Reading ECU Identification..')
	for parameter in ecu_identification_parameters:
		value = bus.execute(ReadEcuIdentification(parameter['value'])).get_data()
		status = value[0]
		value_hex = ' '.join([hex(x) for x in value[1:]])
		value_ascii = ''.join([chr(x) for x in value[1:]])

		print('')
		print('    [*] [{}] {}: (status: {})'.format(hex(parameter['value']), parameter['name'], hex(status)))
		print('        [HEX]: {}'.format(value_hex))
		print('        [ASCII]: {}'.format(value_ascii))

def get_security_key (seed):
	# we don't know the key algo yet so i'll just hardcode known key for now
	return [0xFC, 0xD0]

def enable_security_access (bus):
	print('[*] Security Access 1')
	seed = bus.execute(SecurityAccess([0x01])).get_data()[1:]

	key = get_security_key(seed)

	print('[*] Security Access 2')
	bus.execute(SecurityAccess([0x02] + key))