import crcmod, sys

cks_types = [ # todo: incorporate into ECU definitions
	{
        'name': '2mbit',
        'identification_flag_address': 0xFEFE,
        'regions': [
        	{
	        	'name': 'Calibration', 
		        'flag_address': 0xFEFE,
		        'init_address': 0x00800C,
		        'cks_address': 0x0FEE0,
		        'bin_offset': -0x88000
		    },
		    {
		    	'name': 'Program',
		    	'flag_address': 0xFEFE,
		    	'init_address': 0x010052,
		    	'cks_address': 0x010010,
		    	'bin_offset': -0x88000
		    }
	    ]
	},
	{
        'name': '4mbit',
        'identification_flag_address': 0x017EFE,
        'regions': [
        	{
		        'name': 'Calibration',
		        'flag_address': 0x017EFE,
		        'init_address': 0x01000C,
		        'cks_address': 0x017EE0,
		        'bin_offset': -0x080000
		    },
		    {
		    	'name': 'Program',
		    	'flag_address': 0x17EFE,
		    	'init_address': 0x020052,
		    	'cks_address': 0x020010,
		    	'bin_offset': -0x080000
		    }
	    ]
	},
	{
        'name': 'v6',
        'identification_flag_address': 0xDEFE,
        'regions': [
        	{
	        	'name': 'Calibration',
		        'flag_address': 0xDEFE,
		        'init_address': 0x0800C,
		        'cks_address': 0xDEE0,
		        'bin_offset': -0x88000
		    },
		    {
		    	'name': 'Program',
		    	'flag_address': 0xDEFE,
		    	'init_address': 0x010052,
		    	'cks_address': 0x010010,
		    	'bin_offset': -0x88000
		    }
	    ]
	},
	{
        'name': '8mbit',
        'identification_flag_address': 0x97EFE,
        'regions': [
        	{
	        	'name': 'Calibration',
		        'flag_address': 0x97EFE,
		        'init_address': 0x09000C,
		        'cks_address': 0x097EE0,
		        'bin_offset': 0
	        },
		    {
		    	'name': 'Program',
				'flag_address': 0x97EFE,
		    	'init_address': 0x0A0052,
		    	'cks_address': 0x0A0010,
		    	'bin_offset': 0
		    }
	    ]
    }
]

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

def detect_offsets (payload):
	for cks_type in cks_types:
		flag = payload[cks_type['identification_flag_address']:cks_type['identification_flag_address']+2]
		if (flag == b'OK'):
			return cks_type # todo: unpack

def correct_checksum (filename):
	print('[*] Reading {}'.format(filename))

	with open(filename, 'rb') as file:
		payload = file.read()

	print('[*] Trying to detect type.. ', end='')
	cks_type = detect_offsets(payload)
	print(cks_type['name'])

	for region in cks_type['regions']:
		print('[*] Calculating checksum for region {}'.format(region['name']))
		flag_address, init_address, cks_address, bin_offset = region['flag_address'], region['init_address'], region['cks_address'], region['bin_offset']

		amount_of_zones = int.from_bytes(payload[cks_address+2:cks_address+3], "big")
		print('[*] Amount of zones: {}'.format(amount_of_zones))

		checksums = []

		zone_address = cks_address
		for zone_index in range(amount_of_zones):

			print('[*] Trying to find addresses of zone #{}.. '.format(zone_index+1), end='')
			zone_start_offset = zone_address+0x04
			zone_start = concat_3_bytes(read_and_reverse(payload, zone_start_offset, 3)) + bin_offset

			zone_stop_offset = zone_address+0x08
			zone_stop = concat_3_bytes(read_and_reverse(payload, zone_stop_offset, 3)) + bin_offset + 1
			print('{} - {}'.format(hex(zone_start), hex(zone_stop)))

			print('[*] Trying to find initial value.. ', end='')
			if (zone_index == 0):
				initial_value_bytes = read_and_reverse(payload, init_address, 2)
				initial_value = (initial_value_bytes[0]<< 8) | initial_value_bytes[1]
			else:
				initial_value = checksums[zone_index-1]
			print(hex(initial_value))

			print('[*] checksum of zone #{}: '.format(zone_index+1), end='')
			zone_cks = checksum(payload, zone_start, zone_stop, initial_value)
			print(hex(zone_cks))
			checksums.append(zone_cks)
			zone_address += 0x08

		checksum_b1 = (checksums[-1] >> 8) & 0xFF
		checksum_b2 = (checksums[-1] & 0xFF)
		checksum_reversed = (checksum_b2 << 8) | checksum_b1

		current_checksum = int.from_bytes(payload[cks_address:cks_address+2], "big")
		region['checksum'] = checksum_reversed

		print('[*] OK! Current {} checksum: {}, new checksum: {}'.format(region['name'], hex(current_checksum), hex(checksum_reversed)))

	if (input('[?] Save to {}? [y/n]: '.format(filename)) == 'y'):
		with open(filename, 'rb+') as file:
			for region in cks_type['regions']:
				file.seek(region['cks_address'])
				file.write(region['checksum'].to_bytes(2, "big"))
		print('[*] Done!')

	sys.exit(1)
