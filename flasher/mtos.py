import struct
import time
from contextlib import nullcontext
from pathlib import Path

from alive_progress import alive_bar
from gkbus.channel import CanChannel, KLineChannel, KLineTiming
from gkbus.protocol import kwp2000

from . import mtos_kernel
from .progress import transfer_summary
from .kernel import KERNEL_ASSET, KERNEL_STARTED, Kernel, KernelError, KernelLink, Progress, enter, identification, load_asset, read_partial
from .ecu import ECU, enable_security_access, request_security_seed, end_diagnostic_session, configure_serial_kline
from .mtos_asm import (
	build_blob,
	crc16_mtos,
	KLINE_HELLO,
	MTOS_BASE,
	MTOS_SIGNATURE_ADDRESS,
	MTOS_SIGNATURE_LENGTH,
	payload_bsl_kernel_shim,
	payload_kline_hello,
	payload_calibration_read,
	MTOS_BAUDRATE,
	mtos_baudrate,
	MTOS_TRANSFER_SIZE,
	payload_loader,
	STAGE2_ADDRESS,
)
from ecu_definitions import AccessLevel, Routine

#: modes that run the OpenGK kernel - see mtos_kernel
KERNEL_MODES = ('probe', 'read', 'flash-calibration', 'fullflash')
WRITE_MODES = ('flash-calibration', 'fullflash')
MTOS_MODES = ('hello', 'dump', 'payload', 'loader', 'probe', 'read', 'read-calibration', *WRITE_MODES)


def log (message, log_callback=None):
	# GUI mode
	if log_callback:
		log_callback.emit(message)
	# CLI mode
	else:
		print(message)


def _switch_fast_link(channel, marker=b'MT75', acknowledge=True, log_callback=None, prefix=b'', baudrate=MTOS_BAUDRATE):
	'''The payload signals at entry baud, then waits at the selected rate for the host.'''
	response = prefix + channel.read(len(marker) - len(prefix), timeout=2)
	if response != marker:
		raise IOError('Unexpected MTOS startup: {}'.format(response.hex(' ')))
	channel.set_baudrate(mtos_baudrate(baudrate))
	channel.reset_buffers()
	enter(channel, log_callback)
	if acknowledge:
		channel.write(b'\xA5')


# ---------------------------------------------------------------------------
# low level steps
# ---------------------------------------------------------------------------

def read_signature (ecu: ECU) -> bytes:
	'''Read the firmware-specific 6-byte MTOS signature from flash at 0x3FE8.'''
	return bytes(ecu.read_memory_by_address(MTOS_SIGNATURE_ADDRESS, MTOS_SIGNATURE_LENGTH))


def _siemens_access (ecu: ECU, mtos_key: int, log_callback=None) -> None:
	'''Siemens (0xFD) security access. The grant is per diagnostic session, so
	this has to be redone after every StartDiagnosticSession.'''
	log('[*] Siemens (0xFD) security access', log_callback)
	key = mtos_key.to_bytes(4, 'big')
	seed = request_security_seed(ecu.bus, AccessLevel.SIEMENS_0xFD.value)

	if (sum(seed) == 0):
		log('ECU returned seed=0. Either it\'s unlocked, or previous diagnostics session was still active', log_callback)
		return

	log('[*] Seed {}, sending key {}'.format(seed.hex(), key.hex()), log_callback)
	ecu.bus.execute(
		kwp2000.commands.SecurityAccess().send_key(key, access_level=AccessLevel.SIEMENS_0xFD.value + 1)
	)


def siemens_unlock (ecu: ECU, mtos_key: int, log_callback=None) -> None:
	'''Unlock the MTOS RAM upload session: default on K-Line, passive on CAN.'''
	can = isinstance(ecu.bus.transport.channel, CanChannel)
	session = kwp2000.enums.DiagnosticSession.PASSIVE if can else kwp2000.enums.DiagnosticSession.DEFAULT
	log('[*] {} diagnostic session'.format('Passive' if can else 'Default'), log_callback)
	ecu.bus.execute(
		kwp2000.commands.StartDiagnosticSession(
			session,
			ecu.get_desired_baudrate().index
		)
	)
	_siemens_access(ecu, mtos_key, log_callback=log_callback)


