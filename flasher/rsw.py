import os
import time
from sys import platform
from contextlib import nullcontext
from gkbus.transport import CcpOverCanTransport
from gkbus.protocol import ccp
from alive_progress import alive_bar

class Progress(object):
	def __init__ (self, progress_callback, max_value: int):
		self.progress_callback = progress_callback
		self.progress_callback.emit((max_value, 0))
		self.progress_callback.emit((0,))

	def __call__ (self, value: int):
		self.progress_callback.emit((value,))

	def title (self, title: str):
		pass

# ==========================================================
# LOGGING
# ==========================================================

def log(message, log_callback=None):

    # GUI mode
    if log_callback:
        log_callback.emit(message)

    # CLI mode
    else:
        print(message)

# ==========================================================
# RSW LOADER
# ==========================================================

RSW_FILE = os.path.join(
    os.path.dirname(__file__),
    "../assets/simk43_rsw_fl2.bin"
)

# ==========================================================
# CAN CONFIG
# ==========================================================

TX_ID = 0x6A0 # ca663057 ccp
RX_ID = 0x6A1 # ca663057 ccp
#TX_ID = 0x7EA # ca663020-56 ccp
#RX_ID = 0x7E8 # ca663020-56 ccp

# ==========================================================

hardware = None
transport = None
ccp_client = None

# ==========================================================
# INIT
# ==========================================================

def init_ccp(ecu):

    global hardware
    global transport
    global ccp_client

    hardware = ecu.bus.transport.hardware

    # Prevents a Scapy timeout timeout error on linux during the RSW upload.
    ecu.bus._stop_keepalive()

    transport = CcpOverCanTransport(
        hardware,
        tx_id=TX_ID,
        rx_id=RX_ID,
        crm_only=True
    )

    if platform.startswith('win32'):
        hardware.open(raw=True)
    else:
        transport.init()

    ccp_client = ccp.CcpProtocol(transport)

    hardware.set_timeout(10)

# ==========================================================
# CONNECT
# ==========================================================

def native_connect():

    ccp_client.execute(
        ccp.commands.Connect(station_address=0x01)
    )

# ==========================================================
# CENTRALIZED UNLOCK
# ==========================================================

def native_unlock(resource_mask):

    seed_resp = ccp_client.execute(
        ccp.commands.GetSeedForKey(resource_mask=resource_mask)
    )

    seed_data = seed_resp.get_data()

    seed = seed_data[1:]

    ccp_client.execute(
        ccp.commands.UnlockProtection(key=seed)
    )

# ==========================================================
# MINIMAL BOOTSTRAP
# ==========================================================

def native_bootstrap():

    sequence = [

        ccp.commands.SetMemoryTransferAddress(0, 0, 0x00CEC600),

        ccp.commands.DataDownload(size=0, data=b""),

        ("UNLOCK", 0x01),

        ccp.commands.StartStopSynchronisedDataTransmission(ccp.enums.DataTransmissionRequest.STOP),

        ccp.commands.GetSessionStatus(),

        ccp.commands.SetSessionStatus(status=0x40),

        ccp.commands.SetMemoryTransferAddress(0, 0, 0x00090008),

        ccp.commands.DataDownload(size=0, data=b""),

        ccp.commands.SetMemoryTransferAddress(0, 0, 0x000A0056),

        ccp.commands.DataDownload(size=0, data=b""),

        ccp.commands.SetMemoryTransferAddress(0, 0, 0x00090000),

        ccp.commands.DataDownload(size=0, data=b""),

        ("UNLOCK", 0x40),

        ccp.commands.ActionService(0xC315),

        ccp.commands.ActionService(0xC312),

        ccp.commands.ActionService(0xC313),

        ccp.commands.SetMemoryTransferAddress(0, 0, 0x00082000),
    ]

    for cmd in sequence:

        if isinstance(cmd, tuple):

            action, resource_mask = cmd

            if action == "UNLOCK":

                native_unlock(resource_mask)

        else:

            ccp_client.execute(cmd)

# ==========================================================
# FLASH RSW
# ==========================================================

