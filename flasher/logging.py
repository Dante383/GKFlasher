import csv, time
from datetime import datetime
from gkbus.protocol.kwp2000.commands import *
from gkbus.protocol.kwp2000.enums import *
from .ecu import ECU

# this is not the way to do it, @TODO load data dynamically from GDS definitions
# definitions below are fine-tuned for ca663056
data_sources = [
	{
		'payload': ReadDataByLocalIdentifier(0x01),
		'parameters': [
			{
				'name': 'Oxygen Sensor-Bank1/Sensor1',
				'unit': 'mV',
				'position': 38,
				'size': 2,
				'conversion': lambda a: a * 4.883,
				'precision': 2,
			},
			{
				'name': 'Air Flow Rate from Mass Air Flow Sensor',
				'unit': 'kg/h',
				'position': 15,
				'size': 2,
				'conversion': lambda a: a * 0.03125,
				'precision': 2
			},
			{
				'name': 'Engine Coolant Temperature Sensor',
				'unit': 'C',
				'position': 4,
				'size': 1,
				'conversion': lambda a: 0.75*a-48,
				'precision': 2 
			},
			{
				'name': 'Oil Temperature Sensor',
				'unit': 'C',
				'position': 6,
				'size': 1,
				'conversion': lambda a: a-40,
				'precision': 2
			},
			{
				'name': 'Intake Air Temperature Sensor',
				'unit': 'C',
				'position': 9,
				'size': 1,
				'conversion': lambda a: (a*0.75)-48,
				'precision': 2
			},
			{
				'name': 'Throttle Position',
				'position': 11,
				'size': 1,
				'unit': "'",
				'conversion': lambda a: a * 0.468627,
				'precision': 2
			},
			{
				'name': 'Adapted Throttle Position',
				'position': 12,
				'size': 2,
				'unit': "'",
				'conversion': lambda a: a * 0.001825,
				'precision': 2
			},
			{
				'name': 'Battery voltage',
				'position': 1,
				'size': 1,
				'unit': 'V',
				'conversion': lambda a: a * 0.10159,
				'precision': 2
			},
			{
				'name': 'Cranking Signal',
				'position': 14,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x2) >> 1),
				'precision': 1
			},
			{
				'name': 'Closed Throttle Position',
				'position': 14,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x4) >> 1),
				'precision': 1
			},
			{
				'name': 'Part Load Status',
				'position': 14,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x8) >> 1),
				'precision': 1
			},
			{
				'name': 'Vehicle Speed',
				'position': 30,
				'size': 1,
				'unit': 'km/h',
				'conversion': lambda a : a,
				'precision': 1
			},
			{
				'name': 'Engine Speed',
				'position': 31,
				'size': 2,
				'unit': 'RPM',
				'conversion': lambda a: a,
				'precision': 1
			},
			{
				'name': 'Target Idle Speed',
				'position': 33,
				'size': 2,
				'unit': 'RPM',
				'conversion': lambda a: a,
				'precision': 1
			},
			{
				'name': 'Transaxle Range Switch',
				'position': 36,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x1) >> 1),
				'precision': 1
			},
			{
				'name': 'A/C Switch',
				'position': 37,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x1) >> 1),
				'precision': 1
			},
			{
				'name': 'A/C Pressure Switch',
				'position': 37,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x2) >> 1),
				'precision': 1
			},
			{
				'name': 'A/C Relay',
				'position': 37,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x4) >> 1),
				'precision': 1
			},
			{
				'name': 'Oxygen Sensor-Bank1/Sensor2',
				'position': 40,
				'size': 2,
				'unit': 'mV',
				'conversion': lambda a: a * 4.883,
				'precision': 2
			},
			{
				'name': 'Cylinder Injection Time-Bank1',
				'position': 76,
				'size': 2,
				'unit': 'ms',
				'conversion': lambda a: a * 0.004,
				'precision': 2
			},
			{
				'name': 'Fuel System Status',
				'position': 84,
				'size': 1,
				'unit': '',
				'conversion': lambda a: ((a&0x1) >> 1),
				'precision': 1
			},
			{
				'name': 'Long Term Fuel Trim-Idle Load',
				'position': 89,
				'size': 2,
				'unit': 'ms',
				'conversion': lambda a: a * 0.004,
				'precision': 2
			},
			{
				'name': 'Long Term Fuel Trim-Part Load',
				'position': 91,
				'size': 2,
				'unit': '%',
				'conversion': lambda a: a * 0.001529,
				'precision': 2
			},
			{
				'name': 'Oxygen Sensor Heater Duty-Bank1/Sensor1',
				'position': 93,
				'size': 1,
				'unit': '%',
				'conversion': lambda a: a*0.390625,
				'precision': 2
			},
			{
				'name': 'Oxygen Sensor Heater Duty-Bank1/Sensor2',
				'position': 94,
				'size': 1,
				'unit': '%',
				'conversion': lambda a: a*0.390625, 
				'precision': 2
			},
			{
				'name': 'Idle speed control actuator',
				'position': 99,
				'size': 2,
				'unit': '%',
				'conversion': lambda a: a*0.001529,
				'precision': 2
			},
			{
				'name': 'EVAP Purge valve',
				'position': 101,
				'size': 2,
				'unit': '%',
				'conversion': lambda a: a*0.003052,
				'precision': 2
			},
			{
				'name': 'Ignition dwell time',
				'position': 106,
				'size': 2,
				'unit': 'ms',
				'conversion': lambda a: a*0.004,
				'precision': 2
			},
			{
				'name': 'Camshaft Actual Position',
				'position': 142,
				'size': 1,
				'unit': "'",
				'conversion': lambda a: 0.375*a+60,
				'precision': 2
			},
			{
				'name': 'Camshaft position target',
				'position': 143,
				'size': 1,
				'unit': "'",
				'conversion': lambda a: 0.375*a+60,
				'precision': 2
			},
			{
				'name': 'CVVT Status',
				'position': 145,
				'size': 1,
				'unit': '',
				'precision': 1,
				'conversion': lambda a: 142*((a&0x7) >> 1)
			},
			{
				'name': 'CVVT Actuation Status',
				'position': 146,
				'size': 1,
				'unit': '',
				'precision': 1,
				'conversion': lambda a: 143*((a&0x3) >> 1)
			},
			{
				'name': 'CVVT Duty Control Status',
				'position': 160,
				'size': 1,
				'unit': '',
				'precision': 1,
				'conversion': lambda a: 148*((a&0x3) >> 1)
			},
			{
				'name': 'CVVT Valve Duty',
				'position': 156,
				'size': 2,
				'unit': '%',
				'conversion': lambda a: a * 0.001526,
				'precision': 2
			},
			# parameter below is not present in 2006 gds defs, but seems correct
			{
				'name': 'Ignition Timing Advance for 1 Cylinder',
				'position': 58,
				'size': 1,
				'unit': "'",
				'conversion': lambda a: (a*-0.325)-72,
				'precision': 2
			}
		]
	},
