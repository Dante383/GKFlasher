from gkbus import kwp
from ecu_definitions import Routine

immo_status = {
	0: 'Not learnt',
	1: 'Learnt',
	2: 'Virgin',
	3: 'Locked by timer',
	4: 'Teaching not accepted'
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

	password = int('0x' + input('Enter 4 digit password: '), 0)
	
	password_a = (password >> 8)
	password_b = (password & 0xFF)

	print('[*] starting routine 0x18 with password as parameter')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x18, password_a, password_b))
			
	print(data)

def cli_immo_reset (bus):
	print('[*] starting default diagnostic session')
	bus.execute(kwp.commands.StartDiagnosticSession(kwp.enums.DiagnosticSession.DEFAULT))

	print('[*] starting routine 0x15')
	data = bus.execute(kwp.commands.StartRoutineByLocalIdentifier(0x15)).get_data()
	print(' '.join([hex(x) for x in data]))

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
		immo_menus[int(menu)][1](bus)
	except (KeyError, IndexError, ValueError):
		print('[!] Invalid choice! Try again')
		cli_immo(bus)