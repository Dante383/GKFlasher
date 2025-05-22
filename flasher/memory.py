import logging
from math import ceil
from gkbus.protocol import kwp2000
from gkbus.protocol.kwp2000.commands import ReadMemoryByAddress, WriteMemoryByAddress, RequestDownload, TransferData, RequestTransferExit
from gkbus.protocol.kwp2000.enums import CompressionType, EncryptionType
from gkbus.hardware import TimeoutException
from .ecu import ECU
logger = logging.getLogger(__name__)

page_size_b = 16384

# This function rounds upto the nearest multiple. 
# KWP frames are 256 bytes and the FTDI buffer 512 bytes.
# This prevents an overflow situation when writing different sized binaries.
def round_to_multiple(number: int, multiple: int) -> int:  
        return multiple * ceil(number / multiple)

def dynamic_find_end (payload):
	end_offset = len(payload)-1

	try:
		while payload[end_offset] == 0xFF:
			end_offset -= 1
	except IndexError:
		pass

	return round_to_multiple(end_offset, 254)

def read_page_16kib(ecu: ECU, offset: int, at_a_time: int = 254, progress_callback=False) -> bytearray:
	address_start = offset
	address_stop = offset+page_size_b
	address = address_start

	payload = bytearray([0xFF]*(address_stop-address_start))

	while True:
		if ( (address_stop-address) < at_a_time ):
			at_a_time = (address_stop-address)

		try:
			fetched = ecu.read_memory_by_address(offset=address, size=at_a_time)
		except kwp2000.Kwp2000NegativeResponseException as e:
			logger.warning('Negative KWP response at offset %s! Filling requested section with 0xF. %s', hex(address), e)
			fetched = bytes()
		except TimeoutException:
			logger.warning('Timeout at Offset %s! Trying again...', hex(address))
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
def read_memory(ecu: ECU, address_start: int, address_stop: int, progress_callback=False) -> bytearray:#, progress_callback):
	requested_size = address_stop-address_start
	pages = int(requested_size/page_size_b) # 16kib per page 
	buffer = bytearray([0xFF]*requested_size)
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

def write_memory(ecu: ECU, payload: bytes, flash_start: int, flash_size: int, progress_callback=False) -> None:
	ecu.bus.execute(
		RequestDownload(
			offset=flash_start, 
			size=flash_size, 
			compression_type=CompressionType.UNCOMPRESSED, 
			encryption_type=EncryptionType.UNENCRYPTED
		)
	)

	packets_to_write = int(flash_size/254)
	if (flash_size % 254 != 0):
		packets_to_write += 1

	for packets_written in range(packets_to_write):
		if (progress_callback):
			progress_callback.title('Packet {}/{}'.format(packets_written, packets_to_write))

		payload_packet_start = packets_written*254
		payload_packet_end = payload_packet_start+254
		payload_packet = payload[payload_packet_start:payload_packet_end]

		while True:
			try:
				ecu.bus.execute(TransferData(payload_packet))
				break
			except TimeoutException:
				logger.warning('Timeout at Block %s! Trying again...', packets_written)
				continue
		
		if (progress_callback):
			progress_callback(len(payload_packet))

	ecu.bus.execute(RequestTransferExit())