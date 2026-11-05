import logging
import re
from datetime import datetime
from typing import Self
from gkbus.protocol import kwp2000
from gkbus.channel import KLineChannel, KLineTiming
from gkbus.exceptions import TimeoutException
from flasher.rsw import log
from ecu_definitions import (
    ECU_IDENTIFICATION_TABLE,
    IOIdentifier,
    AccessLevel,
    KEFICO_LAYOUT,
    ECU_FAMILY_SIMK,
    ECU_FAMILY_M797,
    ECU_FAMILY_UNKNOWN,
    M797_ECU_IDENTIFICATION_PARAMETERS,
)
from dataclasses import dataclass

logger = logging.getLogger(__name__)

kwp_ecu_identification_parameters = [
	{'value': 0x86, 'name': 'DCS ECU Identification'},
	{'value': 0x87, 'name': 'DCX/MMC ECU Identification'},
	{'value': 0x88, 'name': 'VIN (original)'},
	{'value': 0x89, 'name': 'Diagnostic Variant Code'},
	{'value': 0x90, 'name': 'VIN (current)'},
	{'value': 0x96, 'name': 'Calibration identification'},
	{'value': 0x97, 'name': 'Calibration Verification Number'},
	{'value': 0x9A, 'name': 'ECU Code Fingerprint'},
	{'value': 0x9B, 'name': 'ECU Data Fingerprint'},
	{'value': 0x9C, 'name': 'ECU Code Software Identification'},
	{'value': 0x9D, 'name': 'ECU Data Software Identification'},
	{'value': 0x9E, 'name': 'ECU Boot Software Identification'},
	{'value': 0x9F, 'name': 'ECU Boot Fingerprint'},
	{'value': 0x8A, 'name': 'System supplier specific'},
	{'value': 0x8B, 'name': 'System supplier specific'},
	{'value': 0x8C, 'name': 'Bootloader version'},
	{'value': 0x8D, 'name': 'Program code version'},
	{'value': 0x8E, 'name': 'Calibration version'},
	{'value': 0x8F, 'name': 'System supplier specific'},
]

def read_ecu_identification(bus, identifier: int) -> bytes:
    """Return an identification payload, accepting an optional echoed identifier."""
    data = bytes(bus.execute(kwp2000.commands.ReadEcuIdentification(identifier)).get_data())
    return data[1:] if data and data[0] == identifier else data

def fetch_ecu_identification(bus, parameters=None):
    if parameters is None:
        parameters = kwp_ecu_identification_parameters
    values = {}
    for parameter in parameters:
        identifier = parameter['value']
        try:
            value = read_ecu_identification(bus, identifier)
        except kwp2000.Kwp2000NegativeResponseException:
            continue
        values[identifier] = {'name': parameter['name'], 'value': value}
    return values

def calculate_key (concat11_seed):
    key = 0x9360
    
    for index in range(0x24):
        key = key * 2 ^ concat11_seed
        
    return key & 0xFFFF

# this stays for now instead of the method built in the ECU class
# @todo - after moving ecu definitions to classes, either start 
# with an empty ECU object that'll implement security access (and then fill the object upon identification),
# or better, come up with an identification way that doesn't require memory reading access
def enable_security_access (bus: kwp2000.Kwp2000Protocol):
	seed = bus.execute(kwp2000.commands.SecurityAccess().request_seed()).get_data()[1:]

	if (sum(seed) == 0):
		logging.info('ECU returned seed=0. Either it\'s unlocked, or previous diagnostics session was still active')
		return

	key = calculate_key(int.from_bytes(seed, 'big')).to_bytes(2, 'big')

	bus.execute(kwp2000.commands.SecurityAccess().send_key(key))

# the SIMK43 advertises p3min=0 in the flash session but drops requests that arrive too soon
# after its response (measured on a bench: 7 ms fails, 10 ms works), so keep a margin floor.
# 20 ms gives ~2x headroom and still cuts a 254-byte read at 125000 baud from ~140 ms to ~50 ms
P3_MIN_FLOOR_MS = 20.0

def negotiate_timing (bus: kwp2000.Kwp2000Protocol) -> bool:
	'''
	Switch the ECU to the fastest timing it allows and shorten the host's pause before each
	request to match - that's what speeds bulk reads up. Lasts until the next
	StartDiagnosticSession: the ECU and gkbus both go back to default timing then, so call this
	again after returning to the flash session. Flash session only - in the default session
	the SIMK43 offers different limits, and using them broke communication on the bench

	:return: False if the ECU doesn't support it, timing stays at the defaults
	'''
	try:
		limits = bus.execute(
			kwp2000.commands.AccessTimingParameters().read_limits_of_possible_timing_parameters()
		).get_data()[1:]
		if len(limits) != 5:
			return False
		bus.execute(
			kwp2000.commands.AccessTimingParameters().set_timing_parameters_to_given_values(*limits)
		)
	except kwp2000.Kwp2000NegativeResponseException:
		return False

	channel = getattr(bus.transport, 'channel', None)
	if isinstance(channel, KLineChannel):
		# p2min p2max >p3min< p3max p4min, 0.5 ms per bit: the flash session's default p3min
		# of 0x6e is the 55 ms the SIMK43 was measured to need
		ecu_p3min = limits[2] / 2
		channel.set_timing(KLineTiming(p3_min=max(ecu_p3min, P3_MIN_FLOOR_MS)))
	return True