def _enter_execution_session (ecu: ECU, mtos_key: int, log_callback=None) -> None:
	'''Put the ECU in the flash-reprogramming session StartRoutine(0xFA) needs.

	On ca663056 the routine ran straight from the default upload session; on
	ca663057 its gate only opens in the flash-reprogramming session, while the
	blob upload still only works in the default one. So the blob is uploaded in
	the default session and left in RAM (it survives the switch), then we come
	here before firing the routine. The 0xFD grant is per session, so it's
	re-established too.'''
	log('[*] Flash-reprogramming diagnostic session', log_callback)
	ecu.bus.execute(
		kwp2000.commands.StartDiagnosticSession(
			kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING,
			ecu.get_desired_baudrate().index
		)
	)
	_siemens_access(ecu, mtos_key, log_callback=log_callback)


def upload_blob (ecu: ECU, blob: bytes, base: int = MTOS_BASE, log_callback=None, progress_callback=None) -> None:
	log('[*] Uploading {} byte blob to RAM at 0x{:04X}'.format(len(blob), base), log_callback)
	progress = Progress(progress_callback, len(blob), 'Uploading MTOS')
	try:
		written = 0
		while written < len(blob):
			chunk = blob[written:written + 16]
			payload = (base + written).to_bytes(3, 'big') + len(chunk).to_bytes(1, 'little') + chunk
			ecu.bus.execute(
				kwp2000.commands.WriteMemoryByAddress(offset=0x0, data_to_write=bytes([0xFF, 0xFF])).set_data(payload)
			)
			written += len(chunk)
			progress(written)
		log(transfer_summary('Uploaded', written, progress.started_at), log_callback)
	finally:
		progress.finish()



def _upload_stage2(link, data, title, log_callback=None, progress_callback=None, prefix=b''):
	progress = Progress(progress_callback, len(data), title)
	try:
		link.send(struct.pack('<H', len(data)) + prefix + data,
			progress=lambda done: progress(max(0, done - 2 - len(prefix))))
		log(transfer_summary('Uploaded', len(data), progress.started_at), log_callback)
	finally:
		progress.finish()



def execute (ecu: ECU, raw: bool = False, mtos_key: int | None = None, log_callback=None,
             fast_marker: bytes | None = None, acknowledge: bool = True, baudrate: int = MTOS_BAUDRATE):
	'''Start MTOS routine 0xFA.

	With raw=True we hand-roll the request so the firmware's KWP bookkeeping
	does not swallow the payload's own K-line output: the buffers are cleared,
	the StartRoutine request is written, and any "response pending"
	frame is consumed. Fast payloads may also start immediately; their first
	output byte is retained for the baud handshake. The live channel is then
	returned so the caller can read the payload's output (or, for the loader,
	stream the second stage to it).

	If the routine is refused instead of running (a `7F 31 xx` negative response
	with an NRC other than the benign 0x78 response-pending - on ca663057 the
	session gate answers 0x33 securityAccessDenied in the default session) and we
	were handed the key, the ECU is moved into the flash-reprogramming session
	the gate wants and the routine is fired once more. The blob is already
	resident in the 0xC000 RAM window and survives the session switch, so it does
	not need re-uploading.'''
	if not raw:
		return ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.EXECUTE_MTOS.value))

	channel = _fire_routine(ecu)
	nrc = _read_routine_reply(channel, preserve_output=fast_marker is not None)
	if isinstance(nrc, int) and nrc != NRC_RESPONSE_PENDING:
		if mtos_key is None:
			raise MtosExecutionDenied(nrc)
		log('[!] StartRoutine(0xFA) refused in this session (NRC 0x{:02X}), retrying after switching session'.format(nrc), log_callback)
		_enter_execution_session(ecu, mtos_key, log_callback=log_callback)
		channel = _fire_routine(ecu)
		nrc = _read_routine_reply(channel, preserve_output=fast_marker is not None)
		if isinstance(nrc, int) and nrc != NRC_RESPONSE_PENDING:
			raise MtosExecutionDenied(nrc)
	if fast_marker is not None:
		_switch_fast_link(channel, marker=fast_marker, acknowledge=acknowledge,
			log_callback=log_callback, prefix=nrc if isinstance(nrc, bytes) else b'', baudrate=baudrate)
	return channel


