# OpenGK high-speed K-Line

OpenGK firmware with the 125k baud preset and fast KWP echo handler supports:

```powershell
python gkflasher.py --protocol kline --interface COM69 --desired-baudrate 0x05 --read-calibration
```

Serial requests use echo pacing by default, and timing negotiation retains a
10 ms minimum between requests. `--kline-p4-min` remains available for an
explicit override. The updated OpenGK firmware is required for the 125000-baud
preset; unmodified CA663057 uses a different physical rate.

The matching custom boot receiver and gkbus serial burst support also accelerate
`--flash-calibration` and `--flash-program`. GKFlasher checks the receiver's
capability before erasing; ordinary speeds and other firmware keep the normal
write path. The matching custom receiver is required: host pacing alone cannot
fix an older receiver that blocks boot housekeeping at 10400 baud.
After erase, download setup uses the channel's normal turnaround interval;
bulk transfers resume the negotiated timing. This applies equally to GUI and CLI.
Bench transfers reached about 5,500 B/s writing and 5,050 B/s reading at 125k.
KWP completion messages time erase and transfer, stopping before the final
`0x02` verification routine. The result is displayed after verification succeeds.

For MTOS, start at 10400 baud and use `--mtos-mode read-calibration` or choose
`read-calibration` in the GUI's MTOS tab. The stream uses the selected Desired
Baudrate and checks CRC16 before saving. Add `--desired-baudrate 0x06` for 750000
baud; MTOS does not require a firmware high-speed KWP selector.


MTOS `read`, `fullflash` and `flash-calibration` support matching 4/8 Mbit BINs.
The MTOS modes and write options are shared by CLI and GUI. MTOS and BSL write
checkboxes have CLI equivalents: `--skip-backup`, `--force-write`, and
`--no-verify` (also `--skip-verify`).

MTOS also offers `--override-safety-checks` / **Override BIN checks** to allow
unrecognized BIN identifiers. Size, reset-vector and hardware checks remain.
Temporary OpenGK definitions cover complete 512 KiB and 1 MiB images.
