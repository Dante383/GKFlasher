"""K-Line transport for the native RSW command processor, relocated into XRAM.

The existing MTOS/kernel bootstrap supplies UART RPC only. Flash operations are
executed by the bundled RSW, not the MTOS flash driver. No RSW flash installation
or firmware patch is required.
"""
import hashlib
import struct

from gkbus.transport import TransportABC, PacketDirection

from .kernel import KernelError, load_asset
from .mtos_asm import _assemble, crc16_mtos

RSW_BASE = 0xC400
BRIDGE_ADDRESS = 0xE400
BATCH_ADDRESS = 0xD500  # native RSW ends at D4AC; extended XRAM ends at D7FF
BATCH_SIZE = 0x300
RSW_SHA256 = '244397119c58306fb043f8902ac26b710dd632824f935fd33fb23134b8944039'
# Instruction relocations audited against simk43_rsw_fl2.bin in IDA. The hash
# guard rejects other loaders; byte patterns alone must never select fixups.
FAR_CALLS = (
    0x46, 0x4E, 0x6E, 0x88, 0xBC, 0xE6, 0x1CC, 0x204, 0x228, 0x22C,
    0x250, 0x254, 0x282, 0x40C, 0x44E, 0x4CE, 0x52A, 0x64A, 0x67C,
    0x6CA, 0x6F2, 0xB60, 0xC16, 0xCBC, 0xD24, 0xD76, 0xD8C, 0xD94,
    0xDE4, 0xDFA, 0xE02, 0xED2, 0xEDA,
)
ABSOLUTE_JUMPS = (
    0x94, 0xAE, 0xD4, 0xDE, 0xEA, 0x30C, 0x314, 0x31A, 0x320, 0x328,
    0x32C, 0x392, 0x39E, 0x3AA, 0x3B4, 0x3C6, 0x3D0, 0x41A, 0x426,
    0x432, 0x48C, 0x498, 0x4A4, 0x50E, 0x51A, 0x526, 0x55C, 0x568,
    0x574, 0x5B6, 0x5C2, 0x5CE, 0xA00, 0xA6A, 0xA86, 0xB74, 0xB9E, 0xC9C,
)
TABLE_PAGES = (
    0x192, 0x64E, 0xA08, 0xA28, 0xA94, 0xAA6, 0xAC0, 0xADC, 0xAF0, 0xB06, 0xB2E, 0xB42,
)


def relocated_image():
    source = load_asset('assets/simk43_rsw_fl2.bin')
    if hashlib.sha256(source).hexdigest() != RSW_SHA256:
        raise KernelError('Unrecognised RSW loader; refusing unaudited relocation')
    image = bytearray(source[:0x10AD])  # code, sector table and status byte
    for offset in (*FAR_CALLS, *ABSOLUTE_JUMPS):
        target = int.from_bytes(image[offset + 2:offset + 4], 'little')
        if not 0x2000 <= target < 0x3084:
            raise KernelError('Invalid RSW relocation target')
        image[offset + 2:offset + 4] = (RSW_BASE + target - 0x2000).to_bytes(2, 'little')
    for offset in FAR_CALLS:
        image[offset + 1] = 0  # segment 8 -> segment 0
    for offset in TABLE_PAGES:
        image[offset + 2:offset + 4] = (RSW_BASE >> 14).to_bytes(2, 'little')
        target = int.from_bytes(image[offset + 6:offset + 8], 'little')
        target = (RSW_BASE + target - 0x2000) & 0x3FFF
        image[offset + 6:offset + 8] = target.to_bytes(2, 'little')
    for offset in (0x19A, 0x656):
        image[offset + 2:offset + 4] = (RSW_BASE + 0x10AC).to_bytes(2, 'little')
        image[offset + 6:offset + 8] = bytes(2)
    image[4:8] = (RSW_BASE + 0x18).to_bytes(4, 'little')
    image[8:12] = (RSW_BASE + 0x10AB).to_bytes(4, 'little')
    # UART RPC replaces only CAN setup/transmit. Initialisation returns before
    # the native CAN interrupt loop; native parser/flash/checksum bodies remain.
    for offset in (0xF0, 0x26A, 0x2B0, 0x2C8):
        image[offset:offset + 2] = b'\xDB\x00'  # RETS
    image[0x72:0x76] = b'\x08\x02\xDB\x00'  # ADD R0,#2; RETS
    return bytes(image)


