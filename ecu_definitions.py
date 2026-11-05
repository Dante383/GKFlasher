from enum import Enum 
import ctypes
from flasher.utils import WordBitfield

ECU_IDENTIFICATION_TABLE = [
	{
		'offset': 0x82014, # RSW zone
		'expected': [b'\x36\x36\x32\x31'], #6621
		'ecu': {
			'name': 'SIMK43 8mbit',
			'eeprom_size_bytes': 1048576, # (1024 KiB)
			'bin_offset': 0,
			'regions': {
				'boot1': {
					'read': {
						'address': 0x80000,
						'size': 0x4000
					},
					'write': {
						'address': 0x80000,
						'size': 0x4000
					}
				},
				'boot2': {
					'read': {
						'address': 0x88000,
						'size': 0x8000
					},
					'write': {
						'address': 0x88000,
						'size': 0x8000
					}
				},
				'calibration': {
					'read': {
						'address': 0x90000,
						'size': 0x10000 # 65536 bytes (64 KiB)
					},
					'write': {
						'address': 0x90000,
						'size': 0x10000
					}
				},
				'program': {
					'read': {
						'address': 0xA0000,
						'size': 0x60000
					},
					'write': {
						'address': 0xA0000,
						'size': 0x60000
					}
				}
			}
		}
	},
	{
		'offset': 0x90040,
		'expected': [b'\x63\x61\x36\x36'], #CA66
		'ecu': {
			'name': 'SIMK43 2.0 4mbit',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'regions': {
				'boot1': {
					'read': {
						'address': 0x80000,
						'size': 0x4000
					},
					'write': {
						'address': 0x80000,
						'size': 0x4000
					}
				},
				'boot2': {
					'read': {
						'address': 0x88000,
						'size': 0x8000
					},
					'write': {
						'address': 0x88000,
						'size': 0x8000
					}
				},
				'calibration': {
					'read': {
						'address': 0x90000,
						'size': 0x10000, # 65536 bytes (64 KiB)
					},
					'write': {
						'address': 0x90000,
						'size': 0x10000
					}
				},
				'program': {
					'read': {
						'address': 0xA0000,
						'size': 0x60000
					},
					'write': {
						'address': 0xA0000,
						'size': 0x60000
					}
				}
			}
		},
	},
	{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x35\x34\x30\x31'], #CA65401 (5WY17)
		'ecu': {
			'name': 'SIMK43 V6 4mbit (5WY17)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'regions': {
				'calibration': {
					'read': {
						'address': 0x88000,
						'size': 0x5F40
					},
					'write': {
						'address': 0x88000,
						'size': 0x5F40
					}
				},
				'program': {
					'read': {
						'address': 0x90000,
						'size': 0x70000
					},
					'write': {
						'address': 0x90000,
						'size': 0x70000
					}
				}
			}
		}
	},
		{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x35\x34', b'\x63\x61\x36\x35\x35'], #CA654, CA655 (5WY18+)
		'ecu': {
			'name': 'SIMK43 V6 4mbit (5WY18+)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'regions': {
				'calibration': {
					'read': {
						'address': 0x88000,
						'size': 0x6EFF, # there is some readable but non-writable section after this
					},
					'write': {
						'address': 0x88000,
						'size': 0x6EFF, # there is some readable but non-writable section after this
					}
				},
				'program': {
					'read': {
						'address': 0x90000,
						'size': 0x70000
					},
					'write': {
						'address': 0x90000,
						'size': 0x70000
					}
				}
			}
		}
	},
	{
		'offset': 0x48040,
		'expected': [b'\x63\x61\x36\x36\x30', b'\x63\x61\x36\x35\x32', b'\x63\x61\x36\x35\x30'], #CA660, CA652, CA650
		'ecu': {
			'name': 'SIMK41 / V6 2mbit',
			'eeprom_size_bytes': 262144, # (256 KiB)
			'bin_offset': -0x40000,
			'regions': {
				'calibration': {
					'read': {
						'address': 0x48000,
						'size': 0x8000, # 32,768 bytes (32 KiB)
					},
					'write': {
						'address': 0x48000,
						'size': 0x8000, # 32,768 bytes (32 KiB)
					}
				},
				'program': {
					'read': {
						'address': 0x50000,
						'size': 0x30000
					},
					'write': {
						'address': 0x50000,
						'size': 0x30000
					}
				}
			}
		}
	},
	{
		'offset': 0x88040,
		'expected': [b'\x63\x61\x36\x36\x31'], #CA661 (Sonata)
		'ecu': {
			'name': 'SIMK43 2.0 4mbit (Sonata)',
			'eeprom_size_bytes': 524288, # (512 KiB)
			'bin_offset': -0x80000,
			'regions': {
				'calibration': {
					'read': {
						'address': 0x88000,
						'size': 0x5FF8, # yes, this is correct. this is a 4mbit ecu with a calibration zone smaller than 2mbit ecus. i dont know either
					},
					'write': {
						'address': 0x88000,
						'size': 0x5FF8, # yes, this is correct. this is a 4mbit ecu with a calibration zone smaller than 2mbit ecus. i dont know either
					}
				},
				'program': {
					'read': {
						'address': 0x90000,
						'size': 0x70000
					},
					'write': {
						'address': 0x90000,
						'size': 0x70000
					}
				}
			}
		}
	},
	{
		'offset': 0x92040,
		'expected': [b'\x07\x71\xda\xba'],
 		'ecu': {
			'name': 'SIMK2K (Elantra 2.0 Beta)',
			'eeprom_size_bytes': 524288,
			'bin_offset': -0x80000,
			'regions': {
				'calibration': {
					'read': {
						'address': 0x90000,
						'size': 0xEF00
					},
					'write': {
						'address': 0x92000,
						'size': 0xCF00
					}
				},
				'program': {
					'read': {
						'address': 0xA0000,
						'size': 0x60000
					},
					'write': {
						'address': 0xA0000,
						'size': 0x60000
					}
				}
			}
		}
	},
]

