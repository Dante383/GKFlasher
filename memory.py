import logging
from gkbus.kwp.commands import ReadMemoryByAddress, WriteMemoryByAddress
from gkbus.kwp import KWPNegativeResponseException
logger = logging.getLogger(__name__)

page_size_b = 16384

def read_page_16kib(ecu, offset, at_a_time=254, progress_callback=False):
	address_start = offset
	address_stop = offset+page_size_b
	address = address_start

	payload = [0xFF]*(address_stop-address_start)
	og_at_a_time = at_a_time

	while True:
		at_a_time = ecu.adjust_bytes_at_a_time(address, at_a_time, og_at_a_time)
		if ( (address_stop-address) < at_a_time ):
			at_a_time = (address_stop-address)

		try:
			fetched = ecu.bus.execute(ReadMemoryByAddress(offset=address, size=at_a_time)).get_data()[:at_a_time] # last 3 bytes are zeros
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


# read memory into a buffer
# this function only cares about reading from address_start to address_stop. 
# it doesn't pad the read with 0xFFs or anything. If you request to read, for example,
# the calibration zone, from 0x090000 to 0x094000 (16364 bytes) - then you'll only
# get 16364 bytes back. 
def read_memory(ecu, address_start, address_stop, progress_callback=False):#, progress_callback):
	requested_size = address_stop-address_start
	pages = int(requested_size/page_size_b) # 16kib per page 
	buffer = [0xFF]*requested_size
	address = address_start

	try:
		page = 0
		while True:
			if (progress_callback):
				progress_callback.title('Page {}/{}, offset {}'.format(page+1, pages+1, hex(address)))

			fetched = read_page_16kib(ecu, offset=address, progress_callback=progress_callback)
			
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