def release_timing (bus: kwp2000.Kwp2000Protocol) -> None:
	'''
	Undo negotiate_timing before letting go of the ECU. Negotiating also raises P3max - how long
	the session outlives the last request - from 5 s to ~63 s, and a session left at a higher
	baudrate ignores the next 10400 baud init that long (each try restarts the countdown).
	Does nothing unless timing was negotiated, errors are ignored. Never call it while MTOS code
	is running - it would take the request for its own input
	'''
	channel = getattr(bus.transport, 'channel', None)
	if not isinstance(channel, KLineChannel) or channel.get_timing() == channel.default_timing:
		return
	timeout = bus.transport.get_timeout()
	bus.transport.set_timeout(1)
	try:
		bus.execute(kwp2000.commands.AccessTimingParameters().set_timing_parameters_to_default_values())
	except (IOError, kwp2000.Kwp2000Exception):
		pass
	finally:
		bus.transport.set_timeout(timeout)

@dataclass
class DesiredBaudrate:
	'''
	Index is the value sent with StartDiagnosticSession, baudrate is the serial interface baudrate
	'''
	index: int
	baudrate: int

@dataclass
class RegionOffsets:
	address: int
	size: int

@dataclass
class Region:
	read: RegionOffsets
	write: RegionOffsets

class ECU:
	# static vars block, probably should be uppercase'd
	name: str
	eeprom_size_bytes: int
	bin_offset: int
	regions: dict

	bus: kwp2000.Kwp2000Protocol
	desired_baudrate: DesiredBaudrate
	diagnostic_session_type: kwp2000.enums.DiagnosticSession 
	access_level: AccessLevel

	def __init__ (self, 
		name: str, 
		eeprom_size_bytes: int,
		bin_offset: int,
		regions: dict
		):
		self.name = name
		self.eeprom_size_bytes = eeprom_size_bytes
		self.bin_offset = bin_offset
		self.regions = regions
		self.desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)

	def get_name (self) -> str:
		return self.name 

	def get_eeprom_size_bytes (self) -> int:
		return self.eeprom_size_bytes

	def get_region (self, name: str) -> Region:
		return Region(
			read=RegionOffsets(
				**self.regions[name]['read']
			),
			write=RegionOffsets(
				**self.regions[name]['write']
			)
		)

	def set_bus (self, bus: kwp2000.Kwp2000Protocol) -> Self:
		self.bus = bus
		return self

	def get_desired_baudrate (self) -> DesiredBaudrate:
		return self.desired_baudrate

	def set_desired_baudrate (self, desired_baudrate: DesiredBaudrate) -> Self:
		'''
		This only sets an attribute, it does not change the currently active baudrate
		'''
		self.desired_baudrate = desired_baudrate
		return self

	def get_diagnostic_session_type (self) -> kwp2000.enums.DiagnosticSession:
		return self.diagnostic_session_type

	def set_diagnostic_session_type (self, diagnostic_session_type: kwp2000.enums.DiagnosticSession) -> Self:
		'''
		This only sets an attribute, it does not change the currently active diagnostic session
		'''
		self.diagnostic_session_type = diagnostic_session_type
		return self

	def calculate_bin_offset (self, offset: int) -> int:
		return offset + self.bin_offset

	def calculate_memory_write_offset (self, offset: int) -> int:
		# @todo: find out why this byte shift is needed
		# this happens only for the calibration zone
		return (0x80000 << 4) + offset

	def _security_access_hyundai (self) -> None:
		seed = self.bus.execute(kwp2000.commands.SecurityAccess().request_seed()).get_data()[1:]

		if (sum(seed) == 0):
			logging.info('ECU returned seed=0. Either it\'s unlocked, or previous diagnostics session was still active')
			return

		key = calculate_key(int.from_bytes(seed, 'big')).to_bytes(2, 'big')

		self.bus.execute(kwp2000.commands.SecurityAccess().send_key(key))

	def _security_access_siemens (self) -> bool:
		logger.info('Attempting privilege escalation via the IOCLID')
		
		self.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT, self.desired_baudrate.index))
		
		try:
			self.bus.execute(
				kwp2000.commands.InputOutputControlByLocalIdentifier(
					IOIdentifier._OPENGK_PATCH_PRIVILEGE_ESCALATION.value, 
					kwp2000.enums.InputOutputControlParameter.LONG_TERM_ADJUSTMENT
				)
			)
			return True
		except kwp2000.Kwp2000NegativeResponseException as e:
			if e.status == kwp2000.Kwp2000NegativeStatusIdentifierEnum.REQUEST_OUT_OF_RANGE:
				logger.debug('Privilege escalation: Received 0x31 out of range, pretty standard - patch likely not present')
			logger.error(str(e))
		except TimeoutException:
			logger.error('Received a timeout while trying to escalate privileges. This might indicate that something went wrong with the patch and the ECU is now being restarted by watchdog.')
		finally:
			self._restore_session()

		return False

	def _restore_session (self) -> None:
		'''
		Back to the ECU's own diagnostic session after a detour to the default one. The session
		change dropped the negotiated timing, so negotiate it again to keep bulk reads fast
		'''
		self.bus.execute(kwp2000.commands.StartDiagnosticSession(self.diagnostic_session_type, self.desired_baudrate.index))
		if self.diagnostic_session_type == kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING:
			negotiate_timing(self.bus)

	def security_access (self, access_level: AccessLevel = AccessLevel.HYUNDAI_0x1) -> bool:
		if access_level == AccessLevel.HYUNDAI_0x1:
			self._security_access_hyundai()
		elif access_level == AccessLevel.SIEMENS_0xFD:
			if not self._security_access_siemens():
				return False
		
		self.access_level = access_level
		return True

	def get_calibration (self) -> str:
		calibration = self.bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=self.get_region('calibration').read.address, size=8)).get_data()
		return ''.join([chr(x) for x in list(calibration)])

	def get_calibration_description (self) -> str:
		description = self.bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=self.get_region('calibration').read.address+0x40, size=8)).get_data()
		return ''.join([chr(x) for x in list(description)])

	def read_memory_by_address (self, offset: int, size: int) -> bytes:
		data = bytes()
		
		try:
			data = self.bus.execute(
				kwp2000.commands.ReadMemoryByAddress(
					offset=offset, 
					size=size
				)
			).get_data()
		except kwp2000.Kwp2000NegativeResponseException as e:
			if e.status.identifier == kwp2000.Kwp2000NegativeStatusIdentifierEnum.CANT_UPLOAD_FROM_SPECIFIED_ADDRESS.value:
				if size == 1:
					raise
				logger.warning('Can\'t upload from %s! This might be a restricted area or more commonly, offset where eeprom pages switch. I\'m gonna try reading 1 byte at a time for the next 16 bytes', hex(offset))
				try:
					one_at_a_time_amount = min(16, size)
					for x in range(one_at_a_time_amount):
						data += self.read_memory_by_address(offset+x, size=1)
					data += self.read_memory_by_address(offset+one_at_a_time_amount, size=size-one_at_a_time_amount)
				except kwp2000.Kwp2000NegativeResponseException as e:
					raise e
			else:
				raise e
		return data

	def clear_adaptive_values (self):
		self.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT, self.desired_baudrate.index))
		self.bus.execute(kwp2000.commands.InputOutputControlByLocalIdentifier(IOIdentifier.ADAPTIVE_VALUES.value, kwp2000.enums.InputOutputControlParameter.RESET_TO_DEFAULT))
		self._restore_session()

