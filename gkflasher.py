import argparse, time, yaml
from alive_progress import alive_bar
from kwp.KWPCommand import KWPCommand
from kwp.commands.ReadStatusOfDTC import ReadStatusOfDTC
from kwp.commands.ReadEcuIdentification import ReadEcuIdentification
from kwp.commands.WriteMemoryByAddress import WriteMemoryByAddress
from kwp.commands.StartCommunication import StartCommunication
from kwp.commands.StartDiagnosticSession import StartDiagnosticSession
from kwp.commands.StopDiagnosticSession import StopDiagnosticSession 
from kwp.commands.StopCommunication import StopCommunication
from memory import find_eeprom_size_and_calibration, read_memory
from interface.CanInterface import CanInterface
from interface.KLineInterface import KLineInterface

def read_vin(bus):
	vin_hex = bus.execute(ReadEcuIdentification(0x90))[2:]
	return ''.join([chr(x) for x in vin_hex])

def read_voltage(bus):
	print('[*] Trying to read voltage (0x42/SAE_VPWR).. ', end='')

	voltage_hex = bus.execute(ReadStatusOfDTC(0x42))
	
	voltage = hex(voltage_hex[0]) + hex(voltage_hex[1])[2:]
	voltage = int(voltage, 16)/1000
	print('{}V'.format(voltage))

def read_eeprom (bus, eeprom_size, address_start=0x000000, address_stop=None, output_filename=None):
	if (address_stop == None):
		address_stop = eeprom_size

	print('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

	requested_size = address_stop-address_start
	print('[*] Requested area\'s size: {} bytes. initializing a table filled with {} 0xFF\'s.'.format(requested_size, eeprom_size))
	eeprom = [0xFF]*eeprom_size

	with alive_bar(requested_size+1, unit='B') as bar:
		fetched = read_memory(bus, address_start=address_start, address_stop=address_stop, progress_callback=bar)

	eeprom_end = address_start + len(fetched)
	eeprom[address_start:eeprom_end] = fetched

	print('[*] received {} bytes of data'.format(len(fetched)))

	if (output_filename == None):
		output_filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))
	
	with open(output_filename, "wb") as file:
		file.write(bytes(eeprom))

	print('[*] saved to {}'.format(output_filename))

def flash_eeprom (bus, input_filename):
	print('\n[*] Loading up {}'.format(input_filename))

	with open(input_filename, 'rb') as file:
		eeprom = file.read()

	print('[*] Loaded {} bytes'.format(len(eeprom)))
	calibration = eeprom[0x090040:0x090048]
	calibration = ''.join([chr(x) for x in calibration])
	print('[*] {} calibration version: {}'.format(input_filename, calibration))

	if (input('[?] Ready to flash! Do you wish to continue? [y/n]: ') != 'y'):
		print('[!] Aborting!')
		return

def load_config (config_filename):
	return yaml.safe_load(open('gkflasher.yml'))

def load_arguments ():
	parser = argparse.ArgumentParser(prog='GKFlasher')
	parser.add_argument('-p', '--protocol', help='Protocol to use. canbus or kline')
	parser.add_argument('-i', '--interface')
	parser.add_argument('-b', '--baudrate')
	parser.add_argument('-f', '--flash', help='Filename to flash')
	parser.add_argument('-r', '--read', action='store_true')
	parser.add_argument('-o', '--output', help='Filename to save the EEPROM dump')
	parser.add_argument('-s', '--address_start', help='Offset to start reading/flashing from.', type=lambda x: int(x,0), default=0x000000)
	parser.add_argument('-e', '--address_stop', help='Offset to stop reading/flashing at.', type=lambda x: int(x,0))
	parser.add_argument('--eeprom-size', help='EEPROM size in bytes. ONLY USE THIS IF YOU REALLY, REALLY KNOW WHAT YOU\'RE DOING!!', type=int)
	parser.add_argument('-c', '--config', help='Config filename', default='gkflasher.yml')
	args = parser.parse_args()

	GKFlasher_config = load_config(args.config)
	
	if (args.protocol):
		GKFlasher_config['protocol'] = args.protocol
	if (args.interface):
		GKFlasher_config[GKFlasher_config['protocol']]['interface'] = args.interface
	if (args.baudrate):
		GKFlasher_config[GKFlasher_config['protocol']]['baudrate'] = args.baudrate

	return GKFlasher_config, args

def initialize_bus (protocol, protocol_config):
	if (protocol == 'canbus'):
		return CanInterface(iface=protocol_config['interface'], rx_id=protocol_config['rx_id'], tx_id=protocol_config['tx_id'])
	elif (protocol == 'kline'):
		return KLineInterface(iface=protocol_config['interface'], baudrate=protocol_config['baudrate'])
	raise Exception('Protocol %s unsupported' % protocol)

def main():
	GKFlasher_config, args = load_arguments()

	print('[*] Selected protocol: {}. Initializing..'.format(GKFlasher_config['protocol']))
	bus = initialize_bus(GKFlasher_config['protocol'], GKFlasher_config[GKFlasher_config['protocol']])	

	bus.execute(StopDiagnosticSession())
	bus.execute(StopCommunication())
	bus.execute(StartCommunication())
	bus.execute(StartDiagnosticSession())

	#read_voltage(bus)

	print('[*] Trying to read VIN... ', end='')
	print(read_vin(bus))
	
	#print('[*] trying to write "GK" in first 2 bytes of calibration section')
	#WriteMemoryByAddress(offset=0x90040, data_to_write=[0x67, 0x6B]).execute(bus)

	print('[*] Trying to find eeprom size and calibration..')
	if (args.eeprom_size):
		print('[!] EEPROM size was selected by the user as {} bytes!'.format(args.eeprom_size))
		if (input('[?] Are you absolutely sure you know what you\'re doing? This could potentially result in bricking your ECU [y/n]: ') != 'y'):
			print('[!] Aborting.')
			return False
		eeprom_size = args.eeprom_size
	else:
		eeprom_size, eeprom_size_human, calibration = find_eeprom_size_and_calibration(bus)
		print('[*] Found! EEPROM is {}mbit, calibration: {}'.format(eeprom_size_human, calibration))

	if (args.read):
		read_eeprom(bus, eeprom_size, address_start=args.address_start, address_stop=args.address_stop, output_filename=args.output)

	if (args.flash):
		flash_eeprom(bus, input_filename=args.flash)

if __name__ == '__main__':
	main()