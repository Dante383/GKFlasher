from gkbus import kwp
from ecu_definitions import Routine

immo_status = {
	0: 'Not learnt',
	1: 'Learnt',
	2: 'Virgin',
	3: 'Virgin',
	4: 'Teaching not accepted (locked by wrong data)'
}

def cli_immo_info (bus):
	bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))
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

def cli_limp_home (bus):
	print('[*] starting default diagnostic session')
	bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))

	print('[*] starting routine 0x16')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x16)).get_data()
	print(' '.join([hex(x) for x in data]))

	if (len(data) > 1):
		if (data[1] == 4):
			print('[!] System is locked by wrong data! It\'ll probably be locked for an hour.')
			return

	password = int('0x' + input('Enter 4 digit password: '), 0)
	
	password_a = (password >> 8)
	password_b = (password & 0xFF)

	print('[*] starting routine 0x18 with password as parameter')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x18, password_a, password_b)).get_data()
	print(' '.join([hex(x) for x in data]))

	if (len(data) > 1):
		if (data[1] == 1):
			print('[*] limp home activated!')

def cli_immo_reset (bus):
	print('[*] starting default diagnostic session')
	bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))

	print('[*] starting routine 0x15')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x15)).get_data()
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
	print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x1A, key_a, key_b, key_c, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)).get_data())

	if (input('[?] Looks good! Continue? [y/n]: ') == 'y'):
		print(bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x20, 0x01)).get_data())

	print('[*] ECU virginized!')

immo_menus = [
	['Information', cli_immo_info],
	['Limp home mode', cli_limp_home],
	['Immo reset', cli_immo_reset]
]

def cli_immo (bus):
	for key, menu in enumerate(immo_menus):
		print('    [{}] {}'.format(key, menu[0]))
	menu = input('Select immo menu: ')
	try:
		handler = immo_menus[int(menu)][1]
	except (KeyError, IndexError, ValueError):
		print('[!] Invalid choice! Try again')
		cli_immo(bus)
	handler(bus)