BAUDRATES = {
	0x01: 10400,
	0x02: 19231,
	0x03: 39474,
	0x04: 57692,
	0x05: 125000,
	0x64: 57600,
	0x82: 108800
}

class ReprogrammingStatus(WordBitfield):
	class bits(ctypes.LittleEndianStructure):
		_fields_ = [
			('checksum_of_calibration_data_is_correct', ctypes.c_uint16, 1),
			('security_keys_for_calibration_data_are_not_written', ctypes.c_uint16, 1),
			('security_keys_for_calibration_data_are_correct', ctypes.c_uint16, 1),
			('calibration_data_is_correct', ctypes.c_uint16, 1),
			('checksum_of_ecu_sw_is_correct', ctypes.c_uint16, 1),
			('security_keys_for_ecu_sw_are_not_written', ctypes.c_uint16, 1),
			('security_keys_for_ecu_sw_are_correct', ctypes.c_uint16, 1),
			('ecu_sw_is_correct', ctypes.c_uint16, 1),
			('ecu_reprogramming_successfully_completed', ctypes.c_uint16, 1),
			('ecu_is_not_at_the_end_of_reprogramming_session', ctypes.c_uint16, 1),
			('coherence_identifiers_fit_together', ctypes.c_uint16, 1),
			('calibration_data_does_not_fit_to_ecu_sw', ctypes.c_uint16, 1),
			('ecu_sw_does_not_fit_to_boot_sw', ctypes.c_uint16, 1),
			('coherence_identifier_in_calibration_data_is_erroneous', ctypes.c_uint16, 1),
			('coherence_identifier_in_ecu_sw_is_erroneous', ctypes.c_uint16, 1),
			('coherence_identifier_in_boot_sw_is_erroneous', ctypes.c_uint16, 1),
		]

class AccessLevel (Enum):
	HYUNDAI_0x1 = 0x1
	SIEMENS_0xFD = 0xFD

class Routine (Enum):
	ERASE_PROGRAM = 0x00
	ERASE_CALIBRATION = 0x01
	VERIFY_BLOCKS = 0x02
	CHECK_REPROGRAMMING_STATUS = 0x03

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
	# these 4 enums below are not actually used, just serve as documentation
	IMMO_TEACH_KEY_1 = 0x1B
	IMMO_TEACH_KEY_2 = 0x1C
	IMMO_TEACH_KEY_3 = 0x1D
	IMMO_TEACH_KEY_4 = 0x1E

	BEFORE_SMARTRA_NEUTRALIZE = 0x25
	SMARTRA_NEUTRALIZE = 0x26
	EXECUTE_MTOS = 0xFA

class IOIdentifier (Enum):
	VERSION_CONFIGURATION_AUTOMATIC_TRANSAXLE = 0x40
	VERSION_CONFIGURATION_TRACTION_CONTROL_SYSTEM = 0x41
	ADAPTIVE_VALUES = 0x50
	_OPENGK_PATCH_PRIVILEGE_ESCALATION = 0xBB 


