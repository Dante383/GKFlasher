import argparse, time
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

GKFlasher_config = {
	'protocol': 'canbus',
	'canbus': {
		'interface': 'can0',
		'tx_id': 0x7e0,
		'rx_id': 0x7e8
	},
	'kline': {
		'interface': '/dev/ttyUSB0',
		'baudrate': 10400
	}
}

def read_vin(bus):
	vin_hex = bus.execute(ReadEcuIdentification(0x90))[2:]
	return ''.join([chr(x) for x in vin_hex])

def read_voltage(bus):
	print('[*] Trying to read voltage (0x42/SAE_VPWR).. ', end='')

	voltage_hex = bus.execute(ReadStatusOfDTC(0x42))
	
	voltage = hex(voltage_hex[0]) + hex(voltage_hex[1])[2:]
	voltage = int(voltage, 16)/1000
	print('{}V'.format(voltage))

def read_eeprom (bus, eeprom_size):
	address_start = 0x000000
	address_stop = 0x0fffff

	print('[*] Reading from {} to {}'.format(hex(address_start), hex(address_stop)))

	requested_size = address_stop-address_start
	print('[*] Requested area\'s size: {} bytes. initializing a table filled with {} 0xFF\'s.'.format(requested_size, eeprom_size))
	eeprom = [0xFF]*eeprom_size

	with alive_bar(requested_size+1, unit='B') as bar:
		fetched = read_memory(bus, address_start=address_start, address_stop=address_stop, progress_callback=bar)

	eeprom_end = address_start + len(fetched)
	eeprom[address_start:eeprom_end] = fetched

	print('[*] received {} bytes of data'.format(len(fetched)))

	filename = "output_{}_to_{}.bin".format(hex(address_start), hex(address_stop))
	
	with open(filename, "wb") as file:
		file.write(bytes(eeprom))

	print('[*] saved to {}'.format(filename))

def flash_eeprom (bus, filename):
	print('\n[*] Loading up {}'.format(filename))

	with open(filename, 'rb') as file:
		eeprom = file.read()

	print('[*] Loaded {} bytes'.format(len(eeprom)))
	calibration = eeprom[0x090040:0x090048]
	calibration = ''.join([chr(x) for x in calibration])
	print('[*] {} calibration version: {}'.format(filename, calibration))

	print('[*] Ready to flash! Do you wish to continue? [y/n]:', end='')

	if (input() != 'y'):
		print('[!] Aborting!')
		return

def main():
	parser = argparse.ArgumentParser(prog='GKFlasher')
	parser.add_argument('-f', '--flash', help='Filename to flash')
	parser.add_argument('-r', '--read', action='store_true')
	parser.add_argument('-p', '--protocol', help='Protocol to use. canbus or kline')
	parser.add_argument('-i', '--interface')
	parser.add_argument('-b', '--baudrate')
	args = parser.parse_args()
	if (args.protocol):
		GKFlasher_config['protocol'] = args.protocol
	if (args.interface):
		GKFlasher_config[GKFlasher_config['protocol']]['interface'] = args.interface
	if (args.baudrate):
		GKFlasher_config[GKFlasher_config['protocol']]['baudrate'] = args.baudrate

	print('[*] Selected protocol: {}. Initializing..'.format(GKFlasher_config['protocol']))
	if (GKFlasher_config['protocol'] == 'canbus'):
		bus = CanInterface(iface=GKFlasher_config['canbus']['interface'], rx_id=GKFlasher_config['canbus']['rx_id'], tx_id=GKFlasher_config['canbus']['tx_id'])
	elif (GKFlasher_config['protocol'] == 'kline'):
		bus = KLineInterface(iface=GKFlasher_config['kline']['interface'], baudrate=GKFlasher_config['kline']['baudrate'])

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
	eeprom_size, eeprom_size_human, calibration = find_eeprom_size_and_calibration(bus)
	print('[*] Found! EEPROM is {}mbit, calibration: {}'.format(eeprom_size_human, calibration))

	if (args.read):
		read_eeprom(bus, eeprom_size)

	if (args.flash):
		flash_eeprom(bus, args.flash)

if __name__ == '__main__':
	main()