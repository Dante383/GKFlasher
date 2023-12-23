from gkbus.kwp.commands import ReadEcuIdentification, SecurityAccess, ReadMemoryByAddress
from gkbus.kwp.enums import *
from gkbus.kwp import KWPNegativeResponseException
from ecu_definitions import ECU_IDENTIFICATION_TABLE

kwp_ecu_identification_parameters = [
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
	{'value': 0x8C, 'name': 'Hardware revision'},
	{'value': 0x8D, 'name': 'Hardware subsystem'},
	{'value': 0x8E, 'name': 'Calibration version'},
	{'value': 0x8F, 'name': 'System supplier specific'},
]

def print_ecu_identification (bus):
	print('[*] Reading ECU Identification..',end='')
	for parameter in kwp_ecu_identification_parameters:
		try:
			value = bus.execute(ReadEcuIdentification(parameter['value'])).get_data()
		except KWPNegativeResponseException:
			continue

		value_hex = ' '.join([hex(x) for x in value[1:]])
		value_ascii = ''.join([chr(x) for x in value[1:]])

		print('')
		print('    [*] [{}] {}:'.format(hex(parameter['value']), parameter['name']))
		print('            [HEX]: {}'.format(value_hex))
		print('            [ASCII]: {}'.format(value_ascii))

def calculate_key(concat11_seed):
    key = 0
    index = 0
    
    while index < 0x10:
        if (concat11_seed & (1 << (index & 0x1f))) != 0:
            key = key ^ 0xffff << (index & 0x1f)
        index += 1
    
    return key

def enable_security_access (bus):
	print('[*] Security Access')
	seed = bus.execute(SecurityAccess(AccessType.PROGRAMMING_REQUEST_SEED)).get_data()[1:]

	if (seed == [0x0, 0x0]):
		print('[*] ECU is not locked.')
		return

	seed_concat = (seed[0]<<8) | seed[1]
	key = calculate_key(seed_concat)

	bus.execute(SecurityAccess(AccessType.PROGRAMMING_SEND_KEY, key))

class ECU:
	def __init__ (self, 
		name: str, 
		eeprom_size_bytes: int,
		memory_offset: int, bin_offset: int,
		calibration_size_bytes: int,
		single_byte_restriction_start: int = 0, single_byte_restriction_stop: int = 0):
		self.name = name
		self.eeprom_size_bytes = eeprom_size_bytes
		self.memory_offset, self.bin_offset = memory_offset, bin_offset
		self.calibration_size_bytes = calibration_size_bytes
		self.single_byte_restriction_start, self.single_byte_restriction_stop = single_byte_restriction_start, single_byte_restriction_stop

	def get_name (self) -> str:
		return self.name 

	def get_eeprom_size_bytes (self) -> int:
		return self.eeprom_size_bytes

	def get_calibration_size_bytes (self) -> int:
		return self.calibration_size_bytes

	def set_bus (self, bus):
		self.bus = bus
		return self

	def calculate_memory_offset (self, offset: int) -> int:
		return offset + self.memory_offset

	def calculate_bin_offset (self, offset: int) -> int:
		return offset + self.bin_offset

	def adjust_bytes_at_a_time (self, offset: int, at_a_time: int, og_at_a_time: int) -> int:
		if (self.single_byte_restriction_start == 0 or self.single_byte_restriction_stop == 0):
			return at_a_time
		if (offset >= self.single_byte_restriction_start and offset < self.single_byte_restriction_stop):
			return 1
		return og_at_a_time

	def get_calibration (self) -> str:
		calibration = self.bus.execute(ReadMemoryByAddress(offset=self.calculate_memory_offset(0x090000), size=8)).get_data()
		return ''.join([chr(x) for x in calibration])

	def get_calibration_description (self) -> str:
		description = self.bus.execute(ReadMemoryByAddress(offset=self.calculate_memory_offset(0x090040), size=8)).get_data()
		return ''.join([chr(x) for x in description])

	def read_memory_by_address (self, offset: int, size: int):
		return self.bus.execute(
			ReadMemoryByAddress(
				offset=self.calculate_memory_offset(offset), 
				size=size
			)
		).get_data()

class ECUIdentificationException (Exception):
	pass

def identify_ecu (bus) -> ECU:
	for ecu_identifier in ECU_IDENTIFICATION_TABLE:
		try:
			result = bus.execute(ReadMemoryByAddress(offset=ecu_identifier['offset'], size=4)).get_data()
		except KWPNegativeResponseException:
			continue

		if result == ecu_identifier['expected']:
			ecu = ECU(**ecu_identifier['ecu'])
			ecu.set_bus(bus)
			return ecu
	raise ECUIdentificationException('Failed to identify ECU!')