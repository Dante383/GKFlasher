import argparse, time, yaml, logging, sys, os, traceback
from datetime import datetime
from dataclasses import replace
from alive_progress import alive_bar
from gkbus.device import Bus, DeviceABC, SerialKLineChannel, discover, find_device
from gkbus.exceptions import DeviceException, GkbusException, TimeoutException
from gkbus.transport import Kwp2000OverKLineTransport, Kwp2000OverCanTransport, RawPacket, PacketDirection
from gkbus.protocol import kwp2000
from gkbus.channel import KLineTiming, KLineChannel
from flasher.memory import read_memory, write_memory, prepare_flash_payloads, prepare_write_memory
from flasher.ecu import KWP_RESPONSE_TIMEOUT, ECU, identify_ecu, fetch_ecu_identification, enable_security_access, configure_serial_kline, negotiate_timing, end_diagnostic_session, ECUIdentificationException, DesiredBaudrate
from flasher.baudrate import discover_baudrate
from flasher.checksum import correct_checksum
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES, Routine, ReprogrammingStatus, AccessLevel
from flasher.logging import logger, logger_raw
from flasher.immo import cli_immo, cli_immo_info
from flasher.lineswap import generate_sie, generate_bin
from flasher.rsw.handler import rsw_handler
from flasher.mtos.handler import mtos_handler, attach_session, KERNEL_MODES, MTOS_MODES, WRITE_MODES, MTOS_BAUDRATE
from flasher.mtos.asm import mtos_baudrate
from flasher import bsl
from flasher.kernel import KERNEL_TIMING
from flasher.progress import transfer_summary
from _version import __version__

def strip (string):
	return ''.join(x for x in string if x.isalnum())

def cli_read_eeprom (ecu: ECU, eeprom_size: int, address_start: int = None, address_stop: int = None, escalate_privileges: bool = False, output_filename: str = None):
	if escalate_privileges:
		print('[*] Attempting privilege escalation with the IOCLID patch')
		if (ecu.security_access(AccessLevel.SIEMENS_0xFD)):
			print('[*] Success!')
		else:
			print('[!] Patch likely not present, failed to escalate privileges.')
			print('    Read will only include the calibration and program zones.')
			print('    If you\'re running ca663056, feel encouraged to apply the IOCLID patch.')
			print('    It will allow you to read the whole memory over OBD2, just like BSL.')
			print('    You can find it at https://github.com/OpenGK-org/opengk-simk')

	if (address_start == None):
		address_start = abs(ecu.bin_offset)
	if (address_stop == None):
		address_stop = address_start+eeprom_size

	print('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

	requested_size = address_stop-address_start
	eeprom = bytearray([0xFF]*eeprom_size)

	started_at = time.monotonic()
	with alive_bar(requested_size, unit='B') as bar:
		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=bar)

	eeprom_start = ecu.calculate_bin_offset(address_start)
	eeprom_end = eeprom_start + len(fetched)
	eeprom[eeprom_start:eeprom_end] = fetched

	if (output_filename == None):
		try:
			calibration = ecu.get_calibration()
			description = ecu.get_calibration_description()
			hw_rev_c = strip(''.join([chr(x) for x in list(ecu.bus.execute(kwp2000.commands.ReadEcuIdentification(0x8c)).get_data())[1:]]))
			hw_rev_d = strip(''.join([chr(x) for x in list(ecu.bus.execute(kwp2000.commands.ReadEcuIdentification(0x8d)).get_data())[1:]]))
			output_filename = "{}_{}_{}_{}_{}.bin".format(description, calibration, hw_rev_c, hw_rev_d, datetime.now().strftime('%Y-%m-%d_%H%M'))
		except: # dirty
			output_filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))

	with open (output_filename, "wb") as file:
		file.write(bytes(eeprom))

	print(transfer_summary('Read', len(fetched), started_at))
	print('[*] Saved to {}'.format(os.path.abspath(output_filename)))

	print('[*] Done!')

