"""Bounded LZSS blocks and CRC-checked RAM helpers shared by flash transports."""
from collections import defaultdict
import struct
from dataclasses import dataclass

from .kernel import DRIVER_ENTRY, FLASH_READ_BASE, DATA_BUFFER, Flash, KernelError, Progress
from .mtos_asm import _assemble, crc16_mtos

OUTPUT_ADDRESS = 0xC400  # C167CS 6 KiB XRAM; the application is stopped.
INPUT_ADDRESS = 0xCC00
BLOCK_SIZE = 0x800
CODE_ADDRESS = 0xE600  # Above the CAN shim, below its state at E7C0.


def encode(data):
	"""Eight LSB-first flags: literal=1; match=0, followed by distance/length."""
	positions = defaultdict(list)
	out = bytearray()
	cursor = 0
	while cursor < len(data):
		flag_at = len(out)
		out.append(0)
		for bit in range(8):
			if cursor == len(data):
				break
			length = distance = 0
			maximum = min(66, len(data) - cursor)
			if maximum >= 3:
				for previous in reversed(positions[data[cursor:cursor + 3]][-128:]):
					if cursor - previous > 1024:
						break
					match = 3
					while match < maximum and data[previous + match] == data[cursor + match]:
						match += 1
					if match > length:
						length, distance = match, cursor - previous
					if length == maximum:
						break
			if length >= 3:
				out.extend(struct.pack('<H', distance - 1 | (length - 3) << 10))
				advance = length
			else:
				out[flag_at] |= 1 << bit
				out.append(data[cursor])
				advance = 1
			for index in range(cursor, cursor + advance):
				if index + 3 <= len(data):
					positions[data[index:index + 3]].append(index)
			cursor += advance
	return bytes(out)


def decode(data: bytes, size: int) -> bytes:
	"""Reject truncated tokens, invalid history, overruns and trailing input."""
	if not 0 <= size <= BLOCK_SIZE:
		raise ValueError('Invalid LZSS output size')
	out = bytearray()
	cursor = 0
	while len(out) < size:
		if cursor == len(data):
			raise ValueError('Truncated LZSS flags')
		flags = data[cursor]
		cursor += 1
		for bit in range(8):
			if len(out) == size:
				break
			count = 1 if flags & (1 << bit) else 2
			if cursor + count > len(data):
				raise ValueError('Truncated LZSS token')
			if count == 1:
				out.append(data[cursor])
			else:
				word = int.from_bytes(data[cursor:cursor + 2], 'little')
				distance, length = (word & 1023) + 1, (word >> 10) + 3
				if distance > len(out) or len(out) + length > size:
					raise ValueError('LZSS history or output range invalid')
				for _ in range(length):
					out.append(out[-distance])
			cursor += count
	if cursor != len(data):
		raise ValueError('Trailing LZSS input')
	return bytes(out)


@dataclass(frozen=True)
class CodecLayout:
	output: int
	input: int
	block_size: int
	input_capacity: int
	code: int
	code_limit: int
	extended_xram: bool = True
	encoder: bool = False
	label: str = 'MTOS'


MTOS_LAYOUT = CodecLayout(OUTPUT_ADDRESS, INPUT_ADDRESS, BLOCK_SIZE, BLOCK_SIZE, CODE_ADDRESS, 0xE7C0)
# Standard 2 KiB XRAM: output E000:E3FF, code E400:E7FF; stock input FC00:FDFF.
BSL_LAYOUT = CodecLayout(0xE000, DATA_BUFFER, 0x400, 0x200, 0xE400, 0xE800,
	extended_xram=False, encoder=True, label='BSL')