class ECUIdentificationException (Exception):
	pass

def identify_ecu (bus: kwp2000.Kwp2000Protocol) -> ECU:
	for ecu_identifier in ECU_IDENTIFICATION_TABLE:
		try:
			result = bus.execute(kwp2000.commands.ReadMemoryByAddress(offset=ecu_identifier['offset'], size=len(ecu_identifier['expected'][0]))).get_data()
		except kwp2000.Kwp2000NegativeResponseException:
			continue

		if result in ecu_identifier['expected']:
			ecu = ECU(**ecu_identifier['ecu'])
			ecu.set_bus(bus)
			return ecu
	raise ECUIdentificationException('Failed to identify ECU!')


class M797ECU(ECU):
    """Kefico layout; session and bulk operations are handled by flasher.m797."""

    def __init__(self):
        super().__init__(**KEFICO_LAYOUT)


def _meaningful_identification_payload(response, identifier: int) -> bool:
    """Reject filler/blank positive responses such as SIMK's all-FF IDs."""
    data = bytes(response)

    if data and data[0] == identifier:
        data = data[1:]

    if not data:
        return False

    # Erased/unimplemented local identifiers observed on SIMK answer with
    # positive SID but contain only 00/FF (occasionally spaces).
    meaningful = bytes(
        value for value in data
        if value not in (0x00, 0x20, 0xFF)
    )

    return len(meaningful) >= 2


