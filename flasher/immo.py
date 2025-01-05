from gkbus import kwp
from ecu_definitions import Routine
from flasher.ecu import enable_security_access
from flasher.smartra import calculate_smartra_pin

immo_status = {
	0: 'Not learnt',
	1: 'Learnt',
	2: 'Virgin',
	3: 'Neutral',
	4: 'Teaching not accepted (locked by wrong data)',
	5: 'Virgin status - no teaching',
	6: 'Invalid key'
}

def cli_immo_info (bus, desired_baudrate):
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))
	try:
		immo_data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.QUERY_IMMO_INFO.value)).get_data()
	except (kwp.KWPNegativeResponseException):
		print('[*] Immo seems to be disabled')
		return

	print('[*] Immo keys learnt: {}'.format(immo_data[1]))
	try:
		ecu_status = immo_status[immo_data[2]]
	except KeyError:
		ecu_status = immo_data[2]
	try:
		key_status = immo_status[immo_data[3]]
	except KeyError:
		key_status = immo_data[3]

	print('[*] Immo ECU status: {}'.format(ecu_status))
	print('[*] Immo key status: {}'.format(key_status))
	if (len(immo_data) > 4):
		print('[*] Smartra status: {}'.format(immo_status[immo_data[4]]))

def cli_limp_home (bus, desired_baudrate):
	print('[*] starting default diagnostic session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))
	try:
		data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.BEFORE_LIMP_HOME.value)).get_data()
	except (kwp.KWPNegativeResponseException):
		print('[!] Received an exception from the ECU! This usually means that immo is inactive or limp home pin wasn\'t set (common for Tiburon FL2)')
		return

	if (len(data) > 1):
		if (data[1] == 4):
			print('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return

	password = int('0x' + input('Enter 4 digit password (default: 2345): '), 0)
	
	password_a = (password >> 8)
	password_b = (password & 0xFF)

	print('[*] starting routine 0x18 with password as parameter')
	try:
		data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.ACTIVATE_LIMP_HOME.value, password_a, password_b)).get_data()
	except (kwp.KWPNegativeResponseException):
		print('[!] Invalid password! Try the default one: 2345')

	print(' '.join([hex(x) for x in data]))

	if (len(data) > 1):
		if (data[1] == 1):
			print('[*] limp home activated!')

def cli_immo_reset (bus, desired_baudrate):
	print('[*] starting default diagnostic session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))

	print('[*] starting routine 0x15')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.BEFORE_IMMO_RESET.value)).get_data()
	print(' '.join([hex(x) for x in data]))

	if (len(data) > 1):
		if (data[1] == 4):
			print('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return

	key = int('0x' + input('Enter 6 digit immo pin: '), 0)
	key_a = (key >> 16) & 0xFF
	key_b = (key >> 8) & 0xFF
	key_c = key & 0xFF


	print('[*] Starting routine 0x1A with key as parameter and some 0xFFs')
	print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.IMMO_INPUT_PASSWORD.value, key_a, key_b, key_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)).get_data())

	if (input('[?] Looks good! Continue? [y/n]: ') == 'y'):
		print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.IMMO_RESET_CONFIRM.value, 0x01)).get_data())

	print('[*] ECU reseted! Turn ignition off for 10 seconds for changes to take effect')

def cli_smartra_neutralize (bus, desired_baudrate):
	print('[*] starting default diagnostic session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))

	print('[*] starting routine 0x25')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.BEFORE_SMARTRA_NEUTRALIZE.value)).get_data()
	print(' '.join([hex(x) for x in data]))

	if (len(data) > 1):
		if (data[1] == 4):
			print('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return

	key = int('0x' + input('Enter 6 digit immo pin: '), 0)
	key_a = (key >> 16) & 0xFF
	key_b = (key >> 8) & 0xFF
	key_c = key & 0xFF


	print('[*] Starting routine 0x1A with key as parameter and some 0xFFs')
	print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.IMMO_INPUT_PASSWORD.value, key_a, key_b, key_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)).get_data())

	if (input('[?] Looks good! Continue? [y/n]: ') == 'y'):
		print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.SMARTRA_NEUTRALIZE.value, 0x01)).get_data())

	print('[*] SMARTRA neutralized! Turn ignition off for 5 seconds for changes to take effect.')