# Some firmware answers StartRoutine with a KWP negative "response pending"
# (7F 31 78) frame before our payload runs: 83 F1 11 7F 31 78 AD = 7 bytes.
# The 0x31 is the StartRoutine service id a KWP negative response echoes back
# (not the 0xFA routine id). A refusal comes back the same shape with a
# different NRC (0x33 securityAccessDenied when the routine's session gate is
# shut).
PENDING_FRAME_LENGTH = 7
START_ROUTINE_SERVICE = 0x31
NRC_RESPONSE_PENDING = 0x78


class MtosExecutionDenied (Exception):
	'''StartRoutine(0xFA) answered with a KWP negative response instead of
	running the payload.'''
	def __init__ (self, nrc: int):
		self.nrc = nrc
		super().__init__('StartRoutine(0xFA) refused with NRC 0x{:02X}'.format(nrc))


def _fire_routine (ecu: ECU):
	'''Clear the buffers and hand-write the StartRoutine(0xFA) request.'''
	transport = ecu.bus.transport
	channel = transport.channel
	channel.reset_buffers()
	channel.write(transport.build_payload(bytes([0x31, Routine.EXECUTE_MTOS.value])))
	return channel


def _read_routine_reply (channel, length: int = PENDING_FRAME_LENGTH, timeout: float = 0.6,
                        preserve_output: bool = False):
	'''Read the firmware's first frame after StartRoutine(0xFA), leaving any
	payload output behind it untouched in the buffer. Returns the NRC byte when
	it's a `7F 31 xx` negative response (0x78 is the benign response-pending that
	precedes the payload; anything else is a refusal), or None when the payload's
	own output is already flowing. With preserve_output, return its first byte
	instead of consuming the payload's startup marker.'''
	frame = bytearray()
	deadline = time.time() + timeout
	if preserve_output:
		frame += read_partial(channel, 1, timeout)
		# Some bootloaders execute immediately, without a response-pending frame.
		# Keep the first payload byte for the fast-link handshake.
		if frame != b'\x83':
			return bytes(frame)
	while len(frame) < length and time.time() < deadline:
		frame += read_partial(channel, length - len(frame), 0.2)
	if len(frame) >= 6 and frame[3] == 0x7F and frame[4] == START_ROUTINE_SERVICE:
		return frame[5]
	return None


def listen (channel, duration: float = 2.0, idle: float = 0.4, log_callback=None) -> bytes:
	'''Collect raw bytes off the K-line. Returns as soon as the stream goes
	quiet for `idle` seconds (once anything has arrived), or after `duration`
	seconds at the latest - so a dump of any length is captured in full without
	having to predict how long it takes.'''
	collected = bytearray()
	deadline = time.time() + duration
	last = time.time()
	with (nullcontext() if log_callback is not None else alive_bar(title='Receiving MTOS', unit='B')) as bar:
		while time.time() < deadline:
			chunk = read_partial(channel, 256, 0.1)
			if chunk:
				collected += chunk
				if bar is not None:
					bar(len(chunk))
				last = time.time()
			elif collected and (time.time() - last) >= idle:
				break
	return bytes(collected)


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------

