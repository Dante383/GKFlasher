'''
MTOS full flash: the BSL kernel and the I4 flash driver (kernel.py), run on a live SIMK43.

The MTOS loader streams a short prologue plus the BSL kernel into XRAM
(mtos_asm.payload_bsl_kernel_shim). From then on the ECU speaks the BSL protocol over the
K-line at the diagnostic session's baudrate. What's different from the BSL: the firmware
set the bus up, but its flash write strobe isn't where the driver writes, and the
watchdog is running and can't be stopped - the driver is patched to service it while it
waits for an erase (mtos_asm.patch_driver_watchdog).

The firmware never gets the CPU back: a session ends with the kernel's software reset.
If anything goes wrong once the flash has been touched, the kernel is left running so
the host can attach to it again (--mtos-attach) rather than rebooting a half written
flash.
'''
import time
from dataclasses import replace

from .kernel import (
	CHIPS, DRIVER_I4, FLASH_READ_BASE, Flash, Kernel, KernelError, KernelLink, identification, log, read_flash,
	run_session as run_kernel_session, write_flash,
)
from .mtos_asm import patch_driver_watchdog

# SIMK43 2.0: 512 KB, bottom boot
SUPPORTED_CHIPS = {ids: CHIPS[ids] for ids in ((0x01, 0xAB), (0x20, 0xD6))}
SECTORS = CHIPS[(0x01, 0xAB)].sectors
FLASH_SIZE = 0x80000

# the firmware runs from the 0x080000 mirror of the flash the driver reads at 0x800000.
# Both are CS0, which only sees A0-A18
FLASH_MIRROR = 0x080000

# the firmware has CS1, the flash write strobe, at 0x100000. The driver writes through
# 0x400000, like the BSL sets it up
ADDRSEL1 = 0xFE18
ADDRSEL1_DRIVER = 0x4008

MTOS_DRIVER = replace(DRIVER_I4, patch=patch_driver_watchdog)

# boot1 (SA0) holds the reset vector and the MTOS/KWP boot code, boot2 (SA3) what it
# jumps to: they go last, boot1 the very last, so for as long as possible a reset still
# boots the old boot code
WRITE_ORDER = (4, 5, 6, 7, 8, 9, 10, 1, 2, 3, 0)
BOOT_SECTORS = (0, 3)


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


def prepare (kernel: Kernel, signature: bytes | None = None, allow_blank: bool = False) -> Flash:
	'''
	The write strobe where the driver expects it, the driver, and proof that both the read
	and the write path reach the flash - before anything gets erased
	'''
	previous = kernel.read_word(ADDRSEL1)
	kernel.write_word(ADDRSEL1, ADDRSEL1_DRIVER)
	kernel.log('[*] ADDRSEL1: 0x{:04X} -> 0x{:04X}'.format(previous, ADDRSEL1_DRIVER))

	flash = Flash(kernel, MTOS_DRIVER)
	flash.upload()

	# the driver reads through 0x80xxxx: it has to be the flash the firmware runs from
	has_data = False
	for offset in (0x00000, 0x08000, 0x10040, 0x20000, 0x7FFC0):
		mirror = kernel.read_memory(FLASH_MIRROR + offset, 0x40)
		if kernel.read_memory(FLASH_READ_BASE + offset, 0x40) != mirror:
			raise KernelError('0x{:06X} and 0x{:06X} differ, the driver wouldn\'t see the flash'.format(
				FLASH_READ_BASE + offset, FLASH_MIRROR + offset))
		has_data |= mirror.count(0xFF) != len(mirror)
	if not has_data and not allow_blank:
		raise KernelError('The flash reads blank everywhere, that\'s not the flash')
	if signature is not None:
		found = kernel.read_memory(FLASH_READ_BASE + 0x3FE8, len(signature))
		if found != signature:
			raise KernelError('MTOS signature {} not at 0x803FE8 ({})'.format(signature.hex(), found.hex()))
	kernel.log('[*] Read path: 0x800000 is the flash')

	flash.identify(SUPPORTED_CHIPS)
	return flash


def probe (kernel: Kernel, signature: bytes | None = None) -> None:
	'''
	Everything a full flash does before the first erase, and whether the kernel is left
	alone for a while - something like an external watchdog would reset it
	'''
	prepare(kernel, signature)
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
	return read_flash(prepare(kernel, signature, allow_blank=allow_blank), output_filename)


def full_flash (kernel: Kernel, image: bytes, backup_filename: str, confirm, signature: bytes | None = None, allow_blank: bool = False) -> bool:
	'''
	Backup, then every sector that differs from `image` (boot sectors last), then a
	full verify. False if nothing needed writing or the user said no
	'''
	flash = prepare(kernel, signature, allow_blank=allow_blank)
	return write_flash(flash, image, backup_filename, confirm, order=WRITE_ORDER, boot_sectors=BOOT_SECTORS)


def run_session (channel, mode: str, image: bytes | None = None, output_filename: str | None = None,
                 signature: bytes | None = None, attached: bool = False, confirm=None, attach_hint: str | None = None,
                 log_callback=None, progress_callback=None) -> bool:
	'''
	Run `mode` (probe, read, fullflash) on a kernel that's up and answering on `channel`.

	Ends with a software reset when it worked, or when it failed before the flash was
	touched. Otherwise the kernel is left running for --mtos-attach.

	:return: whether the ECU was reset
	'''
	stamp = time.strftime('%Y%m%d_%H%M%S')
	if mode == 'probe':
		job = lambda kernel: probe(kernel, signature)
	elif mode == 'read':
		job = lambda kernel: read(kernel, output_filename or 'mtos_read_{}.bin'.format(stamp), signature, allow_blank=attached)
	elif mode == 'fullflash':
		job = lambda kernel: full_flash(kernel, image, output_filename or 'mtos_backup_{}.bin'.format(stamp),
			confirm or (lambda message: False), signature, allow_blank=attached)
	else:
		raise KernelError('Unknown kernel mode {}'.format(mode))

	kernel = Kernel(KernelLink(channel), log_callback=log_callback, progress_callback=progress_callback)
	run_kernel_session(kernel, job, reset_when_done=True, attached=attached, attach_hint=attach_hint)
	return kernel.ecu_reset


def attach (channel, log_callback=None) -> None:
	'''
	Find a kernel a previous session left running on `channel`
	'''
	log('[*] Looking for a kernel at {} baud'.format(channel.get_baudrate()), log_callback)
	kernel = Kernel(KernelLink(channel), log_callback=log_callback)
	if not kernel.resync():
		raise KernelError('No kernel answering at {} baud. Same --desired-baudrate as the session that left it?'.format(channel.get_baudrate()))
	log('[+] Kernel found', log_callback)