def cli_flash_eeprom (ecu, input_filename, flash_calibration=True, flash_program=True):
	print('\n[*] Loading up {}'.format(input_filename))

	try:
		with open(input_filename, 'rb') as file:
			eeprom = file.read()
	except FileNotFoundError:
		print('[!] Error: File not found.')
		return

	print('[*] Loaded {} bytes'.format(len(eeprom)))
	try:
		payloads = prepare_flash_payloads(ecu, eeprom, flash_calibration, flash_program)
	except ValueError as exc:
		print('[!] Error: {}'.format(exc))
		return

	if (input('[?] Ready to flash! Do you wish to continue? [y/n]: ') != 'y'):
		print('[!] Aborting!')
		return

	prepare_write_memory(ecu)
	ecu.bus._stop_keepalive()
	ecu.bus.transport.set_timeout(12)  # Erase can take longer than ordinary diagnostics.

	if flash_program:
		print('[*] start routine 0x00 (erase program code section)')
		ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.ERASE_PROGRAM.value))


		payload = payloads['program']

		flash_start = ecu.get_region('program').write.address + 16
		flash_size = len(payload)

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	if flash_calibration:
		print('[*] Start routine 0x01 (erase calibration section)')
		ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.ERASE_CALIBRATION.value))

		payload = payloads['calibration']

		flash_start = ecu.calculate_memory_write_offset(ecu.get_region('calibration').write.address)
		flash_size = len(payload)

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	ecu.bus.transport.set_timeout(300)

	print('[*] Start routine 0x02 (verify blocks and mark as ready to execute)')
	print('[*] Waiting for ECU...')
	try:
		ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.VERIFY_BLOCKS.value))
	except kwp2000.Kwp2000NegativeResponseException as e:
		print('[*] Verifying blocks failed! Did you forget to correct the checksum?')
		print('[*] Fetching detailed reprogramming status..')
		reprogramming_status_response = ecu.bus.execute(
			kwp2000.commands.StartRoutineByLocalIdentifier(
				Routine.CHECK_REPROGRAMMING_STATUS.value
			)
		).get_data()[1:]

		reprogramming_status = ReprogrammingStatus(int.from_bytes(reprogramming_status_response, 'big'))

		print(str(reprogramming_status))

		print('[!] Your ECU is now soft-bricked. There\'s no need to panic, all you need to do is flash a valid file.')
		return

	ecu.bus.transport.set_timeout(KWP_RESPONSE_TIMEOUT)

	print('[*] ECU Reset')
	print('[*] Done!')
	ecu.bus.execute(kwp2000.commands.ECUReset(kwp2000.enums.ResetMode.POWER_ON_RESET)).get_data()
	ecu.bus.close()
	
def cli_clear_adaptive_values (ecu):
	print('[*] Clearing adaptive values.. ', end='')
	ecu.clear_adaptive_values()
	print('Done! Turn off ignition for 10 seconds to apply changes.')

def cli_read_dtcs (ecu):
	print('[*] Reading diagnostic trouble codes')
	ecu.bus.execute(ecu.get_desired_baudrate().session_command(kwp2000.enums.DiagnosticSession.DEFAULT))

	dtcs_raw = ecu.bus.execute(
		kwp2000.commands.ReadDTCsByStatus(
			kwp2000.enums.DtcStatus.REQUEST_IDENTIFIED_DTC_AND_STATUS,
			kwp2000.enums.DtcGroup.POWERTRAIN
		)
	).get_data()

	dtc_amount = dtcs_raw[0]
	print('[*] Amount of DTCs: {}'.format(dtc_amount))
	dtcs = {}
	for x in range(dtc_amount):
		dtc = int.from_bytes(dtcs_raw[1:][(x * 3):(x * 3) + 2])
		dtc_status = dtcs_raw[1:][(x * 3) + 2]
		dtcs[dtc] = dtc_status

	for dtc, status in dtcs.items():
		print('[*] DTC: P{} ({})'.format(f"{dtc:04x}", bin(status)))

def load_config (config_filename):
	return yaml.safe_load(open('gkflasher.yml'))

