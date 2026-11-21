'''
BSL: the C167's bootstrap loader, with the ECU held in boot mode, starts the SIMK4x
kernel (kernel.py). Reads the external flash or the internal ROM, writes the external
flash - the boot sectors included, and it can't brick the ECU: the boot ROM is in the
CPU, it doesn't need the flash.

Over any gkbus K-line channel (a KKL cable or a J2534 adapter), at a fixed baudrate the
boot ROM picks up from the first byte.

The boot ROM takes 32 bytes (assets/simk4x_bootstrap.bin) and runs them. That loader
takes the kernel and runs it. A kernel a previous run left behind is picked up instead:
nothing resets the ECU at the end, the kernel stays until the ECU is switched off.
'''
import time
from .lzss import CompressedFlash, RamCodec, BSL_LAYOUT

from .kernel import (
	ACK, CHIPS, DONE, DRIVER_I4, DRIVER_V6, DRIVERS, FLASH_READ_BASE,
	KERNEL_ASSET, KERNEL_STARTED, VERIFY_FAILED, Flash, Kernel, KernelError, KernelLink, Progress, enter,
	identification, load_asset, log, read_flash, run_session, save, write_flash,
)
from .ecu import calibration_identification

BOOTSTRAP_ASSET = 'assets/simk4x_bootstrap.bin'
BOOTSTRAP_SIZE = 32
# The generated bootstrap loads the kernel at 0xFA60, below the 0xFC00 data buffer.
MAX_KERNEL_SIZE = 0xFC00 - 0xFA60
# the bootstrap loader's first words once it runs
LOADER_STARTED = 0x01

DEFAULT_BAUDRATE = 750000
BAUDRATES = (750000, 57600, 38400, 19200, 9600)
FAST_PROGRAM_THRESHOLD = 57600
FAST_PROGRAM_BUFFER = 0xE000
FAST_PROGRAM_BLOCK = 0x800
MODES = ('hwinfo', 'read', 'readint', 'write')
DRIVER_CHOICES = ('auto',) + tuple(DRIVERS)

# the boot ROM's answer to the first 0x00
CPU_IDS = {
	0xA5: 'C167 (old)',
	0xC5: 'SAK-C167CR-LM',
	0xD5: 'SAK-C167CS-LM',
}

SYSCON = 0xFF12
# ROMEN = 0: the external flash from 0x000000
SYSCON_EXTERNAL = 0xE204
# ROMEN = 1, ROMS1 = 1: the internal ROM at 0x010000
SYSCON_INTERNAL_ROM = 0xF604
# CS0, the flash: 16 bit demultiplexed bus, 2 wait states
BUSCON0 = 0xFF0C
BUSCON0_FLASH = 0x04AD
# CS1, the flash's write strobe, at 0x400000
ADDRSEL1 = 0xFE18
ADDRSEL1_FLASH = 0x4008
BUSCON1 = 0xFF14
BUSCON1_FLASH = 0x848E

INTERNAL_ROM = 0x010000
INTERNAL_ROM_SIZE = 0x8000

# a failed write is picked up where it stopped
ATTACH_HINT = 'the same BSL command again, it picks up the running kernel. Powering off is safe too: BSL starts from scratch, it doesn\'t need the flash'

# an over-read image
TRAILING_BYTES = 16


def load_image (filename: str) -> bytes:
	'''
	The whole external flash, checked as far as it can be before the chip is known
	'''
	with open(filename, 'rb') as file:
		image = file.read()
	sizes = sorted({chip.size for chip in CHIPS.values()})
	if not any(size <= len(image) <= size + TRAILING_BYTES for size in sizes):
		raise KernelError('{} is {} bytes, a whole flash image is {}'.format(
			filename, len(image), ', '.join('0x{:X}'.format(size) for size in sizes)))
	if image[:4] == b'\xff' * 4:
		raise KernelError('{} has no reset vector (erased at 0x0), not a flash image'.format(filename))
	return image