def flash_rsw(data, progress_callback=None, log_callback=None):
    log('[*] Uploading RSW', log_callback)

    def download(chunk):
        return (ccp.commands.DataDownload6Bytes(data=chunk) if len(chunk) == 6
                else ccp.commands.DataDownload(size=len(chunk), data=chunk))

    _transfer_6byte(data, download, 'Uploading RSW', progress_callback, percentage=True)
    ccp_client.execute(ccp.commands.ActionService(0xC314))
    log('[*] RSW upload complete', log_callback)

# ==========================================================
# PREP BOOT1
# ==========================================================

def prep_boot1(boot1_address, boot1_size):

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, 0x0830AC)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, boot1_address)
    )

    ccp_client.execute(
        ccp.commands.ClearMemory(size=boot1_size)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, boot1_address)
    )

# ==========================================================
# PREP BOOT2
# ==========================================================

def prep_boot2(boot2_address, boot2_size):

    ccp_client.execute(
        ccp.commands.Program(size=5, data=b"\x36\x30\x00\x00\x00")
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, 0x000000)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, boot2_address)
    )

    ccp_client.execute(
        ccp.commands.ClearMemory(size=boot2_size)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, boot2_address)
    )

# ==========================================================
# PREP ASW
# ==========================================================

def prep_asw(asw_address, asw_size):

    ccp_client.execute(
        ccp.commands.Program(size=4, data=b"\xFF\xFF\xFF\xFF")
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, 0x000000)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, asw_address)
    )

    ccp_client.execute(
        ccp.commands.ClearMemory(size=asw_size)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, asw_address)
    )

# ==========================================================
# PREP CAL
# ==========================================================

def prep_cal(cal_address, cal_size):

    ccp_client.execute(
        ccp.commands.Program(size=2, data=b"\xFF\xFF")
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, 0x000000)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, cal_address)
    )

    ccp_client.execute(
        ccp.commands.ClearMemory(size=cal_size)
    )

    ccp_client.execute(
        ccp.commands.SetMemoryTransferAddress(0, 0, cal_address)
    )

# ==========================================================
# GENERIC 6 BYTE FLASHER
# ==========================================================

def _transfer_6byte(data, command, title, progress_callback=None, percentage=False):
    total_chunks = (len(data) + 5) // 6
    progress = (Progress(progress_callback, 100 if percentage else total_chunks)
                if progress_callback is not None else None)

    with (nullcontext() if progress is not None else alive_bar(len(data), title=title, unit='B')) as bar:
        for current, offset in enumerate(range(0, len(data), 6), 1):
            ccp_client.execute(command(data[offset:offset + 6]))
            if progress is not None:
                progress(int(current * 100 / total_chunks) if percentage else current)
            else:
                bar(len(data[offset:offset + 6]))


def flash_file_6byte(name, data, progress_callback=None, log_callback=None):
    log(f'[*] Flashing {name}', log_callback)
    _transfer_6byte(
        data,
        lambda chunk: ccp.commands.Program6Bytes(data=chunk.ljust(6, b"\x00")),
        f'Flashing {name}',
        progress_callback,
    )
    log(f'[*] {name} upload complete', log_callback)

# ==========================================================
# BIN HELPERS
# ==========================================================

def extract_bin_region(
    ecu,
    bin_data,
    address,
    size,
    log_callback=None
):

    start = ecu.calculate_bin_offset(address)
    end = start + size

    log(f"    CCP Address : 0x{address:06X}", log_callback)
    log(f"    BIN Offset  : 0x{start:06X}", log_callback)
    log(f"    Size        : 0x{size:X}", log_callback)

    region = bin_data[start:end]

    if len(region) != size:

        raise Exception(
            f"Region extraction failed: "
            f"expected 0x{size:X} bytes, "
            f"got 0x{len(region):X}"
        )

    return region

# ==========================================================
# FINALIZE
# ==========================================================

def finalize():

    hardware.set_timeout(3)

    try:

        ccp_client.execute(
            ccp.commands.ActionService(0xC310)
        )

    except:
        pass

    hardware.set_timeout(1)

    time.sleep(3)

    try:

        ccp_client.execute(
            ccp.commands.ActionService(0xC311)
        )

    except:
        pass

# ==========================================================
# HANDLER
# ==========================================================

