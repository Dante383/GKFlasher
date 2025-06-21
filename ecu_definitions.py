from enum import Enum 
import ctypes
from flasher.utils import WordBitfield

ECU_IDENTIFICATION_TABLE = [
	{
		'offset': 0x82014, # RSW zone
		'expected': [b'\x36\x36\x32\x31'], #6621
		'ecu': {
			'name': 'SIMK43 8mbit',
			'eeprom_size_bytes': 1048576, # (1024 KiB)
			'bin_offset': 0,
			'calibration_section_address': 0x90000,
			'calibration_size_bytes': 0x10000, # 65536 bytes (64 KiB)
			'program_section_address': 0xA0000,
			'program_section_size': 0x60000
		}
	},
	{
		'offset': 0x90040,
		'expected': [b'\x63\x61\x36\x36'], #CA66
		'ecu': {
			'name': 'SIMK43 2.0 4mbit',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'calibration_section_address': 0x90000,
			'calibration_size_bytes': 0x10000, # 65536 bytes (64 KiB)
			'program_section_address': 0xA0000,
			'program_section_size': 0x60000
		},
	},
	{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x35\x34\x30\x31'], #CA65401 (5WY17)
		'ecu': {
			'name': 'SIMK43 V6 4mbit (5WY17)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'calibration_section_address': 0x88000,
			'calibration_size_bytes': 0x8000, # 32,768 bytes (32 KiB)
			'program_section_address': 0x90000,
			'program_section_size': 0x70000
		}
	},
		{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x35\x34', b'\x63\x61\x36\x35\x35'], #CA654, CA655 (5WY18+)
		'ecu': {
			'name': 'SIMK43 V6 4mbit (5WY18+)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'calibration_section_address': 0x88000,
			'calibration_size_bytes': 0x6EFF, # there is some readable but non-writable section after this
			'program_section_address': 0x90000,
			'program_section_size': 0x70000
		}
	},
	{
		'offset': 0x48040,
		'expected': [b'\x63\x61\x36\x36\x30', b'\x63\x61\x36\x35\x32', b'\x63\x61\x36\x35\x30'], #CA660, CA652, CA650
		'ecu': {
			'name': 'SIMK41 / V6 2mbit',
			'eeprom_size_bytes': 262144, # (256 KiB)
			'bin_offset': -0x40000,
			'calibration_section_address': 0x48000,
			'calibration_size_bytes': 0x8000, # 32,768 bytes (32 KiB)
			'program_section_address': 0x50000, 
			'program_section_size': 0x30000
		}
	},
	{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x36\x31'], #CA661 (Sonata)
		'ecu': {
			'name': 'SIMK43 2.0 4mbit (Sonata)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'calibration_section_address': 0x88000,
			'calibration_size_bytes': 0x5FF8, # yes, this is correct. this is a 4mbit ecu with a calibration zone smaller than 2mbit ecus. i dont know either
			'program_section_address': 0x90000,
			'program_section_size': 0x70000
		}
	},	
]

BAUDRATES = {
	0x01: 10400,
	0x02: 20000,
	0x03: 40000,
	0x04: 60000,
	0x05: 120000
}

class ReprogrammingStatus(WordBitfield):
	class bits(ctypes.LittleEndianStructure):
		_fields_ = [
			('checksum_of_calibration_data_is_correct', ctypes.c_uint16, 1),
			('security_keys_for_calibration_data_are_not_written', ctypes.c_uint16, 1),
			('security_keys_for_calibration_data_are_correct', ctypes.c_uint16, 1),
			('calibration_data_is_correct', ctypes.c_uint16, 1),
			('checksum_of_ecu_sw_is_correct', ctypes.c_uint16, 1),
			('security_keys_for_ecu_sw_are_not_written', ctypes.c_uint16, 1),
			('security_keys_for_ecu_sw_are_correct', ctypes.c_uint16, 1),
			('ecu_sw_is_correct', ctypes.c_uint16, 1),
			('ecu_reprogramming_successfully_completed', ctypes.c_uint16, 1),
			('ecu_is_not_at_the_end_of_reprogramming_session', ctypes.c_uint16, 1),
			('coherence_identifiers_fit_together', ctypes.c_uint16, 1),
			('calibration_data_does_not_fit_to_ecu_sw', ctypes.c_uint16, 1),
			('ecu_sw_does_not_fit_to_boot_sw', ctypes.c_uint16, 1),
			('coherence_identifier_in_calibration_data_is_erroneous', ctypes.c_uint16, 1),
			('coherence_identifier_in_ecu_sw_is_erroneous', ctypes.c_uint16, 1),
			('coherence_identifier_in_boot_sw_is_erroneous', ctypes.c_uint16, 1),
		]

class AccessLevel (Enum):
	HYUNDAI_0x1 = 0x1
	SIEMENS_0xFD = 0xFD

class Routine (Enum):
	ERASE_PROGRAM = 0x00
	ERASE_CALIBRATION = 0x01
	VERIFY_BLOCKS = 0x02
	CHECK_REPROGRAMMING_STATUS = 0x03

	QUERY_IMMO_INFO = 0x12
	BEFORE_LIMP_HOME = 0x16 # what does this actually do?
	ACTIVATE_LIMP_HOME = 0x18 # user 4 pin code password as parameters
	BEFORE_LIMP_HOME_TEACHING = 0x13
	LIMP_HOME_INPUT_NEW_PASSWORD = 0x17
	LIMP_HOME_CONFIRM_NEW_PASSWORD = 0x19

	BEFORE_IMMO_RESET = 0x15 # what does this actually do?
	IMMO_INPUT_PASSWORD = 0x1A # 6 digit pin code as parameter
	IMMO_RESET_CONFIRM = 0x20

	BEFORE_IMMO_KEY_TEACHING = 0x14 # what does this actually do?
	# these 4 enums below are not actually used, just serve as documentation
	IMMO_TEACH_KEY_1 = 0x1B
	IMMO_TEACH_KEY_2 = 0x1C
	IMMO_TEACH_KEY_3 = 0x1D
	IMMO_TEACH_KEY_4 = 0x1E

	BEFORE_SMARTRA_NEUTRALIZE = 0x25
	SMARTRA_NEUTRALIZE = 0x26

class IOIdentifier (Enum):
	VERSION_CONFIGURATION_AUTOMATIC_TRANSAXLE = 0x40
	VERSION_CONFIGURATION_TRACTION_CONTROL_SYSTEM = 0x41
	ADAPTIVE_VALUES = 0x50
	_OPENGK_PATCH_PRIVILEGE_ESCALATION = 0xBB 
