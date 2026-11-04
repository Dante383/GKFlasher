'''
The SIMK4x BSL kernel (assets/simk4x_kernel.bin), its flash drivers, and the jobs run on
them: reading, and writing the whole external flash - the boot sectors included.

The same kernel runs two ways, and only how it starts differs:

* BSL (bsl.py): the C167's boot ROM loads it, with the ECU held in boot mode.
* MTOS (mtos.py, mtos_kernel.py): the running firmware loads it over KWP2000.

From then on it's this module: the kernel's protocol, a flash driver uploaded next to it
to identify, erase and program the chip, and the jobs.

Every exchange is checked before the next byte goes out: a parameter byte the kernel
takes for a command (0x84 in an address, say) has to be ruled out, not just logged.
'''
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gkbus.channel import KLineTiming
from gkbus.exceptions import TimeoutException

from .lineswap import forward_lookup

KERNEL_ASSET = 'assets/simk4x_kernel.bin'
# the kernel's first words once it runs
KERNEL_STARTED = 0x03

C_WRITE_WORD = 0x82
C_WRITE_BLOCK = 0x84
C_READ_BLOCK = 0x85
C_SOFTWARE_RESET = 0x32
C_GET_CHECKSUM = 0x33
C_TEST_COMM = 0x93
C_CALL = 0x9F
C_READ_WORD = 0xCD
# a command byte is acknowledged with ACK, a finished command ends with DONE
ACK = 0xAA
DONE = 0xEA
# instead of DONE: a byte of a WRITE_BLOCK reads back different
VERIFY_FAILED = 0x11

# the drivers read and verify through segments 0x80 and up (CS0, the flash) and write
# through 0x40 and up (CS1, its write strobe - ADDRSEL1 0x4008)
FLASH_READ_BASE = 0x800000
DATA_BUFFER = 0xFC00
PROGRAM_BLOCK = 0x200

# the stock flash drivers are linked to run at 0xF600, entry point 0xF640
DRIVER_ADDRESS = 0x00F600
DRIVER_ENTRY = 0x00F640

# a `rets` in internal RAM that's free in both BSL and MTOS sessions (the BSL kernel
# is at 0xFA60, MTOS' register bank and stack below the driver), for resync's first
# bytes to point a half-received CALL at. To an idle kernel the bytes are no-ops
RESYNC_TRAP = 0x00F9F0
RETS = 0x00DB
RESYNC_PREFIX = RESYNC_TRAP.to_bytes(3, 'little')

FC_PROG = 0x00
FC_ERASE = 0x01
FC_GETSTATE = 0x06
E_ERASE_TIMEOUT = 0x31  # the driver's poll ran out, the chip may still be erasing
E_INVALID_SECTOR = 0x33
# the drivers' sector table: 0-3 the small ones, 4-18 64 KB each
DRIVER_LAST_SECTOR = 18

# under MTOS the kernel doesn't service the watchdog while it transmits. With the longest
# period that's 335 ms at 25 MHz, so every reply has to be well within it
REPLY_BUDGET = 0.15

ACK_TIMEOUT = 1.0
ERASE_TIMEOUT = 30.0
SECTOR_ATTEMPTS = 3
TRANSFER_ATTEMPTS = 3

# where SIMK43 firmware keeps its identification (ca66...)
IDENTIFICATION = 0x10040
IDENTIFICATION_LENGTH = 0x16

# while the host talks to the kernel: no gaps needed, short frames on J2534
KERNEL_TIMING = KLineTiming(p1_max=5.0, p3_min=0.0, p4_min=0.0)


class KernelError(Exception):
	pass


class KernelAbort(KernelError):
	pass


def log (message, log_callback=None):
	if log_callback:
		log_callback.emit(message)
	else:
		print(message)


def resource_path (relative_path: str) -> str:
	'''
	A file shipped with GKFlasher (assets/...), from a checkout or a PyInstaller build
	'''
	base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
	return os.path.join(base, relative_path)


