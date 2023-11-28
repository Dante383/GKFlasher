# GKFlasher

CLI flashing tool for SIMK41/43-based vehicles. 

While it has been developed and tested on a Hyundai Tiburon, the basic functionality
should work on any vehicle supporting KWP2000 over K-line or CANbus. Adjusting some 
parameters might be necessary for other vehicles. 

![Reading over CANbus](assets/gkflasher_canbus_read.png)

## Usage 

Connect your K-line or CANbus adapter to your computer and the vehicle (or ECU for bench setup).
Pinouts for the Tiburon can be found on https://opengk.org. 

Launch `python3 gkflasher.py --protocol {canbus/kline} --interface {can0//dev/ttyUSB0}`. If it detects EEPROM size and calibration - all is good, you can proceed! 

### Reading 

Add `--read` to the parameters. By default the output
will be saved to `output_{address start}_to_{address stop}.bin`. You can use `--output {filename}` to override that

### Flashing 

Add `--flash {filename}` to the parameters. GKFlasher will attempt to detect current ECU calibration version 
and the calibration version of the file you're trying to flash before asking you for confirmation.

### Parameters 

`-p --protocol {protocol}` - Currently supported: `canbus` and `kline`

`-b --baudrate {baudrate}`

`-i --interface {interface}`

`-r --read`

`-o --output {filename}` - Filename to save the EEPROM dump

`-f --flash {input filename}`
