import struct
import time

from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000

from .ecu import ECU
from .mtos_asm import (
	build_blob,
	crc16_mtos,
	MTOS_BASE,
	MTOS_SIGNATURE_ADDRESS,
	MTOS_SIGNATURE_LENGTH,
	payload_kline_hello,
	payload_memory_dump,
	payload_loader,
	STAGE2_ADDRESS,
)
from ecu_definitions import AccessLevel, Routine


def log (message, log_callback=None):
	# GUI mode
	if log_callback:
		log_callback.emit(message)
	# CLI mode
	else:
		print(message)


# ---------------------------------------------------------------------------
# low level steps
# ---------------------------------------------------------------------------

def read_signature (ecu: ECU) -> bytes:
	'''Read the firmware-specific 6-byte MTOS signature from flash at 0x3FE8.'''
	return bytes(ecu.read_memory_by_address(MTOS_SIGNATURE_ADDRESS, MTOS_SIGNATURE_LENGTH))


def siemens_unlock (ecu: ECU, mtos_key: int, log_callback=None) -> None:
	log('[*] Default diagnostic session', log_callback)
	ecu.bus.execute(
		kwp2000.commands.StartDiagnosticSession(
			kwp2000.enums.DiagnosticSession.DEFAULT,
			ecu.get_desired_baudrate().index
		)
	)

	log('[*] Siemens (0xFD) security access', log_callback)
	key = mtos_key.to_bytes(4, 'big')
	seed = ecu.bus.execute(
		kwp2000.commands.SecurityAccess().request_seed(access_level=AccessLevel.SIEMENS_0xFD.value)
	).get_data()[1:]
	log('[*] Seed {}, sending key {}'.format(seed.hex(), key.hex()), log_callback)
	ecu.bus.execute(
		kwp2000.commands.SecurityAccess().send_key(key, access_level=AccessLevel.SIEMENS_0xFD.value + 1)
	)


def upload_blob (ecu: ECU, blob: bytes, base: int = MTOS_BASE, log_callback=None) -> None:
	log('[*] Uploading {} byte blob to RAM at 0x{:04X}'.format(len(blob), base), log_callback)
	written = 0
	while written < len(blob):
		chunk = blob[written:written + 16]
		payload = (base + written).to_bytes(3, 'big') + len(chunk).to_bytes(1, 'little') + chunk
		ecu.bus.execute(
			kwp2000.commands.WriteMemoryByAddress(offset=0x0, data_to_write=bytes([0xFF, 0xFF])).set_data(payload)
		)
		written += len(chunk)


def execute (ecu: ECU, raw: bool = False):
	'''Start MTOS routine 0xFA.

	With raw=True we hand-roll the request so the firmware's KWP bookkeeping
	does not swallow the payload's own K-line output: the buffers are cleared,
	the StartRoutine request is written, and the mandatory "response pending"
	frame the firmware always emits first is consumed. The live channel is then
	returned so the caller can read the payload's output (or, for the loader,
	stream the second stage to it).'''
	if not raw:
		return ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.EXECUTE_MTOS.value))

	transport = ecu.bus.transport
	channel = transport.channel
	channel.reset_buffers()
	channel.write(transport.build_payload(bytes([0x31, Routine.EXECUTE_MTOS.value])))
	_consume_pending(channel)
	return channel


# the firmware answers StartRoutine with a KWP negative "response pending"
# (7F 31 78) frame before our payload runs: 83 F1 11 7F 31 78 AD = 7 bytes
PENDING_FRAME_LENGTH = 7


def _consume_pending (channel, length: int = PENDING_FRAME_LENGTH, timeout: float = 0.6) -> bytes:
	'''Read and discard exactly the KWP response-pending frame, leaving any
	payload output untouched in the buffer.'''
	collected = bytearray()
	deadline = time.time() + timeout
	while len(collected) < length and time.time() < deadline:
		collected += channel._read(length - len(collected), 0.2)
	return bytes(collected)


def listen (channel, duration: float = 2.0, idle: float = 0.4) -> bytes:
	'''Collect raw bytes off the K-line. Returns as soon as the stream goes
	quiet for `idle` seconds (once anything has arrived), or after `duration`
	seconds at the latest - so a dump of any length is captured in full without
	having to predict how long it takes.'''
	collected = bytearray()
	deadline = time.time() + duration
	last = time.time()
	while time.time() < deadline:
		chunk = channel._read(256, 0.1)
		if chunk:
			collected += chunk
			last = time.time()
		elif collected and (time.time() - last) >= idle:
			break
	return bytes(collected)


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------