def fit_image (flash: Flash, image: bytes) -> bytes:
	'''
	`image` for the chip that was found, a few trailing bytes cut off
	'''
	size = flash.chip.size
	if not size <= len(image) <= size + TRAILING_BYTES:
		raise KernelError('The image is {} bytes, the {} is {}'.format(len(image), flash.chip.name, size))
	if len(image) > size:
		flash.log('[!] Ignoring {} trailing byte(s) past 0x{:X} in the image'.format(len(image) - size, size))
	return image[:size]


def bootstrap (kernel: Kernel, initial_reply=None) -> str | None:
	'''
	The kernel up and answering: started through the boot ROM, or one a previous run left
	running.

	:return: the CPU, None for a kernel that was already running
	'''
	link = kernel.link
	# a kernel finishing a block, or the adapter switching to K-line. Whatever it is,
	# the boot ROM has to get 0x00 first: it measures the baudrate on it
	junk = link.drain(quiet=0.02 if link._serial else 0.3) if initial_reply is None else b''
	if junk:
		kernel.log('[*] Ignoring {} byte(s) on the line'.format(len(junk)))

	# to a kernel it's a no-op command, or the last byte of one it was waiting for
	if initial_reply is None:
		link.send(b'\x00')
		reply = link.read(1, 1.0)
	else:
		reply = initial_reply
	if not reply:
		kernel.log('[*] No answer, looking for a kernel left in the middle of a command (up to 20 s)')
		return _find_kernel(kernel)
	if link.drain(quiet=0.02 if link._serial else 0.2):
		kernel.log('[*] More than the boot ROM would say, looking for a kernel left running')
		return _find_kernel(kernel)

	if reply[0] in (ACK, DONE, VERIFY_FAILED):
		if not kernel.test_comm():
			return _find_kernel(kernel)
		kernel.log('[*] Kernel already running')
		return None

	cpu = CPU_IDS.get(reply[0])
	if cpu is None:
		kernel.log('[!] Unknown CPU ID 0x{:02X}, carrying on'.format(reply[0]))
		cpu = 'unknown (0x{:02X})'.format(reply[0])
	else:
		kernel.log('[*] Boot ROM answered: {} (0x{:02X})'.format(cpu, reply[0]))

	loader = load_asset(BOOTSTRAP_ASSET)
	code = load_asset(KERNEL_ASSET)
	if len(loader) != BOOTSTRAP_SIZE or not code or len(code) % 2 or len(code) > MAX_KERNEL_SIZE:
		raise KernelError('Broken install: bootstrap must be {} bytes; kernel must be nonempty, word-aligned and at most {} bytes'.format(
			BOOTSTRAP_SIZE, MAX_KERNEL_SIZE))

	kernel.log('[*] Sending the bootstrap loader')
	link.send(loader)
	_expect_started(link, LOADER_STARTED, 'bootstrap loader')
	kernel.log('[*] Sending the kernel')
	link.send(code)
	_expect_started(link, KERNEL_STARTED, 'kernel')
	if not kernel.test_comm():
		raise KernelError('The kernel started but doesn\'t answer')
	kernel.log('[+] Kernel running')
	return cpu


def _expect_started (link: KernelLink, code: int, what: str) -> None:
	reply = link.read(1, 1.0)
	if reply != bytes([code]):
		raise KernelError('The {} didn\'t start ({} instead of {:02x})'.format(what, reply.hex() or 'no answer', code))


def _find_kernel (kernel: Kernel) -> None:
	if not kernel.resync():
		raise KernelError('No answer at {} baud. Switch the ECU off, then on in boot mode (BSL pin), and try again. '
			'A kernel left running only answers at the baudrate it was started at'.format(kernel.baudrate))
	kernel.log('[*] Kernel already running')
	return None


