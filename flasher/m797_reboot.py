"""RAM-only reset extension for ROM 32.05; see docs/m797-reboot.md."""

import hashlib
import struct
import time

from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000
from flasher.rsw import log # need to relocate

_BASE = 0x382800
_SIZE = 0x500
_LID = 0xF8
_SERIAL_SHUTDOWN = bytes.fromhex(
    'e6 f4 a6 29 e6 f5 00 00 f6 f4 7e e0 f6 f5 80 e0 '
    'e6 f4 74 26 e6 f5 00 00 f6 f4 86 e0 f6 f5 88 e0 db 00')
# Position-independent shutdown of interrupts/peripherals; identical in the
# traced applications. Keep it in RAM rather than scanning flash for a copy.
_INTERRUPT_SHUTDOWN = bytes.fromhex(
    'd1201a88f0f8e6bb0000e6ba0000e6b90000e6cd0000e6cc0000e6b40000e6b30000'
    'e6b10000e6b00000e6cf0000e6ce0000e6cb0000e6ca0000e6c90000e6c80000e6c70000'
    'e6c60000e6c50000e6c40000e6c30000e6c20000e6c10000e6c00000e6bf0000e6be0000'
    'e6bd0000e6bc0000d190e6cf0000e6cb0000d190e6c70000e6c30000d190e6bf0000e6be0000'
    'd190e6bd0000e6ca0000d190e6c60000e6c20000d190e6bc0000e6bb0000d190e6ba0000'
    'e6b90000d190e6b70000e6b60000d190e6b50000e6b40000d190e6b30000e6b20000'
    'd190e6b10000e6b00000db00')
_RESET_SHA256 = '0aabb26f60227299626b3b98dade68c4af62c69c67d27b1b256c9bfedd8cea57'


def _read_reset_handler(read_flash, table):
    """Validate the ROM interface and read only the table-selected reset code."""
    if len(table) != 0x9C:
        return None
    handlers = struct.unpack('<39I', table)
    for index, handler in ((0, 0x3B66), (2, 0x3266), (3, 0x32AC), (5, 0x36B2),
                           (9, 0x381E), (13, 0x395C), (14, 0x3ABC), (15, 0x3B1C)):
        if handlers[index] != handler:
            return None
    address = handlers[16]
    if address & 1 or not 0x820000 <= address <= 0x880000 - 126:
        return None
    handler = read_flash(address, 126)
    return handler if hashlib.sha256(handler).hexdigest() == _RESET_SHA256 else None


def _paged_pointer(address):
    return struct.pack('<HH', address & 0x3FFF, address >> 14)


