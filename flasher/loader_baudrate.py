"""Firmware-independent UART qualification for the shared RAM kernel."""
import struct
import time

from .kernel import ACK, C_CALL, DONE, VERIFY_FAILED, Kernel, KernelLink, KernelError, read_block_size
from .mtos.asm import _assemble

STARTUP_BAUDRATE = 57600
LOADER_BAUDRATES = (750000, 375000, 250000, 125000, 107143, 83333, 57692, 38462, 19231, 10417)


def _selected(kernel, rate):
    kernel.log('[+] Automatically selected {} baud for the RAM loader'.format(rate))
    callback = getattr(kernel, 'progress_callback', None)
    if callback is not None:
        callback.emit((rate, 0, 'baudrate'))
    return rate


def locate_loader(channel, log_callback=None):
    """Identify ROM/kernel at a known entry rate, without blind UART scanning."""
    channel.reset_buffers()
    kernel = Kernel(KernelLink(channel), log_callback=log_callback)
    kernel.link.send(b'\x00')
    response = kernel.link.read(1, 0.3)
    extra = kernel.link.drain(quiet=0.02)
    if not extra and response in (b'\xA5', b'\xC5', b'\xD5'):
        return kernel, response
    if response and response[0] in (ACK, DONE, VERIFY_FAILED) and kernel.test_comm():
        return kernel, None
    raise KernelError('No loader at the entry rate; select an existing kernel\'s baudrate explicitly')


def _helper(kernel):
    image = _assemble('baud_probe').image()
    if image.base != 0xF600 or image.base + len(image.data) > 0xF800:
        raise KernelError('Baud probe exceeds its RAM allocation')
    kernel.write_memory(image.base, image.data)
    if kernel.read_memory(image.base, len(image.data)) != image.data:
        raise KernelError('Baud probe RAM readback failed')
    return image


def _start(kernel, helper, rate, baseline, commit):
    # Boot-ROM sessions may use BRS=1; MTOS normally enters with BRS=0.
    # S0BG itself is not a reliable copy of the configured reload.
    entry_clock = 500000 if kernel.read_word(0xFFB0) & 0x2000 else 750000
    kernel._command(C_CALL, 'UART baud probe')
    registers = [round(750000 / rate) - 1, 0, int(commit), round(entry_clock / baseline) - 1, 0, 0, 0, 0]
    kernel.link.send(helper.base.to_bytes(3, 'little') + struct.pack('<8H', *registers))
    kernel.link.channel.set_baudrate(rate)
    kernel.link.channel.reset_buffers()


def _reply(kernel, data=b''):
    if len(data) < 17:
        data += kernel._expect(17 - len(data), 2, 'UART baud probe')
    if data[-1] != DONE:
        raise KernelError('UART baud probe did not return to the kernel')
    return struct.unpack('<8H', data[:16])


def _finish_trial(kernel):
    # A complete loopback means the UART is already returning to entry speed.
    # Retry only the harmless acknowledgement if the helper is still finishing;
    # never replay a partial reply or a programming command.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        kernel.link.send(b'\xA5')
        data = kernel.link.read(17, 0.2)
        if data:
            return _reply(kernel, data)
    raise KernelError('UART baud probe did not acknowledge restoration')


def restore_loader_baudrate(kernel, rate=STARTUP_BAUDRATE):
    """Leave automatic BSL sessions at their known reconnect rate, without reset."""
    helper = _helper(kernel)
    _start(kernel, helper, rate, kernel.link.channel.get_baudrate(), True)
    if _reply(kernel)[0] or not kernel.test_comm():
        raise KernelError('Could not restore the loader entry baudrate')
    kernel.baudrate = rate
    kernel.block_size = read_block_size(rate)
    kernel.log('[*] Kernel ready for the next automatic operation at {} baud'.format(rate))


def discover_loader_baudrate(kernel):
    """Round-trip data at each trial rate, restoring UART before every verdict.

    Only commit a passed rate; no reset, flash operation or firmware lookup.
    The helper is temporary and precedes flash drivers that reuse internal RAM.
    """
    channel, link = kernel.link.channel, kernel.link
    baseline = channel.get_baudrate()
    helper = _helper(kernel)
    pattern = bytes(value ^ 0x55 for value in range(256))

    kernel.log('[*] Discovering RAM-loader baudrate')
    for rate in LOADER_BAUDRATES:
        if rate < baseline:
            continue
        _start(kernel, helper, rate, baseline, False)
        started = time.monotonic()
        received = b''
        interrupted = False
        try:
            time.sleep(0.4)  # helper switches after the host has changed its UART
            link.send(pattern)
            received = link.read(len(pattern), 0.8)
        except (IOError, KernelError):
            pass
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # The trial restores its saved UART independently of host reception.
            if received != pattern or interrupted:
                time.sleep(max(0, 2 - (time.monotonic() - started)))
            channel.set_baudrate(baseline)
            channel.reset_buffers()
        result = _finish_trial(kernel)
        if interrupted:
            raise KeyboardInterrupt
        if result[0] or received != pattern:
            continue
        _start(kernel, helper, rate, baseline, True)
        if _reply(kernel)[0]:
            raise KernelError('Qualified UART handoff failed')
        kernel.baudrate = rate
        kernel.block_size = read_block_size(rate)
        if not kernel.test_comm() or kernel.read_memory(helper.base, len(helper.data)) != helper.data:
            raise KernelError('Kernel readback failed after UART handoff at {} baud'.format(rate))
        return _selected(kernel, rate)
    return _selected(kernel, baseline)