def run_hello (ecu: ECU, signature: bytes, mtos_key: int | None = None, log_callback=None, progress_callback=None, baudrate: int = MTOS_BAUDRATE) -> bytes:
	'''Safest self-test: upload a payload that sends KLINE_HELLO over the K-line
	and run it. Proves upload + CRC validation + execution + I/O in one shot.'''
	body = payload_kline_hello(fast=True, entry_baudrate=ecu.bus.transport.channel.get_baudrate(), baudrate=baudrate)
	blob = build_blob(body, signature)
	upload_blob(ecu, blob, log_callback=log_callback, progress_callback=progress_callback)
	log('[*] MTOS checksum: 0x{:04X}'.format(crc16_mtos(body)), log_callback)
	log('[*] Executing, listening on K-line', log_callback)
	channel = ecu.bus.transport.channel
	entry_baudrate, timing = channel.get_baudrate(), channel.get_timing()
	try:
		execute(ecu, raw=True, mtos_key=mtos_key, log_callback=log_callback, fast_marker=b'MT75', baudrate=baudrate)
		out = listen(channel, log_callback=log_callback)
	finally:
		channel.set_baudrate(entry_baudrate)
		channel.set_timing(timing)
	log('[*] Received: {} ({})'.format(out.hex(), out), log_callback)
	if KLINE_HELLO in out:
		log('[+] SUCCESS: payload executed and echoed {!r}'.format(KLINE_HELLO), log_callback)
		ecu.bus.init(kwp2000.commands.StartCommunication())
		end_diagnostic_session(ecu.bus)
	else:
		log('[!] did not see {!r} in the response'.format(KLINE_HELLO), log_callback)
	return out


