"""Kefico/Bosch M7.9.7 programming and RAM-only external-flash reading."""

import struct
import time
import zlib
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path

from alive_progress import alive_bar
from gkbus.protocol import kwp2000
from gkbus.exceptions import TimeoutException

from flasher.ecu import get_m797_calibration_id
from flasher.m797_reboot import prepare_reboot, reboot
from flasher.checksum import m797_correct_image_checksums, m797_correct_calibration_checksums
from flasher.rsw import log # need to relocate
from ecu_definitions import (
    KEFICO_LAYOUT,
    M797_IMAGE_SIZE,
    M797Routine,
    M797_FLASH_START,
    M797_FLASH_END,
    M797_FLASH_BLOCK_SIZE,
    M797_TRANSFER_SIZE,
    M797_RESPONSE_TIMEOUT,
    M797_CHECKSUM_TABLE_OFFSET,
    M797_CHECKSUM_TABLE_ENTRIES,
    M797_CHECKSUM_ENTRY_SIZE,
    M797_MIN_SKIP_SIZE,
    M797_DYNAMIC_READ_CHUNK,
    M797_XC167_PAGE_SIZE,
    M797_PROGRAMMING_PROFILES,
    M797_FLASH_REGIONS,
    M797_SECURITY_TABLE,
    M797_READ_STARTUP_BAUD,
    M797_READ_STARTUP_SELECTOR,
    M797_READ_DESCRIPTOR_ADDRESS,
    M797_READ_STATE_LID,
    M797_READ_DATA_LIDS,
    M797_DESCRIPTOR_RELOCATION_DELTA,
    M797_DYNAMIC_ADDRESS_MODES,
    M797_C166_GENERIC_CANDIDATES,
    M797_C166_RAW_DATA_LIDS,
    M797_C166_RAW_DESC_LIDS,
    M797_C166_RAW_WINDOW_SIZE,
    M797_C166_TABLE_WINDOW_SIZE,
    M797_C166_DESC_WINDOW_SIZE,
    M797_C166_DESCRIPTOR_TYPE_MEMORY,
    M797_LCR_RAW_SLOT_BASE,
    M797_LCR_RAW_DATA_LIDS,
    M797_LCR_RAW_DESC_LIDS,
    M797_LCR_RAW_WINDOW_START,
    M797_LCR_RAW_WINDOW_SIZE,
    M797_LCR_TABLE_WINDOW_START,
    M797_LCR_TABLE_WINDOW_SIZE,
    M797_LCR_DESC_WINDOW_START,
    M797_LCR_DESC_WINDOW_SIZE,
)


class M797ReadCleanupError(RuntimeError):
    """RAM restoration failed; no further architecture probing is safe."""


def _m797_cleanup_ram(callback, *args):
    try:
        callback(*args)
    except Exception as exc:
        raise M797ReadCleanupError(
            'Could not restore M7.9.7 RAM: {}. Power-cycle the ECU before further diagnostics.'.format(exc)
        ) from exc


@contextmanager
def _m797_descriptor_gate(bus, baseline: bytes, spoof: bytes):
    """Restore both gate copies even if a RAM-write acknowledgement is lost."""
    try:
        _m797_write_ram(bus, M797_READ_DESCRIPTOR_ADDRESS, spoof)
        yield
    finally:
        _m797_cleanup_ram(_m797_write_ram, bus, M797_READ_DESCRIPTOR_ADDRESS, baseline)


def _int24(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError('24-bit value out of range: {}'.format(hex(value)))
    return value.to_bytes(3, 'big')


def _hex(data: bytes) -> str:
    return data.hex(' ').upper()


def _m797_set_write_delay(bus, delay: float) -> None:
    """Configure K-Line P3_MIN in milliseconds using the channel timing API."""
    channel = bus.transport.channel
    channel.set_timing(replace(channel.get_timing(), p3_min=delay * 1000))


def _m797_stop_keepalive(bus) -> None:
    # gkbus 0.6.11 has no public pause hook. Joining its keepalive thread
    # prevents TesterPresent from interrupting baud handoff or RAM updates.
    bus._stop_keepalive()


def _page_remaining(address: int) -> int:
    return M797_XC167_PAGE_SIZE - (address & (M797_XC167_PAGE_SIZE - 1))


@contextmanager
def _m797_progress(total: int, title: str, progress_callback=None):
    """Use alive_progress for the CLI, or a caller-supplied incremental callback."""
    if progress_callback is None:
        with alive_bar(
            total,
            unit='B',
            title=title,
            enrich_print=False
        ) as bar:
            yield bar
    else:
        yield progress_callback


def m797_calculate_key(seed: bytes) -> bytes:
    if len(seed) != 4:
        raise ValueError('M7.9.7 security seed must be exactly 4 bytes')

    seed_value = int.from_bytes(seed, 'big')
    key = 0
    working = M797_SECURITY_TABLE[0]

    for bit_index in range(32):
        if seed_value & (1 << bit_index):
            key ^= working

        if bit_index == 31:
            break

        if working & 0x80000000:
            working = M797_SECURITY_TABLE[bit_index + 1]
        else:
            working = (working << 1) & 0xFFFFFFFF

    return (key & 0xFFFFFFFF).to_bytes(4, 'big')


def m797_calculate_recovery_key(seed: bytes) -> bytes:
    if len(seed) != 4:
        raise ValueError('M7.9.7 security seed must be exactly 4 bytes')

    value = int.from_bytes(seed, 'big')
    polynomial = 0xFABC568E

    for _ in range(40):
        if value & 0x80000000:
            value = ((value << 1) & 0xFFFFFFFF) ^ polynomial
        else:
            value = (value << 1) & 0xFFFFFFFF

    return value.to_bytes(4, 'big')


def m797_profile_from_selector(selector: int | None) -> str:
    """Resolve the GKFlasher baud selector; an omitted selector uses standard speed."""
    if selector is None:
        return 'standard'
    for name, profile in M797_PROGRAMMING_PROFILES.items():
        if profile['selector'] == selector:
            return name
    raise ValueError(
        'Unsupported M7.9.7 baudrate selector 0x{:X}. '
        'Use 0x64 (57600) or 0x82 (108800).'.format(selector)
    )


def _m797_choose_profile(operation: str, log_callback=None) -> str:
    while True:
        choice = input('[?] Use fast {} (108800 baud)? [y/N]: '.format(operation)).strip().lower()
        if choice in ('', 'n', 'no', 'y', 'yes'):
            profile = 'fast' if choice in ('y', 'yes') else 'standard'
            selected = M797_PROGRAMMING_PROFILES[profile]
            log('[*] {} speed: {} ({} baud)'.format(
                operation.capitalize(), selected['name'], selected['baudrate']), log_callback)
            return profile
        log('[!] Please answer y or n.', log_callback)


def m797_prepare_flash_session(
    bus,
    programming_profile: str = 'standard'
) -> None:
    """Enter an M7.9.7 programming session at the selected baud rate."""
    profile = _m797_profile(programming_profile)
    bus.transport.set_timeout(M797_RESPONSE_TIMEOUT)

    _m797_stop_keepalive(bus)

    bus.execute(
        kwp2000.commands.AccessTimingParameters()
        .set_timing_parameters_to_given_values(
            0x00, 0x28, 0x00, 0x14, 0x00
        )
    )

    bus.execute(
        kwp2000.commands.StartDiagnosticSession(
            kwp2000.enums.DiagnosticSession.FLASH_REPROGRAMMING,
            profile['selector']
        )
    )

    bus.transport.channel.set_baudrate(profile['baudrate'])
    _m797_set_write_delay(bus, profile['write_delay'])


def m797_security_access(
    bus,
    retry_delay: float = 0.5,
    timeout: float = 7.0,
    log_callback=None,
) -> None:
    request = kwp2000.commands.SecurityAccess().request_seed(0x81).append_data(
        bytes([0x05]) + b'SECURITY'
    )

    deadline = time.monotonic() + timeout
    waiting_message_printed = False

    while True:
        try:
            seed_response = bus.execute(request).get_data()
            break
        except kwp2000.Kwp2000NegativeResponseException as exc:
            if exc.status.identifier != 0x37:
                raise

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    'M7.9.7 security delay did not expire within {:.1f}s'
                    .format(timeout)
                ) from exc

            if not waiting_message_printed:
                log('[*] ECU security delay active, waiting...', log_callback)
                waiting_message_printed = True

            time.sleep(retry_delay)

    if len(seed_response) != 5 or seed_response[0] != 0x81:
        raise ValueError(
            'Unexpected M7.9.7 security seed response: {!r}'
            .format(seed_response)
        )

    seed = seed_response[1:]
    log('[*] M7.9.7 security seed: {}'.format(_hex(seed)), log_callback)

    if seed and not any(seed):
        log('[*] ECU reports zero seed; programming security is already unlocked', log_callback)
        return

    try:
        bus.execute(kwp2000.commands.SecurityAccess().send_key(m797_calculate_key(seed), 130))
        return
    except kwp2000.Kwp2000NegativeResponseException as exc:
        if exc.status.identifier != 0x35:
            raise

    # Blank external flash uses the internal-ROM fallback polynomial.
    log('[*] Normal security key rejected; trying blank-flash recovery key', log_callback)
    bus.execute(kwp2000.commands.SecurityAccess().send_key(m797_calculate_recovery_key(seed), 130))


