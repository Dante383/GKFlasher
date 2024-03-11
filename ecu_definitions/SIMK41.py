from flasher.ecu import ECU

class SIMK41 (ECU):
	name = 'SIMK41 / V6 2mbit'
	
	identification_offset = 0x48040
	identification_expected = [[99, 97, 54, 54, 48], [99, 97, 54, 53, 50], [99, 97, 54, 53, 48]] #CA660, CA652, CA650

	eeprom_size_bytes = 262144 # (256 KiB)
	memory_offset = -0x48000
	bin_offset = -0x88000
	memory_write_offset = -0xB800 # write at 0x84800
	single_byte_restriction_start = 0x89FFF #required for SIMK41 only.
	single_byte_restriction_stop = 0x9000F
	calibration_size_bytes = 0x8000 # 32,768 bytes (32 KiB)
	calibration_size_bytes_flash = 0x7F00 #already rounded to 254!
	program_section_offset = 0x98000 #0xA0000 - 0x8000
	program_section_size = 0x30000
	program_section_flash_size = 0x2FFF0 #196,592 bytes | zone size: 0x30000 - 196,608 bytes (192 KiB)
	program_section_flash_bin_offset = 0x10010
	program_section_flash_memory_offset = -0x47FF0 #write at 0x50010
