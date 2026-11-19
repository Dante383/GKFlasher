'''
MTOS full flash: the BSL kernel and the I4 flash driver (kernel.py), run on a live SIMK43.

The MTOS loader streams a short prologue plus the BSL kernel into XRAM
(mtos_asm.payload_bsl_kernel_shim). From then on the ECU speaks the BSL protocol over the
K-line at 750 kbaud after the loader's handshake. What's different from the BSL: the firmware
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
	CHIPS, Chip, DRIVER_I4, FLASH_READ_BASE, Flash, Kernel, KernelError, KernelLink, identification, log, read_flash,
	run_session as run_kernel_session, write_flash,
)
from .mtos_asm import patch_driver_watchdog
from .ecu import identify_ecu_image, identify_ecu_layout, ECUIdentificationException
from .memory import prepare_flash_payloads
from ecu_definitions import ECU_IDENTIFICATION_TABLE

# I4 SIMK43 boards with bottom-boot 4/8 Mbit flash.
SUPPORTED_CHIPS = {ids: CHIPS[ids] for ids in ((0x01, 0xAB), (0x20, 0xD6), (0x01, 0x58))}
IMAGE_SIZES = (0x80000, 0x100000)

# the firmware has CS1, the flash write strobe, at 0x100000. The driver writes through
# 0x400000, like the BSL sets it up
ADDRSEL1 = 0xFE18
ADDRSEL1_DRIVER = 0x4008

MTOS_DRIVER = replace(DRIVER_I4, patch=patch_driver_watchdog)

# SA0 holds the reset vector and the boot code - all of it up to ca663056. ca663057
# moved most of it to boot2 in SA3, where its reset vector jumps; on older firmware SA3
# is data, or blank. The boot code a full flash leaves behind goes last, SA0 the very
# last, so for as long as possible a reset still boots the old boot code

def reset_target (flash: bytes) -> int | None:
	'''Where the reset vector's jmps goes, as a flash offset. None without a jmps'''
	if len(flash) < 4 or flash[0] != 0xFA:
		return None
	return (flash[1] << 16 | flash[3] << 8 | flash[2]) & (len(flash) - 1)


def boot_sectors (flash: bytes) -> set:
	'''SA0, and the sector the reset vector jumps to'''
	target = reset_target(flash)
	return {0} | {number for number, offset, size in Chip("image", len(flash)).sectors
		if target is not None and offset <= target < offset + size}


def write_order (current: bytes, image: bytes) -> tuple:
	'''
	The image's boot code last, SA0 the very last of it. Sectors that only the ECU's
	current boot code needs (ca663057's boot2 when going back to ca663056) come after
	SA0: until then the old boot code is whole, after it the new one is
	'''
	new = boot_sectors(image) - {0}
	old = boot_sectors(current) - new - {0}
	data = tuple(number for number, _, _ in Chip("image", len(image)).sectors if number not in new | old | {0})
	return data + tuple(sorted(new)) + (0,) + tuple(sorted(old))


def warn_sectors (current: bytes, image: bytes) -> tuple:
	'''The boot sectors of both the ECU's current flash and the image'''
	return tuple(sorted(boot_sectors(current) | boot_sectors(image)))


def _image_layout(image):
	try:
		return identify_ecu_image(image)
	except ECUIdentificationException as exc:
		raise KernelError("The selected BIN file failed the MTOS safety checks. Don't override it, you WILL brick the ECU!!") from exc


def load_image (filename: str, log_callback=None, warn_check=None, calibration_only: bool = False, override_safety_checks: bool = False) -> bytes:
	"""Validate a full 4/8 Mbit BIN against the ECU definitions before entering MTOS."""
	with open(filename, 'rb') as file:
		data = file.read()
	size = next((size for size in IMAGE_SIZES if size <= len(data) <= size + 16), None)
	if size is None:
		raise KernelError('{} is {} bytes; a full image must be 524288 or 1048576 bytes'.format(filename, len(data)))
	if len(data) > size:
		log('[!] Ignoring {} trailing byte(s) in {}'.format(len(data) - size, filename), log_callback)
	image = data[:size]
	if not calibration_only:
		target = reset_target(image)
		if target is None:
			raise KernelError('No jmps at the reset vector ({}), not a SIMK43 flash image'.format(image[:4].hex()))
		if image[target:target + 4] == b'\xff' * 4:
			message = 'The image reset vector jumps to erased flash (0x{:05X}). Continue?'.format(target)
			if warn_check is None or not warn_check(message):
				raise KernelError('Reset vector points to erased flash')
	try:
		layout = _image_layout(image)
	except KernelError:
		if not override_safety_checks:
			raise
		log('[!] Overriding unrecognized BIN identification; size, reset-vector and hardware checks remain.', log_callback)
		return image
	try:
		prepare_flash_payloads(layout, image, flash_program=not calibration_only)
	except ValueError as exc:
		raise KernelError(str(exc)) from exc
	return image