@contextmanager
def _m797_programming_session(
    bus,
    programming_profile: str = 'standard',
    log_callback=None,
):
    """Enter programming mode and restore the channel timing on completion."""
    channel = bus.transport.channel
    original_timing = channel.get_timing()
    try:
        m797_prepare_flash_session(bus, programming_profile=programming_profile)
        log('[*] Security Access', log_callback)
        m797_security_access(bus, log_callback=log_callback)
        yield
    finally:
        channel.set_timing(original_timing)


def m797_wait_for_routine(
    bus,
    routine_identifier: int,
    poll_delay: float = 0.2,
    timeout: float = 300.0
):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return bus.execute(
                kwp2000.commands.RequestRoutineResultsByLocalIdentifier(
                    routine_identifier
                )
            )
        except kwp2000.Kwp2000NegativeResponseException as exc:
            if exc.status.identifier != 0x23:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError('M7.9.7 routine 0x{:02X} timed out'.format(routine_identifier)) from exc
            time.sleep(poll_delay)


def m797_erase_region(bus, address: int, size: int, log_callback=None) -> None:
    end_address = address + size - 1
    log('[*] Erasing 0x{:06X}-0x{:06X}'.format(address, end_address), log_callback)

    bus.execute(
        kwp2000.commands.StartRoutineByLocalIdentifier(
            M797Routine.ERASE_FLASH.value,
            *_int24(address),
            *_int24(end_address)
        )
    )
    m797_wait_for_routine(bus, M797Routine.ERASE_FLASH.value)


def m797_request_download(bus, address: int, size: int) -> None:
    bus.execute(
        kwp2000.commands.RequestDownload(
            address,
            kwp2000.enums.CompressionType.UNCOMPRESSED,
            kwp2000.enums.EncryptionType.UNENCRYPTED,
            size
        )
    )


def m797_find_nonblank_ranges(data: bytes) -> list[tuple[int, int]]:
    """
    Return aligned write ranges while skipping erased runs >= 0x1000 bytes.
    Small FF gaps remain inside surrounding transfers to avoid excessive
    RequestDownload fragmentation.
    """
    block_size = M797_FLASH_BLOCK_SIZE
    blank_block = b'\xFF' * block_size
    ranges = []
    for start in range(0, len(data), block_size):
        chunk = data[start:start + block_size]
        if chunk == blank_block[:len(chunk)]:
            continue
        end = start + len(chunk)
        if ranges and start - ranges[-1][1] < M797_MIN_SKIP_SIZE:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def _m797_transfer_chunk_sizes(total_size: int) -> list[int]:
    """
    Preserve the current proven packetization behavior: use 240-byte SID 0x36
    payloads, but reshape a final sub-128-byte tail into two larger packets.
    """
    if total_size < 0:
        raise ValueError('Transfer size cannot be negative')

    if total_size == 0:
        return []

    maximum = M797_TRANSFER_SIZE
    minimum_tail = M797_FLASH_BLOCK_SIZE
    full, remainder = divmod(total_size, maximum)

    if remainder == 0:
        return [maximum] * full

    if remainder >= minimum_tail:
        return [maximum] * full + [remainder]

    if full >= 1:
        penultimate = (maximum - minimum_tail) + remainder
        final = minimum_tail

        if not minimum_tail <= penultimate <= maximum:
            raise RuntimeError(
                'Cannot safely split {} bytes into <= {} byte TransferData '
                'packets with a >= {} byte final block'
                .format(total_size, maximum, minimum_tail)
            )

        return [maximum] * (full - 1) + [penultimate, final]

    return [remainder]


