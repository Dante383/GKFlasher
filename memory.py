from kwp.commands.ReadMemoryByAddress import ReadMemoryByAddress
from kwp.commands.WriteMemoryByAddress import WriteMemoryByAddress

page_size_b = 16384

def read_page_16kib(bus, offset, at_a_time=254, progress_callback=False):
	address_start = offset
	address_stop = offset+page_size_b
	address = address_start

	payload = [0xF]*(address_stop-address_start)

	while True:
		if ( (address_stop-address) < at_a_time ):
			at_a_time = (address_stop-address)

		fetched = bus.execute(ReadMemoryByAddress(offset=address, size=at_a_time)).get_data()[:at_a_time] # last 3 bytes are zeros
		payload_start = address-address_start
		payload_stop = payload_start+len(fetched)
		payload[payload_start:payload_stop] = fetched

		address += at_a_time

		if (progress_callback):
			progress_callback(at_a_time)

		if (address == address_stop):
			break
	return payload

def find_eeprom_size_and_calibration (bus):
	size_bytes, size_human, calibration = 0, 0, ''

	if (bus.execute(ReadMemoryByAddress(offset=0x090040, size=4)).get_data() == [99, 97, 54, 54]): # 8 MiB (mebibyte)
		size_bytes = 1048575
		size_human = 8
		calibration = bus.execute(ReadMemoryByAddress(offset=0x090040, size=8)).get_data()
	elif (bus.execute(ReadMemoryByAddress(offset=0x010008, size=4)).get_data() == [99, 97, 54, 54]): # 4 MiB (mebibyte)
		size_bytes = 524287 # is that correct? verify
		size_human = 4
		calibration = bus.execute(ReadMemoryByAddress(offset=0x010008, size=8)).get_data()
	elif (bus.execute(ReadMemoryByAddress(offset=0x008008, size=4)).get_data() == [99, 97, 54, 54]): # 2 MiB (mebibyte)
		size_bytes = 262143 # is that correct? verify
		size_human = 2
		calibration = bus.execute(ReadMemoryByAddress(offset=0x008008, size=8)).get_data()
	calibration = ''.join([chr(x) for x in calibration])
	return (size_bytes, size_human, calibration)

# read memory into a buffer
# this function only cares about reading from address_start to address_stop. 
# it doesn't pad the read with 0xFFs or anything. If you request to read, for example,
# the calibration zone, from 0x090000 to 0x094000 (16364 bytes) - then you'll only
# get 16364 bytes back. 
def read_memory(bus, address_start, address_stop, progress_callback=False):#, progress_callback):
	requested_size = address_stop-address_start
	pages = int(requested_size/page_size_b) # 16kib per page 
	buffer = [0xF]*requested_size
	address = address_start

	try:
		page = 0
		while True:
			if (progress_callback):
				progress_callback.title('Page {}/{}, offset {}'.format(page+1, pages+1, hex(address)))

			fetched = read_page_16kib(bus, offset=address, progress_callback=progress_callback)
			
			buffer_start = (address-address_start)
			buffer_end = buffer_start + len(fetched)
			buffer[buffer_start:buffer_end] = fetched
			
			address += page_size_b # 16kib per page 
			
			if (address >= address_stop):
				break

			page +=1

	except KeyboardInterrupt:
		pass

	return buffer