def _build_monitor(reset_handler, application_table):
    """Assemble the traced C166 handover without depending on local ROM dumps."""
    blob = bytearray(_SIZE)
    initializers = (_BASE + 0x100, 0x5D02, 0x4F24, 0x7E16, 0x0D4C, 0x7E32, 0x103A, 0)
    blob[:32] = struct.pack('<8I', *initializers)
    blob[0x40:0x62] = _SERIAL_SHUTDOWN
    # Kernel 0x700 installs DPP2=0xE0. The near init-list pointer uses DPP2.
    descriptor = bytearray.fromhex('00 00 e0 02 9a 06 00 00 00 00 00 00')
    descriptor[:2] = (0x8000 + (_BASE & 0x3FFF)).to_bytes(2, 'little')
    blob[0x80:0x8C] = descriptor

    # CALLS 0:20EC (stock dispatch init); MOV E2C4,#2 (monitor state).
    hook = bytearray.fromhex('da 00 ec 20 e0 24 f6 f4 c4 e2')
    for variable, value in ((0xE228, (_BASE + 0x200) & 0x3FFF), (0xE22A, _BASE >> 14),
                            (0xE22C, (_BASE + 0x240) & 0x3FFF), (0xE22E, _BASE >> 14)):
        # MOV r4,#value; MOV variable,r4.
        hook += struct.pack('<4H', 0xF4E6, value, 0xF4F6, variable)
    hook += b'\xDB\x00'  # RETS
    blob[0x100:0x100 + len(hook)] = hook

    # Original ROM 32.05 handlers and SID&0x7F index table, plus ECUReset.
    handlers = (0x3B66, 0x320A, 0x3266, 0x32AC, 0x3648, 0x36B2, 0x3758, 0x3788,
                0x381E, 0x3828, 0x3898, 0x38FA, 0x395C, 0x3ABC, 0x3B1C, _BASE + 0x480)
    blob[0x200:0x240] = struct.pack('<16I', *handlers)
    for sid, index in ((0x10, 4), (0x1A, 6), (0x20, 5), (0x27, 7), (0x31, 9),
                       (0x32, 10), (0x33, 11), (0x34, 12), (0x36, 13), (0x37, 14),
                       (0x3E, 8), (0x41, 1), (0x42, 2), (0x43, 3), (0x11, 15)):
        blob[0x240 + sid] = index

    # The fingerprinted reset handler uses relative branches and ROM calls.
    blob[0x39C:0x47E] = _INTERRUPT_SHUTDOWN
    blob[0x480:0x4FE] = reset_handler
    # Stock application serial shutdown, interrupt shutdown, then ROM kernel
    # entry 0:0700 with our process descriptor in r12:r13. Never returns.
    def calls(address):
        return bytes((0xDA, address >> 16)) + (address & 0xFFFF).to_bytes(2, 'little')

    activation = (calls(_BASE + 0x40) + calls(_BASE + 0x39C) + b'\xE6\xFC'
                  + _paged_pointer(_BASE + 0x80)[:2]
                  + b'\xE6\xFD' + (_BASE >> 14).to_bytes(2, 'little')
                  + bytes.fromhex('e0 24 f6 f4 c4 e2 da 00 00 07 e6 f4 00 00 db 00'))
    blob[0x180:0x180 + len(activation)] = activation
    table = bytearray(application_table)
    table[9 * 4:10 * 4] = (_BASE + 0x180).to_bytes(4, 'little')
    blob[0x300:0x300 + len(table)] = table
    return bytes(blob)


