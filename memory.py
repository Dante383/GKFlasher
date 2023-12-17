import logging
from gkbus.kwp.commands import ReadMemoryByAddress, WriteMemoryByAddress
from gkbus.kwp import KWPNegativeResponseException
logger = logging.getLogger(__name__)

page_size_b = 16384

def read_page_16kib(bus, offset, at_a_time=254, progress_callback=False):
	address_start = offset
	address_stop = offset+page_size_b
	address = address_start

	payload = [0xF]*(address_stop-address_start)

	while True:
		if ( (address_stop-address) < at_a_time ):
			at_a_time = (address_stop-address)

		try:
			fetched = bus.execute(ReadMemoryByAddress(offset=address, size=at_a_time)).get_data()[:at_a_time] # last 3 bytes are zeros
		except KWPNegativeResponseException as e:
			logger.warning('Negative KWP response at offset %s! Filling requested section with 0xF. %s', hex(address), e)
			fetched = []

		payload_start = address-address_start
		payload_stop = payload_start+len(fetched)
		payload[payload_start:payload_stop] = fetched

		address += at_a_time

		if (progress_callback):
			progress_callback(at_a_time)

		if (address == address_stop):
			break
	return payload

ECU_IDENTIFICATION_TABLE = [
	{
		'name': 'SIMK43 8mbit',
		'offset': 0xA00A0,
		'expected': [54, 54, 51, 54],
		'size_bytes': 1048575,
		'size_human': 8,
		'description_offset': 0x90040,
		'calibration_offset': 0x90000
	},
	{
		'name': 'SIMK43 V6 4mbit',
		'offset': 0x88040,
		'expected': [99, 97, 54, 53],
		'size_bytes': 524287,
		'size_human': 4,
		'description_offset': 0x88040,
		'calibration_offset': 0x88000
	},
	{
		'name': 'SIMK43 2.0 4mbit',
		'offset': 0x90040,
		'expected': [99, 97, 54, 54],
		'size_bytes': 524287,
		'size_human': 4,
		'description_offset': 0x90040,
		'calibration_offset': 0x90000
	},
	{
		'name': 'SIMK41 2mbit',
		'offset': 0x48040,
		'expected': [99, 97, 54, 54],
		'size_bytes': 262143,
		'size_human': 2,
		'description_offset': 0x48000,
		'calibration_offset': 0x48040
	}
]

def find_eeprom_size_and_calibration (bus):
	size_bytes, size_human, description, calibration = 0, 0, '', ''

	for ecu in ECU_IDENTIFICATION_TABLE:
		try:
			result = bus.execute(ReadMemoryByAddress(offset=ecu['offset'], size=4)).get_data()
		except KWPNegativeResponseException:
			continue
		if result == ecu['expected']:
			size_bytes = ecu['size_bytes']
			size_human = ecu['size_human']
			description = bus.execute(ReadMemoryByAddress(offset=ecu['description_offset'], size=8)).get_data()
			calibration = bus.execute(ReadMemoryByAddress(offset=ecu['calibration_offset'], size=8)).get_data()

	if size_bytes == 0:
		raise Exception('Failed to identify ECU!')
			
	description = ''.join([chr(x) for x in description])
	calibration = ''.join([chr(x) for x in calibration])
	return (size_bytes, size_human, description, calibration)

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