#	{
#		'payload': ReadDataByLocalIdentifier(0x02),
#		'parameters': [
#			{
#				'name': 'Cylinder 1 Injection Time',
#				'position': 45,
#				'size': 2,
#				'unit': 'ms',
#				'precision': 2,
#				'conversion': lambda a: a * 0.8192
#			},
#			{
#				'name': 'Cylinder 2 Injection Time',
#				'position': 47,
#				'size': 2,
#				'unit': 'ms',
#				'precision': 2,
#				'conversion': lambda a: a * 0.8192
#			},
#			{
#				'name': 'Cylinder 3 Injection Time',
#				'position': 49,
#				'size': 2,
#				'unit': 'ms',
#				'precision': 2,
#				'conversion': lambda a: a * 0.8192
#			},
#			{
#				'name': 'Cylinder 4 Injection Time',
#				'position': 51,
#				'size': 2,
#				'unit': 'ms',
#				'precision': 2,
#				'conversion': lambda a: a * 0.8192
#			}
#		]
#	}
]

def grab (payload: bytes, parameter: list) -> bytes:
	return payload[parameter['position']:parameter['position']+parameter['size']]

def convert (value: bytes, parameter: list):
	return round(
		parameter['conversion'](int.from_bytes(value, 'little')), 
		parameter['precision']
	)

def poll (ecu: ECU) -> list[int]:
	data = []
	for source in data_sources:
		raw_data = ecu.bus.execute(source['payload']).get_data()
		for parameter in source['parameters']:
			value = grab(raw_data, parameter)
			value_converted = convert(value, parameter)
			data.append(value_converted)
			print('{}: {}{} ({})'.format(parameter['name'], value_converted, parameter['unit'], hex(int.from_bytes(value, 'little'))))
	return data

def logger(ecu: ECU, desired_baudrate: int) -> None:
	ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, desired_baudrate))

	print('[*] Building parameter header')
	data = [['Unix timestamp']]
	for source in data_sources:
		for parameter in source['parameters']:
			data[0].append('{} ({})'.format(parameter['name'], parameter['unit']))

	print('[*] Logging..')
	
	try:
		while True:
			data.append([int(time.time()*1000)] + poll(ecu))
	except (KeyboardInterrupt, AttributeError):
		with open('log.csv', 'w') as csvfile:
			logwriter = csv.writer(csvfile)
			for entry in data:
				logwriter.writerow(entry)

# log only raw bytes for XDL conversion
# inefficient, ugly and the file format makes no sense
# for development purposes only

def poll_raw (ecu: ECU) -> bytes:
	data = []
	for source in data_sources:
		raw_data = ecu.bus.execute(source['payload']).get_data()
		data.append(raw_data)
	return data

def logger_raw (ecu: ECU, desired_baudrate: int) -> None:
	ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT, desired_baudrate))

	output_filename = 'log_raw_{}.csv'.format(datetime.now().strftime('%Y-%m-%d_%H%M'))

	print('[*] Logging to {}..\n'.format(output_filename))

	with open(output_filename, 'w') as csvfile:
		logwriter = csv.writer(csvfile)
		try:
			i = 0
			frames = []
			while True:
				data = poll_raw(ecu)[0]
				data_hex = ' '.join([hex(x) for x in list(data)])
				frames.append([int(time.time()*1000), data_hex])
				i += 1

				if i % 10 == 0:
					for frame in frames:
						logwriter.writerow(frame)
					frames = []
					print('\033[Fframes: {}'.format(i))
		except KeyboardInterrupt: # up to 100 last frames might be lost here
			pass