"""
MTOS payload tooling for Siemens SIMK43 (Infineon C167) ECUs.

MTOS ("MT" OS) is a stock Siemens diagnostic feature that lets a tester upload a
short routine into RAM at 0xC000 and execute it via KWP2000
StartRoutineByLocalIdentifier(0xFA). This module provides:

  * crc16_mtos()  - the exact checksum the firmware validates the blob with
  * build_blob()  - wrap a body of machine code in the MTOS header the
                    firmware expects (signature, 'MT' magic, CRC, CRC range)
  * ready-made payload/loader generators, assembled with openc166 from the
    Keil A166 sources in assets/mtos

MTOS execution contract (reverse engineered from ca663056, validator @0x003460):

  Blob is uploaded to RAM starting at 0xC000. Header (little-endian):

    0xC000  u16  stored CRC16 of the body (seed 0x544D, poly 0xA001 reflected)
    0xC002  u16  unused on the 0xFA path
    0xC004  u32  CRC range START, 24-bit linear (offset in low word, segment hi)
    0xC008  u32  CRC range END, inclusive, same encoding
    0xC00C  6    signature - must equal the firmware's flash bytes at 0x3FE8
    0xC012  u8   'M' (0x4D)
    0xC013  u8   'T' (0x54)
    0xC014  ...  entry point; firmware does a far `calls 0xC014`

  The payload is entered at 0xC014 via an inter-segment call and MUST terminate
  with a single `rets`. While it runs the firmware is blocked, so the payload
  has exclusive use of the ASC0 UART (= the K-line).

  Entry rules:
    * terminate with rets (RET inside a PROC FAR)
    * DPP0 is left at 0x24 by the validator; DPP3 stays 3. Use the 0xC000-0xFFFF
      window and SFRs directly; use EXTP for anything else.
    * preserve r0 (stack pointer); save/restore GPRs you clobber
    * SFRs are always reachable (DPP3==3 maps 0xFE00-0xFFFF to itself)

  The signature at 0x3FE8 is firmware-version specific, so callers should read it
  live from the ECU (ReadMemoryByAddress 0x3FE8, 6 bytes) rather than hardcoding.
"""

import re
import struct

from .kernel import DRIVER_ADDRESS, DRIVER_ENTRY, resource_path

MTOS_BASE = 0xC000  # where the blob is uploaded
MTOS_ENTRY = 0xC014  # firmware entry point (calls 0xC014)
MTOS_HEADER_LEN = MTOS_ENTRY - MTOS_BASE  # 0x14
MTOS_SIGNATURE_ADDRESS = 0x3FE8  # flash location of the 6-byte reference signature
MTOS_SIGNATURE_LENGTH = 6
CRC_SEED = 0x544D  # the 'MT' word; firmware seeds the CRC with it
CRC_POLY = 0xA001

# A convenient, known-executable scratch area for a second stage (XRAM 0xE000-0xEEFF).
# bsl_shim.a66 and stage2_hello.a66 are assembled for it
STAGE2_ADDRESS = 0xE000


def crc16_mtos(data: bytes, seed: int = CRC_SEED) -> int:
	"""Reproduce the firmware CRC routine at 0x000E32 byte-for-byte.

	Per byte: for each of 8 bits, test (crc ^ (byte>>i)) & 1; if set,
	crc = (crc>>1) ^ 0xA001, else crc >>= 1.
	"""
	crc = seed & 0xFFFF
	for byte in data:
		bb = byte & 0xFF
		for _ in range(8):
			if (crc ^ bb) & 1:
				crc = (crc >> 1) ^ CRC_POLY
			else:
				crc = crc >> 1
			crc &= 0xFFFF
			bb >>= 1
	return crc & 0xFFFF


def build_blob(body: bytes, signature: bytes,
               crc_start: int = MTOS_ENTRY, segment: int = 0) -> bytes:
	"""Wrap `body` (machine code starting at 0xC014) in the MTOS header.

	`signature` must be the 6 bytes read from the target ECU at 0x3FE8.
	"""
	if len(signature) != MTOS_SIGNATURE_LENGTH:
		raise ValueError('signature must be %d bytes (read live from 0x%X)'
		                 % (MTOS_SIGNATURE_LENGTH, MTOS_SIGNATURE_ADDRESS))
	if not body:
		raise ValueError('empty body')

	crc_end = crc_start + len(body) - 1
	crc = crc16_mtos(body)

	header = bytearray(MTOS_HEADER_LEN)
	struct.pack_into('<H', header, 0x00, crc)                      # 0xC000
	struct.pack_into('<H', header, 0x02, 0x0000)                   # 0xC002 unused
	struct.pack_into('<I', header, 0x04, (segment << 16) | crc_start)  # 0xC004
	struct.pack_into('<I', header, 0x08, (segment << 16) | crc_end)    # 0xC008
	header[0x0C:0x12] = signature                                  # 0xC00C..0xC011
	header[0x12] = 0x4D  # 'M'
	header[0x13] = 0x54  # 'T'
	return bytes(header) + bytes(body)


