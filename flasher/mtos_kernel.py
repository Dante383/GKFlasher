'''
MTOS full flash: the stock SIMK4x BSL kernel and I4 flash driver, run on a live ECU.

The MTOS loader streams a short prologue plus the BSL kernel (assets/simk4x_kernel.bin)
into XRAM (mtos_asm.payload_bsl_kernel_shim). From then on the ECU speaks the BSL
protocol over the K-line at the diagnostic session's baudrate, and the flash is erased
and programmed by the BSL's I4 driver - patched to service the watchdog while it waits
for an erase (mtos_asm.patch_driver_watchdog), since here, unlike in the BSL, the
watchdog is running and can't be stopped.

The firmware never gets the CPU back: a session ends with the kernel's software reset.
If anything goes wrong once the flash has been touched, the kernel is left running so
the host can attach to it again (--mtos-attach) rather than rebooting a half written
flash.

The protocol is bsl.py's, but every exchange is checked before the next byte goes out:
with the firmware's code in the flash being rewritten, a parameter byte the kernel
takes for a command (0x84 in an address, say) has to be ruled out, not just logged.
'''
import os
import signal
import threading
import time

import bsl
from gkbus.channel import KLineTiming
from gkbus.exceptions import TimeoutException

from .mtos_asm import DRIVER_ADDRESS, DRIVER_ENTRY, patch_driver_watchdog

KERNEL_ASSET = 'assets/simk4x_kernel.bin'
DRIVER_ASSET = 'assets/simk4x_driver_i4_a29fx00bx.bin'

# the kernel's first words once it runs
KERNEL_STARTED = 0x03

FLASH_SIZE = 0x80000
# the driver reads and verifies through segments 0x80-0x87, the firmware runs from the
# 0x080000 mirror. Both are CS0, the flash, which only sees A0-A18
FLASH_READ_BASE = 0x800000
FLASH_MIRROR = 0x080000
DATA_BUFFER = 0xFC00
PROGRAM_BLOCK = 0x200

# the firmware has CS1, the flash write strobe, at 0x100000. The driver writes through
# 0x400000, like the BSL sets it up
ADDRSEL1 = 0xFE18
ADDRSEL1_DRIVER = 0x4008

# a `rets` in free IRAM (between the driver and the data buffer) that resync's first
# bytes point a half-received CALL at. The bytes are no-ops to an idle kernel
RESYNC_TRAP = 0x00FAF0
RETS = 0x00DB
RESYNC_PREFIX = bytes([RESYNC_TRAP & 0xFF, RESYNC_TRAP >> 8 & 0xFF, RESYNC_TRAP >> 16])

# AM29F400BB, bottom boot: (number, offset, size)
SECTORS = (
	(0, 0x00000, 0x4000),
	(1, 0x04000, 0x2000),
	(2, 0x06000, 0x2000),
	(3, 0x08000, 0x8000),
) + tuple((number, 0x10000 * (number - 3), 0x10000) for number in range(4, 11))

# boot1 (SA0) holds the reset vector and the MTOS/KWP boot code, boot2 (SA3) what it
# jumps to: they go last, boot1 the very last, so for as long as possible a reset still
# boots the old boot code
WRITE_ORDER = (4, 5, 6, 7, 8, 9, 10, 1, 2, 3, 0)
BOOT_SECTORS = (0, 3)

# manufacturer, device - after un-crossing the data lines (bsl.GetBackCrossedWord)
SUPPORTED_CHIPS = {
	(0x01, 0xAB): 'AMD AM29F400BB',
	(0x20, 0xD6): 'ST M29F400BB',
}

FC_PROG = 0x00
FC_ERASE = 0x01
FC_GETSTATE = 0x06
E_ERASE_TIMEOUT = 0x31  # the driver's poll ran out, the chip may still be erasing
E_INVALID_SECTOR = 0x33

