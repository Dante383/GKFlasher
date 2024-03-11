from flasher.ecu import ECU

class SIMK43_v6 (ECU):
	name = 'SIMK43 V6 4mbit (5WY17)'
	
	identification_offset = 0x88040
	identification_expected = [[99, 97, 54, 53, 52, 48, 49]]

	eeprom_size_bytes = 524288 # (512 KiB)
	memory_offset = -0x8000
	bin_offset = -0x88000
	memory_write_offset = -0x7800
	calibration_size_bytes = 0x8000 # 32,768 bytes (32 KiB)
	calibration_size_bytes_flash = 0x5F40 #rounded upto nearest 254 was 0x5F00
	program_section_offset = 0x98000
	program_section_size = 0x70000
	program_section_flash_size = 0x6FFE4 #rounded down to nearest 254 bytes was 0x6FFF0
	program_section_flash_bin_offset = 0x10010
	program_section_flash_memory_offset = -0x7FF0 # write to 0x90010
