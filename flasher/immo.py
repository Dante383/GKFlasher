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

immo_menus = [
	['Information', cli_immo_info]
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