# the kernel doesn't service the watchdog while it transmits. With the longest period
# that's 335 ms at 25 MHz, so every reply has to be well within it
REPLY_BUDGET = 0.15

ACK_TIMEOUT = 1.0
ERASE_TIMEOUT = 30.0
SECTOR_ATTEMPTS = 3
TRANSFER_ATTEMPTS = 3

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


def load_asset (relative_path: str) -> bytes:
	with open(bsl.resource_path(relative_path), 'rb') as file:
		return file.read()


def read_block_size (baudrate: int) -> int:
	'''
	Largest power of two block, up to 0x200, the kernel sends within REPLY_BUDGET
	'''
	size = PROGRAM_BLOCK
	while size > 0x10 and (size + 1) * 10 / baudrate > REPLY_BUDGET:
		size //= 2
	return size


def load_image (filename: str, log_callback=None) -> bytes:
	'''
	A whole 512 KB flash image, sanity checked so a wrong file doesn't get as far as the
	boot sectors. A few trailing bytes (an over-read) are ignored
	'''
	with open(filename, 'rb') as file:
		data = file.read()

	if len(data) < FLASH_SIZE:
		raise KernelError('{} is {} bytes, a full flash image is {}'.format(filename, len(data), FLASH_SIZE))
	if len(data) > FLASH_SIZE + 16:
		raise KernelError('{} is {} bytes, too big for a {} byte flash'.format(filename, len(data), FLASH_SIZE))
	if len(data) > FLASH_SIZE:
		log('[!] Ignoring {} trailing byte(s) past 0x{:X} in {}'.format(len(data) - FLASH_SIZE, FLASH_SIZE, filename), log_callback)
	image = data[:FLASH_SIZE]

	# reset vector: jmps seg,addr to something that isn't erased
	if image[0] != 0xFA:
		raise KernelError('No jmps at the reset vector ({}), not a SIMK43 flash image'.format(image[:4].hex()))
	target = (image[1] << 16 | image[3] << 8 | image[2]) & (FLASH_SIZE - 1)
	if image[target:target + 4] == b'\xff' * 4:
		raise KernelError('The reset vector jumps to erased flash (0x{:05X})'.format(target))
	for number in BOOT_SECTORS:
		_, offset, size = SECTORS[number]
		if image[offset:offset + size].count(0xFF) == size:
			raise KernelError('Boot sector SA{} is blank in the image'.format(number))
	if image[0x10040:0x10044] != b'ca66':
		raise KernelError('No ca66 identification at 0x10040, not a SIMK43 2.0 image')
	return image


def identification (flash: bytes) -> str:
	return flash[0x10040:0x10056].split(b'\xff')[0].decode('latin-1', 'replace')


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