def run_hello (ecu: ECU, signature: bytes, message: bytes = b'MTOS', log_callback=None) -> bytes:
	'''Safest self-test: upload a payload that sends `message` over the K-line
	and run it. Proves upload + CRC validation + execution + I/O in one shot.'''
	blob = build_blob(payload_kline_hello(message), signature)
	upload_blob(ecu, blob, log_callback=log_callback)
	log('[*] MTOS checksum: 0x{:04X}'.format(crc16_mtos(payload_kline_hello(message))), log_callback)
	log('[*] Executing, listening on K-line', log_callback)
	channel = execute(ecu, raw=True)
	out = listen(channel)
	log('[*] Received: {} ({})'.format(out.hex(), out), log_callback)
	if message in out:
		log('[+] SUCCESS: payload executed and echoed {!r}'.format(message), log_callback)
	else:
		log('[!] did not see {!r} in the response'.format(message), log_callback)
	return out


def run_dump (ecu: ECU, signature: bytes, address: int, length: int, log_callback=None) -> bytes:
	blob = build_blob(payload_memory_dump(address, length), signature)
	upload_blob(ecu, blob, log_callback=log_callback)
	log('[*] Executing memory dump 0x{:06X}+0x{:X} over K-line'.format(address, length), log_callback)
	channel = execute(ecu, raw=True)
	out = listen(channel, duration=max(3.0, length / 300.0))
	log('[*] Received {} bytes: {}'.format(len(out), out.hex()), log_callback)
	return out


def run_payload_file (ecu: ECU, signature: bytes, body: bytes, log_callback=None) -> bytes:
	'''Upload a raw user-authored body (code starting at 0xC014) and run it.'''
	blob = build_blob(body, signature)
	upload_blob(ecu, blob, log_callback=log_callback)
	channel = execute(ecu, raw=True)
	out = listen(channel)
	log('[*] Received: {}'.format(out.hex()), log_callback)
	return out


def run_loader (ecu: ECU, signature: bytes, stage2: bytes, log_callback=None) -> bytes:
	'''Upload the small K-line loader, then stream `stage2` to it and run it.

	Wire protocol: u16 little-endian length, then the stage-2 body. The loader
	copies it to STAGE2_ADDRESS (XRAM 0xE000), far-calls it, and returns.'''
	blob = build_blob(payload_loader(STAGE2_ADDRESS), signature)
	upload_blob(ecu, blob, log_callback=log_callback)
	log('[*] Executing loader, streaming {} byte second stage to 0x{:04X}'.format(
		len(stage2), STAGE2_ADDRESS), log_callback)
	channel = execute(ecu, raw=True)
	channel.write(struct.pack('<H', len(stage2)) + stage2)
	out = listen(channel, duration=3.0)
	log('[*] Received {} bytes: {}'.format(len(out), out.hex()), log_callback)
	return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def mtos_handler (ecu: ECU, mtos_key: int, mode: str = 'hello',
                  payload_filename: str = None, address: int = 0x000000, length: int = 0x40,
                  log_callback=None, progress_callback=None):
	if mtos_key is None:
		log('[!] MTOS requires a key (e.g. 0x2d381c78)', log_callback)
		return

	ecu.bus._stop_keepalive()
	siemens_unlock(ecu, mtos_key, log_callback=log_callback)

	signature = read_signature(ecu)
	log('[*] MTOS signature @0x{:04X}: {} ({})'.format(
		MTOS_SIGNATURE_ADDRESS, signature.hex(), signature.decode('latin-1')), log_callback)

	if mode == 'hello':
		run_hello(ecu, signature, log_callback=log_callback)
	elif mode == 'dump':
		run_dump(ecu, signature, address, length, log_callback=log_callback)
	elif mode == 'payload':
		if not payload_filename:
			log('[!] payload mode requires a payload file', log_callback)
			return
		with open(payload_filename, 'rb') as f:
			run_payload_file(ecu, signature, f.read(), log_callback=log_callback)
	elif mode == 'loader':
		if not payload_filename:
			log('[!] loader mode requires a second-stage payload file', log_callback)
			return
		with open(payload_filename, 'rb') as f:
			run_loader(ecu, signature, f.read(), log_callback=log_callback)
	else:
		log('[!] unknown MTOS mode: {}'.format(mode), log_callback)