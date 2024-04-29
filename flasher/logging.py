from gkbus.kwp.commands import *
from gkbus.kwp.enums import *
import csv, time

# this is not the way to do it, @TODO load data dynamically from GDS definitions
data_id1 = [
	{
		'name': 'Oxygen Sensor-Bank1/Sensor1',
		'unit': 'mV',
		'position': 38,
		'size': 2,
		'conversion': lambda a: a * 4.883
	},
	{
		'name': 'Air Flow Rate from Mass Air Flow Sensor',
		'unit': 'kg/h',
		'position': 15,
		'size': 2,
		'conversion': lambda a: a * 0.03125
	},
	{
		'name': 'Engine Coolant Temperature Sensor',
		'unit': 'C',
		'position': 4,
		'size': 1,
		'conversion': lambda a: a * 0.75 
	},
	{
		'name': 'Oil Temperature Sensor',
		'unit': 'C',
		'position': 6,
		'size': 1,
		'conversion': lambda a: (a * 1)-40
	},
	{
		'name': 'Battery voltage',
		'position': 1,
		'size': 1,
		'unit': 'V',
		'conversion': lambda a: a * 0.10159
	},
	{
		'name': 'Intake Air Temperature Sensor',
		'position': 9,
		'size': 1,
		'unit': 'C',
		'conversion': lambda a: (a*0.75)-48
	},
	{
		'name': 'Throttle Position',
		'position': 11,
		'size': 1,
		'unit': "'",
		'conversion': lambda a: a * 0.468627
	},
	{
		'name': 'Vehicle Speed',
		'position': 30,
		'size': 1,
		'unit': 'km/h',
		'conversion': lambda a : a
	},
	{
		'name': 'Engine Speed',
		'position': 31,
		'size': 2,
		'unit': 'RPM',
		'conversion': lambda a: a
	},
	{
		'name': 'Oxygen Sensor-Bank1/Sensor2',
		'position': 40,
		'size': 2,
		'unit': 'mV',
		'conversion': lambda a: a * 4.883
	},
	{
		'name': 'Ignition Timing Advance for 1 Cylinder',
		'position': 58,
		'size': 1,
		'unit': "'",
		'conversion': lambda a: (a*-0.325)-72
	},
	{
		'name': 'Cylinder Injection Time-Bank1',
		'position': 76,
		'size': 2,
		'unit': 'ms',
		'conversion': lambda a: a * 0.004
	},
	{
		'name': 'Long Term Fuel Trim-Idle Load',
		'position': 89,
		'size': 2,
		'unit': 'ms',
		'conversion': lambda a: a * 0.004
	},
	{
		'name': 'Long Term Fuel Trim-Part Load',
		'position': 91,
		'size': 2,
		'unit': '%',
		'conversion': lambda a: a * 0.001529
	},
	{
		'name': 'Camshaft Actual Position',
		'position': 142,
		'size': 1,
		'unit': "'",
		'conversion': lambda a: (a*0.375)-60
	},
	{
		'name': 'CVVT Valve Duty',
		'position': 156,
		'size': 2,
		'unit': '%',
		'conversion': lambda a: a * 0.001526
	}
]

def grab (payload, parameter):
	return parameter['conversion'](int.from_bytes(payload[parameter['position']:parameter['position']+parameter['size']], "big"))

def poll (ecu):
	data = [time.time()]
	raw_data = bytes(ecu.bus.execute(ReadDataByLocalIdentifier(0x01)).get_data())
	for parameter in data_id1:
		value = grab(raw_data, parameter)
		data.append(value)
		print('{}: {} {}'.format(parameter['name'], value, parameter['unit']))
	return data

def logger(ecu):
	ecu.bus.execute(StartDiagnosticSession(DiagnosticSession.DEFAULT))

	print('[*] Building parameter header')
	data = [['{} ({})'.format(parameter['name'], parameter['unit']) for parameter in data_id1]]
	data[0].insert(0, 'Time (unix timestamp)')

	print('[*] Logging..')
	try:
		while True:
			data.append(poll(ecu))
	except KeyboardInterrupt:
		with open('log.csv', 'w') as csvfile:
			logwriter = csv.writer(csvfile)
			for entry in data:
				logwriter.writerow(entry)