def load_arguments ():
	parser = argparse.ArgumentParser(prog='GKFlasher v{}'.format(__version__))
	parser.add_argument('-p', '--protocol', help='Protocol to use. canbus or kline')
	parser.add_argument('-i', '--interface', help='Linux: can0 for socketCAN. | Windows: gs_usb:0, slcan:COM69, pcan:PCAN_USBBUS1, vector:0 | J2534: j2534, j2534:openport')
	parser.add_argument('-b', '--baudrate', type=int)
	parser.add_argument('--desired-baudrate', type=lambda x: int(x,0),
		help='ECU baudrate selector; omitted = automatic normal K-Line discovery. Normal KWP 0x06 requires --session-baudrate. '
		'Also selects MTOS/RSW transfer speed (automatic by default, 0x06 = 750000)')
	parser.add_argument('--session-baudrate', type=int,
		help='Override the selected K-Line rate; with MTOS/RSW, selects transfer speed without changing startup baud')
	parser.add_argument('--kline-p4-min', type=float,
		help='Request-byte gap in milliseconds (0..20); serial adapters wait for each byte echo when nonzero')
	parser.add_argument('-f', '--flash', help='Filename to full flash')
	parser.add_argument('--flash-calibration', help='Filename to flash calibration zone from')
	parser.add_argument('--flash-program', help='Filename to flash program zone from')
	parser.add_argument('-r', '--read', action='store_true')
	parser.add_argument('--read-calibration', action='store_true')
	parser.add_argument('--read-program', action='store_true')
	parser.add_argument('--read-dtcs', action='store_true')
	parser.add_argument('--id', action='store_true')
	parser.add_argument('--correct-checksum')
	parser.add_argument('--bin-to-sie')
	parser.add_argument('--sie-to-bin')	
	parser.add_argument('--clear-adaptive-values', action='store_true')
	parser.add_argument('-l', '--logger', action='store_true')
	parser.add_argument('-o', '--output', help='Filename to save the EEPROM dump')
	parser.add_argument('-s', '--address-start', help='Offset to start reading/flashing from.', type=lambda x: int(x,0))
	parser.add_argument('-e', '--address-stop', help='Offset to stop reading/flashing at.', type=lambda x: int(x,0))
	parser.add_argument('-c', '--config', help='Config filename', default='gkflasher.yml')
	parser.add_argument('-v', '--verbose', action='count', default=0)
	parser.add_argument('--immo', action='store_true')
	parser.add_argument('--rsw-boot1', help='Flash Boot1 using RSW bootstrap')
	parser.add_argument('--rsw-boot2', help='Flash Boot2 using RSW bootstrap')
	parser.add_argument('--rsw-asw', help='Flash ASW using RSW bootstrap')
	parser.add_argument('--rsw-cal', help='Flash CAL using RSW bootstrap')
	parser.add_argument('--rsw-full', help='Flash full image set using RSW bootstrap')
	parser.add_argument('--rsw-virginize', help='Virginize ECU using CAN RSW bootstrap', action='store_true')
	parser.add_argument('--rsw-key', help='Four-byte Siemens key for the K-Line RSW RAM bootstrap', type=lambda x: int(x, 0))
	parser.add_argument('--mtos', help='Mini Test Operating System. Write options: '
		'[--no-verify] [--skip-backup] [--force-write] [--override-safety-checks]', action='store_true')
	parser.add_argument('--mtos-mode', help='MTOS mode: hello (safe self-test), dump, payload, loader, read-calibration (flow-controlled RAM stream), '
		'probe/read/flash-calibration/fullflash (OpenGK kernel: dry run, whole flash read, whole flash write incl. boot sectors)',
		default='hello', choices=MTOS_MODES)
	parser.add_argument('--mtos-payload', help='BIN file with payload body (payload mode), second stage (loader mode) or full 4/8 Mbit BIN (fullflash/flash-calibration modes)')
	parser.add_argument('--mtos-key', help='Key to use for Siemens access level', type=lambda x: int(x,0))
	parser.add_argument('--mtos-attach', help='Pick up an MTOS kernel left running after a failed kernel job (ECU still powered). '
		'Reconnect at the automatic BSL entry rate, or use the explicit --desired-baudrate in the recovery command', action='store_true')
	parser.add_argument('--bsl', help='Bootstrap loader: ECU in boot mode, on the K-line interface', action='store_true')
	parser.add_argument('--bsl-mode', help='BSL mode: hwinfo (CPU and flash chip), read (external flash), readint (internal ROM), '
		'write (whole external flash incl. boot sectors, changed sectors only)', default='hwinfo', choices=bsl.MODES)
	parser.add_argument('--bsl-image', help='Whole flash image to write (write mode)')
	parser.add_argument('--bsl-baudrate', help='Explicit BSL baudrate; omitted = automatic kernel discovery and UART qualification', type=int)
	parser.add_argument('--bsl-driver', help='Flash driver: i4 (SIMK43 2.0) or v6. Default: detect it', default='auto', choices=bsl.DRIVER_CHOICES)
	parser.add_argument('--bsl-rom-size', help='Internal ROM size to read (readint mode, default: 0x{:X})'.format(bsl.INTERNAL_ROM_SIZE),
		type=lambda x: int(x,0), default=bsl.INTERNAL_ROM_SIZE)
	parser.add_argument('--no-verify', '--skip-verify', help='Skip all read-back verification after an MTOS or BSL write. Normally each sector is '
		'read back and checked right after it\'s written, full-image writes also verify the whole flash at the end. '
		'The sectors are still programmed, this only skips the slow read-back passes over the K-line',
		action='store_true')
	parser.add_argument('--skip-backup', help='Do not save the initial BSL or MTOS flash backup (write modes)', action='store_true')
	parser.add_argument('--force-write', help='Rewrite all selected BSL or MTOS sectors, including sectors that already match', action='store_true')
	parser.add_argument('--override-safety-checks', action='store_true',
		help='Allow unrecognized MTOS BIN identifiers; size, reset-vector and hardware checks remain')
	args = parser.parse_args()
	rsw_count = sum(bool(getattr(args, name)) for name in (
		'rsw_boot1', 'rsw_boot2', 'rsw_asw', 'rsw_cal', 'rsw_full', 'rsw_virginize'))
	args.rsw = rsw_count > 0
	if rsw_count > 1 or (args.rsw and args.mtos):
		parser.error('Select one RSW operation, without --mtos')
	if args.rsw_key is not None and not 0 <= args.rsw_key <= 0xFFFFFFFF:
		parser.error('--rsw-key must be a four-byte value')
	if (args.skip_backup or args.force_write) and not ((args.bsl and args.bsl_mode == 'write') or
		(args.mtos and args.mtos_mode in WRITE_MODES)):
		parser.error('--skip-backup and --force-write require a BSL or MTOS write operation')

	if args.override_safety_checks and not (args.mtos and args.mtos_mode in WRITE_MODES):
		parser.error('--override-safety-checks requires an MTOS write operation')

	if args.no_verify and not ((args.bsl and args.bsl_mode == 'write') or
		(args.mtos and args.mtos_mode in WRITE_MODES)):
		parser.error('--no-verify/--skip-verify requires a BSL or MTOS write operation')

	logging_levels = [logging.WARNING, logging.INFO, logging.DEBUG]
	logging.basicConfig(level=logging_levels[min(args.verbose, len(logging_levels) -1)])
	logging.getLogger("can").setLevel(logging.CRITICAL)

	GKFlasher_config = load_config(args.config)

	if args.protocol:
			GKFlasher_config['protocol'] = args.protocol
	if args.session_baudrate is not None or args.kline_p4_min is not None:
		if GKFlasher_config['protocol'] != 'kline' or args.bsl or (args.mtos and args.kline_p4_min is not None):
			parser.error('--session-baudrate requires K-Line; --kline-p4-min requires normal K-Line diagnostics')
	if args.session_baudrate is not None:
		if args.session_baudrate <= 0 or (args.desired_baudrate != 0x06 and
			(args.desired_baudrate not in BAUDRATES or BAUDRATES[args.desired_baudrate] == 10400)):
			parser.error('--session-baudrate requires a positive speed and a higher-speed --desired-baudrate selector')
	if args.desired_baudrate == 0x06:
		if (args.session_baudrate is None and not (args.mtos or args.rsw)) or (args.session_baudrate is not None and args.session_baudrate > 0xFFFFFF):
			parser.error('--desired-baudrate 0x06 requires --session-baudrate between 1 and 16777215')
	if args.mtos:
		if args.desired_baudrate is not None and args.desired_baudrate not in (*BAUDRATES, 0x06):
			parser.error('Unsupported MTOS baudrate selector')
		try:
			args.mtos_baudrate = mtos_baudrate(args.session_baudrate or (
				750000 if args.desired_baudrate == 0x06 else BAUDRATES.get(args.desired_baudrate, MTOS_BAUDRATE)))
			if args.desired_baudrate is None and args.session_baudrate is None:
				args.mtos_baudrate = None
		except ValueError as exc:
			parser.error(str(exc))
	if args.rsw:
		if args.desired_baudrate is not None and args.desired_baudrate not in (*BAUDRATES, 0x06):
			parser.error('Unsupported RSW baudrate selector')
		try:
			args.rsw_baudrate = mtos_baudrate(args.session_baudrate or (
				750000 if args.desired_baudrate == 0x06 else BAUDRATES.get(args.desired_baudrate, MTOS_BAUDRATE)))
			if args.desired_baudrate is None and args.session_baudrate is None:
				args.rsw_baudrate = None
		except ValueError as exc:
			parser.error(str(exc))
	if args.kline_p4_min is not None:
		if not 0 <= args.kline_p4_min < 18:
			parser.error('--kline-p4-min must be at least 0 and less than 18 milliseconds')
		GKFlasher_config['kline']['p4_min'] = args.kline_p4_min

	if args.interface:
		GKFlasher_config[GKFlasher_config['protocol']]['interface'] = args.interface

	if args.rsw and GKFlasher_config['protocol'] == 'kline':
		if args.rsw_virginize:
			parser.error('K-Line virginize is not supported by the native RSW sector table')
		if args.rsw_key is None:
			parser.error('K-Line RSW requires --rsw-key')

	return GKFlasher_config, args

