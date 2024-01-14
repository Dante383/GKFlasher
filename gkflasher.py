import argparse, time, yaml, logging, sys
from datetime import date
from alive_progress import alive_bar
import gkbus
from gkbus import kwp
from flasher.memory import read_memory, write_memory
from flasher.ecu import ECU, identify_ecu, fetch_ecu_identification, enable_security_access, ECUIdentificationException
from flasher.checksum import correct_checksum
from ecu_definitions import ECU_IDENTIFICATION_TABLE, BAUDRATES, Routine
from flasher.logging import logger
from flasher.checksum import correct_checksum

def cli_read_eeprom (ecu, eeprom_size, address_start=None, address_stop=None, output_filename=None):
	if (address_start == None):
		address_start = abs(ecu.bin_offset)
	if (address_stop == None):
		address_stop = address_start+eeprom_size

	print('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

	requested_size = address_stop-address_start
	eeprom = [0xFF]*eeprom_size

	with alive_bar(requested_size+1, unit='B') as bar:
		fetched = read_memory(ecu, address_start=address_start, address_stop=address_stop, progress_callback=bar)

	eeprom_start = ecu.calculate_bin_offset(address_start)
	eeprom_end = eeprom_start + len(fetched)
	eeprom[eeprom_start:eeprom_end] = fetched

	if (output_filename == None):
		try:
			calibration = ecu.get_calibration()
			description = ecu.get_calibration_description()
			hw_rev_c = ''.join([chr(x) for x in ecu.bus.execute(kwp.commands.ReadEcuIdentification(0x8c)).get_data()[1:]])
			hw_rev_d = ''.join([chr(x) for x in ecu.bus.execute(kwp.commands.ReadEcuIdentification(0x8d)).get_data()[1:]])
			output_filename = "{}_{}_{}_{}_{}.bin".format(description, calibration, hw_rev_c, hw_rev_d, date.today())
		except: # dirty
			output_filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))
	
	with open(output_filename, "wb") as file:
		file.write(bytes(eeprom))

	print('[*] saved to {}'.format(output_filename))

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
		ecu.bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.ERASE_PROGRAM.value))

		flash_start = ecu.get_program_section_offset()
		flash_size = ecu.get_program_section_size()
		payload_start = ecu.get_program_section_flash_offset()
		payload_stop = payload_start + flash_size
		payload = eeprom[payload_start:payload_stop]

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	if flash_calibration:
		print('[*] start routine 0x01 (erase calibration section)')
		ecu.bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.ERASE_CALIBRATION.value))

		flash_start = ecu.calculate_memory_write_offset(0x090000)
		flash_size = ecu.get_calibration_size_bytes()
		payload_start = ecu.calculate_bin_offset(0x090000)
		payload_stop = payload_start + flash_size
		payload = eeprom[payload_start:payload_stop]

		with alive_bar(flash_size, unit='B') as bar:
			write_memory(ecu, payload, flash_start, flash_size, progress_callback=bar)

	ecu.bus.set_timeout(300)
	print('[*] start routine 0x02 (verify blocks and mark as ready to execute)')
	ecu.bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.VERIFY_BLOCKS.value)).get_data()
	ecu.bus.set_timeout(12)

	print('[*] ecu reset')
	print('[*] done!')
	ecu.bus.execute(kwp.commands.ECUReset(kwp.enums.ResetMode.POWER_ON_RESET)).get_data()
	return

def cli_clear_adaptive_values (ecu):
	print('[*] Clearing adaptive values.. ', end='')
	ecu.clear_adaptive_values()
	print('Done!')

def load_config (config_filename):
	return yaml.safe_load(open('gkflasher.yml'))

def load_arguments ():
	parser = argparse.ArgumentParser(prog='GKFlasher')
	parser.add_argument('-p', '--protocol', help='Protocol to use. canbus or kline')
	parser.add_argument('-i', '--interface')
	parser.add_argument('-b', '--baudrate', type=int)
	parser.add_argument('--desired-baudrate', type=lambda x: int(x,0))
	parser.add_argument('-f', '--flash', help='Filename to full flash')
	parser.add_argument('--flash-calibration', help='Filename to flash calibration zone from')
	parser.add_argument('--flash-program', help='Filename to flash program zone from')
	parser.add_argument('-r', '--read', action='store_true')
	parser.add_argument('--read-calibration', action='store_true')
	parser.add_argument('--read-program', action='store_true')
	parser.add_argument('--id', action='store_true')
	parser.add_argument('--correct-checksum')
	parser.add_argument('--clear-adaptive-values', action='store_true')
	parser.add_argument('-l', '--logger', action='store_true')
	parser.add_argument('-o', '--output', help='Filename to save the EEPROM dump')
	parser.add_argument('-s', '--address-start', help='Offset to start reading/flashing from.', type=lambda x: int(x,0))
	parser.add_argument('-e', '--address-stop', help='Offset to stop reading/flashing at.', type=lambda x: int(x,0))
	parser.add_argument('-c', '--config', help='Config filename', default='gkflasher.yml')
	parser.add_argument('-v', '--verbose', action='count', default=0)
	args = parser.parse_args()

	logging_levels = [logging.WARNING, logging.INFO, logging.DEBUG]
	logging.basicConfig(level=logging_levels[min(args.verbose, len(logging_levels) -1)])

	GKFlasher_config = load_config(args.config)
	
	if (args.protocol):
		GKFlasher_config['protocol'] = args.protocol
	if (args.interface):
		GKFlasher_config[GKFlasher_config['protocol']]['interface'] = args.interface
	if (args.baudrate):
		GKFlasher_config[GKFlasher_config['protocol']]['baudrate'] = args.baudrate

	return GKFlasher_config, args