def m797_transfer_region(bus, data: bytes, progress_callback=None) -> None:
    offset = 0

    for chunk_size in _m797_transfer_chunk_sizes(len(data)):
        chunk = data[offset:offset + chunk_size]

        if len(chunk) != chunk_size:
            raise RuntimeError(
                'Internal transfer packetizer error at offset 0x{:X}: '
                'wanted {}, got {}'
                .format(offset, chunk_size, len(chunk))
            )

        bus.execute(kwp2000.commands.TransferData(chunk))

        offset += chunk_size

        if progress_callback:
            progress_callback(chunk_size)

    if offset != len(data):
        raise RuntimeError(
            'Transfer packetizer sent {} bytes, expected {}'
            .format(offset, len(data))
        )


def m797_verify_region(bus, address: int, data: bytes, log_callback=None) -> int:
    end_address = address + len(data) - 1
    crc = zlib.crc32(data) & 0xFFFFFFFF

    log('[*] Verifying 0x{:06X}-0x{:06X}, CRC32 {:08X}'.format(address, end_address, crc), log_callback)

    bus.execute(
        kwp2000.commands.StartRoutineByLocalIdentifier(
            M797Routine.VERIFY_FLASH.value,
            *_int24(address),
            *_int24(end_address),
            *crc.to_bytes(4, 'big')
        )
    )
    m797_wait_for_routine(bus, M797Routine.VERIFY_FLASH.value)
    return crc


def m797_flash_region(
    bus,
    address: int,
    data: bytes,
    verify_ranges,
    progress_callback=None,
    log_callback=None,
) -> None:
    m797_erase_region(bus, address, len(data), log_callback=log_callback)

    log('[*] Flashing 0x{:06X}-0x{:06X}'.format(address, address + len(data) - 1), log_callback)

    write_ranges = m797_find_nonblank_ranges(data)
    progressed = 0

    for range_start, range_end in write_ranges:
        payload = data[range_start:range_end]

        if progress_callback and range_start > progressed:
            progress_callback(range_start - progressed)

        m797_request_download(bus, address + range_start, len(payload))
        m797_transfer_region(bus, payload, progress_callback=progress_callback)
        bus.execute(kwp2000.commands.RequestTransferExit())
        progressed = range_end

    if progress_callback and progressed < len(data):
        progress_callback(len(data) - progressed)

    for verify_offset, verify_size in verify_ranges:
        m797_verify_region(
            bus,
            address + verify_offset,
            data[verify_offset:verify_offset + verify_size],
            log_callback=log_callback,
        )


def m797_flash(
    bus,
    filename: str,
    programming_profile: str | None = None,
    confirm: bool = True,
    progress_callback=None,
    calibration_only: bool = False,
    log_callback=None,
    progress_factory=None,
) -> bool:
    """Program a 512 KiB BIN; return whether application reboot was confirmed.

    progress_factory(total) creates a fresh byte-progress callback for each phase.
    """
    log('[*] Loading up {}'.format(filename), log_callback)

    image = Path(filename).read_bytes()
    log('[*] Loaded {} bytes'.format(len(image)), log_callback)

    if len(image) != M797_IMAGE_SIZE:
        raise ValueError(
            'M7.9.7 full flash must be exactly {} bytes (0x{:X})'
            .format(M797_IMAGE_SIZE, M797_IMAGE_SIZE)
        )

    # Always calculate the final flash image ourselves. Tuned files are not
    # trusted to contain valid Bosch/Kefico checksum-table or boot-integrity
    # values. The source BIN on disk remains untouched.
    regions = M797_FLASH_REGIONS
    if calibration_only:
        calibration = KEFICO_LAYOUT['regions']['calibration']['write']
        regions = tuple(region for region in regions
                        if region['address'] == calibration['address']
                        and region['size'] == calibration['size'])
        if len(regions) != 1:
            raise ValueError('Calibration layout does not match a programming region')
        image = m797_correct_calibration_checksums(image, calibration['address'], calibration['size'])
    else:
        image = m797_correct_image_checksums(image, log_callback=log_callback)

    if confirm:
        if input('[?] Ready to flash! Do you wish to continue? [y/n]: ') != 'y':
            log('[!] Aborting!', log_callback)
            return False

    if programming_profile is None:
        programming_profile = _m797_choose_profile('programming', log_callback=log_callback)
    else:
        # Validate the caller-selected profile without prompting.
        _m797_profile(programming_profile)

    def read_for_flash(*args, **kwargs):
        kwargs['progress_callback'] = progress_factory(kwargs['size']) if progress_factory else None
        return m797_read(*args, **kwargs)

    if calibration_only:
        log('[*] Reading ECU checksum table before calibration erase', log_callback)
        try:
            calibration_id = get_m797_calibration_id(bus)
        except (kwp2000.Kwp2000NegativeResponseException, TimeoutException, RuntimeError):
            calibration_id = 'UNKNOWN'
        current_table = read_for_flash(
            bus, calibration_id, output_filename=None,
            read_profile=programming_profile,
            address_start=M797_FLASH_START + M797_CHECKSUM_TABLE_OFFSET,
            size=M797_CHECKSUM_TABLE_ENTRIES * M797_CHECKSUM_ENTRY_SIZE,
            log_callback=log_callback,
        )
        image = m797_correct_calibration_checksums(
            image, calibration['address'], calibration['size'], current_table=current_table,
        )

    _m797_stop_keepalive(bus)

    def read_reboot_ram(bus, lid, address, size):
        _m797_define_dynamic_lid(bus, lid, address, size)
        return _m797_read_dynamic_lid(bus, lid, size, log_callback=log_callback)

    reboot_id = prepare_reboot(
        bus, read_for_flash, read_reboot_ram, _m797_write_ram, _m797_clear_dynamic_lid,
        get_m797_calibration_id,
        read_profile=programming_profile,
        log_callback=log_callback,
    )
    rebooted = False
    if progress_factory:
        progress_callback = progress_factory(sum(region['size'] for region in regions))

    with _m797_programming_session(
        bus,
        programming_profile=programming_profile,
        log_callback=log_callback,
    ):
        for region in regions:
            start = region['bin_offset']
            stop = start + region['size']
            data = image[start:stop]

            log('[*] Flashing BIN 0x{:05X}-0x{:05X} -> ECU 0x{:06X}'.format(
                start,
                stop - 1,
                region['address']
            ), log_callback)

            with _m797_progress(region['size'], 'Flashing M7.9.7', progress_callback) as bar:
                m797_flash_region(
                    bus, region['address'], data, region['verify_ranges'],
                    progress_callback=bar,
                    log_callback=log_callback,
                )

        if reboot_id is not None:
            try:
                reboot(bus, reboot_id if calibration_only else None, log_callback=log_callback)
                rebooted = True
            except Exception as exc:
                log('[!] Flash verified; automatic reboot not confirmed: {}. '
                      'Power-cycle the ECU.'.format(exc), log_callback)
        elif calibration_only:
            log('[*] Ending programming communication', log_callback)
            channel = bus.transport.channel
            try:
                # The current transport does not implement short-header framing.
                # Send [length, StopCommunication, checksum] at programming baud.
                channel.write(b'\x01\x82\x83')
                response = channel.read(3, timeout=bus.transport.timeout)
                if response == b'\x01\xC2\xC3':
                    log('[+] StopCommunication accepted', log_callback)
                else:
                    log('[!] StopCommunication not confirmed: {}'.format(_hex(response)), log_callback)
            except TimeoutException:
                log('[!] No StopCommunication acknowledgement received', log_callback)
        if reboot_id is None:
            log('[*] Flash verified. Power-cycle the ECU to apply changes.', log_callback)

        log('[*] Done!', log_callback)
    return rebooted