def select_device (interface: str, bus_type: Bus) -> DeviceABC:
	try:
		return find_device(interface, {bus_type})
	except DeviceException:
		print('[!] Interface not found: {}'.format(interface))

	devices = discover({bus_type})
	if not devices:
		print('[!] No interfaces found.')
		raise SystemExit(1)

	print('[*] Available interfaces:')
	for index, device in enumerate(devices, start=1):
		print('    {}. {}'.format(index, device.description()))

	while True:
		try:
			index = int(input('Select interface [1]: ').strip() or '1')
		except ValueError:
			print('Please enter a valid number.')
			continue
		if 1 <= index <= len(devices):
			return devices[index - 1]
		print('Enter a number from 1 to {}.'.format(len(devices)))

def initialize_bus (protocol: str, protocol_config: dict) -> tuple[DeviceABC, kwp2000.Kwp2000Protocol]:
	bus_type = Bus(protocol)
	device = select_device(protocol_config['interface'], bus_type)
	print('[*] Interface: {}'.format(device.description()))

	if bus_type == Bus.CANBUS:
		channel = device.canbus(protocol_config['bitrate'])
		transport = Kwp2000OverCanTransport(channel, tx_id=protocol_config['tx_id'], rx_id=protocol_config['rx_id'])
	else:
		channel = device.kline(timing=KLineTiming(p4_min=protocol_config.get('p4_min', 0)))
		transport = Kwp2000OverKLineTransport(channel, tx_id=protocol_config['tx_id'], rx_id=protocol_config['rx_id'])

	# opened here rather than by the transport, so the channel outlives the KWP2000 session
	# (RSW switches to CCP on it). Closed by device.close()
	channel.open()
	configure_serial_kline(channel, p4_min=protocol_config.get('p4_min', 0.001))

	return device, kwp2000.Kwp2000Protocol(transport)