def cli_immo_teach_keys (bus, desired_baudrate):
	print('[*] starting default diagnostic session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))

	print('[*] starting routine 0x14')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.BEFORE_IMMO_KEY_TEACHING.value)).get_data()
	print(' '.join([hex(x) for x in data]))

	key = int('0x' + input('Enter 6 digit immo pin: '), 0)
	key_a = (key >> 16) & 0xFF
	key_b = (key >> 8) & 0xFF
	key_c = key & 0xFF


	print('[*] Starting routine 0x1A with key as parameter and some 0xFFs')
	print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.IMMO_INPUT_PASSWORD.value, key_a, key_b, key_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)).get_data())

	for x in range(4):
		if (input('[?] Teach immo key {}? [y/n]: '.format(x+1)) == 'y'):
			data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x1B+x, 0x01)).get_data()
		else:
			# bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x1B+x+1, 0x02)) # cascade did this, but it throws 0x10. seems to be not needed?
			break
	print('[*] Done! Turn off ignition for 10 seconds for changes to take effect')

def cli_read_vin (bus, desired_baudrate):
	cmd = kwp.KWPCommand()
	cmd.command = 0x09 # undocumented service
	cmd.data = [0x02]
	
	try:
		vin = bus.execute(cmd).get_data()
	except (kwp.KWPNegativeResponseException):
		print('[!] Not supported!')
		return
		
	print(' '.join([hex(x) for x in vin]))
	print(''.join([chr(x) for x in vin]))

def cli_write_vin (bus, desired_baudrate):
	print('[*] starting flash reprogramming session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.FLASH_REPROGRAMMING))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.FLASH_REPROGRAMMING, desired_baudrate))

	enable_security_access(bus)
	vin = input('Enter VIN. WARNING! No validation!: ')

	cmd = kwp.commands.WriteDataByLocalIdentifier(0x90, [ord(c) for c in vin])
	try:
		bus.execute(cmd)
	except kwp.KWPNegativeResponseException:
		print('[!] Not supported! Maybe the VIN is already written?')
		return
	print('[*] VIN changed! Turn ignition off for 5 seconds for changes to take effect.')

def cli_limp_home_teach (bus, desired_baudrate):
	print('[*] starting default diagnostic session')
	if desired_baudrate is None:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
	else:
		bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT, desired_baudrate))

	status = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.BEFORE_LIMP_HOME_TEACHING.value)).get_data()[1]
	print('[*] Current ECU status: {}'.format(immo_status[status]))

	if (status == 1): # learnt 
		password = int('0x' + input('[*] Enter current 4 digit password: '), 0)
		password_a = (password >> 8)
		password_b = (password & 0xFF)
		try:
			bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.ACTIVATE_LIMP_HOME.value, password_a, password_b))
		except (kwp.KWPNegativeResponseException):
			print('[!] Invalid password!')
			return 

	password = int('0x' + input('[*] Enter new 4 digit password: '), 0)
	password_a = (password >> 8)
	password_b = (password & 0xFF)

	print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.LIMP_HOME_INPUT_NEW_PASSWORD.value, password_a, password_b)).get_data())

	if (input('[?] Are you sure? [y/n]: ') == 'y'):
		print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(Routine.LIMP_HOME_CONFIRM_NEW_PASSWORD.value, 0x01)).get_data())

def get_last_6_digits (text_input: str) -> int:
	try:
		return int(text_input[-6:])
	except (ValueError, IndexError):
		raise ValueError

def cli_vin_to_pin (last_6_digits, calculated_pin):
	print('[*] SMARTRA VIN to PIN calculator')
	print('[*] This calculator should apply for all Hyundai and KIA models equipped with SMARTRA2')
	print('[*] From 2007 or so, some models started using SMARTRA3 and a different algorithm - beware.')

	while True:
		try:
			last_6_digits = get_last_6_digits(input('[*] Enter your VIN (or just the last 6 digits): '))
			calculated_pin = calculate_smartra_pin(last_6_digits)
			print('[*] All good! Your immo pin should be: {}'.format(calculated_pin))
			break
		except ValueError:
			print('[!] Something went wrong. Try again')
	
immo_menus = [
	['Information', cli_immo_info],
	['Limp home mode', cli_limp_home],
	['Immo reset', cli_immo_reset],
	['Smartra neutralize', cli_smartra_neutralize],
	['Teach keys', cli_immo_teach_keys],
	['Limp home password teaching/changing', cli_limp_home_teach],
	['Read VIN', cli_read_vin],
	['Write VIN', cli_write_vin],
	['Smartra VIN to PIN Calculator', cli_vin_to_pin]
]

def cli_immo (bus, desired_baudrate):
	for key, menu in enumerate(immo_menus):
		print('    [{}] {}'.format(key, menu[0]))
	menu = input('Select immo menu: ')
	try:
		handler = immo_menus[int(menu)][1]
	except (KeyError, IndexError, ValueError):
		print('[!] Invalid choice! Try again')
		return cli_immo(bus, desired_baudrate)
	handler(bus, desired_baudrate)
	bus.shutdown()
