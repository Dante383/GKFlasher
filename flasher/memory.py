import logging
from gkbus.kwp.commands import ReadMemoryByAddress, WriteMemoryByAddress, RequestDownload, TransferData, RequestTransferExit
from gkbus.kwp.enums import CompressionType, EncryptionType
from gkbus.kwp import KWPNegativeResponseException
from gkbus import GKBusTimeoutException
from math import ceil
logger = logging.getLogger(__name__)

page_size_b = 16384

# This function rounds upto the nearest multiple. 
# KWP frames are 256 bytes and the FTDI buffer 512 bytes.
# This prevents an overflow situation when writing different sized binaries.
def round_to_multiple(number, multiple):  
        return multiple * ceil(number / multiple)

def dynamic_find_end (payload):
	payload_len = len(payload)
	end_offset = payload_len
	for index, x in enumerate(reversed(payload)):
		if x == 0xFF:
			end_offset = (round_to_multiple(payload_len-index, 254))
		else:
			break
	return end_offset

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
			fetched = ecu.read_memory_by_address(offset=address, size=at_a_time)
		except KWPNegativeResponseException as e:
			logger.warning('Negative KWP response at offset %s! Filling requested section with 0xF. %s', hex(address), e)
			fetched = []
		except GKBusTimeoutException:
			logger.warning('Timeout at offset %s! Trying again..', hex(address))
			continue

		payload_start = address-address_start
		payload_stop = payload_start+len(fetched)
		payload[payload_start:payload_stop] = fetched

		address += at_a_time

		if (progress_callback):
			progress_callback(at_a_time)

		if (address >= address_stop):
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
				progress_callback.title('Page {}/{}, offset {}'.format(page+1, pages, hex(address)))

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

def write_memory(ecu, payload, flash_start, flash_size, progress_callback=False):
	ecu.bus.execute(RequestDownload(offset=flash_start, size=flash_size, compression_type=CompressionType.UNCOMPRESSED, encryption_type=EncryptionType.UNENCRYPTED))

	packets_to_write = int(flash_size)/254
	packets_written = 0

	while packets_to_write > packets_written:
		if (progress_callback):
			progress_callback.title('Packet {}/{}'.format(packets_written+1, packets_to_write))

		payload_packet_start = packets_written*254
		payload_packet_end = payload_packet_start+254
		payload_packet = payload[payload_packet_start:payload_packet_end]

		while True:
			try:
				ecu.bus.execute(TransferData(list(payload_packet)))
				break
			except (GKBusTimeoutException):
				print('Timed Out! Trying again...')
				continue
		packets_written += 1

		if (progress_callback):
			progress_callback(len(payload_packet))

	ecu.bus.execute(RequestTransferExit())