# Memory layout

# Selected by ECU family detection, independently of SIMK memory signatures.
KEFICO_LAYOUT = {
	'name': 'Kefico/Bosch M7.9.7',
	'eeprom_size_bytes': 0x80000,
	'bin_offset': -0x800000,
	'regions': {
		'boot': {
			'read': {'address': 0x800000, 'size': 0x10000},
			'write': {'address': 0x800000, 'size': 0x10000},
		},
		'calibration': {
			'read': {'address': 0x810000, 'size': 0x10000},
			'write': {'address': 0x810000, 'size': 0x10000},
		},
		'program': {
			'read': {'address': 0x820000, 'size': 0x60000},
			'write': {'address': 0x820000, 'size': 0x60000},
		},
	},
}


# ECU family and identification records

ECU_FAMILY_SIMK = 'simk'
ECU_FAMILY_M797 = 'm797'
ECU_FAMILY_UNKNOWN = 'unknown'

M797_ECU_IDENTIFICATION_PARAMETERS = [
	{'value': 0x90, 'name': 'Calibration Identifier'},
	{'value': 0x91, 'name': 'vehicleManufacturerECUHardwareNumber'},
	{'value': 0x92, 'name': 'systemSupplierECUHardwareNumber'},
	{'value': 0x93, 'name': 'systemSupplierECUHardwareVersionNumber'},
	{'value': 0x94, 'name': 'systemSupplierECUSoftwareNumber'},
	{'value': 0x95, 'name': 'systemSupplierECUSoftwareVersionNumber'},
	{'value': 0x96, 'name': 'exhaustRegulationOrTypeApprovalNumber'},
	{'value': 0x97, 'name': 'systemNameOrEngineType'},
	{'value': 0x98, 'name': 'repairShopCodeOrTesterSerialNumber'},
	{'value': 0x99, 'name': 'programmingDate'},
	{'value': 0x9A, 'name': 'calibrationRepairShopCodeOrCalibrationEquipment'},
	{'value': 0x9B, 'name': 'calibrationDate'},
	{'value': 0x9C, 'name': 'calibrationEquipmentSoftwareNumber'},
	{'value': 0x9D, 'name': 'ECUInstallationDate'},
]


# Image, programming and checksum constants

class M797Routine(Enum):
    VERIFY_FLASH = 0x01
    ERASE_FLASH = 0x02


M797_IMAGE_SIZE = KEFICO_LAYOUT['eeprom_size_bytes']
M797_FLASH_START = -KEFICO_LAYOUT['bin_offset']
M797_FLASH_END = M797_FLASH_START + M797_IMAGE_SIZE - 1

M797_FLASH_BLOCK_SIZE = 128
M797_TRANSFER_SIZE = 240
M797_RESPONSE_TIMEOUT = 12.0
M797_MIN_SKIP_SIZE = 0x1000

M797_CHECKSUM_TABLE_OFFSET = 0x1FC20
M797_CHECKSUM_TABLE_ENTRIES = 32
M797_CHECKSUM_ENTRY_SIZE = 0x10

# Internal-ROM boot-integrity fields in the 0x808000 descriptor area.
M797_BOOT_HEADER_CHECKSUM_OFFSET = 0x8038
M797_BOOT_BOUNDARY_CHECKSUM_OFFSET = 0x803C

M797_DYNAMIC_READ_CHUNK = 0xF0
M797_XC167_PAGE_SIZE = 0x4000

M797_PROGRAMMING_PROFILES = {
    'standard': {
        'name': 'STANDARD',
        'selector': 0x64,
        'baudrate': 57600,
        'write_delay': 0.020,
    },
    'fast': {
        'name': 'FAST',
        'selector': 0x82,
        'baudrate': 108800,
        'write_delay': 0.002,
    },
}

# Keep the proven programming order. Verification excludes 0x818000-0x81818F.
M797_FLASH_REGIONS = tuple(
    {
        **KEFICO_LAYOUT['regions'][name]['write'],
        'bin_offset': KEFICO_LAYOUT['regions'][name]['write']['address'] - M797_FLASH_START,
        'verify_ranges': verify_ranges or ((0, KEFICO_LAYOUT['regions'][name]['write']['size']),),
    }
    for name, verify_ranges in (
        ('boot', None),
        ('program', None),
        ('calibration', ((0x0000, 0x8000), (0x8190, 0x7E70))),
    )
)

