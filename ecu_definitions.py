ECU_IDENTIFICATION_TABLE = [
	{
		'offset': 0x090040,
		'expected': [99, 97, 54, 54],
		'ecu': {
			'name': 'SIMK43 2.0 4mbit',
			'eeprom_size_bytes': 524287,
			'memory_offset': 0,
			'bin_offset': -0x080000,
			'memory_write_offset': -0x7000,
			'single_byte_restriction_start': 0x089FFF,
			'single_byte_restriction_stop': 0x09000F,
			'calibration_size_bytes': 0xFFF0,
			'program_section_offset': 0x0A0010,
			'program_section_size': 0x05FFF0,
			'program_section_flash_offset': 0x020010,
		}
	},
	{
		'offset': 0xA00A0,
		'expected': [54, 54, 51, 54],
		'ecu': {
			'name': 'SIMK43 8mbit',
			'eeprom_size_bytes': 1048575,
			'memory_offset': 0,
			'bin_offset': 0,
			'memory_write_offset': -0x7000,
			'calibration_size_bytes': 0xFFF0,
			'program_section_offset': 0xA0010,
			'program_section_size': 0x05FFF0,
			'program_section_flash_offset': 0xA0010
		}
	},
	{
		'offset': 0x88040,
		'expected': [99, 97, 54, 53],
		'ecu': {
			'name': 'SIMK43 V6 4mbit',
			'eeprom_size_bytes': 524287,
			'memory_offset': -0x8000,
			'bin_offset': -0x088000,
			'memory_write_offset': -0x87800,
			'calibration_size_bytes': 0x5F00,
			'program_section_offset': 0x890010,
			'program_section_size': 0x05FFF0,
			'program_section_flash_offset': 0xA0010
		}
	},
	{
		'offset': 0x48040,
		'expected': [99, 97, 54, 54],
		'ecu': {
			'name': 'SIMK41 2mbit',
			'eeprom_size_bytes': 262143,
			'memory_offset': -0x48000,
			'bin_offset': -0x088000,
			'memory_write_offset': -0x7000,
			'single_byte_restriction_start': 0x48000,
			'single_byte_restriction_stop': 0x4800F,
			'calibration_size_bytes': 0x7FFF,
			'program_section_offset': 0x890010,
			'program_section_size': 0x05FFF0,
			'program_section_flash_offset': 0x010010
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
