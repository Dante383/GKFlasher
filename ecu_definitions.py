from enum import Enum 

BAUDRATES = {
	0x01: 10400,
	0x02: 20000,
	0x03: 40000,
	0x04: 60000,
	0x05: 120000
}

class Routine (Enum):
	ERASE_PROGRAM = 0x00
	ERASE_CALIBRATION = 0x01
	VERIFY_BLOCKS = 0x02

	QUERY_IMMO_INFO = 0x12
	BEFORE_LIMP_HOME = 0x16 # what does this actually do?
	ACTIVATE_LIMP_HOME = 0x18 # user 4 pin code password as parameters
	BEFORE_LIMP_HOME_TEACHING = 0x13
	LIMP_HOME_INPUT_NEW_PASSWORD = 0x17
	LIMP_HOME_CONFIRM_NEW_PASSWORD = 0x19

	BEFORE_IMMO_RESET = 0x15 # what does this actually do?
	IMMO_INPUT_PASSWORD = 0x1A # 6 digit pin code as parameter
	IMMO_RESET_CONFIRM = 0x20

	BEFORE_IMMO_KEY_TEACHING = 0x14 # what does this actually do?
	# these enums below are not actually used, just serve as documentation
	IMMO_TEACH_KEY_1 = 0x1B
	IMMO_TEACH_KEY_2 = 0x1C
	IMMO_TEACH_KEY_3 = 0x1D
	IMMO_TEACH_KEY_4 = 0x1E

	BEFORE_SMARTRA_NEUTRALIZE = 0x25
	SMARTRA_NEUTRALIZE = 0x26

class IOIdentifier (Enum):
	CHECK_ENGINE_LIGHT = 0x10
	COOLING_FAN_RELAY_HIGH = 0x1A
	COOLING_FAN_RELAY_LOW = 0x1B
	IDLE_SPEED_ACTUATOR = 0x23
	CVVT_VALVE = 0x24
	ADAPTIVE_VALUES = 0x50