def load_asset (relative_path: str) -> bytes:
	with open(resource_path(relative_path), 'rb') as file:
		return file.read()


def xor_checksum (data: bytes) -> int:
	'''
	The kernel's block checksum
	'''
	checksum = 0
	for byte in data:
		checksum ^= byte
	return checksum


def read_block_size (baudrate: int) -> int:
	'''
	Largest power of two block, up to 0x200, the kernel sends within REPLY_BUDGET
	'''
	size = PROGRAM_BLOCK
	while size > 0x10 and (size + 1) * 10 / baudrate > REPLY_BUDGET:
		size //= 2
	return size


def identification (flash: bytes, offset: int = IDENTIFICATION) -> str:
	'''
	The calibration a SIMK43 image identifies with, at 0x10040
	'''
	ident = flash[offset:offset + IDENTIFICATION_LENGTH].split(b'\xff')[0]
	if not ident.startswith(b'ca66'):
		return 'unknown'
	return ident.decode('latin-1', 'replace')


def available_filename (filename: str) -> str:
	'''
	`filename`, or with a timestamp added if it exists: nothing gets overwritten
	'''
	if not os.path.exists(filename):
		return filename
	root, extension = os.path.splitext(filename)
	return '{}_{}{}'.format(root, time.strftime('%Y%m%d_%H%M%S'), extension or '.bin')


def save (filename: str, data: bytes, log_callback=None) -> str:
	filename = available_filename(filename)
	with open(filename, 'wb') as file:
		file.write(data)
	log('[*] Saved {} bytes to {}'.format(len(data), filename), log_callback)
	return filename


def read_partial (channel, size: int, timeout: float) -> bytes:
	'''
	Up to `size` bytes off a K-line channel, fewer if `timeout` runs out
	'''
	if callable(getattr(channel, '_read', None)):
		return channel._read(size, timeout)
	# J2534 drops what it has on a timeout, a byte at a time loses nothing
	out = bytearray()
	deadline = time.monotonic() + timeout
	while len(out) < size:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			break
		try:
			out += channel.read(1, remaining)
		except TimeoutException:
			break
	return bytes(out)


def enter (channel, log_callback=None) -> KLineTiming:
	'''
	Kernel timing on the channel, returns what it was
	'''
	previous = channel.get_timing()
	try:
		channel.set_timing(KERNEL_TIMING)
	except Exception as e:
		# some J2534 adapters may refuse some values: slower, still works
		log('[!] Couldn\'t set the kernel K-line timing: {}'.format(e), log_callback)
	return previous


class KernelLink:
	'''
	Raw bytes to and from the kernel over a gkbus K-line channel. On serial adapters our
	own echo is checked: a mismatch means the kernel got something else than was sent
	'''

	CHUNK = 255

	def __init__ (self, channel) -> None:
		self.channel = channel
		self._serial = callable(getattr(channel, '_port', None)) and callable(getattr(channel, '_read', None))

	def send (self, data, check_echo: bool = True) -> None:
		data = bytes(data)
		for start in range(0, len(data), self.CHUNK):
			chunk = data[start:start + self.CHUNK]
			if not self._serial:
				self.channel.write(chunk)
				continue
			self.channel._port().write(chunk)
			echo = self.channel._read(len(chunk), len(chunk) * 10 / self.channel.get_baudrate() + 0.5)
			if check_echo and echo != chunk:
				raise KernelError('K-line echo mismatch: sent {}, got {}'.format(chunk.hex(' '), echo.hex(' ')))

	def read (self, size: int, timeout: float) -> bytes:
		return read_partial(self.channel, size, timeout)

	def drain (self, quiet: float = 0.3, limit: float = 3.0) -> bytes:
		'''
		Whatever arrives until the line has been quiet for `quiet` seconds
		'''
		collected = bytearray()
		deadline = time.monotonic() + limit
		while time.monotonic() < deadline:
			chunk = self.read(256, quiet)
			if not chunk:
				break
			collected += chunk
		return bytes(collected)