def cli_choose_ecu ():
	print('[!] Failed to identify your ECU!')
	print('[*] If you know what you\'re doing (like trying to revive a soft bricked ECU), you can choose your ECU from the list below:')

	for index, ecu in enumerate(ECU_IDENTIFICATION_TABLE):
		print('    [{}] {}'.format(index, ecu['ecu']['name']))

	try:
		choice = int(input('ECU or any other char to abort: '))
	except ValueError:
		print('[!] Aborting..')
		return

	try:
		ECU_IDENTIFICATION_TABLE[choice]
	except IndexError:
		print('[!] Invalid value!')
		return cli_choose_ecu()

	return ECU_IDENTIFICATION_TABLE[choice]

def cli_identify_ecu (bus: kwp2000.Kwp2000Protocol):
	print('[*] Trying to identify ECU automatically.. ')
	
	try:
		ecu = identify_ecu(bus)
	except ECUIdentificationException:
		choice = cli_choose_ecu()
		if not choice:
			return None
		ecu = ECU(**choice['ecu'])
		ecu.set_bus(bus)

	print('[*] Found! {}'.format(ecu.get_name()))
	return ecu

def main(bus: kwp2000.Kwp2000Protocol, args):
	channel = getattr(bus.transport, 'channel', None)
	ram_loader = args.mtos or (args.rsw and isinstance(channel, KLineChannel))
	automatic_baudrate = isinstance(channel, KLineChannel) and not ram_loader and args.desired_baudrate is None
	if ram_loader:
		configure_serial_kline(channel, p4_min=0)
	paced_timing = None
	if isinstance(channel, SerialKLineChannel) and args.kline_p4_min is not None and args.kline_p4_min > 0:
		# Keep startup frames contiguous; enable guarded pacing after the baud handoff.
		paced_timing = replace(channel.get_timing(), p4_max=18.0)
		channel.default_timing = replace(channel.default_timing, p4_min=0)
		channel.reset_timing()
	bus.transport.set_timeout(KWP_RESPONSE_TIMEOUT)
	bus.init(kwp2000.commands.StartCommunication(), keepalive_command=kwp2000.commands.TesterPresent(kwp2000.enums.ResponseType.REQUIRED), keepalive_delay=1.5)
	bus.transport.set_buffer_size(20)

	if not ram_loader and args.desired_baudrate and BAUDRATES.get(args.desired_baudrate) != 10400:
		try:
			baudrate = args.session_baudrate if args.desired_baudrate == 0x06 else BAUDRATES[args.desired_baudrate]
			desired_baudrate = DesiredBaudrate(index=args.desired_baudrate, baudrate=baudrate)
			if args.session_baudrate is not None:
				desired_baudrate.baudrate = args.session_baudrate
		except KeyError:
			print('[!] Selected baudrate is invalid! Available baudrates:')
			for key, baudrate in BAUDRATES.items():
				print('{} - {}'.format(hex(key), baudrate))
			print('0x06 - custom (requires --session-baudrate)')
			return

		print('[*] Trying to start diagnostic session with baudrate {}'.format(desired_baudrate.baudrate))
		try:
			bus.execute(desired_baudrate.session_command(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING))
			bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
		except TimeoutException:
			# it's possible that the bus is already running at the desired baudrate - let's check
			bus.transport.channel.reset_buffers()
			bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
			bus.execute(desired_baudrate.session_command(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING))
	else:
		# @todo: not ideal, but its a bridge towards moving this completely to the ECU class. it was a mess
		desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)
		print('[*] Trying to start diagnostic session')
		bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING))
		
	if paced_timing is not None:
		channel.default_timing = paced_timing
		channel.set_timing(paced_timing)
	if not automatic_baudrate:
		print('[*] Set timing parameters to maximum')
		if not negotiate_timing(bus):
			print('[!] Not supported on this ECU!')

	# this stays for now instead of the method built in the ECU class
	# @todo - after moving ecu definitions to classes, either start 
	# with an empty ECU object that'll implement security access (and then fill the object upon identification),
	# or better, come up with an identification way that doesn't require memory reading access
	print('[*] Security Access')
	enable_security_access(bus)

	ecu = cli_identify_ecu(bus)
	if not ecu:
		return

	if automatic_baudrate:
		desired_baudrate = discover_baudrate(ecu)
		print('[*] Set timing parameters to maximum')
		if not negotiate_timing(bus):
			print('[!] Not supported on this ECU!')
	ecu.set_desired_baudrate(desired_baudrate)
	ecu.diagnostic_session_type = kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING
	ecu.access_level = AccessLevel.HYUNDAI_0x1

	print('[*] Trying to find calibration..')
	
	try:
		description, calibration = ecu.get_calibration_description(), ecu.get_calibration()
		print('[*] Found! Description: {}, calibration: {}'.format(description, calibration))
	except kwp2000.Kwp2000NegativeResponseException:
		if (input('[!] Failed! Do you want to continue? [y/n]: ') != 'y'):
			return

	if (args.immo):
		return cli_immo(ecu)

	if (args.id):
		print('[*] Reading ECU Identification..',end='')
		for parameter_key, parameter in fetch_ecu_identification(bus).items():
			value_dec = list(parameter['value'])
			value_hex = ' '.join([hex(x) for x in value_dec])
			value_ascii = strip(''.join([chr(x) for x in value_dec]))

			print('')
			print('    [*] [{}] {}:'.format(hex(parameter_key), parameter['name']))
			print('            [HEX]: {}'.format(value_hex))
			print('            [ASCII]: {}'.format(value_ascii))
			print('')

		cli_immo_info(ecu)

	eeprom_size = ecu.get_eeprom_size_bytes()

	if (args.read):
		cli_read_eeprom(ecu, eeprom_size, address_start=args.address_start, address_stop=args.address_stop, escalate_privileges=True, output_filename=args.output)
	if (args.read_calibration):
		cli_read_eeprom(ecu, eeprom_size, address_start=ecu.get_region('calibration').read.address, address_stop=ecu.get_region('calibration').read.address+ecu.get_region('calibration').read.size, output_filename=args.output)
	if (args.read_program):
		address_start = ecu.get_region('program').read.address
		address_stop = address_start+ecu.get_region('program').read.address
		cli_read_eeprom(ecu, eeprom_size, address_start=address_start, address_stop=address_stop, output_filename=args.output)
	if (args.read_dtcs):
		cli_read_dtcs(ecu)

	if (args.flash):
		cli_flash_eeprom(ecu, input_filename=args.flash)
	if (args.flash_calibration):
		cli_flash_eeprom(ecu, input_filename=args.flash_calibration, flash_calibration=True, flash_program=False)
	if (args.flash_program):
		cli_flash_eeprom(ecu, input_filename=args.flash_program, flash_program=True, flash_calibration=False)

	if (args.clear_adaptive_values):
		cli_clear_adaptive_values(ecu)

	if (args.logger):
		logger(ecu)

	if (args.rsw_boot1):
		rsw_handler(ecu, mode='boot1', bin_file=args.rsw_boot1, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.rsw_boot2):
		rsw_handler(ecu, mode='boot2', bin_file=args.rsw_boot2, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.rsw_asw):
		rsw_handler(ecu, mode='asw', bin_file=args.rsw_asw, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.rsw_cal):
		rsw_handler(ecu, mode='cal', bin_file=args.rsw_cal, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.rsw_full):
		rsw_handler(ecu, mode='full', bin_file=args.rsw_full, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.rsw_virginize):
		rsw_handler(ecu, mode='virginize', bin_file=args.rsw_virginize, key=args.rsw_key, baudrate=args.rsw_baudrate)

	if (args.mtos):
		mtos_address = args.address_start if args.address_start is not None else 0x000000
		if args.address_stop is not None and args.address_start is not None:
			mtos_length = args.address_stop - args.address_start
		else:
			mtos_length = 0x40
		mtos_handler(ecu, args.mtos_key, mode=args.mtos_mode,
			payload_filename=args.mtos_payload, address=mtos_address, length=mtos_length,
			output_filename=args.output, attach_hint=mtos_attach_hint(args), skip_verify=args.no_verify,
			skip_backup=args.skip_backup, force=args.force_write, override_safety_checks=args.override_safety_checks,
			baudrate=args.mtos_baudrate)

