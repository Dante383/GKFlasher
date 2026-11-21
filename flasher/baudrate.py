"""Read-only SIMK K-Line baud discovery, independent of firmware identifiers."""
import time

from gkbus.protocol import kwp2000

from .ecu import DesiredBaudrate, enable_security_access


def _discover_baudrate(ecu, log):
    """Select the fastest validated candidate from an unlocked 10400-baud session.

    Keep default ECU timing until discovery finishes: failed handoffs must expire
    after the normal inactivity window, rather than the negotiated long timeout.
    No programming, reset or firmware-specific memory access is used.
    """
    bus = ecu.bus
    channel = bus.transport.channel
    session = kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING
    startup = DesiredBaudrate(None, 10400)
    if channel.get_baudrate() != startup.baudrate:
        raise ValueError('Automatic baud discovery requires a 10400-baud session')
    address = ecu.get_region('calibration').read.address + 0x10

    def read_sample():
        data = bytearray()
        for offset in range(0, 1024, 254):
            command = kwp2000.commands.ReadMemoryByAddress(address + offset, min(254, 1024 - offset))
            # Retries are disabled by default: a recovered timeout must not qualify a baudrate.
            part = bytes(bus.execute(command).get_data())
            if len(part) != min(254, 1024 - offset):
                raise ValueError('Incomplete baud discovery read')
            data.extend(part)
        return bytes(data)

    def restore_startup():
        try:
            # Retain the diagnostic connection between probes. StopCommunication
            # followed by immediate fast init causes a delay on stock CA663056.
            bus.execute(kwp2000.commands.StartDiagnosticSession(kwp2000.enums.DiagnosticSession.DEFAULT))
            channel.set_baudrate(10400)
            channel.reset_buffers()
            bus.execute(kwp2000.commands.StartDiagnosticSession(session))
            enable_security_access(bus)
            if read_sample() != baseline:
                raise ValueError('Startup read differs after baud discovery')
            return
        except (IOError, ValueError):
            log('[*] Waiting for the diagnostic session to expire')
        # No traffic during recovery: repeated requests can keep the old session alive.
        time.sleep(6)
        channel.set_baudrate(10400)
        channel.reset_buffers()
        bus.init(kwp2000.commands.StartCommunication())
        bus._stop_keepalive()
        bus.execute(kwp2000.commands.StartDiagnosticSession(session))
        enable_security_access(bus)
        if read_sample() != baseline:
            raise RuntimeError('Could not restore startup communication after baud discovery')

    log('[*] Discovering K-Line baudrate')
    try:
        baseline = read_sample()
    except kwp2000.Kwp2000NegativeResponseException as exc:
        # Recovery/identification operations may lack readable calibration data.
        # Leave them at their known startup speed; never guess a faster rate.
        log('[!] Baud discovery unavailable: {}. Keeping 10400 baud.'.format(exc))
        return startup

    # Generic selector/rate candidates, not a firmware lookup. The same selector
    # can produce different physical rates on different UART clock configurations.
    candidates = ((6, (750000,)), (5, (125000, 107143)),
                  (4, (57692,)), (3, (39474, 37500)), (2, (19231,)))
    for selector, rates in candidates:
        handoff_accepted = False
        try:
            requested = DesiredBaudrate(selector, rates[0])
            bus.execute(requested.session_command(session))
            handoff_accepted = True
            for rate in rates:
                channel.set_baudrate(rate)
                channel.reset_buffers()
                try:
                    bus.execute(kwp2000.commands.TesterPresent(kwp2000.enums.ResponseType.REQUIRED))
                except IOError:
                    continue
                enable_security_access(bus)
                if any(read_sample() != baseline for _ in range(2)):
                    raise ValueError('Calibration differs at candidate baudrate')
                log('[+] Automatically selected {} baud'.format(rate))
                # Retain the rate actually proven on the wire, including when a
                # preset's nominal rate differs from its physical UART divider.
                return DesiredBaudrate(selector, rate)
        except kwp2000.Kwp2000NegativeResponseException:
            # An explicitly rejected request never changed the UART. Keep the
            # working startup session instead of reopening and rereading it.
            if not handoff_accepted:
                continue
        except (IOError, ValueError):
            pass
        restore_startup()
    log('[+] Automatically selected 10400 baud')
    return startup


def discover_baudrate(ecu, log=print):
    """Pause keepalive for baud handoffs; resume only after a proven session."""
    bus = ecu.bus
    thread = getattr(bus, '_keepalive_thread', None)
    restart_keepalive = thread is not None and thread.is_alive()
    bus._stop_keepalive()
    selected = _discover_baudrate(ecu, log)
    if restart_keepalive:
        bus._start_keepalive()
    return selected
