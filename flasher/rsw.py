import os
import time
from gkbus.channel import CanChannel, KLineChannel
from gkbus.transport import CcpOverCanTransport
from gkbus.protocol import ccp
from .progress import Progress, transfer_summary

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

transport = None
ccp_client = None

# ==========================================================
# INIT
# ==========================================================

def init_ccp(ecu):

    global transport
    global ccp_client

    channel = ecu.bus.transport.channel

    # CCP takes over the CAN channel, so stop KWP2000 (keepalive and ISO-TP link) first.
    # The channel itself stays open if it was opened before the KWP2000 transport
    ecu.bus.close()

    transport = CcpOverCanTransport(
        channel,
        tx_id=TX_ID,
        rx_id=RX_ID,
        crm_only=True,
        timeout=10,
    )
    transport.init()

    ccp_client = ccp.CcpProtocol(transport)

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
    started_at = time.monotonic()
    log('[*] Uploading RSW', log_callback)

    def download(chunk):
        return (ccp.commands.DataDownload6Bytes(data=chunk) if len(chunk) == 6
                else ccp.commands.DataDownload(size=len(chunk), data=chunk))

    _transfer_6byte(data, download, 'Uploading RSW', progress_callback)
    ccp_client.execute(ccp.commands.ActionService(0xC314))
    log(transfer_summary('Uploaded', len(data), started_at), log_callback)

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

def _transfer_6byte(data, command, title, progress_callback=None, batch=False):
    progress = Progress(progress_callback, len(data), title)
    try:
        offset = 0
        if batch:
            offset = len(data) // 6 * 6
            ccp_client.command_counter = transport.program_data(data[:offset], ccp_client.command_counter, progress)
        for offset in range(offset, len(data), 6):
            chunk = data[offset:offset + 6]
            ccp_client.execute(command(chunk))
            progress(offset + len(chunk))
    finally:
        progress.finish()


def flash_file_6byte(name, data, progress_callback=None, log_callback=None):
    if isinstance(getattr(transport, 'channel', None), KLineChannel):
        # The selected region was erased first. Leave its erased tail alone;
        # retain a complete native 128-byte staging block before the flush.
        end = len(data.rstrip(b'\xFF'))
        data = data[:min(len(data), (end + 127) // 128 * 128)]
    started_at = time.monotonic()
    log(f'[*] Flashing {name}', log_callback)
    _transfer_6byte(
        data,
        lambda chunk: (ccp.commands.Program6Bytes(data=chunk) if len(chunk) == 6
                       else ccp.commands.Program(size=len(chunk), data=chunk)),
        f'Flashing {name}',
        progress_callback,
        batch=isinstance(getattr(transport, 'channel', None), KLineChannel),
    )
    ccp_client.execute(ccp.commands.Program(size=0, data=b''))
    log(transfer_summary('Flashed', len(data), started_at), log_callback)

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

    start = 0 if address == 0 else ecu.calculate_bin_offset(address)
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

    transport.set_timeout(3)

    try:

        ccp_client.execute(
            ccp.commands.ActionService(0xC310)
        )

    except:
        pass

    transport.set_timeout(1)

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

def rsw_handler(ecu, mode, bin_file, progress_callback=None, log_callback=None, key=None, baudrate=None):
    if mode not in ('boot1', 'boot2', 'bsw', 'asw', 'cal', 'full', 'virginize'):
        raise ValueError('Unknown RSW mode: {}'.format(mode))
    if not all(name in ecu.regions for name in ('boot1', 'boot2', 'calibration', 'program')):
        raise ValueError('This ECU layout is not supported by the SIMK43 RSW loader')
    full_bin = None
    if mode != 'virginize':
        with open(bin_file, 'rb') as file:
            full_bin = file.read()
        if len(full_bin) != ecu.get_eeprom_size_bytes():
            raise ValueError('BIN size does not match the selected ECU')
        log('[*] Loading up: {}'.format(bin_file), log_callback)
        log('[*] Size: {} bytes'.format(len(full_bin)), log_callback)
    regions = _region_payloads(ecu, mode, full_bin, log_callback)
    channel = ecu.bus.transport.channel
    if isinstance(channel, KLineChannel):
        from .rsw_kline import run
        return run(ecu, mode, regions, key=key, baudrate=baudrate,
                   progress_callback=progress_callback, log_callback=log_callback)
    if not isinstance(channel, CanChannel):
        raise ValueError('RSW requires a CAN or K-Line interface')
    init_ccp(ecu)
    native_connect()
    native_unlock(0x02)
    native_bootstrap()
    with open(RSW_FILE, 'rb') as file:
        flash_rsw(file.read(), progress_callback=progress_callback, log_callback=log_callback)
    _flash_regions(mode, regions, progress_callback, log_callback)
    finalize()


def _region_payloads(ecu, mode, image, log_callback=None):
    groups = {'bsw': ('boot1', 'boot2'), 'full': ('boot1', 'boot2', 'calibration', 'program'),
              'boot1': ('boot1',), 'boot2': ('boot2',), 'cal': ('calibration',),
              'asw': ('program',), 'virginize': ()}
    regions = []
    for name in groups[mode]:
        region = ecu.regions[name]['write']
        address, size = region['address'], region['size']
        if name == 'boot1' and isinstance(ecu.bus.transport.channel, KLineChannel):
            address = 0  # native RSW uses the low boot alias; BIN offset is also zero
        data = extract_bin_region(ecu, image, address, size - (16 if name == 'calibration' else 0), log_callback)
        if not data or not any(data) or data.count(0xFF) == len(data):
            raise ValueError('The selected {} region is empty'.format(name))
        regions.append((name, address, size, data))
    return regions


def _flash_regions(mode, regions, progress_callback=None, log_callback=None):
    preparations = {'boot1': prep_boot1, 'boot2': prep_boot2,
                    'calibration': prep_cal, 'program': prep_asw}
    for name, address, size, data in regions:
        preparations[name](address, size)
        flash_file_6byte(name, data, progress_callback, log_callback)
    if mode == 'virginize':
        log('[*] Virginizing ECU', log_callback)
        ccp_client.execute(ccp.commands.Program(size=4, data=b'\xFF' * 4))
        ccp_client.execute(ccp.commands.SetMemoryTransferAddress(0, 0, 0))
        ccp_client.execute(ccp.commands.SetMemoryTransferAddress(0, 0, 0x84000))
        ccp_client.execute(ccp.commands.ClearMemory(size=0x4000))
        ccp_client.execute(ccp.commands.SetMemoryTransferAddress(0, 0, 0x84000))
        log('[*] Virginization complete', log_callback)