def run_dump (ecu: ECU, signature: bytes, address: int, length: int, mtos_key: int | None = None, log_callback=None, progress_callback=None, baudrate: int = MTOS_BAUDRATE) -> bytes:
	out = _read_fast_memory(ecu, signature, mtos_key, address, length,
		log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	log('[*] Received {} bytes: {}'.format(len(out), out.hex()), log_callback)
	return out


def _read_fast_memory(ecu, signature, mtos_key, address, length, log_callback=None, progress_callback=None, baudrate=MTOS_BAUDRATE):
	'''Shared flow-controlled memory reader for dump and calibration modes.'''
	if ecu.get_desired_baudrate().baudrate != 10400:
		raise ValueError('MTOS memory reads must start at 10400 baud')
	body = payload_calibration_read(address, length, entry_baudrate=ecu.bus.transport.channel.get_baudrate(), baudrate=baudrate)
	upload_blob(ecu, build_blob(body, signature), log_callback=log_callback, progress_callback=progress_callback)
	channel = ecu.bus.transport.channel
	progress = None
	try:
		execute(ecu, raw=True, mtos_key=mtos_key, log_callback=log_callback,
			fast_marker=b'MTRD', acknowledge=False, baudrate=baudrate)
		log('[*] Reading memory at {} baud'.format(baudrate), log_callback)
		progress = Progress(progress_callback, length, 'Reading memory')
		data = bytearray()
		while len(data) < length:
			channel.write(b'\xA5')
			size = min(MTOS_TRANSFER_SIZE, length - len(data))
			data.extend(channel.read(size, timeout=2))
			progress(len(data))
		stored_crc = int.from_bytes(channel.read(2, timeout=2), 'little')
		if crc16_mtos(data, seed=0xFFFF) != stored_crc:
			raise IOError('MTOS memory read CRC mismatch; no BIN saved')
		log(transfer_summary('Read', len(data), progress.started_at), log_callback)
		return bytes(data)
	finally:
		if progress is not None:
			progress.finish()
		if not check_after_reset(ecu.bus, ecu, log_callback=log_callback):
			raise IOError('MTOS memory reader finished, but diagnostic recovery failed')


def run_calibration_read (ecu: ECU, signature: bytes, mtos_key: int, output_filename=None,
                         log_callback=None, progress_callback=None, baudrate: int = MTOS_BAUDRATE) -> bytes:
	'''Read calibration, save it at normal BIN offsets, and recover diagnostics.'''
	region = ecu.get_region('calibration').read
	data = _read_fast_memory(ecu, signature, mtos_key, region.address, region.size,
		log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	_save_calibration_read(ecu, data, output_filename, log_callback)
	return data


def _save_calibration_read(ecu, data, output_filename, log_callback=None):
	region = ecu.get_region('calibration').read
	image = bytearray(b'\xFF' * ecu.get_eeprom_size_bytes())
	offset = ecu.calculate_bin_offset(region.address)
	image[offset:offset + region.size] = data
	output = Path(output_filename or 'mtos_calibration_{}.bin'.format(time.strftime('%Y-%m-%d_%H%M%S'))).resolve()
	output.write_bytes(image)
	log('[*] Saved to {}'.format(output), log_callback)


def run_payload_file (ecu: ECU, signature: bytes, body: bytes, mtos_key: int | None = None, log_callback=None, progress_callback=None) -> bytes:
	'''Upload a raw user-authored body (code starting at 0xC014) and run it.'''
	blob = build_blob(body, signature)
	upload_blob(ecu, blob, log_callback=log_callback, progress_callback=progress_callback)
	channel = execute(ecu, raw=True, mtos_key=mtos_key, log_callback=log_callback)
	out = listen(channel, log_callback=log_callback)
	log('[*] Received: {}'.format(out.hex()), log_callback)
	return out


def run_loader (ecu: ECU, signature: bytes, stage2: bytes, mtos_key: int | None = None, log_callback=None, progress_callback=None, baudrate: int = MTOS_BAUDRATE) -> bytes:
	'''Upload the small K-line loader, then stream `stage2` to it and run it.

	Wire protocol: u16 little-endian length, then the stage-2 body. The loader
	copies it to STAGE2_ADDRESS (XRAM 0xE000), far-calls it, and returns.'''
	if not 0 < len(stage2) <= 0xE800 - STAGE2_ADDRESS:
		raise ValueError('Second stage must fit the XRAM scratch area')
	blob = build_blob(payload_loader(STAGE2_ADDRESS, fast=True,
		entry_baudrate=ecu.bus.transport.channel.get_baudrate(), baudrate=baudrate), signature)
	upload_blob(ecu, blob, log_callback=log_callback, progress_callback=progress_callback)
	log('[*] Executing loader, streaming {} byte second stage to 0x{:04X}'.format(
		len(stage2), STAGE2_ADDRESS), log_callback)
	channel = execute(ecu, raw=True, mtos_key=mtos_key, log_callback=log_callback)
	entry_baudrate, timing = channel.get_baudrate(), channel.get_timing()
	try:
		_switch_fast_link(channel, log_callback=log_callback, baudrate=baudrate)
		_upload_stage2(KernelLink(channel), stage2,
			'Uploading second stage', log_callback, progress_callback)
		out = listen(channel, duration=3.0, log_callback=log_callback)
	finally:
		channel.set_baudrate(entry_baudrate)
		channel.set_timing(timing)
	log('[*] Received {} bytes: {}'.format(len(out), out.hex()), log_callback)
	return out


def launch_kernel (ecu: ECU, signature: bytes, mtos_key: int | None = None, log_callback=None, progress_callback=None, baudrate: int | None = None):
	'''Turn the ECU over to the OpenGK kernel: the loader, then the kernel as its
	second stage (mtos_asm.payload_bsl_kernel_shim). Returns the channel, with
	the kernel answering at the selected rate and kernel timing set.

	The firmware never comes back from this - the session has to end with the
	kernel's software reset (or a power cycle)'''
	automatic = baudrate is None
	if automatic:
		baudrate = mtos_baudrate(57600)
	stage2 = payload_bsl_kernel_shim(load_asset(KERNEL_ASSET))
	blob = build_blob(payload_loader(STAGE2_ADDRESS, fast=True,
		entry_baudrate=ecu.bus.transport.channel.get_baudrate(), baudrate=baudrate), signature)
	upload_blob(ecu, blob, log_callback=log_callback, progress_callback=progress_callback)

	log('[*] Starting the loader, sending it the OpenGK kernel ({} bytes)'.format(len(stage2)), log_callback)
	channel = execute(ecu, raw=True, mtos_key=mtos_key, log_callback=log_callback, fast_marker=b'MT75', baudrate=baudrate)
	link = KernelLink(channel)
	_upload_stage2(link, stage2, 'Uploading kernel', log_callback, progress_callback)

	# 0x03 once it runs. Anything in front of it is what's left of the KWP exchange
	started = link.read(16, 0.3)
	kernel = Kernel(link, log_callback=log_callback, progress_callback=progress_callback)
	if not (started[-1:] == bytes([KERNEL_STARTED]) and kernel.test_comm()):
		log('[!] Kernel start not seen ({}), looking for it'.format(started.hex() or 'nothing'), log_callback)
		if not kernel.resync():
			raise KernelError('The kernel didn\'t start. Switch the ECU off and on again - the flash wasn\'t touched')
	log('[+] OpenGK kernel running', log_callback)
	if automatic:
		from .loader_baudrate import discover_loader_baudrate
		discover_loader_baudrate(kernel)
	return channel


def check_after_reset (bus, ecu: ECU | None = None, log_callback=None, release=True) -> bool:
	'''Whether the ECU answers KWP2000 again after the kernel's software reset,
	with the calibration it identifies with if `ecu` is known'''
	channel = bus.transport.channel
	can = isinstance(channel, CanChannel)
	if not can:
		channel.set_baudrate(10400)
		channel.set_timing(KLineTiming())
	log('[*] Waiting for the ECU to boot', log_callback)
	time.sleep(2)
	for attempt in range(3):
		try:
			if not can:
				channel.reset_buffers()
			bus.init(kwp2000.commands.StartCommunication())
			if can:
				try:
					bus.execute(kwp2000.commands.TesterPresent(kwp2000.enums.ResponseType.REQUIRED))
				except kwp2000.Kwp2000NegativeResponseException:
					# Some SIMK applications reject TesterPresent, but a negative
					# response still confirms their diagnostic CAN path is alive.
					pass
			bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING))
			break
		except IOError as e:
			if attempt == 2:
				log('[!] The ECU doesn\'t answer KWP2000 ({}). Try switching it off and on again'.format(e or type(e).__name__), log_callback)
				return False
			time.sleep(2)
	log('[+] The ECU is up and answering', log_callback)
	if ecu is not None:
		try:
			enable_security_access(bus)
			log('[*] Calibration: {}, {}'.format(ecu.get_calibration(), ecu.get_calibration_description()), log_callback)
		except IOError as e:
			log('[!] Couldn\'t read the calibration: {}'.format(e or type(e).__name__), log_callback)
	return end_diagnostic_session(bus) if release else True