class InterruptGuard:
	'''
	Ctrl+C only between kernel transactions. Stopping mid-command leaves the kernel
	waiting for the rest of it, and whatever is sent next would complete it
	'''

	def __init__ (self) -> None:
		self.requested = False
		self._previous = None

	def __enter__ (self):
		if threading.current_thread() is threading.main_thread():
			self._previous = signal.signal(signal.SIGINT, self._handler)
		return self

	def __exit__ (self, *exc) -> None:
		if self._previous is not None:
			signal.signal(signal.SIGINT, self._previous)
			self._previous = None

	def _handler (self, signum, frame) -> None:
		if self.requested:
			raise KeyboardInterrupt
		self.requested = True
		print('\n[!] Stopping at the next safe point (not inside a sector write). Ctrl+C again stops right away, mid-command')

	def check (self) -> None:
		if self.requested:
			raise KernelAbort('Stopped by the user')


class Progress:
	def __init__ (self, progress_callback, total: int, label: str) -> None:
		self.progress_callback = progress_callback
		self.total = max(total, 1)
		self.label = label
		self._last = -1
		if progress_callback:
			progress_callback.emit((self.total, 0))
			progress_callback.emit((0,))

	def __call__ (self, done: int) -> None:
		if self.progress_callback:
			self.progress_callback.emit((done,))
			return
		percent = done * 100 // self.total
		if percent != self._last:
			self._last = percent
			print('\r[*] {}: {}%  '.format(self.label, percent), end='', flush=True)

	def finish (self) -> None:
		if not self.progress_callback and self._last >= 0:
			print('')
			self._last = -1


