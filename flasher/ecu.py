import logging
from typing_extensions import Self
from gkbus.protocol import kwp2000
from ecu_definitions import ECU_IDENTIFICATION_TABLE, IOIdentifier
from dataclasses import dataclass
logger = logging.getLogger(__name__)

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
			value = bus.execute(kwp2000.commands.ReadEcuIdentification(parameter['value'])).get_data()
		except kwp2000.Kwp2000NegativeResponseException:
			continue
		values[parameter['value']] = {'name': parameter['name'], 'value': value[1:]}
	return values

def calculate_key (concat11_seed):
    key = 0x9360
    
    for index in range(0x24):
        key = key * 2 ^ concat11_seed
        
    return key & 0xFFFF

def enable_security_access (bus: kwp2000.Kwp2000Protocol):
	seed = bus.execute(kwp2000.commands.SecurityAccess().request_seed()).get_data()[1:]

	if (sum(seed) == 0):
		logging.info('ECU returned seed=0. Either it\'s unlocked, or previous diagnostics session was still active')
		return

	key = calculate_key(int.from_bytes(seed, 'big'))

	bus.execute(kwp2000.commands.SecurityAccess().send_key(key))

@dataclass
class DesiredBaudrate:
	'''
	Index is the value sent with StartDiagnosticSession, baudrate is the serial interface baudrate
	'''
	index: int
	baudrate: int

class ECU:
	def __init__ (self, 
		name: str, 
		eeprom_size_bytes: int,
		bin_offset: int,
		calibration_section_address: int, calibration_size_bytes: int,
		program_section_address: int, program_section_size: int
		):
		self.name = name
		self.eeprom_size_bytes = eeprom_size_bytes
		self.bin_offset = bin_offset
		self.calibration_section_address, self.calibration_size_bytes = calibration_section_address, calibration_size_bytes
		self.program_section_address, self.program_section_size = program_section_address, program_section_size

	def get_name (self) -> str:
		return self.name 

	def get_eeprom_size_bytes (self) -> int:
		return self.eeprom_size_bytes

	def get_calibration_section_address (self) -> int:
		return self.calibration_section_address

	def get_calibration_size_bytes (self) -> int:
		return self.calibration_size_bytes

	def get_program_section_address (self) -> int:
		return self.program_section_address

	def get_program_section_size (self) -> int:
		return self.program_section_size

	def set_bus (self, bus: kwp2000.Kwp2000Protocol) -> Self:
		self.bus = bus
		return self

	def calculate_bin_offset (self, offset: int) -> int:
		return offset + self.bin_offset

	def calculate_memory_write_offset (self, offset: int) -> int:
		# @todo: find out why this byte shift is needed
		# this happens only for the calibration zone
		return (0x80000 << 4) + offset

	def get_calibration (self) -> str:
		calibration = self.bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=self.get_calibration_section_address(), size=8)).get_data()
		return ''.join([chr(x) for x in list(calibration)])

	def get_calibration_description (self) -> str:
		description = self.bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=self.get_calibration_section_address()+0x40, size=8)).get_data()
		return ''.join([chr(x) for x in list(description)])

	def read_memory_by_address (self, offset: int, size: int) -> bytes:
		data = bytes()
		
		try:
			data = self.bus.execute(
				kwp2000.commands.ReadMemoryByAddress(
					offset=offset, 
					size=size
				)
			).get_data()
		except kwp2000.Kwp2000NegativeResponseException as e:
			if e.status.identifier == kwp2000.Kwp2000NegativeStatusIdentifierEnum.CANT_UPLOAD_FROM_SPECIFIED_ADDRESS.value:
				if size == 1:
					raise
				logger.warning('Can\'t upload from %s! This might be a restricted area or more commonly, offset where eeprom pages switch. I\'m gonna try reading 1 byte at a time for the next 16 bytes', hex(offset))
				try:
					one_at_a_time_amount = min(16, size)
					for x in range(one_at_a_time_amount):
						data += self.read_memory_by_address(offset+x, size=1)
					data += self.read_memory_by_address(offset+one_at_a_time_amount, size=size-one_at_a_time_amount)
				except kwp2000.Kwp2000NegativeResponseException as e:
					raise e
			else:
				raise e
		return data

	def clear_adaptive_values (self, desired_baudrate: DesiredBaudrate):
		self.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT, desired_baudrate.index))
		self.bus.execute(kwp2000.commands.InputOutputControlByLocalIdentifier(IOIdentifier.ADAPTIVE_VALUES.value, kwp2000.enums.InputOutputControlParameter.RESET_TO_DEFAULT))

class ECUIdentificationException (Exception):
	pass

def identify_ecu (bus: kwp2000.Kwp2000Protocol) -> ECU:
	for ecu_identifier in ECU_IDENTIFICATION_TABLE:
		try:
			result = bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=ecu_identifier['offset'], size=len(ecu_identifier['expected'][0]))).get_data()
		except kwp2000.Kwp2000NegativeResponseException:
			continue

		if result in ecu_identifier['expected']:
			ecu = ECU(**ecu_identifier['ecu'])
			ecu.set_bus(bus)
			return ecu
	raise ECUIdentificationException('Failed to identify ECU!')
