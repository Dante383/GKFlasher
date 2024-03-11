from gkbus import kwp
from ecu_definitions import IOIdentifier
import os, importlib.util, inspect

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
	def get_name (self) -> str:
		return self.name 

	def get_eeprom_size_bytes (self) -> int:
		return self.eeprom_size_bytes

	def get_calibration_size_bytes (self) -> int:
		return self.calibration_size_bytes

	def get_calibration_size_bytes_flash (self) -> int:
		return self.calibration_size_bytes_flash

	def get_program_section_offset (self) -> int:
		return self.program_section_offset

	def get_program_section_size (self) -> int:
		return self.program_section_size

	def get_program_section_flash_size (self) -> int:
		return self.program_section_flash_size	

	def get_program_section_flash_bin_offset (self) -> int:
		return self.program_section_flash_bin_offset

	def get_program_section_flash_memory_offset (self) -> int:
		return self.program_section_flash_memory_offset

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
		self.bus.execute(kwp.commands.InputOutputControlByLocalIdentifier(IOIdentifier.ADAPTIVE_VALUES.value, kwp.enums.InputOutputControlParameter.RESET_TO_DEFAULT))

	@staticmethod
	def get_identification_offset(subclass):
		return subclass.identification_offset

	@staticmethod
	def get_identification_expected(subclass):
		return subclass.identification_expected

class ECUIdentificationException (Exception):
	pass

def load_identification_table ():
	identification_table = []
	for module_file in [f for f in os.listdir('ecu_definitions') if f.endswith('.py')]:
		module_name = os.path.splitext(module_file)[0]
		
		spec = importlib.util.spec_from_file_location(module_name, os.path.join('ecu_definitions', module_file))
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)

		ecu = getattr(module, module_name)
		identification_table.append({
			'offset': ECU.get_identification_offset(ecu),
			'expected': ECU.get_identification_expected(ecu),
			'ecu': ecu
		})
	return identification_table

def identify_ecu (bus) -> ECU:
	for ecu_identifier in ECU_IDENTIFICATION_TABLE:
		continue
		try:
			result = bus.execute(kwp.commands.ReadMemoryByAddress(offset=ecu_identifier['offset'], size=len(ecu_identifier['expected'][0]))).get_data()
		except kwp.KWPNegativeResponseException:
			continue

		if result in ecu_identifier['expected']:
			ecu = ecu_identifier['ecu']()
			ecu.set_bus(bus)
			return ecu
	raise ECUIdentificationException('Failed to identify ECU!')

ECU_IDENTIFICATION_TABLE = load_identification_table()