# The stock application-side diagnostic implementation can temporarily open
# the external-flash class-4 SID 0x2C path by changing BOTH live runtime
# descriptor copies in RAM from the live calibration-specific CLOSED value
# to that calibration's class-4 descriptor value.  The descriptors are created
# while the spoof is active, then the original eight RAM bytes are restored
# before SID 0x21 retrieves any flash data.
#
# This read path performs no external-flash erase/program operation.


def _m797_profile(profile_name: str):
    try:
        return M797_PROGRAMMING_PROFILES[profile_name]
    except KeyError as exc:
        raise ValueError(
            'Unknown M7.9.7 profile: {}'.format(profile_name)
        ) from exc


def _m797_start_session_86(bus, selector: int):
    return bus.execute(kwp2000.commands.StartDiagnosticSession(
        kwp2000.enums.DiagnosticSession.ENGINEERING, selector
    ))


def _m797_tester_present(bus) -> bytes:
    return bytes(
        bus.execute(
            kwp2000.commands.TesterPresent(
                kwp2000.enums.ResponseType.REQUIRED
            )
        ).get_data()
    )


def _m797_switch_read_baud(bus, profile_name: str, log_callback=None) -> None:
    """
    Enter application session 0x86 and switch from startup 10400 baud to the
    selected read speed.  Standard = 0x64 / 57600; fast = 0x82 / 108800.
    """
    profile = _m797_profile(profile_name)
    bus.transport.set_timeout(M797_RESPONSE_TIMEOUT)

    _m797_stop_keepalive(bus)

    # Keep a conservative host-side delay during the speed handoff.  Once the
    # new baud is proven alive, the bulk reader tries zero host delay.
    _m797_set_write_delay(bus, profile['write_delay'])

    log('[*] Trying to start diagnostic session', log_callback)

    _m797_start_session_86(bus, profile['selector'])

    bus.transport.channel.set_baudrate(profile['baudrate'])
    bus.transport.channel.reset_buffers()
    time.sleep(0.03)

    try:
        _m797_tester_present(bus)
    except Exception as exc:
        # Check whether the ECU failed to change speed before giving up.  Do
        # not perform any RAM write unless communication at the selected baud
        # has first been proven.
        bus.transport.channel.set_baudrate(M797_READ_STARTUP_BAUD)
        bus.transport.channel.reset_buffers()
        time.sleep(0.03)

        try:
            startup_response = _m797_tester_present(bus)
        except Exception:
            raise RuntimeError(
                'M7.9.7 did not respond at requested {} baud or startup '
                '10400 baud after 10 86 {:02X}; power-cycle the ECU before '
                'retrying'
                .format(profile['baudrate'], profile['selector'])
            ) from exc

        raise RuntimeError(
            'M7.9.7 rejected/failed the {} baud read-speed transition; ECU '
            'still responds at 10400 baud ({})'
            .format(profile['baudrate'], _hex(startup_response))
        ) from exc

    # Use the timing values already proven by the previous reader.  If a
    # firmware rejects them at the higher baud, the session defaults remain
    # usable; the read can still continue.
    try:
        bus.execute(
            kwp2000.commands.AccessTimingParameters()
            .set_timing_parameters_to_given_values(
                0x00, 0x28, 0x00, 0x14, 0x00
            )
        )
        log('[+] ECU read timing parameters accepted.', log_callback)
    except kwp2000.Kwp2000NegativeResponseException as exc:
        log(
            '[!] ECU read timing optimization not supported (NRC 0x{:02X}); '
            'using session defaults.'
            .format(exc.status.identifier),
            log_callback,
        )

    # The old stable 10400-baud reader proved that host-side write delay can
    # be zero during reads.  Test that at the selected baud; if it is not
    # stable, fall back to the already-proven programming-profile delay.
    _m797_set_write_delay(bus, 0.0)
    try:
        _m797_tester_present(bus)
        log('[+] Host write delay: 0 ms', log_callback)
    except Exception:
        _m797_set_write_delay(bus, profile['write_delay'])
        bus.transport.channel.reset_buffers()
        time.sleep(0.02)
        _m797_tester_present(bus)
        log('[*] Host write delay fallback: {:.0f} ms'.format(profile['write_delay'] * 1000.0), log_callback)


def _m797_restore_startup_baud(bus, active_profile_name: str, log_callback=None) -> None:
    """Best-effort return from 57.6/108.8 kbaud to the normal 10400 baud."""
    _m797_profile(active_profile_name)

    try:
        _m797_start_session_86(bus, M797_READ_STARTUP_SELECTOR)
    except Exception as exc:
        log('[!] Could not negotiate the return to 10400 baud: {}'.format(exc), log_callback)
        log('[!] ECU flash was not modified; power-cycle the ECU if needed.', log_callback)
        return

    bus.transport.channel.set_baudrate(M797_READ_STARTUP_BAUD)
    bus.transport.channel.reset_buffers()

    _m797_set_write_delay(bus, 0.0)

    time.sleep(0.03)

    try:
        _m797_tester_present(bus)
    except Exception as exc:
        log(
            '[!] Baud-return request was accepted, but final 10400-baud '
            'TesterPresent failed: {}'
            .format(exc),
            log_callback,
        )