def configure (kernel: Kernel, internal_rom: bool = False) -> None:
	'''
	The bus the way the firmware would set it up: the flash on CS0, its write strobe on
	CS1. Or the internal ROM mapped in instead
	'''
	if internal_rom:
		registers = (('SYSCON', SYSCON, SYSCON_INTERNAL_ROM),)
	else:
		registers = (
			('SYSCON', SYSCON, SYSCON_EXTERNAL),
			('BUSCON0', BUSCON0, BUSCON0_FLASH),
			('ADDRSEL1', ADDRSEL1, ADDRSEL1_FLASH),
			('BUSCON1', BUSCON1, BUSCON1_FLASH),
		)
	for name, address, value in registers:
		# some bits read back different, what matters is that the flash answers
		kernel.write_word(address, value, verify=False)
		readback = kernel.read_word(address)
		if readback != value:
			kernel.log('[!] {} reads 0x{:04X} after writing 0x{:04X}'.format(name, readback, value))


def load_flash (kernel: Kernel, driver: str = 'auto') -> Flash:
	'''
	The flash, through the driver that gets known IDs out of it. The I4 board has the
	data lines crossed, so the wrong driver reads them crossed. The V6 driver goes first:
	on an I4 board it can't get the chip further than autoselect
	'''
	configure(kernel)
	if driver != 'auto':
		flash = CompressedFlash(kernel, DRIVERS[driver], layout=BSL_LAYOUT)
		flash.upload()
		flash.identify()
		return flash

	tried = []
	for candidate in (DRIVER_V6, DRIVER_I4):
		flash = CompressedFlash(kernel, candidate, layout=BSL_LAYOUT)
		flash.upload()
		ids = flash.read_ids()
		if ids in CHIPS:
			kernel.log('[*] The flash answers to the {} driver'.format(candidate.name))
			flash.identify(ids=ids)
			return flash
		found = '0x{:02X}/0x{:02X}'.format(*ids)
		kernel.log('[*] Not a known chip through the {} driver ({})'.format(candidate.name, found))
		tried.append('{}: {}'.format(candidate.name, found))
	raise KernelError('No flash driver gets known chip IDs ({}). Not a SIMK4x, or the flash doesn\'t answer'.format(', '.join(tried)))


# -- jobs -------------------------------------------------------------------

def hwinfo (kernel: Kernel, cpu: str | None, driver: str) -> None:
	kernel.log('[*] CPU: {}'.format(cpu or 'not known, the kernel was already running'))
	flash = load_flash(kernel, driver)
	head = kernel.read_memory(FLASH_READ_BASE, 4)
	ident = calibration_identification(lambda offset, size: kernel.read_memory(FLASH_READ_BASE + offset, size), flash.chip.size)
	kernel.log('[*] Reset vector {}, identification {}'.format(head.hex(), ident))


def read (kernel: Kernel, output_filename: str, driver: str) -> None:
	flash = load_flash(kernel, driver)
	read_flash(flash, output_filename)


def read_internal_rom (kernel: Kernel, output_filename: str, size: int) -> None:
	configure(kernel, internal_rom=True)
	codec = RamCodec(kernel, BSL_LAYOUT)
	codec.upload()
	progress = Progress(kernel.progress_callback, size, 'Reading the internal ROM')
	started_at = time.monotonic()
	try:
		data = codec.read_memory(INTERNAL_ROM, size, progress)
	finally:
		progress.finish()
	save(output_filename, data, kernel.log_callback, started_at=started_at)


