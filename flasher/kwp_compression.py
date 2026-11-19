"""Optional OpenGK KWP compression; stock firmware keeps ordinary transfers.

SID23 extension: address(3, BE), maximum output(2, BE), 'LZ'.
SID34 uses vendor compression nibble F. Each SID36 block, and each extended
SID23 reply, contains: 'L', codec(0=raw, 1=LZSS), size(LE16), CRC16(LE16), data.
Read lengths are adaptive; writes have no sequence counter and are never replayed.
"""
from enum import Enum
import struct

from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000
from gkbus.protocol.kwp2000.kwp2000_command import Kwp2000Command

from .lzss import BLOCK_SIZE, encode, decode
from .mtos_asm import crc16_mtos

CAPABILITY_ADDRESS = 0xE5E14
CAPABILITY = b'OGKLZS01'
PACKED_LIMIT = 248
HEADER = struct.Struct('<BBHH')


class Compression(Enum):
	LZSS = 0xF  # OpenGK vendor-defined format, not a standard KWP algorithm ID.


class ReadCompressedMemory(Kwp2000Command):
	service_identifier = 0x23

	def init(self, address: int, size: int):
		if not 1 <= size <= BLOCK_SIZE:
			raise ValueError('Invalid compressed-read size')
		self.set_data(address.to_bytes(3, 'big') + size.to_bytes(2, 'big') + b'LZ')


def available(ecu) -> bool:
	state = getattr(ecu, 'kwp_compression', None)
	if state is None:
		try:
			data = ecu.bus.execute(kwp2000.commands.ReadMemoryByAddress(
				CAPABILITY_ADDRESS, len(CAPABILITY))).get_data()
			state = bytes(data) == CAPABILITY
		except (kwp2000.Kwp2000NegativeResponseException, TimeoutException):
			state = False
		ecu.kwp_compression = state
	return state


def unpack(packet: bytes, maximum: int) -> bytes:
	if len(packet) < HEADER.size:
		raise IOError('Truncated compressed KWP header')
	magic, codec, size, crc = HEADER.unpack_from(packet)
	if magic != ord('L') or codec not in (0, 1) or not 0 < size <= maximum:
		raise IOError('Invalid compressed KWP header')
	data = packet[HEADER.size:]
	try:
		data = decode(data, size) if codec else data
	except ValueError as exc:
		raise IOError('Invalid compressed KWP data: {}'.format(exc)) from exc
	if len(data) != size or crc16_mtos(data, seed=0xFFFF) != crc:
		raise IOError('Compressed KWP length or CRC mismatch')
	return bytes(data)


def read(ecu, address: int, maximum: int) -> bytes:
	packet = ecu.bus.execute(ReadCompressedMemory(address, maximum)).get_data()
	return unpack(bytes(packet), maximum)


def packets(data: bytes):
	"""Prefer larger logical blocks only when their encoding fits one KWP frame."""
	position = 0
	while position < len(data):
		remaining = len(data) - position
		for size in sorted({min(remaining, n) for n in (BLOCK_SIZE, 1024, 512, PACKED_LIMIT)}, reverse=True):
			block = data[position:position + size]
			packed = encode(block)
			if len(packed) <= PACKED_LIMIT and len(packed) < len(block):
				codec = 1
				break
		else:
			block = data[position:position + PACKED_LIMIT]
			packed, codec = block, 0
		header = HEADER.pack(ord('L'), codec, len(block), crc16_mtos(block, seed=0xFFFF))
		yield header + packed, len(block)
		position += len(block)
