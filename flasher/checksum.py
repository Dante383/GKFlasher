import crcmod

offset_address = 0x010000
init_address = 0x01000C
cks_address = 0x017EE0
flag_address = 0x017EFE # (inhibit)

bin_offset = -0x080000

def read_and_reverse (payload, start, length):
	bts = list(payload[start:start+length])
	bts.reverse()
	return bts

def concat_3_bytes (payload):
	return ( (payload[0] << 8 | payload[1]) << 8 | payload[2])

def checksum (payload, start, stop, init):
	crc16 = crcmod.mkCrcFun(0x18005, initCrc=init)

	checksum = crc16(payload[start:stop])
	return checksum

def fix_checksum (filename):
	print('[*] Reading {}'.format(filename))

	with open(filename, 'rb') as file:
		payload = file.read()

	print('[*] Trying to find addresses of Zone1.. ', end='')
	zone1_start_offset = cks_address+0x04
	zone1_start = concat_3_bytes(read_and_reverse(payload, zone1_start_offset, 3)) + bin_offset

	zone1_stop_offset = cks_address+0x08
	zone1_stop = concat_3_bytes(read_and_reverse(payload, zone1_stop_offset, 3)) + bin_offset + 1
	print('{} - {}'.format(hex(zone1_start), hex(zone1_stop)))

	print('[*] Trying to find initial value.. ', end='')
	initial_value_bytes = read_and_reverse(payload, init_address, 2)
	initial_value = (initial_value_bytes[0]<< 8) | initial_value_bytes[1]
	print(hex(initial_value))

	print('[*] checksum of zone1: ', end='')
	zone1_cks = checksum(payload, zone1_start, zone1_stop, initial_value)
	print(hex(zone1_cks))

	print('[*] Trying to find addresses of Zone2.. ',end='')
	zone2_start_offset = cks_address+0xC
	zone2_start = concat_3_bytes(read_and_reverse(payload, zone2_start_offset, 3)) + bin_offset

	zone2_stop_offset = cks_address+0x10
	zone2_stop = concat_3_bytes(read_and_reverse(payload, zone2_stop_offset, 3)) + bin_offset + 1
	print('{} - {}'.format(hex(zone2_start), hex(zone2_stop)))

	print('[*] checksum of zone2: ',end='')
	zone2_cks = checksum(payload, zone2_start, zone2_stop, zone1_cks)
	print(hex(zone2_cks))

	checksum_b1 = (zone2_cks >> 8) & 0xFF
	checksum_b2 = (zone2_cks & 0xFF)
	checksum_reversed = (checksum_b2 << 8) | checksum_b1

	current_checksum = int.from_bytes(payload[cks_address:cks_address+2], "big")

	print('[*] OK! Current checksum: {}, new checksum: {}'.format(hex(current_checksum), hex(checksum_reversed)))

	if (input('[?] Save to {}? [y/n]: '.format(filename)) == 'y'):
		with open(filename, 'rb+') as file:
			file.seek(cks_address)
			file.write(checksum_reversed.to_bytes(2, "big"))
		print('[*] Done!')
