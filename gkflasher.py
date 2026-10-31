import argparse, time, yaml, logging, sys, os, traceback
from datetime import datetime
from alive_progress import alive_bar
from gkbus.device import Bus, DeviceABC, discover, find_device
from gkbus.exceptions import DeviceException, GkbusException, TimeoutException
from gkbus.transport import Kwp2000OverKLineTransport, Kwp2000OverCanTransport, RawPacket, PacketDirection
from gkbus.protocol import kwp2000
from flasher.memory import read_memory, write_memory, dynamic_find_end
from flasher.ecu import ECU, identify_ecu, fetch_ecu_identification, enable_security_access, ECUIdentificationException, DesiredBaudrate
from flasher.checksum import correct_checksum
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES, Routine, ReprogrammingStatus, AccessLevel
from flasher.logging import logger, logger_raw
from flasher.immo import cli_immo, cli_immo_info
from flasher.lineswap import generate_sie, generate_bin
from flasher.rsw import rsw_handler
from flasher.mtos import mtos_handler
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

	print('[*] saved to {}'.format(output_filename))

	print('[*] Done!')

def cli_flash_eeprom (ecu, input_filename, flash_calibration=True, flash_program=True):
	print('\n[*] Loading up {}'.format(input_filename))

	with open(input_filename, 'rb') as file:
		eeprom = file.read()

	print('[*] Loaded {} bytes'.format(len(eeprom)))

	if (input('[?] Ready to flash! Do you wish to continue? [y/n]: ') != 'y'):
		print('[!] Aborting!')
		return

	if flash_program:
		print('[*] start routine 0x00 (erase program code section)')
		ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.ERASE_PROGRAM.value))


		# we need to start 16 bytes later as the program section starts with a flag that we can't write
		payload_start = ecu.calculate_bin_offset(ecu.get_region('program').write.address) + 16
		payload_stop = payload_start + dynamic_find_end(eeprom[payload_start:(payload_start+ecu.get_region('program').write.size-16)])
		payload = eeprom[payload_start:payload_stop]

		flash_start = ecu.get_region('program').write.size + 16
		flash_size = payload_stop-payload_start

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	if flash_calibration:
		print('[*] start routine 0x01 (erase calibration section)')
		ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.ERASE_CALIBRATION.value))

		payload_start = ecu.calculate_bin_offset(ecu.get_region('calibration').write.address)
		# we need to shave 16 bytes off the top as this is where a flag that we can't write is located
		payload_stop = payload_start + dynamic_find_end(eeprom[payload_start:(payload_start+ecu.get_region('calibration').write.size-16)])
		payload = eeprom[payload_start:payload_stop]

		flash_start = ecu.calculate_memory_write_offset(ecu.get_region('calibration').write.address)
		flash_size = payload_stop-payload_start

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	ecu.bus.transport.set_timeout(300)

	print('[*] start routine 0x02 (verify blocks and mark as ready to execute)')
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

	ecu.bus.transport.set_timeout(12)

	print('[*] ecu reset')
	print('[*] done!')
	ecu.bus.execute(kwp2000.commands.ECUReset(kwp2000.enums.ResetMode.POWER_ON_RESET)).get_data()
	ecu.bus.close()
	
def cli_clear_adaptive_values (ecu):
	print('[*] Clearing adaptive values.. ', end='')
	ecu.clear_adaptive_values()
	print('Done! Turn off ignition for 10 seconds to apply changes.')