def mtos_attach_hint (args) -> str:
	'''
	The command that picks a kernel left running by this session back up
	'''
	command = 'python gkflasher.py -p {} -i {} --mtos --mtos-mode {} --mtos-attach'.format(
		GKFlasher_config['protocol'], GKFlasher_config[GKFlasher_config['protocol']]['interface'], args.mtos_mode)
	if args.mtos_payload:
		command += ' --mtos-payload {}'.format(args.mtos_payload)
	if GKFlasher_config['protocol'] == 'kline' and args.mtos_baudrate is not None:
		command += ' --desired-baudrate 0x06 --session-baudrate {}'.format(args.mtos_baudrate)
	return command

def mtos_attach (bus, args) -> None:
	if args.mtos_mode not in KERNEL_MODES:
		print('[!] --mtos-attach works with --mtos-mode {}'.format(', '.join(KERNEL_MODES)))
		return
	if args.mtos_mode in WRITE_MODES and not args.mtos_payload:
		print('[!] {} mode requires --mtos-payload (the full 4/8 Mbit BIN)'.format(args.mtos_mode))
		return
	attach_session(bus, args.mtos_mode, image_filename=args.mtos_payload, output_filename=args.output,
		baudrate=args.mtos_baudrate, attach_hint=mtos_attach_hint(args), skip_verify=args.no_verify,
			skip_backup=args.skip_backup, force=args.force_write, override_safety_checks=args.override_safety_checks)