def _m797_dynamic_address_bytes(
    address: int,
    address_mode: str
) -> bytes:
    if not 0 <= address <= 0xFFFFFF:
        raise ValueError('Dynamic-LID address must fit in 24 bits')

    if address_mode == 'linear':
        return address.to_bytes(3, 'big')

    if address_mode == 'c166':
        page = address >> 14
        offset = address & 0x3FFF
        return bytes([offset & 255, offset >> 8 & 255, page & 255])

    raise ValueError(
        'Unknown M7.9.7 dynamic address mode: {}'
        .format(address_mode)
    )


def _m797_define_dynamic_lid(
    bus,
    lid: int,
    address: int,
    size: int,
    address_mode: str = 'linear'
):
    if not 1 <= size <= M797_DYNAMIC_READ_CHUNK:
        raise ValueError(
            'Dynamic read size must be 1..0x{:02X}'
            .format(M797_DYNAMIC_READ_CHUNK)
        )

    address_bytes = _m797_dynamic_address_bytes(address, address_mode)

    payload = bytes([
        lid,
        0x04,       # clear existing definition
        0x03,       # define by memory address
        0x01,       # output position 1
        size,
        *address_bytes,
    ])

    return bus.execute(kwp2000.commands.DynamicallyDefineLocalIdentifier(payload))


def _m797_read_dynamic_lid(
    bus,
    lid: int,
    size: int,
    retries: int = 3,
    log_callback=None,
) -> bytes:
    if retries < 1:
        raise ValueError('At least one dynamic-LID read attempt is required')
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = bytes(
                bus.execute(
                    kwp2000.commands.ReadDataByLocalIdentifier(lid)
                ).get_data()
            )

            if len(response) == size + 1 and response[0] == lid:
                return response[1:]

            if len(response) == size:
                return response

            raise RuntimeError(
                'Unexpected dynamic LID 0x{:02X} response length {} '
                '(expected {} or {}, raw={})'
                .format(
                    lid,
                    len(response),
                    size,
                    size + 1,
                    _hex(response)
                )
            )

        except (kwp2000.Kwp2000NegativeResponseException, TimeoutException) as exc:
            last_error = exc
            if attempt >= retries:
                break

            log(
                '[!] LID 0x{:02X} read attempt {}/{} failed: {}; retrying.'
                .format(lid, attempt, retries, exc),
                log_callback,
            )
            time.sleep(0.02)

    raise last_error


def _m797_clear_dynamic_lid(bus, lid: int) -> None:
    bus.execute(kwp2000.commands.DynamicallyDefineLocalIdentifier(bytes([lid, 0x04])))


def _m797_write_ram(bus, address: int, data: bytes) -> bytes:
    """Direct application-side SID 0x3D RAM write."""
    return bytes(bus.execute(kwp2000.commands.WriteMemoryByAddress(address, data)).get_data())


def _m797_read_descriptor_pair(
    bus,
    address_mode: str,
    log_callback=None,
) -> bytes:
    _m797_define_dynamic_lid(
        bus,
        M797_READ_STATE_LID,
        M797_READ_DESCRIPTOR_ADDRESS,
        8,
        address_mode=address_mode
    )
    return _m797_read_dynamic_lid(bus, M797_READ_STATE_LID, 8, log_callback=log_callback)


def _m797_validate_descriptor_pair(baseline: bytes) -> None:
    if len(baseline) != 8:
        raise RuntimeError(
            'Expected 8 descriptor bytes at E048:E04F, got {}'
            .format(len(baseline))
        )

    if baseline[:4] != baseline[4:8]:
        raise RuntimeError(
            'Runtime descriptor copies are not identical: {}'
            .format(_hex(baseline))
        )

def _m797_detect_dynamic_address_mode(bus, log_callback=None):
    """
    Detect the stock SID 0x2C address parser without using calibration ID.

    A candidate is accepted only if F0 can define/read E048:E04F and the
    returned eight bytes form two identical 4-byte runtime descriptor copies.
    """
    errors = []

    for address_mode in M797_DYNAMIC_ADDRESS_MODES:
        try:
            baseline = _m797_read_descriptor_pair(bus, address_mode, log_callback=log_callback)
            _m797_validate_descriptor_pair(baseline)
        except Exception as exc:
            errors.append((address_mode, exc))
            log('[*] {} address mode rejected: {}'.format(address_mode, exc), log_callback)
            continue

        return address_mode, baseline

    raise RuntimeError(
        'Could not establish a usable stock SID 0x2C dynamic-memory parser. '
        'Tried {}. This ECU may belong to an older parser family that still '
        'requires RAM-side replacements for the historical normalization/'
        'page-high/slot fixes. Details: {}'
        .format(
            ', '.join(M797_DYNAMIC_ADDRESS_MODES),
            '; '.join(
                '{}={}'.format(mode, exc)
                for mode, exc in errors
            )
        )
    )


def _m797_c166_raw_slot_address(lid: int, raw_slot_base: int) -> int:
    if not 0 <= lid <= 0xFF:
        raise ValueError('C166 raw LID must fit in one byte')
    return raw_slot_base + (lid * 6)


def _m797_c166_window_addresses(raw_slot_base: int):
    table = _m797_c166_raw_slot_address(M797_C166_RAW_DATA_LIDS[0], raw_slot_base)
    return table, table + M797_C166_TABLE_WINDOW_SIZE


def _m797_c166_far_pointer_words(address: int):
    if not 0 <= address <= 0xFFFFFF:
        raise ValueError('C166 far pointer address must fit in 24 bits')

    page = address >> 14
    offset = address & 0x3FFF

    return offset, page


def _m797_c166_descriptor(address: int, size: int) -> bytes:
    offset, page = _m797_c166_far_pointer_words(address)
    return struct.pack('<BBHH', M797_C166_DESCRIPTOR_TYPE_MEMORY, size, offset, page)


def _m797_lcr_descriptor(address: int, size: int) -> bytes:
    if not 0 <= address <= 0xFFFFFF:
        raise ValueError('LCR address must fit in 24 bits')
    return struct.pack('<BBHH', size, 0, address & 0xFFFF, address >> 16)