def cli_read_dtcs (ecu):
	print('[*] Reading diagnostic trouble codes')
	ecu.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

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
	parser.add_argument('-i', '--interface', help='Linux: can0 for socketCAN. | Windows: gs_usb:0, slcan:COM69, pcan:PCAN_USBBUS1, vector:0')
	parser.add_argument('-b', '--baudrate', type=int)
	parser.add_argument('--desired-baudrate', type=lambda x: int(x,0))
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
	parser.add_argument('--rsw-virginize', help='Virginize ECU using RSW bootstrap')
	parser.add_argument('--mtos',  help='Mini Test Operating System', action='store_true')
	parser.add_argument('--mtos-payload', help='BIN file to use for MTOS (full dump)')
	parser.add_argument('--mtos-key', help='Key to use for Siemens access level', type=lambda x: int(x,0))
	args = parser.parse_args()

	logging_levels = [logging.WARNING, logging.INFO, logging.DEBUG]
	logging.basicConfig(level=logging_levels[min(args.verbose, len(logging_levels) -1)])
	logging.getLogger("can").setLevel(logging.CRITICAL)

	GKFlasher_config = load_config(args.config)

	if args.protocol:
			GKFlasher_config['protocol'] = args.protocol

	if args.interface:
		GKFlasher_config[GKFlasher_config['protocol']]['interface'] = args.interface

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
		channel = device.kline()
		transport = Kwp2000OverKLineTransport(channel, tx_id=protocol_config['tx_id'], rx_id=protocol_config['rx_id'])

	# opened here rather than by the transport, so the channel outlives the KWP2000 session
	# (RSW switches to CCP on it). Closed by device.close()
	channel.open()

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
	bus.init(kwp2000.commands.StartCommunication(), keepalive_command=kwp2000.commands.TesterPresent(kwp2000.enums.ResponseType.REQUIRED), keepalive_delay=1.5)
	bus.transport.set_buffer_size(20)

	if args.desired_baudrate and BAUDRATES.get(args.desired_baudrate) != 10400:
		try:
			desired_baudrate = DesiredBaudrate(index=args.desired_baudrate, baudrate=BAUDRATES[args.desired_baudrate])
		except KeyError:
			print('[!] Selected baudrate is invalid! Available baudrates:')
			for key, baudrate in BAUDRATES.items():
				print('{} - {}'.format(hex(key), baudrate))
			return

		print('[*] Trying to start diagnostic session with baudrate {}'.format(desired_baudrate.baudrate))
		try:
			bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate.index))
			bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
		except TimeoutException:
			# it's possible that the bus is already running at the desired baudrate - let's check
			bus.transport.channel.reset_buffers()
			bus.transport.channel.set_baudrate(desired_baudrate.baudrate)
			bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate.index))
	else:
		# @todo: not ideal, but its a bridge towards moving this completely to the ECU class. it was a mess
		desired_baudrate = DesiredBaudrate(index=None, baudrate=10400)
		print('[*] Trying to start diagnostic session')
		bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING))
		
	bus.transport.set_timeout(12)

	print('[*] Set timing parameters to maximum')
	try:
		available_timing = bus.execute(
			kwp2000.commands.AccessTimingParameters().read_limits_of_possible_timing_parameters()
		).get_data()

		bus.execute(
			kwp2000.commands.AccessTimingParameters().set_timing_parameters_to_given_values(
				*available_timing[1:]
			)
		)
	except kwp2000.Kwp2000NegativeResponseException:
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
		rsw_handler(ecu, mode='boot1', bin_file=args.rsw_boot1)

	if (args.rsw_boot2):
		rsw_handler(ecu, mode='boot2', bin_file=args.rsw_boot2)

	if (args.rsw_asw):
		rsw_handler(ecu, mode='asw', bin_file=args.rsw_asw)

	if (args.rsw_cal):
		rsw_handler(ecu, mode='cal', bin_file=args.rsw_cal)

	if (args.rsw_full):
		rsw_handler(ecu, mode='full', bin_file=args.rsw_full)

	if (args.rsw_virginize):
		rsw_handler(ecu, mode='virginize', bin_file=args.rsw_virginize)

	if (args.mtos):
		mtos_handler(ecu, args.mtos_payload, args.mtos_key)
	bus.close()

def packet2hex (packet: RawPacket) -> str:
	direction = 'Incoming' if packet.direction == PacketDirection.INCOMING else 'Outgoing'
	data = ' '.join([hex(x)[2:].zfill(2) for x in packet.data])
	parsed = 'RawPacket({}, ts={}, data={})'.format(direction, packet.timestamp, data)
	return parsed

if __name__ == '__main__':
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
	
	print('[*] Selected protocol: {}. Initializing..'.format(GKFlasher_config['protocol']))
	try:
		device, bus = initialize_bus(GKFlasher_config['protocol'], GKFlasher_config[GKFlasher_config['protocol']])
	except GkbusException as e:
		print('[!] Failed to open the interface: {}'.format(e))
		sys.exit(1)

	try:
		main(bus, args)
	except KeyboardInterrupt:
		pass
	except Exception:
		print('\n\n[!] Exception in main thread!')
		print(traceback.format_exc())
		print('[*] Dumping buffer:\n')
		print('\n'.join([packet2hex(packet) for packet in bus.transport.buffer_dump()]))
		print('\n[!] Shutting down due to an exception in the main thread. For exception details, see above')
	bus.close()
	device.close()
	os._exit(0)
