"""MTOS over the RSW CAN message objects, using the shared SIMK kernel.

KWP authenticates and uploads the loader. Once MTOS owns the CPU, sequenced
seven-byte packets replace the kernel's UART I/O. These packets use the RSW
mailboxes, but are a byte-stream protocol, not native CCP commands.
"""
import secrets
import time

from gkbus.channel import CanFilter, CanFrame
from gkbus.exceptions import TimeoutException

from .kernel import Kernel, KernelError, KernelLink, load_asset, xor_checksum, run_session
from .mtos_asm import _code, build_blob
from .progress import Progress, transfer_summary
from .rsw import TX_ID, RX_ID


class CanKernelLink(KernelLink):

	def __init__(self, channel):
		super().__init__(channel)
		# SLCAN's ASCII/USB receive queue needs shorter bursts than PCAN's.
		self.window = 16 if getattr(channel, 'interface', '') == 'pcan' else 4
		self.tx_sequence = self.rx_sequence = 0
		self._buffer = bytearray()

	@property
	def rate(self):
		return self.channel.bitrate

	@property
	def description(self):
		return '{:g} Kbps CAN1'.format(self.rate / 1000)

	def _receive(self, timeout):
		try:
			data = self.channel.recv(timeout).data
		except TimeoutException as exc:
			raise KernelError('MTOS CAN receive timed out') from exc
		if len(data) != 8:
			raise KernelError('Invalid MTOS CAN packet length')
		return data

	def _acknowledge(self, sequence):
		self.channel.send(CanFrame(TX_ID, bytes([0, sequence]) + bytes(6)))

	def _accept(self, data):
		count = data[0] & 7
		if not count:
			return
		sequence = (data[0] >> 3) & 15
		if sequence != self.rx_sequence:
			raise KernelError('MTOS CAN sequence {} != {}'.format(sequence, self.rx_sequence))
		self.rx_sequence = (sequence + 1) & 15
		self._buffer.extend(data[1:1 + count])
		if data[0] & 0x80:
			self._acknowledge(sequence)

	def send(self, data, check_echo=True, progress=None):
		data = bytes(data)
		for start in range(0, len(data), self.window * 7):
			burst = data[start:start + self.window * 7]
			for offset in range(0, len(burst), 7):
				payload = burst[offset:offset + 7]
				last = offset + 7 >= len(burst)
				sequence = self.tx_sequence
				header = len(payload) | (sequence << 3) | (0x80 if last else 0)
				self.channel.send(CanFrame(TX_ID, bytes([header]) + payload + bytes(7 - len(payload))))
				self.tx_sequence = (sequence + 1) & 15
			deadline = time.monotonic() + 2
			while True:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					raise KernelError('MTOS CAN acknowledgement timed out')
				response = self._receive(remaining)
				if response[:2] == bytes([0, sequence]):
					break
				self._accept(response)
			if progress:
				progress(start + len(burst))

	def read(self, size, timeout):
		deadline = time.monotonic() + timeout
		while len(self._buffer) < size:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				break
			try:
				self._accept(self._receive(remaining))
			except KernelError as exc:
				if isinstance(exc.__cause__, TimeoutException):
					break
				raise
		result = bytes(self._buffer[:size])
		del self._buffer[:size]
		return result

	def synchronize(self):
		"""Attach or recover without knowing the previous process's counters."""
		token = secrets.randbelow(0xFFFF).to_bytes(2, 'little')
		request = bytes([0, 0xFF, self.window - 1]) + token + bytes(3)
		deadline = time.monotonic() + 3
		next_request = 0
		while time.monotonic() < deadline:
			now = time.monotonic()
			if now >= next_request:
				self.channel.send(CanFrame(TX_ID, request))
				next_request = now + 0.1
			try:
				data = self._receive(min(next_request - now, deadline - now))
			except KernelError as exc:
				if isinstance(exc.__cause__, TimeoutException):
					continue
				raise
			if data[:4] == b'\x00\xFF' + token:
				self.tx_sequence = self.rx_sequence = 0
				self._buffer.clear()
				return
			# Finish a response the previous host left waiting for its ACK.
			if data[0] & 0x80:
				self._acknowledge((data[0] >> 3) & 15)
		raise KernelError('Could not synchronize the MTOS CAN stream')