def detect_ecu_family(bus: kwp2000.Kwp2000Protocol) -> str:
    """
    Non-destructive early family detection before any SIMK-specific setup.

    M7.9.7 exposes supplier/hardware/software IDs; SIMK exposes boot/program/
    calibration versions before security unlock. Require multiple meaningful
    responses so a disconnected ECU cannot be classified as either family.
    """
    for family, identifiers in (
        # SIMK V6 also implements 0x91/0x92, so its version IDs take priority.
        (ECU_FAMILY_SIMK, (0x8C, 0x8D, 0x8E)),
        (ECU_FAMILY_M797, (0x91, 0x92, 0x93, 0x94, 0x95)),
    ):
        positive_ids = 0
        for identifier in identifiers:
            try:
                response = bus.execute(
                    kwp2000.commands.ReadEcuIdentification(identifier)
                ).get_data()
            except (kwp2000.Kwp2000NegativeResponseException, TimeoutException):
                continue
            if _meaningful_identification_payload(response, identifier):
                positive_ids += 1
                if positive_ids >= 2:
                    return family

    # Some SIMK variants also expose memory signatures without security.
    try:
        identify_ecu(bus)
        return ECU_FAMILY_SIMK
    except (ECUIdentificationException, kwp2000.Kwp2000NegativeResponseException, TimeoutException):
        return ECU_FAMILY_UNKNOWN


def display_m797_identification(bus: kwp2000.Kwp2000Protocol, log_callback=None):
    log('[*] Reading Kefico/Bosch M7.9.7 ECU Identification', log_callback)

    for parameter_key, parameter in fetch_ecu_identification(bus, M797_ECU_IDENTIFICATION_PARAMETERS).items():
        value = parameter['value']
        value_hex = ' '.join('{:02X}'.format(x) for x in value)
        value_ascii = ''.join(
            chr(x) if 0x20 <= x <= 0x7E else '.'
            for x in value
        ).rstrip()

        log('    [*] [0x{:02X}] {}:'.format(
            parameter_key,
            parameter['name']
        ), log_callback)
        log('            [HEX]: {}'.format(value_hex), log_callback)
        log('            [ASCII]: {}'.format(value_ascii), log_callback)



def display_m797_dtcs(bus: kwp2000.Kwp2000Protocol, log_callback=None):
    log('[*] Reading Kefico/Bosch M7.9.7 diagnostic trouble codes', log_callback)
    data = bus.execute(kwp2000.commands.ReadDTCsByStatus(
        kwp2000.enums.DtcStatus.REQUEST_IDENTIFIED_DTC_AND_STATUS,
        kwp2000.enums.DtcGroup.ALL,
    )).get_data()
    if not data:
        log('[!] ECU returned an empty DTC response', log_callback)
        return
    log('[*] Amount of DTCs: {}'.format(data[0]), log_callback)
    for offset in range(1, 1 + data[0] * 3, 3):
        if offset + 3 > len(data):
            log('[!] DTC response is shorter than expected', log_callback)
            break
        dtc = int.from_bytes(data[offset:offset + 2], 'big')
        status = data[offset + 2]
        log('[*] DTC: P{:04X}, Status: 0x{:02X}'.format(dtc, status), log_callback)


def _sanitize_m797_filename_component(value: str) -> str:
    value = value.strip().strip('\x00')
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', value)
    value = re.sub(r'\s+', '_', value)
    return value.strip(' ._')


def _m797_text_identification(bus, identifier: int, name: str) -> str:
    value = read_ecu_identification(bus, identifier).decode('ascii', errors='ignore').strip(' \t\r\n\x00')
    if not value:
        raise RuntimeError('M7.9.7 {} ID 0x{:02X} was blank'.format(name, identifier))
    return value


def get_m797_calibration_id(bus: kwp2000.Kwp2000Protocol) -> str:
    return _m797_text_identification(bus, 0x90, 'calibration')


def get_m797_engine_id(bus: kwp2000.Kwp2000Protocol) -> str:
    return _m797_text_identification(bus, 0x97, 'engine').title()


def make_m797_read_filename(
        calibration_id: str,
        engine_id: str | None = None,
        bus=None
    ) -> str:
    parts = [calibration_id or 'UNKNOWN', engine_id or 'M797']
    for identifier in (0x91, 0x94):
        value = 'UNKNOWN'
        if bus is not None:
            try:
                data = read_ecu_identification(bus, identifier)
                value = data.decode('ascii', errors='ignore')
            except (kwp2000.Kwp2000NegativeResponseException, TimeoutException):
                pass
        parts.append(value)
    parts = [_sanitize_m797_filename_component(value) or 'UNKNOWN' for value in parts]
    parts.append(datetime.now().strftime('%Y-%m-%d_%H%M'))
    return '_'.join(parts) + '.bin'