def _m797_build_c166_direct_window(definitions, raw_slot_base: int) -> bytes:
    """F0..F4: six-byte tables; F5..F9: type/size/offset/page descriptors."""
    window_start = _m797_c166_raw_slot_address(M797_C166_RAW_DATA_LIDS[0], raw_slot_base)
    window = bytearray(M797_C166_RAW_WINDOW_SIZE)
    for index, (lid, address, size) in enumerate(definitions):
        if lid not in M797_C166_RAW_DATA_LIDS:
            raise ValueError('Direct C166 reader only supports F0..F4 data LIDs')
        if not 1 <= size <= M797_DYNAMIC_READ_CHUNK:
            raise ValueError('C166 descriptor size must be 1..0xF0')
        if size > _page_remaining(address):
            raise ValueError('C166 direct descriptor crosses a 0x4000 page boundary')
        descriptor = _m797_c166_raw_slot_address(M797_C166_RAW_DESC_LIDS[index], raw_slot_base)
        offset, page = _m797_c166_far_pointer_words(descriptor)
        table_offset = _m797_c166_raw_slot_address(lid, raw_slot_base) - window_start
        descriptor_offset = descriptor - window_start
        window[table_offset:table_offset + 6] = struct.pack('<HHH', 1, offset, page)
        window[descriptor_offset:descriptor_offset + 6] = _m797_c166_descriptor(address, size)
    return bytes(window)


def _m797_install_c166_direct_window(
    bus,
    window: bytes,
    raw_slot_base: int
) -> None:
    if len(window) != M797_C166_RAW_WINDOW_SIZE:
        raise ValueError(
            'Expected {} C166 raw-window bytes, got {}'
            .format(M797_C166_RAW_WINDOW_SIZE, len(window))
        )

    table_address, descriptor_address = _m797_c166_window_addresses(raw_slot_base)

    table_bytes = window[:M797_C166_TABLE_WINDOW_SIZE]
    descriptor_bytes = window[
        M797_C166_TABLE_WINDOW_SIZE:
        M797_C166_TABLE_WINDOW_SIZE + M797_C166_DESC_WINDOW_SIZE
    ]

    # Descriptor records first; activate F0..F4 only after they are complete.
    _m797_write_ram(bus, descriptor_address, descriptor_bytes)

    _m797_write_ram(bus, table_address, table_bytes)


def _m797_clear_c166_direct_window(bus, raw_slot_base: int) -> None:
    table_address, descriptor_address = _m797_c166_window_addresses(raw_slot_base)

    # Disable F0..F4 before clearing their descriptor records.
    _m797_write_ram(bus, table_address, bytes(M797_C166_TABLE_WINDOW_SIZE))
    _m797_write_ram(bus, descriptor_address, bytes(M797_C166_DESC_WINDOW_SIZE))


def _m797_probe_direct_layout(bus, raw_f0, raw_f1, table, descriptor, table_size, log_callback=None):
    """Prove adjacent 16-byte reads, then disable F0 before clearing its scratch descriptor."""
    table_dirty = descriptor_dirty = False
    try:
        descriptor_dirty = True
        _m797_write_ram(bus, raw_f1, descriptor(M797_FLASH_START, 16))
        table_dirty = True
        _m797_write_ram(bus, raw_f0, table)
        for address in (M797_FLASH_START, M797_FLASH_START + 16):
            if address != M797_FLASH_START:
                _m797_write_ram(bus, raw_f1, descriptor(address, 16))
            data = _m797_read_dynamic_lid(bus, 0xF0, 16, retries=1, log_callback=log_callback)
            log('[+] 0x{:06X}: {}'.format(address, _hex(data)), log_callback)
    finally:
        if table_dirty:
            _m797_cleanup_ram(_m797_write_ram, bus, raw_f0, bytes(table_size))
        if descriptor_dirty:
            _m797_cleanup_ram(_m797_write_ram, bus, raw_f1, bytes(6))


def _m797_c166_generic_preflight(bus, candidate, log_callback=None):
    # The caller preserves the descending-base order to protect lower-base count fields.
    base = candidate['raw_slot_base']
    raw_f0 = _m797_c166_raw_slot_address(0xF0, base)
    raw_f1 = _m797_c166_raw_slot_address(0xF1, base)
    offset, page = _m797_c166_far_pointer_words(raw_f1)
    log('[*] Trying {}'.format(candidate['description']), log_callback)
    _m797_probe_direct_layout(
        bus, raw_f0, raw_f1, struct.pack('<HHH', 1, offset, page), _m797_c166_descriptor, 6,
        log_callback=log_callback,
    )
    return candidate


def _m797_read_batches(address, size, lids, clip_pages=True):
    """Yield ordered LID definitions, respecting C166 page boundaries when required."""
    end = address + size
    while address < end:
        definitions = []
        for lid in lids:
            if address >= end:
                break
            chunk_size = min(M797_DYNAMIC_READ_CHUNK, end - address)
            if clip_pages:
                chunk_size = min(chunk_size, _page_remaining(address))
            definitions.append((lid, address, chunk_size))
            address += chunk_size
        yield definitions


def _m797_read_direct(bus, result, bar, address_start, size, lids,
                  build_window, install_window, clear_window, clip_pages, log_callback=None):
    dirty = False
    try:
        for definitions in _m797_read_batches(address_start, size, lids, clip_pages):
            window = build_window(definitions)
            dirty = True
            install_window(window)
            for lid, address, chunk_size in definitions:
                chunk = _m797_read_dynamic_lid(bus, lid, chunk_size, log_callback=log_callback)
                result.extend(chunk)
                bar(chunk_size)
    finally:
        if dirty:
            _m797_cleanup_ram(clear_window)


def _m797_read_flash_c166_direct(bus, result, bar, profile,
                             address_start=M797_FLASH_START, size=M797_IMAGE_SIZE, log_callback=None):
    raw_slot_base = profile['raw_slot_base']
    _m797_read_direct(
        bus, result, bar, address_start, size, M797_C166_RAW_DATA_LIDS,
        partial(_m797_build_c166_direct_window, raw_slot_base=raw_slot_base),
        partial(_m797_install_c166_direct_window, bus, raw_slot_base=raw_slot_base),
        partial(_m797_clear_c166_direct_window, bus, raw_slot_base),
        clip_pages=True,
        log_callback=log_callback,
    )


def _m797_lcr_raw_slot_address(lid: int) -> int:
    if not 0 <= lid <= 0xFF:
        raise ValueError('LCR raw LID must fit in one byte')
    return M797_LCR_RAW_SLOT_BASE + (lid * 8)