# ---------------------------------------------------------------------------
# Ready-made payloads, written as Keil A166 source in assets/mtos and assembled
# with openc166. Each MTOS payload returns the body (code from 0xC014); wrap
# it with build_blob(body, signature).
# ---------------------------------------------------------------------------

MTOS_SOURCES = 'assets/mtos'

#: what kline_hello.a66 sends
KLINE_HELLO = b'MTOS'

# the stock I4 flash driver's erase poll, which driver_patch.a66 moves into its helper
_DRIVER_POLL = bytes.fromhex('dc02a851')   # exts r2,#1 ; mov r5,[r1]


def _assemble(name: str, **values: int):
	'''Assemble assets/mtos/<name>.a66 with openc166 and return its openc166.Result.

	Keil A166 can't set a symbol from outside, so each of `values` goes into the
	source instead, in place of the value on that symbol's EQU line: START=0x86000
	turns `START EQU 8100H` into `START EQU 086000H`. Raises
	openc166.AssemblyError if the source has errors'''
	# only MTOS needs the assembler, so the rest of GKFlasher runs without it
	import openc166

	path = resource_path('{}/{}.a66'.format(MTOS_SOURCES, name))
	with open(path, 'rb') as file:
		source = file.read().decode('ascii')
	for symbol, value in values.items():
		if value < 0:
			raise ValueError('{} must not be negative'.format(symbol))
		source, count = re.subn(r'(?im)^({}\s+EQU\s+)\S+'.format(re.escape(symbol)),
			lambda match: '{}0{:X}H'.format(match.group(1), value), source)
		if count != 1:
			raise ValueError('{} has no {} EQU line'.format(path, symbol))
	result = openc166.assemble(source, file_name=path)
	if not result.ok:
		raise openc166.AssemblyError(result)
	return result


def _code(name: str, address: int, **values: int) -> bytes:
	'''The machine code of assets/mtos/<name>.a66, which has to start at `address`'''
	image = _assemble(name, **values).image()
	if image.base != address:
		raise ValueError('{}.a66 starts at 0x{:X}, not 0x{:X}'.format(name, image.base, address))
	return image.data


def payload_marker(marker: int = 0xBEEF, address: int = 0xC002) -> bytes:
	"""Proof-of-execution with no I/O: write `marker` to RAM and return.

	Verify by reading `address` back with ReadMemoryByAddress; it changes from
	the uploaded header value to `marker`.
	"""
	return _code('marker', MTOS_ENTRY, MARKER=marker, TARGET=address)


def payload_kline_hello() -> bytes:
	"""Proof-of-I/O: send KLINE_HELLO over the K-line, then return."""
	return _code('kline_hello', MTOS_ENTRY)


def payload_memory_dump(address: int, length: int) -> bytes:
	"""Sample larger payload: stream `length` bytes starting at 24-bit linear
	`address` out of the K-line, then return."""
	if length <= 0 or length > 0xFFFF:
		raise ValueError('length must be 1..0xFFFF')
	if not 0 <= address <= 0xFFFFFF:
		raise ValueError('address must be 24-bit')
	if (address & 0x3FFF) + length > 0x4000:
		raise ValueError('dump must stay within one 16KB page for this sample')
	return _code('memory_dump', MTOS_ENTRY, START=address, LENGTH=length)


