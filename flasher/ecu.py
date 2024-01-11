from gkbus import kwp
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
	{'value': 0x8C, 'name': 'Bootloader version'},
	{'value': 0x8D, 'name': 'Program code version'},
	{'value': 0x8E, 'name': 'Calibration version'},
	{'value': 0x8F, 'name': 'System supplier specific'},
]

def fetch_ecu_identification (bus):
	values = {}
	for parameter in kwp_ecu_identification_parameters:
		try:
			value = bus.execute(kwp.commands.ReadEcuIdentification(parameter['value'])).get_data()
		except kwp.KWPNegativeResponseException:
			continue
		values[parameter['value']] = {'name': parameter['name'], 'value': value[1:]}
	return values

def calculate_key(concat11_seed):
    key = 0
    index = 0
    
    while index < 0x10:
        if (concat11_seed & (1 << (index & 0x1f))) != 0:
            key = key ^ 0xffff << (index & 0x1f)
        index += 1
    
    return key & 0xFFFF

def enable_security_access (bus):
	seed = bus.execute(kwp.commands.SecurityAccess(kwp.enums.AccessType.PROGRAMMING_REQUEST_SEED)).get_data()[1:]

	if (seed == [0x0, 0x0]):
		return

	seed_concat = (seed[0]<<8) | seed[1]
	key = calculate_key(seed_concat)

	bus.execute(kwp.commands.SecurityAccess(kwp.enums.AccessType.PROGRAMMING_SEND_KEY, key))

class ECU:
	def __init__ (self, 
		name: str, 
		eeprom_size_bytes: int,
		memory_offset: int, bin_offset: int, memory_write_offset: int,
		calibration_size_bytes: int,
		program_section_offset: int, program_section_size: int,
		program_section_flash_offset: int,
		single_byte_restriction_start: int = 0, single_byte_restriction_stop: int = 0):
		self.name = name
		self.eeprom_size_bytes = eeprom_size_bytes
		self.memory_offset, self.bin_offset, self.memory_write_offset = memory_offset, bin_offset, memory_write_offset
		self.calibration_size_bytes = calibration_size_bytes
		self.program_section_offset, self.program_section_size = program_section_offset, program_section_size
		self.program_section_flash_offset = program_section_flash_offset
		self.single_byte_restriction_start, self.single_byte_restriction_stop = single_byte_restriction_start, single_byte_restriction_stop

	def get_name (self) -> str:
		return self.name 

	def get_eeprom_size_bytes (self) -> int:
		return self.eeprom_size_bytes

	def get_calibration_size_bytes (self) -> int:
		return self.calibration_size_bytes

	def get_program_section_offset (self) -> int:
		return self.program_section_offset

	def get_program_section_size (self) -> int:
		return self.program_section_size

	def get_program_section_flash_offset (self) -> int:
		return self.program_section_flash_offset

	def set_bus (self, bus):
		self.bus = bus
		return self

	def calculate_memory_offset (self, offset: int) -> int:
		return offset + self.memory_offset

	def calculate_bin_offset (self, offset: int) -> int:
		return offset + self.bin_offset

	def calculate_memory_write_offset (self, offset: int) -> int:
		return (offset + self.memory_write_offset) << 4

	def adjust_bytes_at_a_time (self, offset: int, at_a_time: int, og_at_a_time: int) -> int:
		if (self.single_byte_restriction_start == 0 or self.single_byte_restriction_stop == 0):
			return at_a_time
		if (offset >= self.single_byte_restriction_start and offset < self.single_byte_restriction_stop):
			return 1
		return og_at_a_time

	def get_calibration (self) -> str:
		calibration = self.bus.execute(kwp.commands.ReadMemoryByAddress(offset=self.calculate_memory_offset(0x090000), size=8)).get_data()
		return ''.join([chr(x) for x in calibration])

	def get_calibration_description (self) -> str:
		description = self.bus.execute(kwp.commands.ReadMemoryByAddress(offset=self.calculate_memory_offset(0x090040), size=8)).get_data()
		return ''.join([chr(x) for x in description])

	def read_memory_by_address (self, offset: int, size: int):
		return self.bus.execute(
			kwp.commands.ReadMemoryByAddress(
				offset=self.calculate_memory_offset(offset), 
				size=size
			)
		).get_data()

	def clear_adaptive_values (self):
		self.bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
		self.bus.execute(kwp.commands.InputOutputControlByLocalIdentifier(0x50, kwp.enums.InputOutputControlParameter.RESET_TO_DEFAULT))

class ECUIdentificationException (Exception):
	pass

def identify_ecu (bus) -> ECU:
	for ecu_identifier in ECU_IDENTIFICATION_TABLE:
		try:
			result = bus.execute(kwp.commands.ReadMemoryByAddress(offset=ecu_identifier['offset'], size=4)).get_data()
		except kwp.KWPNegativeResponseException:
			continue

		if result == ecu_identifier['expected']:
			ecu = ECU(**ecu_identifier['ecu'])
			ecu.set_bus(bus)
			return ecu
	raise ECUIdentificationException('Failed to identify ECU!')