class RamCodec:
	"""Qualify scratch RAM and keep encoded data separate from executable code."""

	def __init__(self, kernel, layout: CodecLayout):
		self.kernel, self.layout = kernel, layout
		self.entries = {}

	def upload(self):
		kernel, layout = self.kernel, self.layout
		if not kernel.read_word(0xFF12) & 4:
			return
		if layout.extended_xram and not kernel.read_word(0xF024) & 2:
			return
		result = _assemble('lzss_codec', source_directory='assets', CODE_ADDRESS=layout.code,
			WITH_ENCODER=int(layout.encoder), WITH_PROGRAM=1, DRIVER=DRIVER_ENTRY)
		code = result.image()
		if code.base != layout.code or code.base + len(code.data) > layout.code_limit:
			raise KernelError('The LZSS helper exceeds its RAM allocation')
		buffers = ((layout.output, layout.block_size), (layout.input, layout.input_capacity))
		probes = [(address, (bytes(range(256)) * ((size + 255) // 256))[:size])
			for address, size in buffers]
		probes[1] = (probes[1][0], bytes(value ^ 255 for value in probes[1][1]))
		for address, data in (*probes, (layout.code, code.data)):
			kernel.write_memory(address, data)
		for address, data in (*probes, (layout.code, code.data)):
			if kernel.read_memory(address, len(data)) != data:
				raise KernelError('{} LZSS RAM verification failed; flash has not been erased'.format(layout.label))
		self.entries = {name: symbol.value for name, symbol in result.symbols.items()
			if name in ('Encode', 'Decode', 'Program')}

	def read_memory(self, address, size, progress=None):
		if 'Encode' not in self.entries:
			return self.kernel.read_memory(address, size, progress)
		out = bytearray()
		while len(out) < size:
			self.kernel.check_stop()
			source = address + len(out)
			count = min(BLOCK_SIZE, size - len(out), 0x10000 - (source & 0xFFFF))
			result = self.kernel.call(self.entries['Encode'], [source & 0xFFFF, source >> 16,
				count, self.layout.output, self.layout.block_size, 0, 0, 0])
			consumed, packed_size, crc = result[:3]
			if result[7] or not 0 < consumed <= count or not 0 < packed_size <= self.layout.block_size:
				raise KernelError('Invalid compressed-read result')
			if packed_size >= consumed:
				data = self.kernel.read_memory(source, count)
			else:
				packed = self.kernel.read_memory(self.layout.output, packed_size)
				data = decode(packed, consumed)
				if crc16_mtos(data, seed=0xFFFF) != crc:
					raise KernelError('Compressed-read CRC mismatch')
			out.extend(data)
			if progress:
				progress(len(out))
		return bytes(out)


class CompressedFlash(Flash):
	"""Retain the stock flash driver, erase ordering and readback verification."""

	def __init__(self, kernel, driver, layout=MTOS_LAYOUT):
		super().__init__(kernel, driver)
		self.codec = RamCodec(kernel, layout)

	def upload(self):
		super().upload()
		self.codec.upload()
		if 'Program' in self.codec.entries:
			self.buffer_address = self.codec.layout.output
			self.program_block_size = self.codec.layout.block_size
			self.log('[*] {} programming: {}-byte blocks with LZSS/raw selection'.format(
				self.codec.layout.label, self.program_block_size))

	def read(self, label):
		if 'Encode' not in self.codec.entries:
			return super().read(label)
		progress = Progress(self.kernel.progress_callback, self.chip.size, label)
		try:
			return self.codec.read_memory(FLASH_READ_BASE, self.chip.size, progress)
		finally:
			progress.finish()

	def _program_block(self, offset, data):
		if 'Program' not in self.codec.entries:
			return super()._program_block(offset, data)
		packed = encode(data)
		layout = self.codec.layout
		if len(packed) > min(layout.input_capacity, len(data) - max(64, len(data) // 8)):
			return super()._program_block(offset, data)
		address = FLASH_READ_BASE + offset
		self.kernel.write_memory(layout.input, packed)
		registers = [layout.input, len(packed), layout.output, len(data),
			crc16_mtos(data, seed=0xFFFF), address & 0xFFFF, address >> 16, 0]
		return self.kernel.call(self.codec.entries['Program'], registers)[7]