def prepare_reboot(bus, read_flash, read_ram, write_ram, clear_lid, get_calibration_id,
                   read_profile='standard', log_callback=None):
    """Install a compatible monitor before erase; return None for stock flashing."""
    try:
        calibration_id = get_calibration_id(bus)
        expected_id = bytes(bus.transport.send_read_pdu(b'\x1A\x94'))
    except (kwp2000.Kwp2000NegativeResponseException, TimeoutException, RuntimeError):
        return None  # A blank/recovery ECU still needs the stock ROM path.
    transport = bus.transport
    if not expected_id.startswith(b'\x5A\x94') or not expected_id[2:].strip():
        return None

    log('[*] Checking firmware for automatic reboot support', log_callback)
    def read_flash_range(address, size):
        return read_flash(bus, calibration_id, output_filename=None, read_profile=read_profile,
                          address_start=address, size=size, log_callback=log_callback)

    # Resolve the running application's table rather than assuming a BIN offset.
    bus.execute(kwp2000.commands.StartDiagnosticSession(
        kwp2000.enums.DiagnosticSession.ENGINEERING, 0x14))
    try:
        try:
            pointers = read_ram(bus, _LID, 0xE228, 8)
        finally:
            clear_lid(bus, _LID)
    except (kwp2000.Kwp2000NegativeResponseException, TimeoutException):
        return None  # Older RAM parsers retain the stock programming path.
    if len(pointers) != 8:
        return None
    table_offset, table_page, map_offset, map_page = struct.unpack('<4H', pointers)
    if max(table_offset, map_offset) >= 0x4000:
        return None
    table_address = table_offset + (table_page << 14)
    map_address = map_offset + (map_page << 14)
    if not (0x800000 <= table_address <= 0x87FF64 and 0x800000 <= map_address <= 0x87FF80):
        return None
    if map_address == table_address + 0x9C:
        tables = read_flash_range(table_address, 0x9C + 128)
        table, mapping = tables[:0x9C], tables[0x9C:]
    else:
        table = read_flash_range(table_address, 0x9C)
        mapping = read_flash_range(map_address, 128)
    if (len(table) != 0x9C or len(mapping) != 128
            or mapping[0x11] != 16 or mapping[0x3E] != 9 or max(mapping) >= 39):
        return None
    reset_handler = _read_reset_handler(read_flash_range, table)
    if reset_handler is None:
        log('[!] Reboot code layout is not recognized; using stock programming.', log_callback)
        return None

    bus.execute(kwp2000.commands.StartDiagnosticSession(
        kwp2000.enums.DiagnosticSession.ENGINEERING, 0x14))
    blob = _build_monitor(reset_handler, table)

    def read_window():
        return b''.join(read_ram(bus, _LID, _BASE + offset, 128)
                        for offset in range(0, _SIZE, 128))

    def write_window(data):
        for offset in range(0, _SIZE, 128):
            chunk = data[offset:offset + 128]
            write_ram(bus, _BASE + offset, chunk)
            if read_ram(bus, _LID, _BASE + offset, len(chunk)) != chunk:
                raise RuntimeError('Reboot monitor RAM verification failed')

    original = read_window()
    # Require stable, zeroed scratch storage before uploading executable code.
    if original != bytes(_SIZE) or read_window() != original:
        clear_lid(bus, _LID)
        raise RuntimeError('Reboot monitor RAM is occupied or unstable; no flash was erased')
    activated = False
    try:
        write_window(blob)
        clear_lid(bus, _LID)
        # Treat dispatch-pointer changes as activation even if their response
        # is lost: restoring scratch RAM after that could erase active code.
        activated = True
        write_ram(bus, 0xE228, _paged_pointer(_BASE + 0x300))
        transport.send_pdu(b'\x3E\x01')
        time.sleep(2.5)
        transport.channel.reset_buffers()
        transport.init(b'\x81')
        if bytes(transport.send_read_pdu(b'\x1A\x94')) != b'\x5A\x94\x32\x05':
            raise RuntimeError('Expected ROM 32.05 after reboot-monitor handover')
    except Exception as exc:
        if not activated:
            try:
                write_window(original)
                clear_lid(bus, _LID)
            except Exception as cleanup_exc:
                raise RuntimeError('Reboot preparation RAM restoration failed; no flash was erased. '
                                   'Power-cycle the ECU before retrying.') from cleanup_exc
        raise RuntimeError('Reboot preparation failed; no flash was erased. '
                           'Power-cycle the ECU before retrying.') from exc
    log('[+] Automatic reboot monitor ready', log_callback)
    return expected_id


def reboot(bus, expected_id, log_callback=None):
    """Confirm application recovery after reset, without re-entering programming."""
    log('[*] Rebooting ECU', log_callback)
    response = bus.execute(kwp2000.commands.ECUReset(kwp2000.enums.ResetMode.POWER_ON_RESET))
    if response.frame.status != 0x51:
        raise RuntimeError('ECUReset acknowledgement not confirmed')
    time.sleep(1)
    transport = bus.transport
    transport.channel.set_baudrate(10400)
    transport.channel.reset_buffers()
    transport.init(b'\x81')
    response = bytes(transport.send_read_pdu(b'\x1A\x94'))
    calibration = bytes(transport.send_read_pdu(b'\x1A\x90'))
    # A full flash may replace the program ID; calibration-only flashes retain it.
    if (not response.startswith(b'\x5A\x94') or not response[2:].strip()
            or response[2:].strip() == b'\x32\x05'
            or (expected_id is not None and response != expected_id)
            or not calibration.startswith(b'\x5A\x90') or not calibration[2:].strip()):
        raise RuntimeError('Application identification after reset was not confirmed')
    log('[+] ECU rebooted; application responding at 10400 baud', log_callback)
