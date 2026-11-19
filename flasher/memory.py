import logging
from dataclasses import replace
from math import ceil
from gkbus.protocol import kwp2000
from gkbus.protocol.kwp2000.commands import ReadMemoryByAddress, WriteMemoryByAddress, RequestDownload, TransferData, RequestTransferExit
from gkbus.protocol.kwp2000.enums import CompressionType, EncryptionType
from gkbus.exceptions import TimeoutException
from gkbus.device import SerialKLineChannel
from .ecu import ECU
logger = logging.getLogger(__name__)

page_size_b = 16384

# Public program-space capability, sampled before erase revokes read access.
FAST_KWP_ADDRESS = 0xE5E0C
FAST_KWP_MARKER = b'OGKKWP06'


def prepare_write_memory(ecu: ECU) -> None:
	"""Remember receiver support before the ECU enters its protected boot loop."""
	from .kwp_compression import available
	available(ecu)
	ecu.fast_kwp_write = False
	channel = getattr(ecu.bus.transport, 'channel', None)
	if (isinstance(channel, SerialKLineChannel) and channel.get_baudrate() >= 107000
			and hasattr(channel.get_timing(), 'p4_burst_size')):
		try:
			ecu.fast_kwp_write = ecu.read_memory_by_address(FAST_KWP_ADDRESS, len(FAST_KWP_MARKER)) == FAST_KWP_MARKER
		except (kwp2000.Kwp2000NegativeResponseException, TimeoutException):
			pass


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

def prepare_flash_payloads(ecu: ECU, image: bytes, flash_calibration=True, flash_program=True) -> dict[str, bytes]:
	"""Validate all requested SIMK regions before any flash erase is allowed."""
	if not image:
		raise ValueError('BIN file is empty.')
	if len(image) != ecu.get_eeprom_size_bytes():
		raise ValueError('BIN size does not match the selected ECU. Check the ECU type and BIN file.')

	payloads = {}
	for name, selected in (('program', flash_program), ('calibration', flash_calibration)):
		if not selected:
			continue
		region = ecu.get_region(name).write
		start = ecu.calculate_bin_offset(region.address)
		stop = start + region.size
		start += 16 if name == 'program' else 0
		stop -= 16 if name == 'calibration' else 0
		if not 0 <= start < stop <= len(image):
			raise ValueError('BIN does not contain the selected {} region. Check the ECU type.'.format(name))
		section = image[start:stop]
		payload = section[:max(0, dynamic_find_end(section))]
		if not payload or not any(payload) or all(byte == 0xFF for byte in payload):
			raise ValueError('The selected {} region is empty. Check the ECU type and BIN file.'.format(name))
		payloads[name] = payload
	return payloads


def read_page_16kib(ecu: ECU, offset: int, at_a_time: int = 254, progress_callback=False) -> bytearray:
	from . import kwp_compression
	compressed = kwp_compression.available(ecu) and 0x80000 <= offset < 0x100000
	address_start = offset
	address_stop = offset+page_size_b
	address = address_start

	payload = bytearray([0xFF]*(address_stop-address_start))
	read_failures = 0

	while True:
		if compressed:
			at_a_time = min(kwp_compression.BLOCK_SIZE, address_stop - address)
		if ( (address_stop-address) < at_a_time ):
			at_a_time = (address_stop-address)

		try:
			fetched = (kwp_compression.read(ecu, address, at_a_time) if compressed else
				ecu.read_memory_by_address(offset=address, size=at_a_time))
		except kwp2000.Kwp2000NegativeResponseException as e:
			if compressed:
				raise
			logger.warning('Negative KWP response at offset %s! Filling requested section with 0xF. %s', hex(address), e)
			fetched = bytes()
		except TimeoutException:
			read_failures += 1
			if read_failures >= 3:
				raise
			logger.warning('Timeout at Offset %s! Trying again...', hex(address))
			continue
		read_failures = 0
		if compressed:
			at_a_time = len(fetched)

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
		raise

	return buffer

def write_memory(ecu: ECU, payload: bytes, flash_start: int, flash_size: int, progress_callback=False) -> None:
	from . import kwp_compression
	compressed = kwp_compression.available(ecu)
	channel = getattr(ecu.bus.transport, 'channel', None)
	original_timing = channel.get_timing() if isinstance(channel, SerialKLineChannel) else None
	transfer_timing = None
	if original_timing is not None:
		if channel.get_baudrate() > 20000 and getattr(ecu, 'fast_kwp_write', False):
			# Only TransferData uses bursts; setup and exit keep normal KWP timing.
			transfer_timing = replace(original_timing, p4_min=max(0.001, original_timing.p4_min),
				p4_max=18.0, p4_prefix=9, p4_burst_size=254)
		else:
			# Stock programming accepts contiguous payloads at its supported baudrate.
			# Per-byte USB echo waits throttle transfers and can exceed the byte-gap limit.
			transfer_timing = replace(original_timing, p4_min=0, p4_max=None, p4_prefix=None)

	try:
		if original_timing is not None:
			# Erase enters the boot driver. Use its normal turnaround for setup;
			# bulk payload timing is selected below.
			channel.set_timing(replace(original_timing,
				p3_min=max(original_timing.p3_min, channel.default_timing.p3_min)))
		try:
			ecu.bus.execute(RequestDownload(offset=flash_start, size=flash_size,
				compression_type=kwp_compression.Compression.LZSS if compressed else CompressionType.UNCOMPRESSED,
				encryption_type=EncryptionType.UNENCRYPTED))
		except kwp2000.Kwp2000NegativeResponseException as exc:
			if not compressed or exc.status.identifier != 0x22:
				raise
			# A capability-bearing firmware can still lack the extended programming RAM.
			compressed = False
			ecu.bus.execute(RequestDownload(offset=flash_start, size=flash_size,
				compression_type=CompressionType.UNCOMPRESSED, encryption_type=EncryptionType.UNENCRYPTED))
		if original_timing is not None:
			channel.set_timing(transfer_timing or original_timing)
		packets = (list(kwp_compression.packets(payload)) if compressed else
			[(payload[start:start + 254], len(payload[start:start + 254])) for start in range(0, flash_size, 254)])
		for packets_written, (packet, size) in enumerate(packets):
			if progress_callback:
				progress_callback.title('Packet {}/{}'.format(packets_written + 1, len(packets)))
			# No sequence counter: a lost acknowledgement may still mean data was
			# programmed. Replaying it could advance the ECU cursor a second time.
			ecu.bus.execute(TransferData(packet))
			if progress_callback:
				progress_callback(size)
	finally:
		if original_timing is not None:
			channel.set_timing(original_timing)
	ecu.bus.execute(RequestTransferExit())
