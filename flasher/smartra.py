def get_msb (byte):
	return (byte >> 0x10) & 0x0FFFF

def get_lsb (byte):
	return byte & 0xFFFF

def calculate_smartra_pin (last_6_digits_of_vin: int) -> int:
	output = last_6_digits_of_vin
	index = 0x0

	while True:
		if (index > 0x27):
			break

		if ((get_msb(output) & 0x8000) == 0):
			output = output << 1
		else:
			output = output << 1

			msb = (get_msb(output) ^ 0x7798)
			lsb = (get_lsb(output) ^ 0x2990)

			output = (msb << 16) | lsb
			
		index += 1

	return output % 1000000