def bsl_main (args) -> int:
	'''
	BSL over the bare K-line, no KWP2000 session

	:return: exit code
	'''
	interface = args.interface or GKFlasher_config['kline']['interface']
	device = select_device(interface, Bus.KLINE)
	print('[*] Interface: {}'.format(device.description()))
	channel = None
	try:
		channel = device.kline(baudrate=args.bsl_baudrate or 57600, timing=KERNEL_TIMING)
		channel.open()
		finished = bsl.bsl_handler(channel, args.bsl_mode, image_filename=args.bsl_image, output_filename=args.output,
			driver=args.bsl_driver, rom_size=args.bsl_rom_size, skip_verify=args.no_verify,
			skip_backup=args.skip_backup, force=args.force_write, auto_baudrate=args.bsl_baudrate is None)
		return 0 if finished else 1
	except GkbusException as e:
		print('[!] K-line interface: {}'.format(e))
		return 1
	except KeyboardInterrupt:
		print('\n[!] Interrupted')
		return 1
	finally:
		try:
			if channel is not None:
				channel.close()
			device.close()
		except GkbusException as e:
			print('[!] Couldn\'t close the interface: {}'.format(e))

def packet2hex (packet: RawPacket) -> str:
	direction = 'Incoming' if packet.direction == PacketDirection.INCOMING else 'Outgoing'
	data = ' '.join([hex(x)[2:].zfill(2) for x in packet.data])
	parsed = 'RawPacket({}, ts={}, data={})'.format(direction, packet.timestamp, data)
	return parsed