def rsw_handler(
    ecu,
    mode,
    bin_file,
    progress_callback=None,
    log_callback=None
):
    # ======================================================
    # CAN HARDWARE CHECK
    # ======================================================
    if 'CanHardware' not in ecu.bus.transport.hardware.__class__.__name__:
        raise Exception(
            'RSW mode requires a CANBUS interface.'
        )

    if mode != 'virginize':
        with open(bin_file, "rb") as file:
            full_bin = file.read()

        log(f"[*] Loading up: {bin_file}", log_callback)
        log(f"[*] Size: {len(full_bin)} bytes", log_callback)

    init_ccp(ecu)

    native_connect()

    native_unlock(0x02)

    native_bootstrap()

    # ======================================================
    # FLASH RSW
    # ======================================================

    with open(RSW_FILE, "rb") as file:

        rsw_data = file.read()

    flash_rsw(
        rsw_data,
        progress_callback=progress_callback,
        log_callback=log_callback
    )
    # ======================================================
    # ECU REGION INFO
    # ======================================================

    boot1_region = ecu.regions['boot1']['write']
    boot2_region = ecu.regions['boot2']['write']
    cal_region   = ecu.regions['calibration']['write']
    asw_region   = ecu.regions['program']['write']

    boot1_address = boot1_region['address']
    boot1_size    = boot1_region['size']

    boot2_address = boot2_region['address']
    boot2_size    = boot2_region['size']

    cal_address = cal_region['address']
    cal_size    = cal_region['size']

    asw_address = asw_region['address']
    asw_size    = asw_region['size']

    # ======================================================
    # BOOT1
    # ======================================================

    if mode == "bsw" or mode == "full":

        log("[*] Extracting Boot1", log_callback)

        boot1_data = extract_bin_region(
            ecu,
            full_bin,
            boot1_address,
            boot1_size,
            log_callback
        )

        prep_boot1(boot1_address, boot1_size)

        flash_file_6byte(
            "Boot1",
            boot1_data,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    # ======================================================
    # BOOT2
    # ======================================================

    if mode == "bsw" or mode == "full":

        log("[*] Extracting Boot2", log_callback)

        boot2_data = extract_bin_region(
            ecu,
            full_bin,
            boot2_address,
            boot2_size,
            log_callback
        )

        prep_boot2(boot2_address, boot2_size)

        flash_file_6byte(
            "Boot2",
            boot2_data,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    # ======================================================
    # CAL
    # ======================================================

    if mode == "cal" or mode == "full":

        log("[*] Extracting Calibration", log_callback)

        cal_data = extract_bin_region(
            ecu,
            full_bin,
            cal_address,
            cal_size - 0x10,
            log_callback
        )

        log(f"[*] Calibration Size: 0x{len(cal_data):X}", log_callback)

        prep_cal(cal_address, cal_size)

        flash_file_6byte(
            "CAL",
            cal_data,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    # ======================================================
    # ASW
    # ======================================================

    if mode == "asw" or mode == "full":

        log("[*] Extracting ASW", log_callback)

        asw_data = extract_bin_region(
            ecu,
            full_bin,
            asw_address,
            asw_size,
            log_callback
        )

        log(f"[*] ASW Size: 0x{len(asw_data):X}", log_callback)

        prep_asw(asw_address, asw_size)

        flash_file_6byte(
            "ASW",
            asw_data,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    # ======================================================
    # VIRGINIZE
    # ======================================================

    if mode == "virginize":

        log("[*] Viginizating ECU", log_callback)

        ccp_client.execute(
            ccp.commands.Program(size=4, data=b"\xFF\xFF\xFF\xFF")
        )

        ccp_client.execute(
            ccp.commands.SetMemoryTransferAddress(0, 0, 0x000000)
        )

        ccp_client.execute(
            ccp.commands.SetMemoryTransferAddress(0, 0, 0x084000)
        )

        ccp_client.execute(
            ccp.commands.ClearMemory(size=0x4000)
        )

        ccp_client.execute(
            ccp.commands.SetMemoryTransferAddress(0, 0, 0x084000)
        )
        
        log("[*] Viginization Complete", log_callback)

    # ======================================================
    # FINALIZE
    # ======================================================

    finalize()