M797_SECURITY_TABLE = (
    0x24DE49D0, 0x00000000, 0x00000000, 0x516A6710,
    0x00000000, 0x3231B5D0, 0x00000000, 0x00000000,
    0xE6158710, 0xBBB327B0, 0x00FE66F0, 0x00000000,
    0x00000000, 0x00000000, 0x00000000, 0x00000000,
    0x00000000, 0x00000000, 0x00000000, 0x8B55C990,
    0x6133BAB0, 0x00000000, 0xF356C350, 0x9135AF30,
    0x55F377F0, 0x00000000, 0x2055F650, 0x00000000,
    0x00000000, 0x75379B10, 0x00000000, 0xA34645D0,
)


# Patchless read profiles and RAM layouts

M797_READ_STARTUP_BAUD = 10400
M797_READ_STARTUP_SELECTOR = 0x14
M797_READ_DESCRIPTOR_ADDRESS = 0x00E048
M797_READ_STATE_LID = 0xF0
M797_READ_DATA_LIDS = tuple(range(0xF1, 0xF5))  # F1..F4 proven batch
M797_DESCRIPTOR_RELOCATION_DELTA = 0x00E6

# Dynamic-memory address encodings observed in M7.9.7 application code.
M797_DYNAMIC_ADDRESS_MODES = ('linear', 'c166')

# Probe all firmware using the runtime-known RAM architectures.
#
# ORDER MATTERS for the two six-byte c166_four layouts:
#
#   base 0x380044 is tried first, then 0x38003A, then 0x380036.
#
# The descending-base order is deliberate. A higher-base candidate leaves
# the F0 descriptor-count word of the known lower-base layouts untouched,
# so a wrong-layout 21 F0 fails without dereferencing a bogus F0 pointer.
# The reverse direction is unsafe: the old ADE 0x380036 probe placed its
# descriptor-page bytes over FCE2N64's real F0 count field at 0x3805DA,
# which wedged application diagnostics.
M797_C166_GENERIC_CANDIDATES = (
    {
        'name': 'c166_four_8044',
        'raw_slot_base': 0x380044,
        'description': 'c166_four base 0x8044 / physical 0x380044',
    },
    {
        'name': 'c166_four_shifted_803A',
        'raw_slot_base': 0x38003A,
        'description': 'shifted c166_four base 0x803A / physical 0x38003A',
    },
    {
        'name': 'c166_four_8036',
        'raw_slot_base': 0x380036,
        'description': 'c166_four base 0x8036 / physical 0x380036',
    },
)

M797_C166_RAW_DATA_LIDS = tuple(range(0xF0, 0xF5))       # F0..F4
M797_C166_RAW_DESC_LIDS = tuple(range(0xF5, 0xFA))       # F5..F9 storage
M797_C166_RAW_WINDOW_SIZE = 10 * 6
M797_C166_TABLE_WINDOW_SIZE = 5 * 6
M797_C166_DESC_WINDOW_SIZE = 5 * 6
M797_C166_DESCRIPTOR_TYPE_MEMORY = 0x09

# LCR0N53 / c166_lcr uses a different dynamic-LID structure:
#
#   near table base = 0x8014
#   stride          = 8 bytes
#   physical RAM    = 0x380014 + (LID * 8)
#
# Raw F0..F9 therefore occupy:
#   0x380794 .. 0x3807E3
#
# F0..F4 are used as active table entries and F5..F9 as descriptor storage.
# Each 40-byte half fits comfortably inside the KWP short-frame payload limit.
M797_LCR_RAW_SLOT_BASE = 0x380014
M797_LCR_RAW_DATA_LIDS = tuple(range(0xF0, 0xF5))
M797_LCR_RAW_DESC_LIDS = tuple(range(0xF5, 0xFA))
M797_LCR_RAW_WINDOW_START = M797_LCR_RAW_SLOT_BASE + (0xF0 * 8)
M797_LCR_RAW_WINDOW_SIZE = 10 * 8

M797_LCR_TABLE_WINDOW_START = M797_LCR_RAW_WINDOW_START
M797_LCR_TABLE_WINDOW_SIZE = 5 * 8
M797_LCR_DESC_WINDOW_START = (
    M797_LCR_RAW_WINDOW_START + M797_LCR_TABLE_WINDOW_SIZE
)
M797_LCR_DESC_WINDOW_SIZE = 5 * 8