class CcpOverKLineTransport(TransportABC):
    """CRC-protected native RSW calls through the already running UART kernel."""

    def __init__(self, kernel, writable=False):
        super().__init__(kernel.link.channel, timeout=10)
        self.kernel, self.writable = kernel, writable
        self._reply = None

    def upload(self):
        kernel = self.kernel
        if not kernel.read_word(0xFF12) & 4 or not kernel.read_word(0xF024) & 2:
            raise KernelError('K-Line RSW requires enabled C167CS extended XRAM')
        kernel.log('[*] Loading native RSW into RAM')
        image = relocated_image()
        result = _assemble('kline_rpc', source_directory='assets/rsw')
        helper = result.image()
        if helper.base != BRIDGE_ADDRESS or helper.base + len(helper.data) > 0xE800:
            raise KernelError('RSW serial bridge exceeds its RAM allocation')
        for address, data in ((RSW_BASE, image), (BRIDGE_ADDRESS, helper.data)):
            kernel.write_memory(address, data)
        for address, data in ((RSW_BASE, image), (BRIDGE_ADDRESS, helper.data)):
            if kernel.read_memory(address, len(data)) != data:
                raise KernelError('RSW RAM readback failed; flash has not been erased')
        kernel.write_memory(0xC0FC, bytes(0x40))
        kernel.write_memory(0xC12D, b'\x02')  # native flash programming class
        if kernel.call(BRIDGE_ADDRESS, [0] * 8)[7]:
            raise KernelError('RSW initialisation or native supply-voltage qualification failed')
        scratch = bytes(range(256)) * (BATCH_SIZE // 256)
        kernel.write_memory(BATCH_ADDRESS, scratch)
        if kernel.read_memory(BATCH_ADDRESS, len(scratch)) != scratch:
            raise KernelError('RSW batch RAM readback failed; flash has not been erased')

    def send_pdu(self, pdu):
        if len(pdu) != 8 or self._reply is not None:
            raise KernelError('Invalid or outstanding RSW request')
        self.kernel.check_stop()
        registers = list(struct.unpack('<4H', pdu)) + [crc16_mtos(pdu, seed=0xFFFF), 0,
            0x5257 if self.writable else 0, 1]
        # A timeout may follow a completed write. Never replay a native command.
        if self.writable and pdu[0] in (0x10, 0x18, 0x22):
            self.kernel.flash_modified = True
            self.kernel.critical = True
        self._record(PacketDirection.OUTGOING, pdu)
        reply = self.kernel.call(BRIDGE_ADDRESS, registers, timeout=self.timeout)
        if reply[7] or not 3 <= reply[5] <= 8:
            raise KernelError('RSW serial bridge rejected the request')
        data = struct.pack('<4H', *reply[:4])[:reply[5]]
        self._record(PacketDirection.INCOMING, data)
        if (crc16_mtos(data, seed=0xFFFF) != reply[4]
                or data[0] != 0xFF or data[2] != pdu[1]):
            raise KernelError('RSW response CRC or counter mismatch; command not replayed')
        self._reply = data.ljust(8, b'\x00')
        return len(pdu)

    def read_pdu(self, timeout=None):
        if self._reply is None:
            raise KernelError('No outstanding RSW response')
        data, self._reply = self._reply, None
        return data

    def program_data(self, data, counter, progress):
        """Run checked native commands locally; never replay a programming batch."""
        if not self.writable or self._reply is not None or len(data) % 6:
            raise KernelError('Invalid RSW programming batch')
        payload_size = BATCH_SIZE // 8 * 6
        for start in range(0, len(data), payload_size):
            self.kernel.check_stop()
            chunk = data[start:start + payload_size]
            count = len(chunk) // 6
            frames = b''.join(bytes((0x22, (counter + index) & 0xFF)) + chunk[index * 6:index * 6 + 6]
                              for index in range(count))
            self.kernel.write_memory(BATCH_ADDRESS, frames)
            self.kernel.flash_modified = True
            self.kernel.critical = True
            reply = self.kernel.call(BRIDGE_ADDRESS,
                [BATCH_ADDRESS, len(frames), 0, 0, crc16_mtos(frames, seed=0xFFFF), 0, 0x5257, 2],
                timeout=self.timeout)
            if reply[7] or not 3 <= reply[5] <= 8:
                raise KernelError('RSW programming batch rejected (status {}, registers {}); commands not replayed'
                                  .format(reply[7], reply))
            response = struct.pack('<4H', *reply[:4])[:reply[5]]
            if (crc16_mtos(response, seed=0xFFFF) != reply[4]
                    or response[:2] not in (b'\xFF\x00', b'\xFF\xFF')
                    or response[2] != (counter + count - 1) & 0xFF):
                raise KernelError('RSW programming batch response mismatch; commands not replayed')
            counter = (counter + count) & 0xFF
            progress(start + len(chunk))
        return counter

def _preserved_ranges(chip, regions):
    intervals = sorted((address % chip.size, address % chip.size + size)
                       for _, address, size, _ in regions)
    for _, start, size in chip.sectors:
        stop = start + size
        selected = [(max(start, a), min(stop, b)) for a, b in intervals if a < stop and b > start]
        if not selected:
            continue
        cursor = start
        for a, b in selected + [(stop, stop)]:
            if cursor < a:
                address = cursor if chip.size == 0x100000 else cursor + 0x80000
                yield address, a - cursor
            cursor = max(cursor, b)


def _validate_checksum_layout(kernel, regions):
    """Check the final image metadata against the native loader's fixed ABI."""
    def read(address, size):
        for _, start, _, data in regions:
            if start <= address and address + size <= start + len(data):
                return data[address - start:address - start + size]
        return kernel.read_memory(address, size)

    for address, maximum in ((0x3EEC, 8), (0x97EE0, 16), (0xA0010, 32)):
        header = read(address, 4)
        count = int.from_bytes(header[2:4], 'little')
        if not 1 <= count <= maximum:
            raise KernelError('Checksum layout is incompatible with the native FL2 RSW loader')
        table = read(address + 4, count * 8)
        for offset in range(0, len(table), 8):
            start, stop = struct.unpack_from('<II', table, offset)
            if not 0 <= start <= stop < 0x100000:
                raise KernelError('Native RSW checksum range is outside external flash')
    # The original RSW reads the boot seed at 3FE6; GKFlasher's format uses 3FE4.
    if read(0x3FE4, 2) != read(0x3FE6, 2):
        raise KernelError('Boot checksum seed is incompatible with the native RSW loader')
    for address, start, stop in ((0xA0496, 0xA0000, 0x100000), (0xA04BA, 0x90000, 0xA0000)):
        pointer = int.from_bytes(read(address, 4), 'little')
        if not start <= pointer < stop - 1:
            raise KernelError('Native RSW checksum seed pointer is outside its flash region')


def run(ecu, mode, regions, key=None, baudrate=None, progress_callback=None, log_callback=None):
    from gkbus.protocol import ccp
    from . import rsw, mtos
    from .ecu import configure_serial_kline
    from .kernel import Kernel, KernelLink, run_session
    from .mtos_kernel import prepare
    from .mtos_asm import mtos_baudrate
    from .progress import Progress

    if key is None or not 0 <= key <= 0xFFFFFFFF:
        raise ValueError('K-Line RSW requires a four-byte Siemens key (--rsw-key)')
    if mode == 'virginize':
        raise ValueError('The native RSW sector table does not define the virginize erase region; K-Line virginize is not supported')
    layout = ecu.regions
    if (ecu.get_eeprom_size_bytes() not in (0x80000, 0x100000)
            or layout['boot1']['write']['size'] != 0x4000
            or layout['boot2']['write'] != {'address': 0x88000, 'size': 0x8000}
            or layout['calibration']['write'] != {'address': 0x90000, 'size': 0x10000}
            or layout['program']['write'] != {'address': 0xA0000, 'size': 0x60000}):
        raise ValueError('K-Line RSW requires the SIMK43 I4 4/8 Mbit memory layout')
    speed = mtos_baudrate(baudrate) if baudrate is not None else None
    ecu.bus._stop_keepalive()
    channel = ecu.bus.transport.channel
    channel.reset_timing()
    configure_serial_kline(channel, p4_min=0 if channel.get_baudrate() == 10400 else 0.001)
    mtos.siemens_unlock(ecu, key, log_callback)
    signature = mtos.read_signature(ecu)
    try:
        channel = mtos.launch_kernel(ecu, signature, mtos_key=key, baudrate=speed,
                                    log_callback=log_callback, progress_callback=progress_callback)
        speed = channel.get_baudrate()
    except BaseException:
        # The RAM handoff may already have happened; do not send KWP cleanup.
        ecu.bus.close()
        raise
    kernel = Kernel(KernelLink(channel), log_callback=log_callback, progress_callback=progress_callback)

    def program(kernel):
        # Identify hardware and qualify its read/write mapping, without using
        # the MTOS flash programming driver for any erase or write operation.
        flash = prepare(kernel, signature)
        if flash.chip.size != ecu.get_eeprom_size_bytes():
            raise KernelError('Detected flash size does not match the selected ECU')
        _validate_checksum_layout(kernel, regions)
        preserved = [(address, kernel.read_memory(address, size))
                     for address, size in _preserved_ranges(flash.chip, regions)]
        transport = CcpOverKLineTransport(kernel, writable=True)
        transport.upload()
        rsw.transport, rsw.ccp_client = transport, ccp.CcpProtocol(transport)
        rsw.log_running(channel, log_callback)
        rsw._flash_regions(mode, regions, progress_callback, log_callback)
        for address, data in preserved:
            rsw.ccp_client.execute(ccp.commands.SetMemoryTransferAddress(0, 0, address))
            rsw.flash_file_6byte('preserved sector data', data, progress_callback, log_callback)
        rsw.ccp_client.execute(ccp.commands.ActionService(0xC310))
        status = rsw.ccp_client.execute(ccp.commands.DataUpload(1)).get_data()[0]
        if status:
            raise KernelError('Native RSW checksum validation failed: 0x{:02X}'.format(status))
        expected = [(address, data) for _, address, _, data in regions] + preserved
        kernel.log('[*] Verifying eeprom...')
        progress = Progress(progress_callback, sum(len(data) for _, data in expected), 'Verifying RSW')
        done = 0
        try:
            for address, data in expected:
                if kernel.read_memory(address, len(data), progress, done=done) != data:
                    raise KernelError('RSW readback differs at 0x{:06X}'.format(address))
                done += len(data)
        finally:
            progress.finish()
        kernel.critical = False
        kernel.log('[+] Native RSW checksums and readback verified')

    try:
        finished = run_session(kernel, program, reset_when_done=True)
        if kernel.ecu_reset:
            finished = mtos.check_after_reset(ecu.bus, ecu, log_callback) and finished
        if not finished and not kernel.ecu_reset:
            kernel.log('[!] Recover with MTOS --mtos-attach at {} baud; keep the ECU powered.'.format(speed))
        kernel.log('[*] Done!' if finished else '[!] RSW did not complete')
        return finished
    finally:
        ecu.bus.close()