def prepare_programming (flash: Flash) -> None:
	'''Qualify the C167's 2 KiB XRAM staging area before any erase at high serial speeds.'''
	kernel = flash.kernel
	if isinstance(flash, CompressedFlash) and flash.codec.entries:
		return  # Keep the qualified codec workspace clear of raw 2 KiB staging.
	if not kernel.link._serial or kernel.baudrate <= FAST_PROGRAM_THRESHOLD:
		return
	probe = bytes(range(256)) * (FAST_PROGRAM_BLOCK // 256)
	kernel.write_memory(FAST_PROGRAM_BUFFER, probe)
	if kernel.read_memory(FAST_PROGRAM_BUFFER, FAST_PROGRAM_BLOCK) != probe:
		raise KernelError('BSL XRAM staging verification failed; flash has not been erased')
	flash.buffer_address = FAST_PROGRAM_BUFFER
	flash.program_block_size = FAST_PROGRAM_BLOCK


def write (kernel: Kernel, image: bytes, backup_filename: str, confirm, driver: str,
           skip_verify: bool = False, skip_backup: bool = False, force: bool = False) -> None:
	flash = load_flash(kernel, driver)
	flash.check_writable()
	prepare_programming(flash)
	write_flash(flash, fit_image(flash, image), backup_filename, confirm,
		skip_verify=skip_verify, skip_backup=skip_backup, force=force)


def cli_confirm (message: str) -> bool:
	return input('[?] {} [y/N] '.format(message)).strip().lower() in ('y', 'yes')


def bsl_handler (channel, mode: str, image_filename: str | None = None, output_filename: str | None = None,
                 driver: str = 'auto', rom_size: int = INTERNAL_ROM_SIZE, confirm=cli_confirm,
                 skip_verify: bool = False, log_callback=None, progress_callback=None,
                 skip_backup: bool = False, force: bool = False, auto_baudrate: bool = False) -> bool:
	'''
	Run `mode` (MODES) over BSL on an open K-line channel at the boot ROM's baudrate.

	:return: whether it went through
	'''
	if mode not in MODES:
		log('[!] Unknown BSL mode {}, one of {}'.format(mode, ', '.join(MODES)), log_callback)
		return False
	if driver not in DRIVER_CHOICES:
		log('[!] Unknown flash driver {}, one of {}'.format(driver, ', '.join(DRIVER_CHOICES)), log_callback)
		return False
	if not 0 < rom_size <= 0x100000:
		log('[!] Internal ROM size 0x{:X} makes no sense'.format(rom_size), log_callback)
		return False

	image = None
	if mode == 'write':
		if not image_filename:
			log('[!] BSL write needs the image to write', log_callback)
			return False
		try:
			image = load_image(image_filename)
		except (KernelError, OSError) as e:
			log('[!] {}'.format(e), log_callback)
			return False
		log('[*] Image {}: {} bytes, {}'.format(image_filename, len(image), identification(image)), log_callback)

	previous = enter(channel, log_callback)
	try:
		link = KernelLink(channel)
		kernel = Kernel(link, log_callback=log_callback, progress_callback=progress_callback)
		log('[*] BSL {}: switch the ECU on in boot mode first'.format(
			'with automatic baudrate discovery' if auto_baudrate else 'at {} baud'.format(kernel.baudrate)), log_callback)
		try:
			if auto_baudrate:
				from .loader_baudrate import locate_loader, discover_loader_baudrate
				kernel, reply = locate_loader(channel, log_callback)
				kernel.progress_callback = progress_callback
				cpu = bootstrap(kernel, reply) if reply is not None else None
				discover_loader_baudrate(kernel)
			else:
				cpu = bootstrap(kernel)
		except KernelError as e:
			log('[!] {}'.format(e), log_callback)
			return False

		stamp = time.strftime('%Y%m%d_%H%M%S')
		if mode == 'hwinfo':
			job = lambda kernel: hwinfo(kernel, cpu, driver)
		elif mode == 'read':
			job = lambda kernel: read(kernel, output_filename or 'bsl_read_{}.bin'.format(stamp), driver)
		elif mode == 'readint':
			job = lambda kernel: read_internal_rom(kernel, output_filename or 'bsl_irom_{}.bin'.format(stamp), rom_size)
		else:
			job = lambda kernel: write(kernel, image, output_filename or 'bsl_backup_{}.bin'.format(stamp),
				confirm or (lambda message: False), driver,
				skip_verify=skip_verify, skip_backup=skip_backup, force=force)

		hint = ('the same BSL command with --bsl-baudrate {}'.format(kernel.baudrate)
			if auto_baudrate else ATTACH_HINT)
		finished = run_session(kernel, job, reset_when_done=False, attach_hint=hint)
		if finished and auto_baudrate and channel.get_baudrate() != 57600:
			from .loader_baudrate import restore_loader_baudrate
			restore_loader_baudrate(kernel)
		if finished:
			log('[*] Done!', log_callback)
		return finished
	finally:
		try:
			channel.set_timing(previous)
		except Exception:
			pass