class Kernel:
	'''
	The BSL kernel at the other end of `link`: its commands, and memory reads and writes
	that survive a broken exchange
	'''

	def __init__ (self, link: KernelLink, log_callback=None, progress_callback=None) -> None:
		self.link = link
		self.guard = InterruptGuard()
		self.log_callback = log_callback
		self.progress_callback = progress_callback
		self.baudrate = link.channel.get_baudrate()
		self.block_size = read_block_size(self.baudrate)
		# set by the first erase: from then on a failure leaves the kernel running
		self.flash_modified = False
		# set while a sector is between erase and verify: a stop request waits
		self.critical = False
		# the kernel's software reset went through: it's gone, the ECU is booting
		self.ecu_reset = False

	def log (self, message: str) -> None:
		log(message, self.log_callback)

	def check_stop (self) -> None:
		if not self.critical:
			self.guard.check()

	# -- protocol ------------------------------------------------------------

	def _expect (self, size: int, timeout: float, what: str) -> bytes:
		data = self.link.read(size, timeout)
		if len(data) != size:
			raise KernelError('{}: got {} of {} bytes ({})'.format(what, len(data), size, data[:16].hex(' ')))
		return data

	def _command (self, code: int, what: str) -> None:
		'''
		The command byte, then nothing more unless the kernel acknowledged exactly it
		'''
		self.link.send([code])
		ack = self.link.read(1, ACK_TIMEOUT)
		if ack != bytes([ACK]):
			raise KernelError('{} not acknowledged ({})'.format(what, ack.hex() or 'no answer'))

	def _done (self, what: str, timeout: float = ACK_TIMEOUT) -> None:
		end = self.link.read(1, timeout)
		if end != bytes([DONE]):
			raise KernelError('{} failed ({})'.format(what, end.hex() or 'no answer'))

	@staticmethod
	def _address (address: int) -> bytes:
		return address.to_bytes(3, 'little')

	def test_comm (self) -> bool:
		try:
			self._command(C_TEST_COMM, 'TEST_COMM')
			self._done('TEST_COMM')
		except KernelError:
			return False
		return True

	def checksum (self) -> int:
		'''
		XOR of the bytes of the last READ_BLOCK/WRITE_BLOCK
		'''
		self._command(C_GET_CHECKSUM, 'GET_CHECKSUM')
		reply = self._expect(2, ACK_TIMEOUT, 'GET_CHECKSUM')
		if reply[1] != DONE:
			raise KernelError('GET_CHECKSUM failed ({})'.format(reply.hex()))
		return reply[0]

	def read_word (self, address: int) -> int:
		self._command(C_READ_WORD, 'READ_WORD')
		self.link.send(self._address(address))
		reply = self._expect(3, ACK_TIMEOUT, 'READ_WORD 0x{:06X}'.format(address))
		if reply[2] != DONE:
			raise KernelError('READ_WORD 0x{:06X} failed ({})'.format(address, reply.hex()))
		return reply[0] | reply[1] << 8

	def write_word (self, address: int, value: int, verify: bool = True) -> None:
		self._command(C_WRITE_WORD, 'WRITE_WORD')
		self.link.send(self._address(address) + value.to_bytes(2, 'little'))
		self._done('WRITE_WORD 0x{:06X}'.format(address))
		if not verify:
			return
		readback = self.read_word(address)
		if readback != value:
			raise KernelError('Wrote 0x{:04X} to 0x{:06X}, reads 0x{:04X}'.format(value, address, readback))

	def write_block (self, address: int, data: bytes) -> None:
		'''
		The kernel compares every byte it writes (VERIFY_FAILED instead of DONE), and the
		block checksum covers what it received
		'''
		what = 'WRITE_BLOCK 0x{:06X}+0x{:X}'.format(address, len(data))
		self._command(C_WRITE_BLOCK, 'WRITE_BLOCK')
		self.link.send(self._address(address) + len(data).to_bytes(2, 'little') + bytes(data))
		status = self.link.read(1, ACK_TIMEOUT)
		if status == bytes([VERIFY_FAILED]):
			raise KernelError('{}: memory reads back different'.format(what))
		if status != bytes([DONE]):
			raise KernelError('{} failed ({})'.format(what, status.hex() or 'no answer'))
		if self.checksum() != xor_checksum(data):
			raise KernelError('{}: checksum mismatch'.format(what))

	def read_block (self, address: int, size: int) -> bytes:
		what = 'READ_BLOCK 0x{:06X}+0x{:X}'.format(address, size)
		self._command(C_READ_BLOCK, 'READ_BLOCK')
		self.link.send(self._address(address) + size.to_bytes(2, 'little'))
		reply = self._expect(size + 1, (size + 1) * 10 / self.baudrate + ACK_TIMEOUT, what)
		if reply[-1] != DONE:
			raise KernelError('{} failed (ends with {:02x})'.format(what, reply[-1]))
		data = reply[:-1]
		if self.checksum() != xor_checksum(data):
			raise KernelError('{}: checksum mismatch'.format(what))
		return data

	def call (self, address: int, registers: list, timeout: float = 2.0) -> list:
		'''
		Far call `address` with r8-r15, returns them as it left them
		'''
		what = 'CALL 0x{:06X} (r8=0x{:X})'.format(address, registers[0])
		self._command(C_CALL, 'CALL')
		self.link.send(self._address(address) + b''.join(r.to_bytes(2, 'little') for r in registers))
		reply = self._expect(17, timeout, what)
		if reply[16] != DONE:
			raise KernelError('{} failed ({})'.format(what, reply.hex()))
		return [reply[i] | reply[i + 1] << 8 for i in range(0, 16, 2)]

	def software_reset (self) -> None:
		self._command(C_SOFTWARE_RESET, 'SOFTWARE_RESET')
		self._done('SOFTWARE_RESET')
		self.ecu_reset = True

	def resync (self) -> bool:
		'''
		Back to the kernel's command loop from wherever an exchange broke off.

		First the line has to go quiet - the kernel may still be sending a block. Then
		single bytes: as commands they're no-ops the kernel acknowledges, as parameters
		they make an address or a length harmless. The first three are RESYNC_PREFIX:
		a CALL that only got its command byte calls the `rets` there. The rest are 0x00
		(a length of 0 sends or writes nothing). Once something comes back, another 0x00
		has to get exactly one acknowledge. If nothing comes back the kernel is probably
		taking in a block: 0x00s in bursts, continuous so that an answer can't collide
		into anything but 0x00 on the kernel's end.

		Not covered: a CALL that got part of its address (bytes lost on the line)
		'''
		self.link.drain(quiet=1.0, limit=20.0)
		filler = RESYNC_PREFIX + b'\x00' * 29
		sent = 0
		while sent < PROGRAM_BLOCK + 0x40:
			burst = filler[sent:sent + 1] if sent < len(filler) else b'\x00' * 16
			self.link.send(burst, check_echo=False)
			sent += len(burst)
			if not self.link.drain(quiet=0.3):
				continue
			self.link.send(b'\x00', check_echo=False)
			if self.link.drain(quiet=0.3) == bytes([ACK]) and self.test_comm():
				try:
					# a WRITE_WORD finished by the prefix wrote over it
					self.arm_trap()
				except KernelError:
					return False
				return True
		return False

	def arm_trap (self) -> None:
		self.write_word(RESYNC_TRAP, RETS)

	def recover (self) -> None:
		self.log('[*] Getting back in sync with the kernel')
		if not self.resync():
			raise KernelError('Lost the kernel')

	# -- memory --------------------------------------------------------------

	def read_memory (self, address: int, size: int, progress: Progress | None = None, done: int = 0) -> bytes:
		out = bytearray()
		while len(out) < size:
			self.check_stop()
			length = min(self.block_size, size - len(out))
			for attempt in range(TRANSFER_ATTEMPTS):
				try:
					out += self.read_block(address + len(out), length)
					break
				except KernelError as e:
					if attempt == TRANSFER_ATTEMPTS - 1:
						raise
					self.log('\n[!] {}, retrying'.format(e))
					self.recover()
			if progress:
				progress(done + len(out))
		return bytes(out)

	def write_memory (self, address: int, data: bytes) -> None:
		for attempt in range(TRANSFER_ATTEMPTS):
			try:
				self.write_block(address, data)
				return
			except KernelError as e:
				if attempt == TRANSFER_ATTEMPTS - 1:
					raise
				self.log('\n[!] {}, retrying'.format(e))
				self.recover()