def attach(channel, log_callback=None):
	channel.open()
	channel.set_filters([CanFilter.for_id(RX_ID)])
	link = CanKernelLink(channel)
	if not Kernel(link, log_callback=log_callback).resync():
		raise KernelError('No MTOS CAN kernel is answering')
	return link


def launch(ecu, signature, mtos_key, log_callback=None, progress_callback=None):
	from . import mtos

	channel = ecu.bus.transport.channel
	for address, identifier in ((0xEE12, RX_ID), (0xEE22, TX_ID)):
		arbitration = int.from_bytes(ecu.read_memory_by_address(address, 2), 'little')
		if (arbitration & 255) * 8 + (arbitration >> 13) != identifier:
			raise KernelError('The ECU CAN message objects do not match the RSW layout')
	stage2 = load_asset('assets/mtos_can_stage2.bin')
	loader = _code('can_loader', 0xC014)
	mtos.upload_blob(ecu, build_blob(loader, signature), log_callback=log_callback, progress_callback=progress_callback)
	mtos._enter_execution_session(ecu, mtos_key, log_callback=log_callback)
	transport = ecu.bus.transport
	tx_id, rx_id = transport.get_tx_id(), transport.get_rx_id()
	transport.close()
	channel.set_filters([CanFilter.for_id(rx_id), CanFilter.for_id(RX_ID)])
	channel.send(CanFrame(tx_id, b'\x02\x31\xFA' + b'\xCC' * 5))
	deadline = time.monotonic() + 2
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise KernelError('The MTOS CAN loader did not start')
		frame = channel.recv(remaining)
		if frame.identifier == RX_ID and frame.data[:4] == b'MTC1':
			break
		if frame.identifier == rx_id and len(frame.data) >= 4 and frame.data[1:3] == b'\x7F\x31' and frame.data[3] != 0x78:
			raise mtos.MtosExecutionDenied(frame.data[3])
	channel.set_filters([CanFilter.for_id(RX_ID)])
	link = CanKernelLink(channel)
	mtos._upload_stage2(link, stage2, 'Uploading CAN kernel', log_callback, progress_callback,
		prefix=bytes([xor_checksum(stage2)]))
	link = CanKernelLink(channel)
	if link.read(1, 2) != b'\x03':
		raise KernelError('The MTOS CAN kernel did not start')
	link.synchronize()
	if not Kernel(link).test_comm():
		raise KernelError('The MTOS CAN kernel did not acknowledge TEST_COMM')
	return link


def run(ecu, signature, mtos_key, mode, address, length, **options):
	from . import mtos, mtos_kernel

	log_callback = options.get('log_callback')
	progress_callback = options.get('progress_callback')
	link = launch(ecu, signature, mtos_key, log_callback, progress_callback)
	if mode in mtos.KERNEL_MODES:
		finished, reset = mtos_kernel.run_session(link.channel, mode, signature=signature, link=link, **options)
	else:
		kernel = Kernel(link, log_callback=log_callback, progress_callback=progress_callback)
		def job(kernel):
			if mode == 'hello':
				if not kernel.test_comm():
					raise KernelError('The MTOS CAN kernel did not answer')
				kernel.log('[+] MTOS CAN kernel is answering')
				return
			region = ecu.get_region('calibration').read if mode == 'read-calibration' else None
			start, size = (region.address, region.size) if region else (address, length)
			progress = Progress(progress_callback, size, 'Reading memory')
			try:
				data = kernel.read_memory(start, size, progress)
				kernel.log(transfer_summary('Read', len(data), progress.started_at))
			finally:
				progress.finish()
			if region:
				mtos._save_calibration_read(ecu, data, options.get('output_filename'), log_callback)
			else:
				kernel.log('[*] Received {} bytes: {}'.format(len(data), data.hex()))
		finished = run_session(kernel, job, reset_when_done=True, attach_hint=options.get('attach_hint'))
		reset = kernel.ecu_reset
	if reset:
		finished = mtos.check_after_reset(ecu.bus, ecu, log_callback=log_callback) and finished
	mtos.log('[*] Done!' if finished else '[!] MTOS {} did not complete'.format(mode), log_callback)
	return finished