def cli_confirm (message: str) -> bool:
	return input('[?] {} [y/n]: '.format(message)).strip().lower() == 'y'


def cli_warn_check (warning: str) -> bool:
	'''
	Warn the user about a potentially dangerous condition with more prominent messaging.
	Returns True to continue, False to abort.
	'''
	message = (
		'\n' +
		'[!] ⚠️  WARNING ⚠️\n' +
		'[!] {}\n'.format(warning) +
		'[!]\n' +
		'[!] If you aren\'t COMPLETELY sure what you\'re doing, you could BRICK your ECU.\n' +
		'[!] If in doubt, ask on the OpenGK Discord: https://discord.gg/opengk\n' +
		'[!]\n'
	)
	return input(message + '[?] Do you want to continue anyway? [y/N]: ').strip().lower() == 'y'


def attach_session (bus, mode: str, image_filename: str | None = None, output_filename: str | None = None,
                    baudrate: int | None = None, confirm=cli_confirm, warn_check=cli_warn_check, attach_hint: str | None = None,
                    skip_verify: bool = False, skip_backup: bool = False, force: bool = False, override_safety_checks: bool = False, log_callback=None, progress_callback=None) -> bool:
	'''Pick up a OpenGK kernel an earlier session left running (the ECU still
	powered, at that session's baudrate) and run `mode` on it. No KWP2000 on
	the way in, the firmware isn't running'''
	channel = bus.transport.channel
	link = None
	try:
		image = mtos_kernel.load_image(image_filename, log_callback, warn_check=warn_check, calibration_only=mode == 'flash-calibration', override_safety_checks=override_safety_checks) if mode in WRITE_MODES else None
		if isinstance(channel, CanChannel):
			from .mtos_can import attach
			link = attach(channel, log_callback)
		else:
			enter(channel, log_callback)
			if baudrate is None:
				from .loader_baudrate import STARTUP_BAUDRATE, locate_loader, discover_loader_baudrate
				channel.set_baudrate(STARTUP_BAUDRATE)
				kernel, boot_reply = locate_loader(channel, log_callback)
				if boot_reply is not None:
					raise KernelError('The ECU is in boot ROM, not a running MTOS kernel')
				discover_loader_baudrate(kernel)
			else:
				channel.set_baudrate(mtos_baudrate(baudrate))
				mtos_kernel.attach(channel, log_callback=log_callback)
	except (KernelError, OSError) as e:
		log('[!] {}'.format(e), log_callback)
		return False
	finished, reset = mtos_kernel.run_session(
		channel, mode, image=image, output_filename=output_filename, attached=True, confirm=confirm,
		warn_check=warn_check, attach_hint=attach_hint, skip_verify=skip_verify, skip_backup=skip_backup, force=force, override_safety_checks=override_safety_checks, log_callback=log_callback, progress_callback=progress_callback, link=link,
	)
	if reset:
		finished = check_after_reset(bus, log_callback=log_callback) and finished
	log('[*] Done!' if finished else '[!] MTOS {} did not complete'.format(mode), log_callback)
	return finished