def prepare (kernel: Kernel, signature: bytes | None = None, allow_blank: bool = False, warn_check=None, compressed: bool = False) -> Flash:
	'''
	The write strobe where the driver expects it, the driver, and proof that both the read
	and the write path reach the flash - before anything gets erased.
	
	:param allow_blank: if True, allow a flash that reads blank everywhere
	:param warn_check: optional callback(warning_message) -> bool for risky conditions.
	                     Defaults to always returning True if not provided.
	'''
	previous = kernel.read_word(ADDRSEL1)
	kernel.write_word(ADDRSEL1, ADDRSEL1_DRIVER)
	kernel.log('[*] ADDRSEL1: 0x{:04X} -> 0x{:04X}'.format(previous, ADDRSEL1_DRIVER))

	if compressed:
		from .lzss import CompressedFlash
		flash = CompressedFlash(kernel, MTOS_DRIVER)
	else:
		flash = Flash(kernel, MTOS_DRIVER)
	flash.upload()
	chip = flash.identify(SUPPORTED_CHIPS)
	mirror_base = 0x80000 if chip.size == 0x80000 else 0

	# the driver reads through 0x80xxxx: it has to be the flash the firmware runs from
	has_data = False
	offsets = {0, chip.size - 0x40}
	for identifier in ECU_IDENTIFICATION_TABLE:
		definition = identifier['ecu']
		if definition['eeprom_size_bytes'] == chip.size:
			offsets.update(region['read']['address'] + definition['bin_offset']
				for region in definition['regions'].values())
	for offset in sorted(offsets):
		cpu_address = mirror_base + offset
		mirror = kernel.read_memory(cpu_address, 0x40)
		if kernel.read_memory(FLASH_READ_BASE + offset, 0x40) != mirror:
			raise KernelError('0x{:06X} and 0x{:06X} differ, the driver wouldn\'t see the flash'.format(
				FLASH_READ_BASE + offset, cpu_address))
		has_data |= mirror.count(0xFF) != len(mirror)
	if not has_data and not allow_blank:
		if warn_check is None or warn_check('The flash appears to be blank (all 0xFF). This is unusual unless you just erased it. Continue?'):
			kernel.log('[*] Allowing blank flash')
		else:
			raise KernelError('The flash reads blank everywhere, that\'s not the flash')
	if signature is not None:
		found = kernel.read_memory(FLASH_READ_BASE + 0x3FE8, len(signature))
		if found != signature:
			raise KernelError('MTOS signature {} not at 0x803FE8 ({})'.format(signature.hex(), found.hex()))
	kernel.log('[*] Read path: 0x800000 is the flash')

	return flash


def probe (kernel: Kernel, signature: bytes | None = None) -> None:
	'''
	Everything a full flash does before the first erase, and whether the kernel is left
	alone for a while - something like an external watchdog would reset it
	'''
	flash = prepare(kernel, signature)
	kernel.log('[*] Idle for 5 s, then pinging the kernel for 10 s')
	time.sleep(5)
	started = time.monotonic()
	while time.monotonic() - started < 10:
		kernel.guard.check()
		if not kernel.test_comm():
			raise KernelError('The kernel stopped answering')
		time.sleep(0.5)
	head = kernel.read_memory(FLASH_READ_BASE, 4)
	try:
		layout = identify_ecu_layout(lambda offset, size: kernel.read_memory(FLASH_READ_BASE + offset, size), flash.chip.size)
		region = layout.get_region('calibration').read
		offset = layout.calculate_bin_offset(region.address) + 0x40
		ident = kernel.read_memory(FLASH_READ_BASE + offset, 0x16).split(b'\xff')[0].decode('latin-1', 'replace')
	except ECUIdentificationException:
		ident = 'unknown'
	kernel.log('[*] Reset vector {}, identification {}'.format(head.hex(), ident))
	kernel.log('[+] Probe passed: kernel, driver, read and write path all good')


