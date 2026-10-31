from gkbus.exceptions import TimeoutException
from gkbus.protocol import kwp2000

from .ecu import ECU
from ecu_definitions import AccessLevel, Routine
# this is garbage prototype code

RSW_ADDRESS = 0xC000
RSW_LENGTH = 0x163

def mtos_handler (ecu: ECU, payload_filename: str, mtos_key: int):
    try:
        with open(payload_filename, 'rb') as f:
            f.seek(RSW_ADDRESS)
            mtos_payload = f.read(RSW_LENGTH)
    except (FileNotFoundError, IOError) as e:
        print(e)
        return
    ecu.bus._stop_keepalive()

    print('[*] default diagnostic session') # otherwise siemens level access is impossible?
    ecu.bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT, ecu.get_desired_baudrate().index))

    print('[*] Siemens security access level')
    mtos_key = mtos_key.to_bytes(4)
    seed = ecu.bus.execute(kwp2000.commands.SecurityAccess().request_seed(access_level=AccessLevel.SIEMENS_0xFD.value)).get_data()[1:]
    print('[*] Seed {}, sending key {}'.format(seed, mtos_key))
    ecu.bus.execute(kwp2000.commands.SecurityAccess().send_key(mtos_key, access_level=AccessLevel.SIEMENS_0xFD.value+1)) # garbage

    print('[*] Writing payload to RAM')

    bytes_written = 0
    while len(mtos_payload) > 0:
        len_bytes_to_write = min(16, len(mtos_payload))
        bytes_to_write = mtos_payload[:len_bytes_to_write]

        # @todo: fix gkbus
        write_memory_address = (RSW_ADDRESS + bytes_written).to_bytes(3, 'big')
        write_memory_data = write_memory_address + len_bytes_to_write.to_bytes(1, 'little') + bytes_to_write
        #	ecu.bus.execute(kwp2000.commands.WriteMemoryByAddress(offset=0x0, data_to_write=[0xFF, 0xFF]).set_data(write_memory_data))

        data = bytes([0xFF, 0xFF])
        cmd = kwp2000.commands.WriteMemoryByAddress(
            offset=0x0,
            data_to_write=data,
        ).set_data(write_memory_data)

        ecu.bus.execute(cmd)

        # print(rsw_bytes[bytes_written:bytes_written+len_bytes_to_write])
        bytes_written += len_bytes_to_write
        mtos_payload = mtos_payload[len_bytes_to_write:]

    print('[*] RSW writen to RAM!')
    print(bytes_written)

    print('[*] executing')
    channel = ecu.bus.transport.channel
    try:
        ecu.bus.transport.set_timeout(0)
        ecu.bus.execute(kwp2000.commands.StartRoutineByLocalIdentifier(Routine.EXECUTE_MTOS.value))
    except TimeoutException:
        pass # to be expected
    channel.reset_buffers()
    channel.write(b'\x00')
    print('[*] listening on serial port')

    try:
        while True:
            try:
                print(channel.read(1, timeout=1))
            except TimeoutException:
                pass
    except KeyboardInterrupt:
        pass