def _m797_build_lcr_direct_window(definitions) -> bytes:
    """F0..F4: eight-byte tables; F5..F9: size/padding/linear-address descriptors."""
    window = bytearray(M797_LCR_RAW_WINDOW_SIZE)
    for index, (lid, address, size) in enumerate(definitions):
        if lid not in M797_LCR_RAW_DATA_LIDS:
            raise ValueError('Direct LCR reader only supports F0..F4 data LIDs')
        if not 1 <= size <= M797_DYNAMIC_READ_CHUNK:
            raise ValueError('LCR descriptor size must be 1..0xF0')
        descriptor = _m797_lcr_raw_slot_address(M797_LCR_RAW_DESC_LIDS[index])
        table_offset = _m797_lcr_raw_slot_address(lid) - M797_LCR_RAW_WINDOW_START
        descriptor_offset = descriptor - M797_LCR_RAW_WINDOW_START
        window[table_offset:table_offset + 8] = struct.pack(
            '<HHHH', 1, descriptor & 0xFFFF, descriptor >> 16, 0,
        )
        window[descriptor_offset:descriptor_offset + 8] = _m797_lcr_descriptor(address, size) + b'\x00\x00'
    return bytes(window)


def _m797_install_lcr_direct_window(bus, window: bytes) -> None:
    if len(window) != M797_LCR_RAW_WINDOW_SIZE:
        raise ValueError(
            'Expected {} LCR raw-window bytes, got {}'
            .format(M797_LCR_RAW_WINDOW_SIZE, len(window))
        )

    table_bytes = window[:M797_LCR_TABLE_WINDOW_SIZE]
    descriptor_bytes = window[
        M797_LCR_TABLE_WINDOW_SIZE:
        M797_LCR_TABLE_WINDOW_SIZE + M797_LCR_DESC_WINDOW_SIZE
    ]

    # Descriptors first; active tables second.
    _m797_write_ram(bus, M797_LCR_DESC_WINDOW_START, descriptor_bytes)

    _m797_write_ram(bus, M797_LCR_TABLE_WINDOW_START, table_bytes)


def _m797_clear_lcr_direct_window(bus) -> None:
    # Disable F0..F4 before clearing descriptor storage.
    _m797_write_ram(bus, M797_LCR_TABLE_WINDOW_START, bytes(M797_LCR_TABLE_WINDOW_SIZE))
    _m797_write_ram(bus, M797_LCR_DESC_WINDOW_START, bytes(M797_LCR_DESC_WINDOW_SIZE))


def _m797_lcr_generic_preflight(bus, log_callback=None):
    raw_f0 = _m797_lcr_raw_slot_address(0xF0)
    raw_f1 = _m797_lcr_raw_slot_address(0xF1)
    table = struct.pack('<HHHH', 1, raw_f1 & 0xFFFF, raw_f1 >> 16, 0)
    log('[*] Trying c166_lcr eight-byte table / linear descriptor', log_callback)
    _m797_probe_direct_layout(bus, raw_f0, raw_f1, table, _m797_lcr_descriptor, 8, log_callback=log_callback)


def _m797_read_flash_lcr_direct(bus, result, bar,
                            address_start=M797_FLASH_START, size=M797_IMAGE_SIZE, log_callback=None):
    # LCR's linear copy loop carries into the high word across 16 KiB pages.
    _m797_read_direct(
        bus, result, bar, address_start, size, M797_LCR_RAW_DATA_LIDS,
        _m797_build_lcr_direct_window,
        partial(_m797_install_lcr_direct_window, bus),
        partial(_m797_clear_lcr_direct_window, bus),
        clip_pages=False,
        log_callback=log_callback,
    )


def _m797_build_descriptor_spoof(
    baseline: bytes,
    class4_word: int
) -> bytes:
    # Preserve the ECU's live page word exactly; only the classifier word is
    # changed, and both runtime copies are always written atomically.
    half = int(class4_word & 0xFFFF).to_bytes(2, 'little') + baseline[2:4]
    return half + half

def _m797_patchless_read_preflight(bus, baseline: bytes, address_mode: str, log_callback=None):
    """Prove the paired RAM gate; return (None, None) if flash is already readable."""
    _m797_write_ram(bus, M797_READ_DESCRIPTOR_ADDRESS, baseline)
    if _m797_read_descriptor_pair(bus, address_mode, log_callback=log_callback) != baseline:
        raise M797ReadCleanupError('Descriptor changed during the no-op proof; power-cycle the ECU')
    lid = M797_READ_DATA_LIDS[0]
    try:
        _m797_define_dynamic_lid(bus, lid, M797_FLASH_START, 16, address_mode=address_mode)
    except kwp2000.Kwp2000NegativeResponseException as exc:
        if exc.status.identifier != 0x12:
            raise
    else:
        data = _m797_read_dynamic_lid(bus, lid, 16, log_callback=log_callback)
        log('[!] Flash is already readable without a RAM spoof: {}'.format(_hex(data)), log_callback)
        log('[!] Any existing persistent read patch will remain present in the dump.', log_callback)
        return None, None

    word = (int.from_bytes(baseline[:2], 'little') + M797_DESCRIPTOR_RELOCATION_DELTA) & 0xFFFF
    spoof = _m797_build_descriptor_spoof(baseline, word)
    with _m797_descriptor_gate(bus, baseline, spoof):
        _m797_define_dynamic_lid(bus, lid, M797_FLASH_START, 16, address_mode=address_mode)
    restored = _m797_read_descriptor_pair(bus, address_mode, log_callback=log_callback)
    if restored != baseline:
        raise M797ReadCleanupError(
            'Descriptor restoration mismatch: expected {}, got {}. Power-cycle the ECU.'.format(
                _hex(baseline), _hex(restored)))
    _m797_read_dynamic_lid(bus, lid, 16, log_callback=log_callback)
    return spoof, word