# -- flash ------------------------------------------------------------------

@dataclass(frozen=True)
class Chip:
	name: str
	size: int
	top_boot: bool = False

	@property
	def sectors (self) -> tuple:
		'''
		(number, offset, size): bottom boot 16+8+8+32 KB then 64 KB each, top boot reversed
		'''
		small = [0x4000, 0x2000, 0x2000, 0x8000]
		large = [0x10000] * (self.size // 0x10000 - 1)
		sizes = large + small[::-1] if self.top_boot else small + large
		offsets = [sum(sizes[:index]) for index in range(len(sizes))]
		return tuple(zip(range(len(sizes)), offsets, sizes))


# (manufacturer, device) as the chip answers autoselect, data lines un-crossed
CHIPS = {
	(0x01, 0x57): Chip('AMD AM29F200BB', 0x40000),
	(0x01, 0xAB): Chip('AMD AM29F400BB', 0x80000),
	(0x01, 0x58): Chip('AMD AM29F800BB', 0x100000),
	(0x01, 0x51): Chip('AMD AM29F200BT', 0x40000, top_boot=True),
	(0x01, 0x23): Chip('AMD AM29F400BT', 0x80000, top_boot=True),
	(0x01, 0xD6): Chip('AMD AM29F800BT', 0x100000, top_boot=True),
	(0x20, 0xD4): Chip('ST M29F200BB', 0x40000),
	(0x20, 0xD6): Chip('ST M29F400BB', 0x80000),
	(0x20, 0xD3): Chip('ST M29F200BT', 0x40000, top_boot=True),
	(0x20, 0xD5): Chip('ST M29F400BT', 0x80000, top_boot=True),
}


@dataclass(frozen=True)
class Driver:
	'''
	A stock SIMK4x flash driver. The I4 board (SIMK43 2.0) has the flash's data lines
	crossed, so its driver writes the chip's commands crossed, and what it reads (the IDs)
	comes back crossed
	'''
	name: str
	asset: str
	crossed: bool
	patch: Callable[[bytes], bytes] | None = None

	def code (self) -> bytes:
		code = load_asset(self.asset)
		return self.patch(code) if self.patch else code

	def uncross (self, value: int) -> int:
		return forward_lookup(value) if self.crossed else value


DRIVER_I4 = Driver('I4', 'assets/simk4x_driver_i4_a29fx00bx.bin', crossed=True)
DRIVER_V6 = Driver('V6', 'assets/simk4x_driver_v6_a29fx00bx.bin', crossed=False)
DRIVERS = {'i4': DRIVER_I4, 'v6': DRIVER_V6}


class Flash:
	'''
	The external flash, through `driver` uploaded next to the kernel
	'''

	def __init__ (self, kernel: Kernel, driver: Driver, chip: Chip | None = None) -> None:
		self.kernel = kernel
		self.driver = driver
		self.chip = chip

	def log (self, message: str) -> None:
		self.kernel.log(message)

	def upload (self) -> None:
		code = self.driver.code()
		self.log('[*] Uploading the {} flash driver ({} bytes{}) to 0x{:04X}'.format(
			self.driver.name, len(code), ', patched' if self.driver.patch else '', DRIVER_ADDRESS))
		self.kernel.write_memory(DRIVER_ADDRESS, code)
		if self.kernel.read_memory(DRIVER_ADDRESS, len(code)) != code:
			raise KernelError('The flash driver reads back different')

	def read_ids (self) -> tuple:
		'''
		(manufacturer, device). Autoselect is written to the chip, so they prove the write
		path too
		'''
		segment = FLASH_READ_BASE >> 16
		ids = []
		for index in (0, 1):
			raw = self.kernel.call(DRIVER_ENTRY, [FC_GETSTATE, 0, segment, segment, 0, 0, index, 1])[1]
			ids.append(self.driver.uncross(raw) & 0xFF)
		return tuple(ids)

	def identify (self, chips: dict = CHIPS, ids: tuple | None = None) -> Chip:
		manufacturer, device = ids or self.read_ids()
		chip = chips.get((manufacturer, device))
		if chip is None:
			raise KernelError('Unexpected flash IDs 0x{:02X}/0x{:02X}, refusing to touch it'.format(manufacturer, device))
		self.chip = chip
		self.log('[*] Flash chip: {} (0x{:02X}/0x{:02X}), {} KB, {} boot'.format(
			chip.name, manufacturer, device, chip.size // 1024, 'top' if chip.top_boot else 'bottom'))
		return chip

	def check_writable (self) -> None:
		'''
		The drivers erase by sector number, from a bottom boot sector map
		'''
		if self.chip.top_boot:
			raise KernelError('{} is a top boot chip. The flash drivers only know bottom boot sectors, '
				'they\'d erase the wrong ones: reading works, writing doesn\'t'.format(self.chip.name))
		if len(self.chip.sectors) - 1 > DRIVER_LAST_SECTOR:
			raise KernelError('{} has more sectors than the flash drivers know'.format(self.chip.name))

	def read (self, label: str) -> bytes:
		progress = Progress(self.kernel.progress_callback, self.chip.size, label)
		try:
			return self.kernel.read_memory(FLASH_READ_BASE, self.chip.size, progress)
		finally:
			progress.finish()

	def erase_sector (self, number: int) -> None:
		self.check_writable()
		_, offset, size = self.chip.sectors[number]
		address = FLASH_READ_BASE + offset
		registers = [FC_ERASE, address & 0xFFFF, address >> 16, address >> 16,
			(address + size - 2) & 0xFFFF, 0, number, 1]
		self.kernel.flash_modified = True
		for _ in range(4):
			result = self.kernel.call(DRIVER_ENTRY, registers, timeout=ERASE_TIMEOUT)[7]
			if result == 0:
				return
			if result == E_ERASE_TIMEOUT:
				# commands are ignored while the chip erases, asking again just waits more
				self.log('[*] SA{} still erasing'.format(number))
				continue
			if result == E_INVALID_SECTOR:
				raise KernelError('The driver doesn\'t know sector {}'.format(number))
			raise KernelError('SA{} isn\'t blank after erasing (0x{:02X})'.format(number, result))
		raise KernelError('SA{} erase timed out'.format(number))

	def program_block (self, offset: int, data: bytes) -> None:
		address = FLASH_READ_BASE + offset
		self.kernel.write_memory(DATA_BUFFER, data)
		registers = [FC_PROG, len(data), DATA_BUFFER, 0, address >> 16, address & 0xFFFF, address >> 16, 1]
		result = self.kernel.call(DRIVER_ENTRY, registers)[7]
		if result != 0:
			raise KernelError('Programming 0x{:05X} failed (0x{:02X})'.format(offset, result))

	def write_sector (self, number: int, image: bytes) -> None:
		_, offset, size = self.chip.sectors[number]
		target = image[offset:offset + size]
		blocks = [start for start in range(0, size, PROGRAM_BLOCK)
			if target[start:start + PROGRAM_BLOCK].count(0xFF) != PROGRAM_BLOCK]

		self.kernel.guard.check()
		self.kernel.critical = True
		try:
			self._write_sector(number, offset, size, target, blocks)
		finally:
			self.kernel.critical = False

	def _write_sector (self, number: int, offset: int, size: int, target: bytes, blocks: list) -> None:
		kernel = self.kernel
		for attempt in range(1, SECTOR_ATTEMPTS + 1):
			self.log('[*] SA{} (0x{:05X}, {} KB){}'.format(
				number, offset, size // 1024, '' if attempt == 1 else ', attempt {}'.format(attempt)))
			try:
				self.erase_sector(number)
				progress = Progress(kernel.progress_callback, size * 2, 'SA{} program + verify'.format(number))
				try:
					for index, start in enumerate(blocks):
						self.program_block(offset + start, target[start:start + PROGRAM_BLOCK])
						progress((index + 1) * size // len(blocks))
					written = kernel.read_memory(FLASH_READ_BASE + offset, size, progress, done=size)
				finally:
					progress.finish()
			except KernelError as e:
				self.log('[!] SA{}: {}'.format(number, e))
				kernel.recover()
				continue

			if written == target:
				self.log('[+] SA{} written and verified'.format(number))
				return
			first = next(index for index in range(size) if written[index] != target[index])
			self.log('[!] SA{} verify failed at 0x{:05X}'.format(number, offset + first))

		raise KernelError('SA{} failed {} times'.format(number, SECTOR_ATTEMPTS))

	def changed_sectors (self, current: bytes, image: bytes) -> list:
		return [number for number, offset, size in self.chip.sectors
			if current[offset:offset + size] != image[offset:offset + size]]


# -- jobs -------------------------------------------------------------------

def read_flash (flash: Flash, output_filename: str) -> str:
	data = flash.read('Reading flash')
	flash.log('[*] Identification: {}'.format(identification(data)))
	return save(output_filename, data, flash.kernel.log_callback)


def write_flash (flash: Flash, image: bytes, backup_filename: str, confirm, order=None,
                 boot_sectors=()) -> bool:
	'''
	Backup, then every sector that differs from `image` in `order` (by default the one
	with the reset vector last), then a full verify. False if nothing needed writing or
	the user said no.

	:param order: sector numbers in write order, or a function of (current flash, image)
		that returns them
	:param boot_sectors: sectors the ECU can't start without, the user is warned about
		them. Or a function of (current flash, image) that returns them
	'''
	flash.check_writable()
	sectors = flash.chip.sectors
	if len(image) != flash.chip.size:
		raise KernelError('The image is {} bytes, the {} is {}'.format(len(image), flash.chip.name, flash.chip.size))

	flash.log('[*] Reading the whole flash first')
	current = flash.read('Backup')
	save(backup_filename, current, flash.kernel.log_callback)

	if order is None:
		order = tuple(number for number, _, _ in sectors[1:]) + (0,)
	elif callable(order):
		order = tuple(order(current, image))
	if callable(boot_sectors):
		boot_sectors = boot_sectors(current, image)
	if sorted(order) != [number for number, _, _ in sectors]:
		raise KernelError('The write order {} isn\'t every sector once'.format(order))

	changed = flash.changed_sectors(current, image)
	flash.log('[*] ECU: {}, image: {}'.format(identification(current), identification(image)))
	if not changed:
		flash.log('[+] The flash already matches the image')
		return False
	flash.log('[*] To rewrite, in order: {}'.format(', '.join('SA{}'.format(n) for n in order if n in changed)))

	boot = [number for number in boot_sectors if number in changed]
	warning = 'This erases and rewrites {} of {} sectors'.format(len(changed), len(sectors))
	if boot:
		warning += ', boot sector(s) {} included. If it fails or loses power while those are written, the ECU won\'t boot until it\'s recovered over BSL'.format(
			' and '.join('SA{}'.format(number) for number in boot))
	if not confirm(warning + '. Continue?'):
		flash.log('[!] Aborted, nothing written')
		return False

	for number in order:
		if number in changed:
			flash.write_sector(number, image)

	for attempt in range(3):
		bad = flash.changed_sectors(flash.read('Verifying'), image)
		if not bad:
			flash.log('[+] The whole flash matches the image')
			return True
		if attempt == 2:
			break
		flash.log('[!] Verify failed in {}, rewriting'.format(', '.join('SA{}'.format(n) for n in bad)))
		for number in order:
			if number in bad:
				flash.write_sector(number, image)
	raise KernelError('The flash still doesn\'t match the image')


def run_session (kernel: Kernel, job, reset_when_done: bool, attached: bool = False, attach_hint: str | None = None) -> bool:
	'''
	Run `job(kernel)` on a kernel that's up and answering.

	With `reset_when_done` (MTOS) the kernel's software reset ends the session when the
	job worked, or when it failed before the flash was touched. Without it (BSL) the
	kernel is always left running. Once the flash has been touched, a failure leaves the
	kernel running either way, for the host to pick it up again (`attach_hint`) rather
	than rebooting a half written flash. `kernel.ecu_reset` tells whether it was reset.

	:return: whether the job finished
	'''
	with kernel.guard:
		try:
			kernel.log('[*] Kernel at {} baud, reading in 0x{:X} byte blocks'.format(kernel.baudrate, kernel.block_size))
			kernel.arm_trap()
			job(kernel)
		except BaseException as e:
			touched = kernel.flash_modified or attached
			if isinstance(e, KeyboardInterrupt):
				kernel.log('\n[!] Interrupted')
			else:
				kernel.log('\n[!] {}'.format(e))
			if touched:
				kernel.log('[!] The flash may be partly written. The kernel is left running: keep the ECU powered')
				if attach_hint:
					kernel.log('[!] and run: {}'.format(attach_hint))
			elif reset_when_done:
				if not isinstance(e, KeyboardInterrupt) and _reset(kernel, resync=not isinstance(e, KernelAbort)):
					kernel.log('[*] The flash wasn\'t touched, the ECU was reset')
					return False
				kernel.log('[!] The flash wasn\'t touched: switch the ECU off and on again')
			if not isinstance(e, KernelError):
				raise
			return False

		if reset_when_done:
			_reset(kernel, resync=False)
		return True


def _reset (kernel: Kernel, resync: bool) -> bool:
	try:
		if resync and not kernel.test_comm():
			kernel.recover()
		kernel.software_reset()
	except KernelError as e:
		kernel.log('[!] Couldn\'t reset the ECU: {}'.format(e))
		return False
	kernel.log('[*] ECU reset')
	return True