def changed_sectors (current: bytes, image: bytes) -> list:
	return [number for number, offset, size in SECTORS
		if current[offset:offset + size] != image[offset:offset + size]]


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
	The BSL kernel at the other end of `link`, and the flash operations built on it
	'''

	def __init__ (self, link: KernelLink, guard: InterruptGuard | None = None, log_callback=None, progress_callback=None) -> None:
		self.link = link
		self.guard = guard or InterruptGuard()
		self.log_callback = log_callback
		self.progress_callback = progress_callback
		self.baudrate = link.channel.get_baudrate()
		self.block_size = read_block_size(self.baudrate)
		self.flash_modified = False
		self.chip = None
		# set while a sector is between erase and verify: a stop request waits
		self.critical = False

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
		if ack != bytes([bsl.A_ACK1]):
			raise KernelError('{} not acknowledged ({})'.format(what, ack.hex() or 'no answer'))

	def _done (self, what: str, timeout: float = ACK_TIMEOUT) -> None:
		end = self.link.read(1, timeout)
		if end != bytes([bsl.A_ACK2]):
			raise KernelError('{} failed ({})'.format(what, end.hex() or 'no answer'))

	@staticmethod
	def _address (address: int) -> bytes:
		return address.to_bytes(3, 'little')

	def test_comm (self) -> bool:
		try:
			self._command(bsl.C_TEST_COMM, 'TEST_COMM')
			self._done('TEST_COMM')
		except KernelError:
			return False
		return True

	def checksum (self) -> int:
		'''
		XOR of the bytes of the last READ_BLOCK/WRITE_BLOCK
		'''
		self._command(bsl.C_GETCHECKSUM, 'GET_CHECKSUM')
		reply = self._expect(2, ACK_TIMEOUT, 'GET_CHECKSUM')
		if reply[1] != bsl.A_ACK2:
			raise KernelError('GET_CHECKSUM failed ({})'.format(reply.hex()))
		return reply[0]

	def read_word (self, address: int) -> int:
		self._command(bsl.C_READ_WORD, 'READ_WORD')
		self.link.send(self._address(address))
		reply = self._expect(3, ACK_TIMEOUT, 'READ_WORD 0x{:06X}'.format(address))
		if reply[2] != bsl.A_ACK2:
			raise KernelError('READ_WORD 0x{:06X} failed ({})'.format(address, reply.hex()))
		return reply[0] | reply[1] << 8

	def write_word (self, address: int, value: int) -> None:
		self._command(bsl.C_WRITE_WORD, 'WRITE_WORD')
		self.link.send(self._address(address) + value.to_bytes(2, 'little'))
		self._done('WRITE_WORD 0x{:06X}'.format(address))
		readback = self.read_word(address)
		if readback != value:
			raise KernelError('Wrote 0x{:04X} to 0x{:06X}, reads 0x{:04X}'.format(value, address, readback))

	def write_block (self, address: int, data: bytes) -> None:
		'''
		The kernel compares every byte it writes (0x11 instead of the final acknowledge),
		and the block checksum covers what it received
		'''
		what = 'WRITE_BLOCK 0x{:06X}+0x{:X}'.format(address, len(data))
		self._command(bsl.C_WRITE_BLOCK, 'WRITE_BLOCK')
		self.link.send(self._address(address) + len(data).to_bytes(2, 'little') + bytes(data))
		status = self.link.read(1, ACK_TIMEOUT)
		if status == b'\x11':
			raise KernelError('{}: memory reads back different'.format(what))
		if status != bytes([bsl.A_ACK2]):
			raise KernelError('{} failed ({})'.format(what, status.hex() or 'no answer'))
		if self.checksum() != bsl.CalcBlockChecksum(data):
			raise KernelError('{}: checksum mismatch'.format(what))

	def read_block (self, address: int, size: int) -> bytes:
		what = 'READ_BLOCK 0x{:06X}+0x{:X}'.format(address, size)
		self._command(bsl.C_READ_BLOCK, 'READ_BLOCK')
		self.link.send(self._address(address) + size.to_bytes(2, 'little'))
		reply = self._expect(size + 1, (size + 1) * 10 / self.baudrate + ACK_TIMEOUT, what)
		if reply[-1] != bsl.A_ACK2:
			raise KernelError('{} failed (ends with {:02x})'.format(what, reply[-1]))
		data = reply[:-1]
		if self.checksum() != bsl.CalcBlockChecksum(data):
			raise KernelError('{}: checksum mismatch'.format(what))
		return data

	def call (self, address: int, registers: list, timeout: float = 2.0) -> list:
		'''
		Far call `address` with r8-r15, returns them as it left them
		'''
		what = 'CALL 0x{:06X} (r8=0x{:X})'.format(address, registers[0])
		self._command(bsl.C_CALL_FUNCTION, 'CALL')
		self.link.send(self._address(address) + b''.join(r.to_bytes(2, 'little') for r in registers))
		reply = self._expect(17, timeout, what)
		if reply[16] != bsl.A_ACK2:
			raise KernelError('{} failed ({})'.format(what, reply.hex()))
		return [reply[i] | reply[i + 1] << 8 for i in range(0, 16, 2)]

	def software_reset (self) -> None:
		self._command(bsl.C_SWRESET, 'SOFTWARE_RESET')
		self._done('SOFTWARE_RESET')

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
			if self.link.drain(quiet=0.3) == bytes([bsl.A_ACK1]) and self.test_comm():
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

	# -- flash ---------------------------------------------------------------

	def prepare (self, signature: bytes | None = None, allow_blank: bool = False) -> None:
		'''
		The write strobe where the driver expects it, the driver, and proof that both
		the read and the write path reach the flash - before anything gets erased
		'''
		self.log('[*] Kernel at {} baud, reading in 0x{:X} byte blocks'.format(self.baudrate, self.block_size))
		self.arm_trap()

		previous = self.read_word(ADDRSEL1)
		self.write_word(ADDRSEL1, ADDRSEL1_DRIVER)
		self.log('[*] ADDRSEL1: 0x{:04X} -> 0x{:04X}'.format(previous, ADDRSEL1_DRIVER))

		driver = patch_driver_watchdog(load_asset(DRIVER_ASSET))
		self.log('[*] Uploading the I4 flash driver ({} bytes, watchdog patched) to 0x{:04X}'.format(len(driver), DRIVER_ADDRESS))
		self.write_memory(DRIVER_ADDRESS, driver)
		if self.read_memory(DRIVER_ADDRESS, len(driver)) != driver:
			raise KernelError('The flash driver reads back different')

		# the driver reads through 0x80xxxx: it has to be the flash the firmware runs from
		has_data = False
		for offset in (0x00000, 0x08000, 0x10040, 0x20000, 0x7FFC0):
			mirror = self.read_memory(FLASH_MIRROR + offset, 0x40)
			if self.read_memory(FLASH_READ_BASE + offset, 0x40) != mirror:
				raise KernelError('0x{:06X} and 0x{:06X} differ, the driver wouldn\'t see the flash'.format(
					FLASH_READ_BASE + offset, FLASH_MIRROR + offset))
			has_data |= mirror.count(0xFF) != len(mirror)
		if not has_data and not allow_blank:
			raise KernelError('The flash reads blank everywhere, that\'s not the flash')
		if signature is not None:
			found = self.read_memory(FLASH_READ_BASE + 0x3FE8, len(signature))
			if found != signature:
				raise KernelError('MTOS signature {} not at 0x803FE8 ({})'.format(signature.hex(), found.hex()))
		self.log('[*] Read path: 0x800000 is the flash')

		# autoselect is written to the chip, so the IDs prove the write path too
		segment = FLASH_READ_BASE >> 16
		manufacturer = bsl.GetBackCrossedWord(self.call(DRIVER_ENTRY, [FC_GETSTATE, 0, segment, segment, 0, 0, 0, 1])[1]) & 0xFF
		device = bsl.GetBackCrossedWord(self.call(DRIVER_ENTRY, [FC_GETSTATE, 0, segment, segment, 0, 0, 1, 1])[1]) & 0xFF
		chip = SUPPORTED_CHIPS.get((manufacturer, device))
		if chip is None:
			raise KernelError('Unexpected flash IDs 0x{:02X}/0x{:02X}, refusing to touch it'.format(manufacturer, device))
		self.chip = chip
		self.log('[*] Flash chip: {} (0x{:02X}/0x{:02X})'.format(chip, manufacturer, device))

	def read_flash (self, label: str) -> bytes:
		progress = Progress(self.progress_callback, FLASH_SIZE, label)
		try:
			return self.read_memory(FLASH_READ_BASE, FLASH_SIZE, progress)
		finally:
			progress.finish()

	def erase_sector (self, number: int) -> None:
		_, offset, size = SECTORS[number]
		address = FLASH_READ_BASE + offset
		registers = [FC_ERASE, address & 0xFFFF, address >> 16, address >> 16,
			(address + size - 2) & 0xFFFF, 0, number, 1]
		self.flash_modified = True
		for _ in range(4):
			result = self.call(DRIVER_ENTRY, registers, timeout=ERASE_TIMEOUT)[7]
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
		self.write_memory(DATA_BUFFER, data)
		registers = [FC_PROG, len(data), DATA_BUFFER, 0, address >> 16, address & 0xFFFF, address >> 16, 1]
		result = self.call(DRIVER_ENTRY, registers)[7]
		if result != 0:
			raise KernelError('Programming 0x{:05X} failed (0x{:02X})'.format(offset, result))

	def write_sector (self, number: int, image: bytes) -> None:
		_, offset, size = SECTORS[number]
		target = image[offset:offset + size]
		blocks = [start for start in range(0, size, PROGRAM_BLOCK)
			if target[start:start + PROGRAM_BLOCK].count(0xFF) != PROGRAM_BLOCK]

		self.guard.check()
		self.critical = True
		try:
			self._write_sector(number, offset, size, target, blocks)
		finally:
			self.critical = False

	def _write_sector (self, number: int, offset: int, size: int, target: bytes, blocks: list) -> None:
		for attempt in range(1, SECTOR_ATTEMPTS + 1):
			self.log('[*] SA{} (0x{:05X}, {} KB){}'.format(
				number, offset, size // 1024, '' if attempt == 1 else ', attempt {}'.format(attempt)))
			try:
				self.erase_sector(number)
				progress = Progress(self.progress_callback, size * 2, 'SA{} program + verify'.format(number))
				try:
					for index, start in enumerate(blocks):
						self.program_block(offset + start, target[start:start + PROGRAM_BLOCK])
						progress((index + 1) * size // len(blocks))
					written = self.read_memory(FLASH_READ_BASE + offset, size, progress, done=size)
				finally:
					progress.finish()
			except KernelError as e:
				self.log('[!] SA{}: {}'.format(number, e))
				self.recover()
				continue

			if written == target:
				self.log('[+] SA{} written and verified'.format(number))
				return
			first = next(index for index in range(size) if written[index] != target[index])
			self.log('[!] SA{} verify failed at 0x{:05X}'.format(number, offset + first))

		raise KernelError('SA{} failed {} times'.format(number, SECTOR_ATTEMPTS))


# -- sessions ---------------------------------------------------------------

def probe (kernel: Kernel, signature: bytes | None = None) -> None:
	'''
	Everything a full flash does before the first erase, and whether the kernel is left
	alone for a while - something like an external watchdog would reset it
	'''
	kernel.prepare(signature)
	kernel.log('[*] Idle for 5 s, then pinging the kernel for 10 s')
	time.sleep(5)
	started = time.monotonic()
	while time.monotonic() - started < 10:
		kernel.guard.check()
		if not kernel.test_comm():
			raise KernelError('The kernel stopped answering')
		time.sleep(0.5)
	head = kernel.read_memory(FLASH_READ_BASE, 4)
	ident = kernel.read_memory(FLASH_READ_BASE + 0x10040, 0x16).split(b'\xff')[0].decode('latin-1', 'replace')
	kernel.log('[*] Reset vector {}, identification {}'.format(head.hex(), ident))
	kernel.log('[+] Probe passed: kernel, driver, read and write path all good')


def read (kernel: Kernel, output_filename: str, signature: bytes | None = None, allow_blank: bool = False) -> str:
	kernel.prepare(signature, allow_blank=allow_blank)
	flash = kernel.read_flash('Reading flash')
	kernel.log('[*] Identification: {}'.format(identification(flash)))
	return save(output_filename, flash, kernel.log_callback)


def full_flash (kernel: Kernel, image: bytes, backup_filename: str, confirm, signature: bytes | None = None, allow_blank: bool = False) -> bool:
	'''
	Backup, then every sector that differs from `image` (boot sectors last), then a
	full verify. False if nothing needed writing or the user said no
	'''
	kernel.prepare(signature, allow_blank=allow_blank)

	kernel.log('[*] Reading the whole flash first')
	current = kernel.read_flash('Backup')
	save(backup_filename, current, kernel.log_callback)

	changed = changed_sectors(current, image)
	kernel.log('[*] ECU: {}, image: {}'.format(identification(current), identification(image)))
	if not changed:
		kernel.log('[+] The flash already matches the image')
		return False
	kernel.log('[*] To rewrite, in order: {}'.format(', '.join('SA{}'.format(n) for n in WRITE_ORDER if n in changed)))

	boot = [number for number in BOOT_SECTORS if number in changed]
	warning = 'This erases and rewrites {} of 11 sectors'.format(len(changed))
	if boot:
		warning += ', boot sector(s) {} included. If it fails or loses power while those are written, the ECU won\'t boot until it\'s recovered over BSL'.format(
			' and '.join('SA{}'.format(number) for number in boot))
	if not confirm(warning + '. Continue?'):
		kernel.log('[!] Aborted, nothing written')
		return False

	for number in WRITE_ORDER:
		if number in changed:
			kernel.write_sector(number, image)

	for attempt in range(3):
		bad = changed_sectors(kernel.read_flash('Verifying'), image)
		if not bad:
			kernel.log('[+] The whole flash matches the image')
			return True
		if attempt == 2:
			break
		kernel.log('[!] Verify failed in {}, rewriting'.format(', '.join('SA{}'.format(n) for n in bad)))
		for number in WRITE_ORDER:
			if number in bad:
				kernel.write_sector(number, image)
	raise KernelError('The flash still doesn\'t match the image')


def run_session (channel, mode: str, image: bytes | None = None, output_filename: str | None = None,
                 signature: bytes | None = None, attached: bool = False, confirm=None, attach_hint: str | None = None,
                 log_callback=None, progress_callback=None) -> bool:
	'''
	Run `mode` (probe, read, fullflash) on a kernel that's up and answering on `channel`.

	Ends with a software reset when it worked, or when it failed before the flash was
	touched. Otherwise the kernel is left running for --mtos-attach.

	:return: whether the ECU was reset
	'''
	link = KernelLink(channel)
	stamp = time.strftime('%Y%m%d_%H%M%S')
	with InterruptGuard() as guard:
		kernel = Kernel(link, guard, log_callback=log_callback, progress_callback=progress_callback)
		try:
			if mode == 'probe':
				probe(kernel, signature)
			elif mode == 'read':
				read(kernel, output_filename or 'mtos_read_{}.bin'.format(stamp), signature, allow_blank=attached)
			elif mode == 'fullflash':
				full_flash(kernel, image, output_filename or 'mtos_backup_{}.bin'.format(stamp),
					confirm or (lambda message: False), signature, allow_blank=attached)
			else:
				raise KernelError('Unknown kernel mode {}'.format(mode))
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
				if not isinstance(e, KernelError):
					raise
				return False
			if not isinstance(e, KeyboardInterrupt) and _reset(kernel, resync=not isinstance(e, KernelAbort)):
				kernel.log('[*] The flash wasn\'t touched, the ECU was reset')
				return True
			kernel.log('[!] The flash wasn\'t touched: switch the ECU off and on again')
			if not isinstance(e, KernelError):
				raise
			return False

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


def attach (channel, log_callback=None) -> None:
	'''
	Find a kernel a previous session left running on `channel`
	'''
	log('[*] Looking for a kernel at {} baud'.format(channel.get_baudrate()), log_callback)
	kernel = Kernel(KernelLink(channel), log_callback=log_callback)
	if not kernel.resync():
		raise KernelError('No kernel answering at {} baud. Same --desired-baudrate as the session that left it?'.format(channel.get_baudrate()))
	log('[+] Kernel found', log_callback)


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