def initialize_bus (protocol, protocol_config):
	interface = protocol_config['interface']
	del protocol_config['interface']

	return gkbus.Bus(protocol, interface=interface, **protocol_config)

def cli_choose_ecu ():
	print('[!] Failed to identify your ECU!')
	print('[*] If you know what you\'re doing (like trying to revive a soft bricked ECU), you can choose your ECU from the list below:')

	for index, ecu in enumerate(ECU_IDENTIFICATION_TABLE):
		print('    [{}] {}'.format(index, ecu['ecu']['name']))

	try:
		choice = int(input('ECU or any other char to abort: '))
	except ValueError:
		print('[!] Aborting..')
		sys.exit(1)

	try:
		ECU_IDENTIFICATION_TABLE[choice]
	except IndexError:
		print('[!] Invalid value!')
		return cli_choose_ecu()

	return ECU_IDENTIFICATION_TABLE[choice]

def cli_identify_ecu (bus):
	print('[*] Trying to identify ECU automatically.. ')
	
	try:
		ecu = identify_ecu(bus)
	except ECUIdentificationException:
		ecu = ECU(**cli_choose_ecu()['ecu'])
		ecu.set_bus(bus)

	print('[*] Found! {}'.format(ecu.get_name()))
	return ecu

def main():
	GKFlasher_config, args = load_arguments()

	if (args.correct_checksum):
		correct_checksum(filename=args.correct_checksum)
		return 

	print('[*] Selected protocol: {}. Initializing..'.format(GKFlasher_config['protocol']))
	bus = initialize_bus(GKFlasher_config['protocol'], GKFlasher_config[GKFlasher_config['protocol']])	

	try:
		bus.execute(kwp.commands.StopDiagnosticSession())
		bus.execute(kwp.commands.StopCommunication())
	except (kwp.KWPNegativeResponseException, gkbus.GKBusTimeoutException):
		pass

	bus.init(kwp.commands.StartCommunication())

	if args.desired_baudrate:
		try:
			desired_baudrate = BAUDRATES[args.desired_baudrate]
		except KeyError:
			print('[!] Selected baudrate is invalid! Available baudrates:')
			for key, baudrate in BAUDRATES.items():
				print('{} - {}'.format(hex(key), baudrate))
			sys.exit(1)

		print('[*] Trying to start diagnostic session with baudrate {}'.format(BAUDRATES[args.desired_baudrate]))
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.FLASH_REPROGRAMMING, args.desired_baudrate))
		bus.socket.socket.baudrate = BAUDRATES[args.desired_baudrate]
	else:
		print('[*] Trying to start diagnostic session')
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.FLASH_REPROGRAMMING))
	bus.set_timeout(12)

	print('[*] Set timing parameters to maximum')
	try:
		available_timing = bus.execute(
			kwp.commands.AccessTimingParameters(
				kwp.enums.TimingParameterIdentifier.READ_LIMITS_OF_POSSIBLE_TIMING_PARAMETERS
			)
		).get_data()

		bus.execute(
			kwp.commands.AccessTimingParameters(
				kwp.enums.TimingParameterIdentifier.SET_TIMING_PARAMETERS_TO_GIVEN_VALUES, 
				*available_timing[1:]
			)
		)
	except kwp.KWPNegativeResponseException:
		print('[!] Not supported on this ECU!')

	print('[*] Security Access')
	enable_security_access(bus)

	ecu = cli_identify_ecu(bus)

	print('[*] Trying to find calibration..')
	
	eeprom_size = ecu.get_eeprom_size_bytes()
	try:
		description, calibration = ecu.get_calibration_description(), ecu.get_calibration()
		print('[*] Found! Description: {}, calibration: {}'.format(description, calibration))
	except kwp.KWPNegativeResponseException:
		if (input('[!] Failed! Do you want to continue? [y/n]: ') != 'y'):
			sys.exit(1)

	if (args.id):
		print('[*] Reading ECU Identification..',end='')
		for parameter_key, parameter in fetch_ecu_identification(bus).items():
			value_hex = ' '.join([hex(x) for x in parameter['value']])
			value_ascii = ''.join([chr(x) for x in parameter['value']])

			print('')
			print('    [*] [{}] {}:'.format(hex(parameter_key), parameter['name']))
			print('            [HEX]: {}'.format(value_hex))
			print('            [ASCII]: {}'.format(value_ascii))

	if (args.read):
		cli_read_eeprom(ecu, eeprom_size, address_start=args.address_start, address_stop=args.address_stop, output_filename=args.output)
	if (args.read_calibration):
		cli_read_eeprom(ecu, eeprom_size, address_start=0x090000, address_stop=0x090000+ecu.get_calibration_size_bytes(), output_filename=args.output)
	if (args.read_program):
		address_start = ecu.get_program_section_offset()
		address_stop = address_start+ecu.get_program_section_size()
		cli_read_eeprom(ecu, eeprom_size, address_start=address_start, address_stop=address_stop, output_filename=args.output)

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

	try:
		bus.execute(kwp.commands.StopCommunication())
	except (kwp.KWPNegativeResponseException, gkbus.GKBusTimeoutException):
		pass
if __name__ == '__main__':
	main()