def m797_read(
    bus,
    calibration_id: str,
    output_filename: str | None = 'm797_read.bin',
    progress_callback=None,
    read_profile: str | None = None,
    address_start: int = M797_FLASH_START,
    size: int = M797_IMAGE_SIZE,
    log_callback=None,
) -> bytes:
    """
    Read an external-flash range, defaulting to the complete 512 KiB image.

    Standard read: 57.6 kbaud (selector 0x64)
    Fast read:     108.8 kbaud (selector 0x82)

    Auto-detected read architectures:

      1) Native/linear-style application parser:
           SID 0x2C + paired E048:E04F RAM spoof + SID 0x21

      2) C166 six-byte descriptor parser:
           probed raw F0..F9 table base + six-byte descriptors
           in 0x38xxxx RAM, using SID 0x3D then SID 0x21

      3) C166 LCR parser:
           8-byte raw table + linear six-byte descriptor

    Direct-RAM layouts are architecture-probed independently of calibration ID.
    None of these paths writes or erases external flash.
    Pass output_filename=None to return the requested bytes without saving a BIN.
    """
    if size <= 0 or address_start < M797_FLASH_START or address_start + size > M797_FLASH_END + 1:
        raise ValueError('Requested read range is outside M7.9.7 external flash')

    requested_size = size

    calibration_id = (calibration_id or 'UNKNOWN').strip().upper()

    if output_filename == '':
        output_filename = 'm797_read.bin'

    if read_profile is None:
        read_profile = _m797_choose_profile('read', log_callback=log_callback)
    profile = _m797_profile(read_profile)


    result = bytearray()
    baseline = None
    spoof = None
    native_parser = False
    channel = bus.transport.channel
    original_timing = channel.get_timing()
    read_method = None
    c166_profile = None

    with _m797_progress(requested_size, 'Reading M7.9.7', progress_callback) as bar:
        try:
            _m797_switch_read_baud(bus, read_profile, log_callback=log_callback)

            # Method A: native dynamic-LID parser + paired runtime gate.


            try:
                address_mode, baseline = _m797_detect_dynamic_address_mode(bus, log_callback=log_callback)
            except Exception as native_exc:
                log('[*] Native dynamic-LID method unavailable: {}'.format(native_exc), log_callback)
                address_mode = None
            else:
                native_parser = True

                spoof, _ = _m797_patchless_read_preflight(
                    bus,
                    baseline,
                    address_mode,
                    log_callback=log_callback,
                )

                read_method = 'native'


            if read_method is None:
                log('[*] METHOD B - probing direct-RAM architectures', log_callback)

                # LCR first: its table lives well away from the six-byte c166
                # tables and wrong-family probes have so far failed cleanly.
                try:
                    _m797_lcr_generic_preflight(bus, log_callback=log_callback)
                except M797ReadCleanupError:
                    raise
                except Exception as exc:
                    log('[*] LCR candidate rejected: {}'.format(exc), log_callback)
                else:
                    read_method = 'lcr_direct'

            if read_method is None:
                # Then the c166_four layouts in deliberately descending-base
                # order: 0x8044, 0x803A, then ADE-style 0x8036.
                for candidate in M797_C166_GENERIC_CANDIDATES:
                    try:
                        c166_profile = _m797_c166_generic_preflight(bus, candidate, log_callback=log_callback)
                    except M797ReadCleanupError:
                        raise
                    except Exception as exc:
                        log('[*] {} candidate rejected: {}'.format(candidate['name'], exc), log_callback)
                        continue

                    read_method = 'c166_direct'
                    break

            if read_method is None:
                raise RuntimeError(
                    'None of the known patchless read architectures matched '
                    'calibration {}.'
                    .format(calibration_id)
                )

            if read_method == 'native':
                log('[+] Read architecture: native SID 0x2C + SID 0x21', log_callback)
            elif read_method == 'c166_direct':
                log('[+] Read architecture: c166_four raw RAM descriptor + SID 0x21', log_callback)
            else:
                log('[+] Read architecture: c166_lcr linear RAM descriptor + SID 0x21', log_callback)

            log('[*] Reading M7.9.7 0x{:06X}-0x{:06X} ({} bytes, {} baud)'.format(
                address_start, address_start + size - 1, size, profile['baudrate']), log_callback)

            if read_method == 'c166_direct':
                _m797_read_flash_c166_direct(
                    bus,
                    result,
                    bar,
                    c166_profile,
                    address_start=address_start,
                    size=size,
                    log_callback=log_callback,
                )

            elif read_method == 'lcr_direct':
                _m797_read_flash_lcr_direct(
                    bus,
                    result,
                    bar,
                    address_start=address_start,
                    size=size,
                    log_callback=log_callback,
                )

            else:
                for batch_index, definitions in enumerate(
                    _m797_read_batches(address_start, size, M797_READ_DATA_LIDS), start=1
                ):
                    gate = _m797_descriptor_gate(bus, baseline, spoof) if spoof is not None else nullcontext()
                    with gate:
                        for lid, chunk_address, size in definitions:
                            _m797_define_dynamic_lid(
                                bus, lid, chunk_address, size, address_mode=address_mode,
                            )

                    for lid, chunk_address, size in definitions:
                        chunk = _m797_read_dynamic_lid(bus, lid, size, log_callback=log_callback)

                        result.extend(chunk)
                        bar(size)

                    if batch_index == 1 or (batch_index % 32) == 0:
                        current = _m797_read_dynamic_lid(bus, M797_READ_STATE_LID, 8, log_callback=log_callback)

                        if current != baseline:
                            raise RuntimeError(
                                'Runtime descriptor drift after batch {}: '
                                'expected {}, got {}'
                                .format(
                                    batch_index,
                                    _hex(baseline),
                                    _hex(current)
                                )
                            )

            if len(result) != requested_size:
                raise RuntimeError(
                    'Final dump size is {}, expected {}'
                    .format(len(result), requested_size)
                )

            if output_filename is not None:
                # Partial files retain full-image BIN offsets; in-memory reads return only the requested bytes.
                image = bytearray(b'\xFF' * M797_IMAGE_SIZE)
                bin_offset = address_start - M797_FLASH_START
                image[bin_offset:bin_offset + requested_size] = result
                output_path = Path(output_filename).resolve()
                output_path.write_bytes(image)
                log('[*] saved to {}'.format(output_path), log_callback)
                log('[*] Done!', log_callback)

            return bytes(result)

        finally:
            if native_parser:
                for lid in (
                    M797_READ_STATE_LID,
                    *M797_READ_DATA_LIDS
                ):
                    try:
                        _m797_clear_dynamic_lid(bus, lid)
                    except Exception as exc:
                        log('[!] Could not clear dynamic LID 0x{:02X}: {}'.format(lid, exc), log_callback)

            try:
                if channel.get_baudrate() != M797_READ_STARTUP_BAUD:
                    _m797_restore_startup_baud(bus, read_profile, log_callback=log_callback)
            finally:
                channel.set_timing(original_timing)