def payload_loader(stage2_address: int = STAGE2_ADDRESS) -> bytes:
	"""Small K-line loader (stage 1).

	MTOS only gives us a tiny RAM window at 0xC000, so instead of uploading a
	large payload through the slow WriteMemoryByAddress path we upload this
	loader once and then stream the real ("stage 2") code to it over the
	K-line at full ASC0 speed.

	Protocol (all on the K-line, after StartRoutine(0xFA) launches the loader):

	    host -> ecu : u16 length, little-endian
	    host -> ecu : <length> bytes of stage-2 machine code
	    ecu         : runs the stage (far call), which may talk on the K-line
	    ecu  -> host : nothing added by the loader itself

	The loader copies the stream to `stage2_address`, far-calls it (so the stage
	returns to the loader with `rets`), then returns to the firmware.

	`stage2_address` must be writable RAM reachable through DPP3 (0xC000-0xFBFF);
	XRAM 0xE000 is the verified choice. The second stage must end with `rets`.

	Every receive services the watchdog, so the loader can wait indefinitely
	between bytes without the C167 resetting.
	"""
	if not (0xC000 <= stage2_address <= 0xFBFF):
		raise ValueError('stage2 must sit in the DPP3 window (0xC000-0xFBFF)')
	return _code('loader', MTOS_ENTRY, STAGE2=stage2_address)


def payload_bsl_kernel_shim(kernel: bytes) -> bytes:
	"""Second stage for the loader that turns the halted firmware into a BSL
	session: a short prologue (bsl_shim.a66) followed by the stock SIMK4x BSL
	kernel (assets/simk4x_kernel.bin), which then talks the usual BSL protocol
	over the K-line at the current ASC0 baudrate.

	The kernel is position independent (relative jumps only, and its CALL
	command builds the return address from a pushed IP), so it runs in place
	right behind the prologue. The prologue gives it the environment the boot
	ROM would have given it, minus the parts the running firmware already
	set up (SYSCON, BUSCON0/1 - locked or fine as they are):

	  * interrupts off for good - the firmware never gets the CPU back, the
	    session ends with the kernel's software reset (C_SWRESET)
	  * longest watchdog period (WDTCON 0x0001, ~335-420 ms). The kernel
	    services it while waiting for host bytes, not while transmitting, so
	    the host keeps every reply below that
	  * system stack at 0xF300-0xF5FF and register bank at 0xF200, clear of
	    the flash driver (0xF600) and the data buffer (0xFC00). The bounds are
	    widened first so the switch can't trip a stack trap
	  * DPP3 is 3 at runtime, which the kernel's CALL relies on

	Never returns to the loader. Size stays even so the kernel stays aligned.
	"""
	if not kernel or len(kernel) % 2:
		raise ValueError('kernel must be a non-empty, even number of bytes')

	stage2 = _code('bsl_shim', STAGE2_ADDRESS) + bytes(kernel)
	if STAGE2_ADDRESS + len(stage2) > 0xEF00:
		raise ValueError('second stage does not fit the XRAM scratch area')
	return stage2


def patch_driver_watchdog(driver: bytes) -> bytes:
	"""Make the stock I4 flash driver service the watchdog while erasing.

	In the BSL the watchdog is never started, so the driver's erase completion
	poll (up to several seconds) never services it. Once the firmware has
	started it there is no way to stop it, so driver_patch.a66 turns the poll's
	flash read at 0xF706 into a call to a helper placed in the unused padding
	at 0xF630:

	    0xF630  srvwdt
	            exts r2,#1
	            mov r5,[r1]      ; the two instructions moved here
	            ret
	    0xF706  callr 0xF630
	            nop

	Programming polls a word at a time and needs no help. Refuses anything
	that isn't the expected driver."""
	patch = _assemble('driver_patch')
	pad = patch.sections['PAD'].address - DRIVER_ADDRESS
	poll = patch.sections['ERASE'].address - DRIVER_ADDRESS
	data = bytearray(driver)
	if (data[poll:poll + len(_DRIVER_POLL)] != _DRIVER_POLL
			or set(data[pad:DRIVER_ENTRY - DRIVER_ADDRESS]) != {0xFF}):
		raise ValueError('unexpected flash driver, refusing to patch it')

	for chunk in patch.chunks:
		offset = chunk.address - DRIVER_ADDRESS
		data[offset:offset + len(chunk.data)] = chunk.data
	return bytes(data)


def payload_stage2_hello() -> bytes:
	"""Sample stage-2 payload for the loader: frame "OK" on the K-line.

	Emits AA 55 C2 'OK' so the host can spot it unambiguously, then rets back
	to the loader. Because it only touches ASC0 SFRs and uses relative jumps,
	it is position independent and runs correctly at `STAGE2_ADDRESS`.
	"""
	return _code('stage2_hello', STAGE2_ADDRESS)


