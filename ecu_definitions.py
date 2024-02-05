from enum import Enum 

ECU_IDENTIFICATION_TABLE = [
	{
		'offset': 0x3FE0, # BL workaround
		'expected': [[53, 50, 52, 50]], #5242
		'ecu': {
			'name': 'SIMK43 8mbit',
			'eeprom_size_bytes': 1048576, # (1024 KiB)
			'memory_offset': 0,
			'bin_offset': 0,
			'memory_write_offset': -0x7000,
			'calibration_size_bytes': 0x10000, # 65536 bytes (64 KiB)
			'calibration_size_bytes_flash': 0xFFF0,
			'program_section_offset': 0xA0000,
			'program_section_size': 0x60000,
			'program_section_flash_size': 0x5FFF0,
			'program_section_flash_bin_offset': 0xA0010,
			'program_section_flash_memory_offset': 0x10
		}
	},
	{
		'offset': 0x90040,
		'expected': [[99, 97, 54, 54]], #CA66
		'ecu': {
			'name': 'SIMK43 2.0 4mbit',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'memory_offset': 0,
			'bin_offset': -0x80000,
			'memory_write_offset': -0x7000,
			'single_byte_restriction_start': 0x89FFF,
			'single_byte_restriction_stop': 0x9000F,
			'calibration_size_bytes': 0x10000, # 65536 bytes (64 KiB)
			'calibration_size_bytes_flash': 0xFFF0,
			'program_section_offset': 0xA0000,
			'program_section_size': 0x60000,
			'program_section_flash_size': 0x5FFF0,
			'program_section_flash_bin_offset': 0x20010,
			'program_section_flash_memory_offset': 0x10
		}
	},
	{
		'offset': 0x88040,
		'expected': [[99, 97, 54, 53, 52], [99, 97, 54, 53, 53]], #CA654, CA655
		'ecu': {
			'name': 'SIMK43 V6 4mbit',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'memory_offset': -0x8000,
			'bin_offset': -0x88000,
			'memory_write_offset': -0x7800,
			'calibration_size_bytes': 0x8000, # 32,768 bytes (32 KiB)
			'calibration_size_bytes_flash': 0x5F00,
			'program_section_offset': 0x90000,
			'program_section_size': 0x70000,
			'program_section_flash_size': 0x6FFF0,
			'program_section_flash_bin_offset': 0x10010,
			'program_section_flash_memory_offset': 0x10
		}
	},
	{
		'offset': 0x48040,
		'expected': [[99, 97, 54, 54, 48], [99, 97, 54, 53, 50], [99, 97, 54, 53, 48]], #CA660, CA652, CA650
		'ecu': {
			'name': 'SIMK41 / V6 2mbit',
			'eeprom_size_bytes': 262144, # (256 KiB)
			'memory_offset': -0x48000,
			'bin_offset': -0x88000,
			'memory_write_offset': -0x8800,
			'single_byte_restriction_start': 0x89FFF,
			'single_byte_restriction_stop': 0x9000F,
			'calibration_size_bytes': 0x8000, # 32,768 bytes (32 KiB)
			'calibration_size_bytes_flash': 0x5F00,
			'program_section_offset': 0x90000,
			'program_section_size': 0x30000,
			'program_section_flash_size': 0x2FE22, # 196,130 bytes | zone size: 0x30000 - 196,608 bytes (192 KiB)
			'program_section_flash_bin_offset': 0x10010,
			'program_section_flash_memory_offset': 0x10
		}
	}
]

BAUDRATES = {
	0x01: 10400,
	0x02: 20000,
	0x03: 40000,
	0x04: 60000,
	0x05: 120000
}

class Routine (Enum):
	ERASE_PROGRAM = 0x00
	ERASE_CALIBRATION = 0x01
	VERIFY_BLOCKS = 0x02

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
	# these enums below are not actually used, just serve as documentation
	IMMO_TEACH_KEY_1 = 0x1B
	IMMO_TEACH_KEY_2 = 0x1C
	IMMO_TEACH_KEY_3 = 0x1D
	IMMO_TEACH_KEY_4 = 0x1E

	BEFORE_SMARTRA_NEUTRALIZE = 0x25
	SMARTRA_NEUTRALIZE = 0x26

class IOIdentifier (Enum):
	CHECK_ENGINE_LIGHT = 0x10
	COOLING_FAN_RELAY_HIGH = 0x1A
	COOLING_FAN_RELAY_LOW = 0x1B
	IDLE_SPEED_ACTUATOR = 0x23
	CVVT_VALVE = 0x24
	ADAPTIVE_VALUES = 0x50