def kernel_session (ecu: ECU, signature: bytes, mode: str, mtos_key: int | None = None, image: bytes | None = None, output_filename: str | None = None,
                    confirm=None, warn_check=cli_warn_check, attach_hint: str | None = None, skip_verify: bool = False, skip_backup: bool = False, force: bool = False, override_safety_checks: bool = False, log_callback=None, progress_callback=None, baudrate: int | None = None) -> bool:
	try:
		channel = launch_kernel(ecu, signature, mtos_key=mtos_key, log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	except KernelError as e:
		log('[!] {}'.format(e), log_callback)
		return False
	if baudrate is None and attach_hint:
		attach_hint += ' --desired-baudrate 0x06 --session-baudrate {}'.format(channel.get_baudrate())
	finished, reset = mtos_kernel.run_session(
		channel, mode, image=image, output_filename=output_filename, signature=signature, confirm=confirm,
		warn_check=warn_check, attach_hint=attach_hint, skip_verify=skip_verify, skip_backup=skip_backup, force=force, override_safety_checks=override_safety_checks, log_callback=log_callback, progress_callback=progress_callback,
	)
	if reset:
		finished = check_after_reset(ecu.bus, ecu, log_callback=log_callback) and finished
	log('[*] Done!' if finished else '[!] MTOS {} did not complete'.format(mode), log_callback)
	return finished


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def mtos_handler (ecu: ECU, mtos_key: int, mode: str = 'hello',
                  payload_filename: str = None, address: int = 0x000000, length: int = 0x40,
                  output_filename: str | None = None, confirm_callback=cli_confirm, warn_check=cli_warn_check,
                  attach_hint: str | None = None, skip_verify: bool = False, skip_backup: bool = False, force: bool = False, override_safety_checks: bool = False, log_callback=None, progress_callback=None, baudrate: int | None = None):
	if mtos_key is None:
		log('[!] MTOS requires a key (e.g. 0x2d381c78)', log_callback)
		return

	image = None
	if mode in WRITE_MODES:
		if not payload_filename:
			log('[!] {} mode requires a full 4/8 Mbit BIN image'.format(mode), log_callback)
			return
		# a bad file is better caught before the firmware is gone
		try:
			image = mtos_kernel.load_image(payload_filename, log_callback, warn_check=warn_check, calibration_only=mode == 'flash-calibration', override_safety_checks=override_safety_checks)
		except (KernelError, OSError) as e:
			log('[!] {}'.format(e), log_callback)
			return
		log('[*] Image {}: {}'.format(payload_filename, identification(image)), log_callback)

	can = isinstance(ecu.bus.transport.channel, CanChannel)
	if not can and baudrate is not None:
		baudrate = mtos_baudrate(baudrate)
	if can and mode in ('payload', 'loader'):
		log('[!] Custom MTOS payload/loader files currently require K-Line', log_callback)
		return False
	ecu.bus._stop_keepalive()
	channel = ecu.bus.transport.channel
	if isinstance(channel, KLineChannel) and channel.get_baudrate() == 10400:
		# At startup speed, send complete frames: per-byte USB echo waits can exceed P4.
		channel.reset_timing()
		configure_serial_kline(channel, p4_min=0)
	siemens_unlock(ecu, mtos_key, log_callback=log_callback)

	signature = read_signature(ecu)
	log('[*] MTOS signature @0x{:04X}: {} ({})'.format(
		MTOS_SIGNATURE_ADDRESS, signature.hex(), signature.decode('latin-1')), log_callback)
	if can:
		from . import mtos_can
		return mtos_can.run(ecu, signature, mtos_key, mode, address, length,
			image=image, output_filename=output_filename, confirm=confirm_callback,
			warn_check=warn_check, attach_hint=attach_hint, skip_verify=skip_verify,
			skip_backup=skip_backup, force=force, override_safety_checks=override_safety_checks,
			log_callback=log_callback, progress_callback=progress_callback)

	if baudrate is None and mode not in (*KERNEL_MODES, 'payload'):
		# Stream/hello payloads have their own UART. Qualify the same link through
		# the common kernel first, then restore diagnostics before launching them.
		channel = launch_kernel(ecu, signature, mtos_key=mtos_key,
			log_callback=log_callback, progress_callback=progress_callback, baudrate=None)
		baudrate = channel.get_baudrate()
		Kernel(KernelLink(channel)).software_reset()
		if not check_after_reset(ecu.bus, ecu, log_callback, release=False):
			raise KernelError('Diagnostic recovery failed after baud discovery')
		siemens_unlock(ecu, mtos_key, log_callback=log_callback)
		signature = read_signature(ecu)
	if mode in KERNEL_MODES:
		kernel_session(ecu, signature, mode, mtos_key=mtos_key, image=image, output_filename=output_filename, confirm=confirm_callback,
			warn_check=warn_check, attach_hint=attach_hint, skip_verify=skip_verify, skip_backup=skip_backup, force=force, override_safety_checks=override_safety_checks, log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	elif mode == 'hello':
		run_hello(ecu, signature, mtos_key=mtos_key, log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	elif mode == 'dump':
		run_dump(ecu, signature, address, length, mtos_key=mtos_key,
			log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	elif mode == 'read-calibration':
		run_calibration_read(ecu, signature, mtos_key, output_filename,
			log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	elif mode == 'payload':
		if not payload_filename:
			log('[!] payload mode requires a payload file', log_callback)
			return
		with open(payload_filename, 'rb') as f:
			run_payload_file(ecu, signature, f.read(), mtos_key=mtos_key, log_callback=log_callback, progress_callback=progress_callback)
	elif mode == 'loader':
		if not payload_filename:
			log('[!] loader mode requires a second-stage payload file', log_callback)
			return
		with open(payload_filename, 'rb') as f:
			run_loader(ecu, signature, f.read(), mtos_key=mtos_key, log_callback=log_callback, progress_callback=progress_callback, baudrate=baudrate)
	else:
		log('[!] unknown MTOS mode: {}'.format(mode), log_callback)