def cli_exception_hook(exception_type, exception, traceback):
	if issubclass(exception_type, KeyboardInterrupt):
		print('\n[!] Interrupted', flush=True)
	else:
		sys.__excepthook__(exception_type, exception, traceback)


if __name__ == '__main__':
	sys.excepthook = cli_exception_hook
	GKFlasher_config, args = load_arguments()

	print('[*] GKFlasher v{}'.format(__version__))

	if (args.correct_checksum):
		correct_checksum(filename=args.correct_checksum)

	if (args.bin_to_sie):
		generate_sie(filename=args.bin_to_sie)
		sys.exit()

	if (args.sie_to_bin):
		generate_bin(filename=args.sie_to_bin)
		sys.exit()

	if args.bsl:
		code = bsl_main(args)
		sys.stdout.flush()
		os._exit(code)
	
	print('[*] Selected protocol: {}. Initializing..'.format(GKFlasher_config['protocol']))
	try:
		device, bus = initialize_bus(GKFlasher_config['protocol'], GKFlasher_config[GKFlasher_config['protocol']])
	except GkbusException as e:
		print('[!] Failed to open the interface: {}'.format(e))
		sys.exit(1)

	try:
		if args.mtos and args.mtos_attach:
			mtos_attach(bus, args)
		else:
			main(bus, args)
	except KeyboardInterrupt:
		print('\n[!] Interrupted')
	except Exception:
		print('\n\n[!] Exception in main thread!')
		print(traceback.format_exc())
		print('[*] Dumping buffer:\n')
		print('\n'.join([packet2hex(packet) for packet in bus.transport.buffer_dump()]))
		print('\n[!] Shutting down due to an exception in the main thread. For exception details, see above')
	if not args.mtos and bus.transport.is_open():
		end_diagnostic_session(bus)
	bus.close()
	device.close()
	sys.stdout.flush()
	os._exit(0)
