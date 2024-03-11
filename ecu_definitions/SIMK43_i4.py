from flasher.ecu import ECU

class SIMK43_i4 (ECU):
	name = 'SIMK43 2.0 4mbit'

	identification_offset = 0x90040
	identification_expected = [[99, 97, 54, 54]]

	eeprom_size_bytes = 524288 # (512 KiB)
	memory_offset = 0
	bin_offset = -0x80000
	memory_write_offset = -0x7000
	single_byte_restriction_start = 0x89FFF
	single_byte_restriction_stop = 0x9000F
	calibration_size_bytes = 0x10000 # 65536 bytes (64 KiB)
	calibration_size_bytes_flash = 0xFEFE #rounded down to nearest 254 bytes was 0xFFF0
	program_section_offset = 0xA0000
	program_section_size = 0x60000
	program_section_flash_size = 0x5FFE8 #rounded down to nearest 254 bytes was 0x5FFF0
	program_section_flash_bin_offset = 0x20010
	program_section_flash_memory_offset = 0x10