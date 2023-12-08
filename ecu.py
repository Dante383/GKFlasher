from gkbus.kwp.commands.ReadEcuIdentification import ReadEcuIdentification

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

		print('    [*] [{}] {}: (status: {})'.format(hex(parameter['value']), parameter['name'], hex(status)))
		print('        [HEX]: {}'.format(value_hex))
		print('        [ASCII]: {}'.format(value_ascii))