if __name__ == '__main__':
	# Offline self-tests (no hardware): python -m flasher.mtos_asm
	assert payload_marker() == bytes.fromhex('e6f4efbe' 'f6f402c0' 'db00'), \
		payload_marker().hex()
	# CRC sanity: seed with 'MT', empty-ish vectors
	assert crc16_mtos(b'') == CRC_SEED
	known = crc16_mtos(b'\x01\x02\x03\x04')
	assert 0 <= known <= 0xFFFF
	sig = b'111663'
	blob = build_blob(payload_marker(), sig)
	assert blob[0x12] == 0x4D and blob[0x13] == 0x54
	assert blob[0x0C:0x12] == sig
	# stored CRC must equal CRC over the body
	body = payload_marker()
	assert struct.unpack_from('<H', blob, 0)[0] == crc16_mtos(body)
	# CRC range start/end
	assert struct.unpack_from('<I', blob, 4)[0] == MTOS_ENTRY
	assert struct.unpack_from('<I', blob, 8)[0] == MTOS_ENTRY + len(body) - 1
	print('marker   :', payload_marker().hex())
	print('hello    :', payload_kline_hello().hex())
	print('dump 0x40:', payload_memory_dump(0x000000, 0x40).hex())
	print('loader   :', payload_loader().hex())
	print('blob     :', blob.hex())

	# the bytes of the bench-tested payloads, from before openc166 assembled them
	assert payload_marker(0x1234, 0xC004) == bytes.fromhex('e6f43412' 'f6f404c0' 'db00')
	assert payload_kline_hello() == bytes.fromhex(
		'6eb6' 'e7584d00' '9ab6fe70' '7eb6' 'e7585400' '9ab6fe70' '7eb6' 'e7584f00' '9ab6fe70' '7eb6'
		'e7585300' '9ab6fe70' '7eb6' 'e7580000' '9ab6fe70' '7eb6' '6fb6' 'db00')
	dump = '6eb6{}{}a758a7a7d740{}a904f7f0b0fe9ab6fe707eb6084128513df3e75800009ab6fe707eb66fb6db00'
	assert payload_memory_dump(0x86000, 0x200) == bytes.fromhex(dump.format('e6f40020', 'e6f50002', '2100'))
	# like Keil A166, MOV takes the short #data4 form when the value fits: mov r4,#0
	assert payload_memory_dump(0xFFC000, 0x4000) == bytes.fromhex(dump.format('e004', 'e6f50040', 'ff03'))
	loader = ('6eb76eb6f3f0b2fe7eb7a758a7a79ab7fc70f3f0b2fe7eb7c006a758a7a79ab7fc70f3f0b2fe7eb7c007'
		'5c870067e6f400{0}a758a7a79ab7fc70f3f0b2fe7eb7b904084128613df5da0000{0}6fb76fb6db00')
	assert payload_loader() == bytes.fromhex(loader.format('e0'))
	assert payload_loader(0xC800) == bytes.fromhex(loader.format('c8'))
	assert payload_stage2_hello() == bytes.fromhex(
		'e758aa00' '9ab6fe70' '7eb6' 'e7585500' '9ab6fe70' '7eb6' 'e758c200' '9ab6fe70' '7eb6'
		'e7584f00' '9ab6fe70' '7eb6' 'e7584b00' '9ab6fe70' '7eb6' 'db00')
	shim = payload_bsl_kernel_shim(b'\xcc\x00')
	assert shim == bytes.fromhex('be88cc00' 'e6d70100' 'a758a7a7' 'e60a00f2' 'e60bfefd' 'e60900f6' 'cc00'
		'e60a00f3' 'e60b00f6' 'e60800f2' 'cc00' 'cc00'), shim.hex()
	print('shim     :', shim[:-2].hex())
	try:
		_assemble('kline_hello', START=0)
		raise AssertionError('replaced a symbol the source does not have')
	except ValueError:
		pass

	import os
	driver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'simk4x_driver_i4_a29fx00bx.bin')
	if os.path.exists(driver_path):
		driver = open(driver_path, 'rb').read()
		patched = patch_driver_watchdog(driver)
		assert patched[0x106:0x10A] == bytes.fromhex('bb94cc00')
		assert patched[0x30:0x40] == bytes.fromhex('a758a7a7dc02a851cb00') + b'\xff' * 6
		assert sum(a != b for a, b in zip(driver, patched)) <= 14 and len(patched) == len(driver)
		try:
			patch_driver_watchdog(patched)
			raise AssertionError('patched twice')
		except ValueError:
			pass
	print('all self-tests passed')