def read (kernel: Kernel, output_filename: str, signature: bytes | None = None, allow_blank: bool = False, warn_check=None) -> str:
	return read_flash(prepare(kernel, signature, allow_blank=allow_blank, warn_check=warn_check), output_filename)


def full_flash (kernel: Kernel, image: bytes, backup_filename: str, confirm, signature: bytes | None = None, allow_blank: bool = False, warn_check=None, skip_verify: bool = False, calibration_only: bool = False, skip_backup: bool = False, force: bool = False, override_safety_checks: bool = False) -> bool:
	'''
	Backup, then every sector that differs from `image` (boot code last, see
	write_order), then a full verify. False if nothing needed writing or the user said no
	'''
	flash = prepare(kernel, signature, allow_blank=allow_blank, warn_check=warn_check, compressed=True)
	write_range = None
	if calibration_only:
		try:
			layout = _image_layout(image)
		except KernelError:
			if not override_safety_checks:
				raise
			# Unknown input identifiers must not guess a calibration erase range.
			layout = identify_ecu_layout(lambda offset, size: kernel.read_memory(FLASH_READ_BASE + offset, size), flash.chip.size)
		region = layout.get_region('calibration').write
		write_range = (layout.calculate_bin_offset(region.address), region.size)
	return write_flash(flash, image, backup_filename, confirm,
		order=None if calibration_only else write_order,
		boot_sectors=() if calibration_only else warn_sectors,
		skip_verify=skip_verify, skip_backup=skip_backup, force=force, write_range=write_range)


def run_session (channel, mode: str, image: bytes | None = None, output_filename: str | None = None,
                 signature: bytes | None = None, attached: bool = False, confirm=None, warn_check=None,
                 attach_hint: str | None = None, skip_verify: bool = False, skip_backup: bool = False, force: bool = False, override_safety_checks: bool = False, log_callback=None, progress_callback=None, link=None) -> tuple[bool, bool]:
	'''
	Run `mode` (probe, read, flash-calibration, fullflash) on a kernel that's up and answering on `channel`.

	Ends with a software reset when it worked, or when it failed before the flash was
	touched. Otherwise the kernel is left running for --mtos-attach.

	:param warn_check: optional callback(warning_message) -> bool for risky conditions
	:param skip_verify: skip the final whole-flash verify pass after a fullflash
	:return: (whether the job finished, whether the ECU was reset)
	'''
	stamp = time.strftime('%Y%m%d_%H%M%S')
	if mode == 'probe':
		job = lambda kernel: probe(kernel, signature)
	elif mode == 'read':
		job = lambda kernel: read(kernel, output_filename or 'mtos_read_{}.bin'.format(stamp), signature, allow_blank=attached, warn_check=warn_check)
	elif mode in ('fullflash', 'flash-calibration'):
		job = lambda kernel: full_flash(kernel, image, output_filename or 'mtos_backup_{}.bin'.format(stamp),
			confirm or (lambda message: False), signature, allow_blank=attached, warn_check=warn_check, skip_verify=skip_verify, calibration_only=mode == 'flash-calibration', skip_backup=skip_backup, force=force, override_safety_checks=override_safety_checks)
	else:
		raise KernelError('Unknown kernel mode {}'.format(mode))

	kernel = Kernel(link or KernelLink(channel), log_callback=log_callback, progress_callback=progress_callback)
	finished = run_kernel_session(kernel, job, reset_when_done=True, attached=attached, attach_hint=attach_hint)
	if finished:
		log('[+] MTOS {} complete'.format(mode), log_callback)
	return finished, kernel.ecu_reset


def attach (channel, log_callback=None) -> None:
	'''
	Find a kernel a previous session left running on `channel`
	'''
	log('[*] Looking for a kernel at {} baud'.format(channel.get_baudrate()), log_callback)
	kernel = Kernel(KernelLink(channel), log_callback=log_callback)
	if not kernel.resync():
		raise KernelError('No kernel answering at {} baud'.format(channel.get_baudrate()))
	log('[+] Kernel found', log_callback)
