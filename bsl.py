import os
import serial
import serial.tools
import serial.tools.list_ports
import time
import sys
import traceback
import logging

# Logger configuration
logger = logging.getLogger("bsl")
logger.setLevel(logging.DEBUG)

class GuiLogHandler(logging.Handler, logging.Formatter):
    """Log handler for directing logs to the GUI"""
    def __init__(self, gui_callback):
        super().__init__()
        self.gui_callback = gui_callback

    def emit(self, record):
        message = self.format(record)
        self.gui_callback(message)

    def format(self, record):
        if not getattr(record, "suppress_format", False):
            if record.levelno == logging.INFO:
                record.msg = f"[*] {record.msg}"
            elif record.levelno == logging.WARNING:
                record.msg = f"[!] {record.msg}"
            elif record.levelno == logging.ERROR:
                record.msg = f"[!] {record.msg}"            
        return super().format(record)


def set_gui_log_handler(gui_callback):
    """ Add the GUI log handler dynamically"""
    gui_handler = GuiLogHandler(gui_callback)
    gui_handler.setLevel(logging.DEBUG)
    gui_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(gui_handler)

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = (os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

def ResetAdapter(ser):
    ser.setDTR(1)
    time.sleep(0.1)
    ser.setDTR(0)
    time.sleep(0.1)

def SetAdapterKKL(ser):
    ser.setDTR(0)
    ser.setRTS(0)
    time.sleep(0.1)


def SendCharwEcho(ser, data):
#    time.sleep(self.sleeptime)
#    now = time.time()
#    while( time.time() < now + self.sleeptime):
#        pass
    ret = True
    ser.write(data)
    #time.sleep(self.sleeptime)
    echo = ser.read(1)
    if(len(echo) != 0):
        if(echo[0] != data[0]):
            logger.error(f"Echo error sent: {hex(data[0])}, received: {hex(echo[0])}")
            ret = False
    else:
        logger.error("got no echo")
    return ret

def SendDatawEcho(ser, data):
#    for d in data:
#        #logger.info("sendDatawEcho char: ", hex(d))
#        SendCharwEcho(ser,[d])
    ser.write(data)
    echo = ser.read(len(data))

    if(len(echo) != len(data)):
        logger.error("got wrong echo length")
        return False

    counter = 0
    while counter < len(data):
        if(data[counter] != echo[counter]):
            logger.error(f"Echo error sent: {hex(data[counter])}, received: {hex(echo[counter])}, at data pos: {hex(counter)}")
            ret = False
        counter += 1

    return True



def GetAddressAsLittleEndian(address):
    add = [ (address)&0xff, (address>>8)&0xff, (address>>16)&0xff,]
    return add

def GetWordAsLittleEndian(data):
    add = [ (data)&0xff, (data>>8)&0xff]
    return add

I_LOADER_STARTED        = 0x01	#// Loader successfully launched
I_APPLICATION_LOADED    = 0x02	#// Application succ. loaded
I_APPLICATION_STARTED   = 0x03	#// Application succ. launched
I_AUTOBAUD_ACKNOWLEDGE  = 0x04  #// Autobaud detection acknowledge

A_ACK1                  =0xAA  	#// 1st Acknowledge to function code
A_ACK2                  =0xEA	#// 2nd Acknowledge (last byte)
A_ASC1_CON_INIT         =0xCA	#// ASC1 Init OK
A_ASC1_CON_OK           =0xD5	#// ASC1 Connection OK

C_WRITE_BLOCK		    =0x84	#// Write memory block to target mem
C_READ_BLOCK		    =0x85   #// Read memory block from target mem
C_EINIT			        =0x31   #// Execute Einit Command
C_SWRESET		   	    =0x32   #// Execute Software Reset
C_GO			        =0x41   #// Jump to user program
C_GETCHECKSUM    	    =0x33   #// get checksum of previous sent block
C_TEST_COMM		   	    =0x93   #// Test communication
C_CALL_FUNCTION		    =0x9F   #// Mon. extension interface: call driver
C_WRITE_WORD		    =0x82   #// write word to memory/register
C_MON_EXT               =0x9F   #// call driver routine
C_ASC1_CON              =0xCC   #// Connection over ASC1
C_READ_WORD             =0xCD   #// Read Word from memory/register
C_WRITE_WORD_SEQUENCE   =0xCE   #// Write Word Sequence
C_CALL_MEMTOOL_DRIVER   =0xCF   #// Call memtool driver routine
C_AUTOBAUD              =0xD0   #// Call autobaud routine
C_SETPROTECTION         =0xD1   #// Call security function

# C167 Port Register Addresses:
Port4Address = 0xFFC8
Port4Address8bit = 0xE4
DirectionPort4Address = 0xFFCA
DirectionPort4Address8bit = 0xE5

# C167 variants
variantByteC167Old      = 0xA5
variantByteC167         = 0xC5
variantByteC167WithID   = 0xD5

def SendCommand(ser, data):
    SendCharwEcho(ser, data)
    echo = ser.read(1)
    if(len(echo) != 1):
        logger.error("got no command ackn")
        return False
    if(echo[0] != A_ACK1):
        logger.error(f"Got different response than ackn1: {hex(echo[0])}")
        return False
    return True

def SendData(ser, data):
    SendDatawEcho(ser, data)
    echo = ser.read(1)
    if(len(echo) != 1):
        logger.error("got no send data ackn")
        return False
    if(echo[0] != A_ACK2):
        logger.error("got different response than ackn2: ", hex(echo[0]))
        return False
    return True

def GetData(ser, data):
    SendDatawEcho(ser, data)
    echo = ser.read(3)
    if(len(echo) != 3):
        logger.error("got no get data ackn")
        return False, []
    if(echo[2] != A_ACK2):
        logger.error(f"Got different response than ackn2: {hex(echo[0])}")
        return False, []
    return True, [echo[0] | echo[1]<<8]

def TestComm(ser):
    logger.info("Testing BSL Communication")
    SendDatawEcho(ser, [C_TEST_COMM])

    data = ser.read(2)
    if(len(data) != 2):
        logger.info("", extra={"suppress_format": True}) #blank line
        logger.error("no response from ecu after test comm")
        return -1
    if(data[0] != A_ACK1) | (data[1] != A_ACK2):
        logger.info("", extra={"suppress_format": True}) #blank line
        logger.error(f"Wrong response from ECU after sending test comm: {hex(data[0])}, {hex(data[1])}")
        return -1

    logger.info(f"Got kernel acknowledgment: {hex(A_ACK1)}, {hex(A_ACK2)}")

def SetWordAtAddress(ser, addr, data):
    ret = SendCommand(ser, [C_WRITE_WORD])
    ret &= SendData(ser, GetAddressAsLittleEndian(addr)+GetWordAsLittleEndian(data))
    ret &= SendCommand(ser, [C_READ_WORD])
    ret , readData = GetData(ser, GetAddressAsLittleEndian(addr))
    if(ret == False):
        logger.error("Failed to set control registers!")
        return False
    if(readData[0] != data):
        logger.error(f"Registers not successfully set: {data}, readback: {readData[0]}")
        return False
    logger.info(f"Set control register at: {hex(addr)} success: {ret}")
    return True

def GetBackCrossedWord(inputData):
    # Define reversed SIMK41/3 mapping (AD -> DQ pins)
    reverse_mapping = {
        15: 0,  # AD15 -> DQ0
        13: 1,  # AD13 -> DQ1
        11: 2,  # AD11 -> DQ2
        9: 3,   # AD9 -> DQ3
        0: 4,   # AD0 -> DQ4
        2: 5,   # AD2 -> DQ5
        4: 6,   # AD4 -> DQ6
        6: 7,   # AD6 -> DQ7
        14: 8,  # AD14 -> DQ8
        12: 9,  # AD12 -> DQ9
        10: 10, # AD10 -> DQ10
        8: 11,  # AD8 -> DQ11
        1: 12,  # AD1 -> DQ12
        3: 13,  # AD3 -> DQ13
        5: 14,  # AD5 -> DQ14
        7: 15   # AD7 -> DQ15
    }

    # Convert value to binary (16-bit)
    binary_value = f"{inputData:016b}"

    # Initialize raw binary as a list
    raw_binary = ['0'] * 16

    # Apply the reverse line swap
    for ad_pin, dq_pin in reverse_mapping.items():
        raw_binary[15 - dq_pin] = binary_value[15 - ad_pin]

    # Convert raw binary back to an integer
    out = int(''.join(raw_binary), 2)

    return out


def GetBlockChecksum(ser):
    SendCharwEcho(ser, [C_GETCHECKSUM])
    echo = ser.read(3)
    if(len(echo) != 3):
        logger.error("got no command ackn")
        return False,[]
    if(echo[2] != A_ACK2):
        logger.error("got different response than ackn2: ", hex(echo[2]))
        return False,[]
    return True,[echo[1]]

def CalcBlockChecksum(data):
    checksum = 0

    for d in data:
        checksum = (checksum ^ (d))&0xFF

    return checksum

def SetBlockAtAddress(ser, addr, data):
    ret = SendCommand(ser, [C_WRITE_BLOCK])
    ret &= SendData(ser, GetAddressAsLittleEndian(addr)+GetWordAsLittleEndian(len(data))+data)

    ret , checksum = GetBlockChecksum(ser)
    calcChecksum = CalcBlockChecksum(data)
    if(ret == False):
        logger.info("set Block not successful")
        return False
    if(calcChecksum != checksum[0]):
        logger.info(f"Get block at: {hex(addr)} Wrong Checksum: {ret}, got checksum: {hex(checksum[0])}, calculated checksum: {hex(calcChecksum)}")
        return False,[]
    return True

def GetBlockAtAddress(ser, addr, size):
    ret = SendCommand(ser, [C_READ_BLOCK])
    SendDatawEcho(ser,  GetAddressAsLittleEndian(addr)+GetWordAsLittleEndian(size))
    echo = ser.read(size+1)
    if(len(echo) != size+1):
        logger.error(f"GetBlockAtAddress got no acknowledgment: {echo}")
        return False, []
    if(echo[size] != A_ACK2):
        logger.error(f"GetBlockAtAddress got different response than ackn2: {hex(echo[size])}")
        return False, []

    ret , checksum = GetBlockChecksum(ser)
    calcChecksum = CalcBlockChecksum(echo[:size])
    if(ret == False):
        logger.error("Get Block not successful, got no checksum ")
        return False,[]
    if(calcChecksum != checksum[0]):
        logger.error(f"Get block at: {hex(addr)} Wrong Checksum: {ret}, got checksum: {hex(checksum[0])}, calculated checksum: {hex(calcChecksum)}")
        return False,[]
    return True, echo[:size]

def CallAtAddress(ser, addr, register):     # 8 register words on r8-r15
    ret = SendCommand(ser, [C_CALL_FUNCTION])
    regdata =[]
    for r in register:
        regdata += GetWordAsLittleEndian(r)

    SendDatawEcho(ser, GetAddressAsLittleEndian(addr)+regdata)
    echo = ser.read(17)
    if(len(echo) != 17):
        logger.error(f"CallAtAddress got no acknowledgment: {echo}")
        logger.info("Debug Registers")
        for r in regdata:
            logger.info(hex(r))
        logger.info("", extra={"suppress_format": True}) #blank line
        return False, []
    if(echo[16] != A_ACK2):
        logger.error(f"CallAtAddress got different response than ackn2: {hex(echo[16])}")
        return False, []

    retreg = [echo[0] | echo[1]<<8, echo[2] | echo[3]<<8, echo[4] | echo[5]<<8, echo[6] | echo[7]<<8,
                 echo[8] | echo[9]<<8, echo[10] | echo[11]<<8, echo[12] | echo[13]<<8, echo[14] | echo[15]<<8]

##    logger.info(" CallAtAddress at: ", hex(addr)," successful")#, register after call: ")
##    for r in returnreg:
##        logger.info(hex(r))
    return True, retreg

def RunFunc(exit, ser, file, job, size, eetype, portAddr4, directionPortAddress4, pinnum, progress_callback=None, log_callback2=None):
    ResetAdapter(ser)
    SetAdapterKKL(ser)

    ser.reset_input_buffer()

    SYSCON_Addr         = 0x00ff12
    SYSCON_Data_ext     = 0xe204        # from 15 -0: 3b stksz, 1b ROMS1, 1b SGTDIS, 1b ROMEN, 1b BYTDIS, 1b CLKEN, 1b WRCFG, 1b CSCFG, 1b reserved, 
                                        # 1b OWDDIS, 1b BDRSTEN, 1b XPEN, 1b VISIBLE, 1b SPER-SHARE
                                        # e -> rom mapped at >0x0 0000 and a Stacksize = ???, 2 -> Romen = 0 & BytDis = 1

    SYSCON_Data_int     = 0xf604        # from 15 -0: 3b stksz, 1b ROMS1, 1b SGTDIS, 1b ROMEN, 1b BYTDIS, 1b CLKEN, 1b WRCFG, 1b CSCFG, 1b reserved, 
                                        # 1b OWDDIS, 1b BDRSTEN, 1b XPEN, 1b VISIBLE, 1b SPER-SHARE
                                        # f -> rom mapped at >0x01 0000 and a Stacksize = ???, 6 -> Romen = 1 & BytDis = 1
    BUSCON0_Addr    = 0x00ff0c
    BUSCON0_Data    = 0x04ad            # d = 2 memory cycle wait state, a = 16bit demultiplexed bus - no tristate wait state - 1 read/write delay,
                                        # 4= external bus active, c chipSelect read/write enable

    ADDRSEL1_Addr   = 0x00fe18   
    ADDRSEL1_SIMK4X_Data = 0x4008       # 1024kByte window starting at 0x40 0000 ??
    
    BUSCON1_Addr = 0x00ff14
    BUSCON1_SIMK4X_Data = 0x848e        #  d = 2 memory cycle wait state, 0 = 8bit demultiplexed bus - 1 tristate wait state - 1 read/write delay, 0= external bus inactive

    driverAddress = 0x00F600
    FlashDriverEntryPoint = 0x00F640
    
    #driver commands
    C_GETSTATE = 0x0093
    C_READSPI = 0x0036
    C_WRITESPI = 0x0037

    #flash driver commands
    FC_PROG                     =	0x00     #Program Flash
    FC_ERASE                    =	0x01	 #Erase Flash
    FC_SETTIMING                =	0x03	 #Set Timing
    FC_GETSTATE                 =	0x06	 #Get State
    FC_GETSTATE_ADDR_MANUFID    =   0x00    
    FC_GETSTATE_ADDR_DEVICEID   =   0x01    
    FC_LOCK                     =	0x10	 #Lock Flash bank
    FC_UNLOCK                   =	0x11	 #Unlock Flash bank
    FC_PROTECT                  =	0x20	 #Protect entire Flash
    FC_UNPROTECT                =	0x21	 #Unprotect Flash
    FC_BLANKCHECK               =	0x34	 #OTP/ Flash blankcheck
    FC_GETID		            =	0x35     #Get Manufacturer ID/ Device ID ->not implemented


#    --------------------- Error Values -----------------------------
    E_NOERROR               =	0x00	 #No error
    E_UNKNOWN_FC            =	0x01	 #Unknown function code

    E_PROG_NO_VPP           =	0x10	 #No VPP while programming
    E_PROG_FAILED           =	0x11	 #Programming failed
    E_PROG_VPP_NOT_CONST	=	0x12	 #VPP not constant while programming

    E_INVALID_BLOCKSIZE     =	0x1B	 #Invalid blocksize
    E_INVALID_DEST_ADDR     =	0x1C	 #Invalid destination address

    E_ERASE_NO_VPP          =	0x30	 #No VPP while erasing
    E_ERASE_FAILED          =	0x31	 #Erasing failed
    E_ERASE_VPP_NOT_CONST	=	0x32	 #VPP not constant while erasing

    E_INVALID_SECTOR        =	0x33	 #Invalid sector number
    E_Sector_LOCKED         =	0x34	 #Sector locked
    E_FLASH_PROTECTED       =	0x35	 #Flash protected
    
    IntRomAddress = 0x010000
    ExtFlashAddress = 0x800000
    DriverCopyAddress = 0xFC00
    BlockLength = 0x200

## Upload Kernel
    SendDatawEcho(ser,[0])  #say hello, how ya doin'?
    byte = ser.read(1)
    if(len(byte) != 1):
        logger.error(f"No response from ECU, set device into bootmode: {byte}")
        return -1
    if(byte[0] != 0xaa):
        
        if byte[0] == variantByteC167Old:
            variant_info = "variantByteC167Old, tell dmg what cpu you have?"
        elif byte[0] == variantByteC167:
            variant_info = "SAK-C167CR-LM"
        elif byte[0] == variantByteC167WithID:
            variant_info = "SAK-C167CS-LM"
        else:
            variant_info = "no C16x Variant detected, tell dmg what cpu you have?"

        logger.info("", extra={"suppress_format": True}) #blank line
        logger.info(f"Got CPU Version: {variant_info} (ID: {hex(byte[0])})")

        logger.info("Sending SIMK4x Bootstrap")

        path = resource_path("assets/simk4x_bootstrap.bin")
        
        loaderfile = open(path, 'rb')
        loader = loaderfile.read()
        loaderfile.close()

        SendDatawEcho(ser,loader)
        byte = ser.read(1)
        if(len(byte) != 1):
            logger.info("", extra={"suppress_format": True}) #blank line
            logger.error("no response from ecu after sending loader")
            return -1
        if(byte[0] != I_LOADER_STARTED):
            logger.error(f"Wrong response from ECU after sending loader, got: {hex(byte[0])} instead of 0x01")
            return -1

        logger.info(f"Got loader acknowledgment: {I_LOADER_STARTED}")

        logger.info("Sending SIMK4x Kernel")

        path = resource_path("assets/simk4x_kernel.bin")
        
        kernelfile = open(path, 'rb')
        kernel = kernelfile.read()
        kernelfile.close()

        SendDatawEcho(ser,kernel)
        byte = ser.read(1)
        if(len(byte) != 1):
            logger.info("", extra={"suppress_format": True}) #blank line
            logger.error("no response from ecu after sending loader")
            return -1
        if(byte[0] != I_APPLICATION_STARTED):
            logger.error(f"Wrong response from ECU after sending loader, got: {hex(byte[0])} instead of 0x01")
            return -1

        logger.info(f"Got kernel acknowledgment: {I_APPLICATION_STARTED}")

    else:
        logger.info("Kernel already running...\n")

    TestComm(ser)

    if(job == jobReadIntRom):
        logger.info("\n*************  start read IntRom  *************\n", extra={"suppress_format": True})

        SetWordAtAddress(ser, SYSCON_Addr, SYSCON_Data_int)

        offset = 0x0
        readsize = BlockLength

        logger.info(f"Reading internal ROM of size {size}kb...")
        try:
            while (offset < size):
                if((size - offset) < BlockLength):
                    readsize = (size - offset)
                success, blockData = GetBlockAtAddress(ser, IntRomAddress+offset, readsize)
                if(success == True):
                    file.write(bytes(blockData))
                else:
                    logger.error("read IntRom not successful!!")
                    return 1

                offset += readsize

                percentage = int(offset * 100 / size)
                
                # GUI Progress Update
                if progress_callback:
                    progress_callback(percentage)
                 # Construct the message to send back to the gui
                message = f"read at {hex(offset)}  finished {int(offset * 100 / size)} %"

                if log_callback2:
                    log_callback2.emit(message)

                # CLI Progress
                else:
                    sys.stdout.write("\r  read at "+ str(hex(offset)) +"  finished " + str(int(offset *100 / size)) +" % ")
        except Exception as e:
            logger.error(f"An error occurred during read: {str(e)}")
            raise
        finally:
            logger.info("", extra={"suppress_format": True}) #blank line
            logger.info(f"File has been saved to: {filename}")
            logger.info("\n*************  finish read IntRom  *************\n", extra={"suppress_format": True})



    def detect_ecu_type_and_configure(ser):
        """
        Detects the ECU type or uses the provided `eetype` override, sends the appropriate driver,
        retrieves EEPROM IDs (with reverse mapping for 2.0L), determines flash size, and identifies boot sector type.
        """
        global writeAddressBase, botBootSector, manId, devId, flashSize

        # Lookup table for manufacturer IDs
        manufacturerDetails = {
            0x01: "AMD",
            0x20: "ST"
        }

        # Lookup table for device IDs (using only the last 2 digits)
        chipDetails = {
            # AMD chips
            0x57: ("AM29F200BB", 1 << 18, "Bottom"),
            0xAB: ("AM29F400BB", 1 << 19, "Bottom"),
            0x58: ("AM29F800BB", 1 << 20, "Bottom"),
            0x51: ("AM29F200BT", 1 << 18, "Top"),
            0x23: ("AM29F400BT", 1 << 19, "Top"),
            0xD6: ("AM29F800BT", 1 << 20, "Top"),
            # ST chips
            0xD4: ("M29F200BB", 1 << 18, "Bottom"),
            0xD6: ("M29F400BB", 1 << 19, "Bottom"),
            0xD5: ("M29F200BT", 1 << 18, "Top"),
            0xD3: ("M29F400BT", 1 << 19, "Top"),
        }

        # Always initialize writeAddressBase
        writeAddressBase = ExtFlashAddress

        SetWordAtAddress(ser, SYSCON_Addr, SYSCON_Data_ext)
        SetWordAtAddress(ser, BUSCON0_Addr, BUSCON0_Data)

        SetWordAtAddress(ser, ADDRSEL1_Addr, ADDRSEL1_SIMK4X_Data)
        SetWordAtAddress(ser, BUSCON1_Addr, BUSCON1_SIMK4X_Data)

        def read_eeprom_ids(apply_reverse=False):
            """
            Reads Manufacturer and Device IDs from EEPROM.
            Optionally applies reverse mapping for the 2.0L ECU.
            """
            # Retrieve Manufacturer ID
            writeAddressHigh = (writeAddressBase >> 16) & 0xFFFF
            readAddressHigh = (ExtFlashAddress >> 16) & 0xFFFF
            register = [FC_GETSTATE, 0x0000, writeAddressHigh, readAddressHigh, 0x000, 0x000, FC_GETSTATE_ADDR_MANUFID, 0x0001]
            success, retRegister = CallAtAddress(ser, FlashDriverEntryPoint, register)
            if not success or len(retRegister) < 2:
                logger.warning("Failed to retrieve Manufacturer ID.")
                return None, None
            manId = GetBackCrossedWord(retRegister[1]) if apply_reverse else retRegister[1]

            # Retrieve Device ID
            register = [FC_GETSTATE, 0x0000, writeAddressHigh, readAddressHigh, 0x000, 0x000, FC_GETSTATE_ADDR_DEVICEID, 0x0001]
            success, retRegister = CallAtAddress(ser, FlashDriverEntryPoint, register)
            if not success or len(retRegister) < 2:
                logger.warning("Failed to retrieve Device ID.")
                return None, None
            devId = GetBackCrossedWord(retRegister[1]) & 0xFF if apply_reverse else retRegister[1] & 0xFF

            return manId, devId

        # Determine the driver path and reverse mapping upfront
        if eetype == "T_29FX00B_SIMK4X_V6":
            driver_path = resource_path("assets/simk4x_driver_v6_a29fx00bx.bin")
            apply_reverse = False
            driver_type = "V6"
        elif eetype == "T_29FX00B_SIMK4X_I4":
            driver_path = resource_path("assets/simk4x_driver_i4_a29fx00bx.bin")
            apply_reverse = True
            driver_type = "2.0L"
        else:
            # Attempt to detect driver automatically
            driver_path = resource_path("assets/simk4x_driver_v6_a29fx00bx.bin")
            apply_reverse = False
            driver_type = "V6"

            # Try V6 driver first
            logger.info("", extra={"suppress_format": True}) #blank line
            logger.info("Trying V6 driver...")
            with open(driver_path, 'rb') as driverFile:
                eepromDriver = list(driverFile.read())
            SetBlockAtAddress(ser, driverAddress, eepromDriver)
            manId, devId = read_eeprom_ids()

            # Switch to 2.0L driver if Manufacturer ID is not for V6
            if manId != 0x01 and manId != 0x20:
                logger.warning(f"Unexpected Manufacturer ID: {hex(manId)}. Switching to 2.0L driver.\n")
                driver_path = resource_path("assets/simk4x_driver_i4_a29fx00bx.bin")
                apply_reverse = True
                driver_type = "2.0L"

        # Send the selected driver once
        logger.info(f"Sending {driver_type} driver...")
        with open(driver_path, 'rb') as driverFile:
            eepromDriver = list(driverFile.read())
        SetBlockAtAddress(ser, driverAddress, eepromDriver)

        # Read Manufacturer and Device IDs
        manId, devId = read_eeprom_ids(apply_reverse)
        if manId is None or devId is None:
            logger.warning("Failed to detect EEPROM IDs.")
            input("\n Warning: EEPROM ID not recognized, press ENTER to proceed manually or CTRL + C to abort.\n")
            return "Unknown", 0, "Unknown"

        # Log manufacturer name if available
        manufacturerName = manufacturerDetails.get(manId, "Unknown Manufacturer")
        logger.info(f"Manufacturer: {manufacturerName} (ID: {hex(manId)})")

        # Determine chip type
        chipType, flashSize, bootSectorType = chipDetails.get(devId, ("Unknown Chip", 0, "Unknown"))
        if chipType == "Unknown Chip":
            logger.error(f"Unknown Device ID: {hex(devId)}.")

        logger.info(f"Detected Chip: {chipType}, Size: {hex(flashSize)}, Boot Sector: {bootSectorType}")

        # Set boot sector flag
        botBootSector = bootSectorType == "Bottom"

        return chipType, flashSize, bootSectorType


    if(job == jobHwInfo):
        logger.info("\n*************  start hwinfo  *************\n", extra={"suppress_format": True})

        # Detect ECU type and configure
        chipType, flashSize, bootSectorType = detect_ecu_type_and_configure(ser)

        # Progress bar simulation for display purposes
        if progress_callback:
            for percentage in range(0, 101, 10):
                progress_callback(percentage)
                time.sleep(0.1)  # Simulate processing delay

        logger.info("\n*************  end hwinfo  *************\n", extra={"suppress_format": True})

        return 1



    if(job == jobWriteExtFlash):    
        logger.info("\n*************  start write extFlash  *************\n", extra={"suppress_format": True})

##        logger.info("\n *************  write reset to read (f0)  *************\n", extra={"suppress_format": True})
##        SetWordAtAddress(ser, 0x400000, 0xf0)

##        GetFlashManufacturerID(ser)
        
        # Detect ECU type and configure
        chipType, flashSize, bootSectorType = detect_ecu_type_and_configure(ser)

        offset = 0x0

        # read data to know size
        writeData = file.read()
        size = len(writeData)

#************* Erase*********
        sector = 0
        sectorSize = 0
        while (offset < size):
            if(botBootSector == False): # Top boot sector
                if(flashSize ==  (1<<20)): #size f800 1024kB
                    if(sector == 18):
                        sectorSize = 0x4000
                    elif(sector == 17) | (sector == 16):
                        sectorSize = 0x2000
                    elif(sector == 15):
                        sectorSize = 0x8000
                    else:
                        sectorSize = 0x10000

                else: #size f400 512kB
                    if(sector == 10):
                        sectorSize = 0x4000
                    elif(sector == 9) | (sector == 8):
                        sectorSize = 0x2000
                    elif(sector == 7):
                        sectorSize = 0x8000
                    else:
                        sectorSize = 0x10000

            else:# BOT boot sector
                if(sector == 0):
                    sectorSize = 0x4000
                elif(sector == 1) | (sector == 2):
                    sectorSize = 0x2000
                elif(sector == 3):
                    sectorSize = 0x8000
                else:
                    sectorSize = 0x10000

            writeAddressHigh = ((writeAddressBase + offset) >>16) & 0xFFFF
            writeAddressLow = (writeAddressBase + offset) & 0xFFFF
            readAddressHigh = ((ExtFlashAddress + offset) >>16) & 0xFFFF

            lastWordAddress = (ExtFlashAddress + offset + sectorSize -2) & 0xFFFF
            
            register = [FC_ERASE, writeAddressLow, writeAddressHigh, readAddressHigh, lastWordAddress, 0x000, sector, 0x0001]

            success , retRegister = CallAtAddress(ser, FlashDriverEntryPoint,register)
            if(success == False):   # | (retRegister[7] != 0x0): prevent list index out of range, when running this on 2.0L python .\bsl.py 57600 -writeextflash .\blank_with_1234.bin simk4x_v6
                logger.error("Call FC_ERASE failed\n")
                return -1
            
            #logger.info(f"Call FC_ERASE Sector {sector} successful: {retRegister[7] == 0x0}, return sector address: {hex(retRegister[5])}, timeout counter: {hex(retRegister[6])}")

            # GUI Progress Bar Update
            percentage = int(offset * 100 / size)

            if progress_callback:
                progress_callback(percentage)

            # Construct the message to send back to the gui
            message = f"Call FC_ERASE Sector {str(sector)}  successful: {str(retRegister[7] == 0x0)}"

            if log_callback2:
                log_callback2.emit(message)

            # CLI Progress
            else:
                sys.stdout.write("\rCall FC_ERASE Sector "+ str(sector) +" successful: "  + str(retRegister[7]== 0x0))

            offset += sectorSize
            sector += 1

        logger.info("", extra={"suppress_format": True}) #blank line
        logger.info("Successfully Erased %s Sectors: %s", sector-1, retRegister[7] == 0x0)

#************* Write*********
        offset = 0x0
        writesize = BlockLength

        logger.info(f"Writing external flash of size {size}kb...")
        while (offset < size):
            if((size - offset) < BlockLength):
                writesize = (size - offset)
            blockData = writeData[offset:(offset+writesize)]
            write = False
            for e in blockData:
                if(e != 0xff):
                    write = True
            if (write == True):
                success = SetBlockAtAddress(ser, DriverCopyAddress, list(writeData[offset:(offset+writesize)]))

                if(success != True):
                    logger.error("Write not successful!!")
                    return -1

                writeAddress = writeAddressBase + offset
                writeAddressHigh = writeAddress >>16
                writeAddressLow = writeAddress & 0xFFFF
                
                readAddressHigh = ((ExtFlashAddress + offset) >>16) & 0xFFFF
                
                register = [FC_PROG, writesize, DriverCopyAddress, 0x0000, readAddressHigh, writeAddressLow, writeAddressHigh, 0x0001]

    ##            for i in register:
    ##                logger.info(hex(i))
    ##            logger.info("", extra={"suppress_format": True}) #blank line
                success , retRegister = CallAtAddress(ser, FlashDriverEntryPoint,register)
                if(success == False) | (retRegister[7] != 0x0):
                    logger.error("Call FC_PROGRAM failed")
                    logger.info("Debug Registers")
                    for r in retRegister:
                        logger.info(hex(r))
                    logger.info("", extra={"suppress_format": True}) #blank line
                    return -1
                
                offset += writesize

                # GUI Progress Bar Update
                percentage = int(offset * 100 / size)

                if progress_callback:
                    progress_callback(percentage)

                # Construct the message to send back to the gui
                message = f"Call FC_PROGRAM BLOCK {str(hex(offset))}  successful: {str(retRegister[7] == 0x0)} finished {str(int(offset *100 / size))} %"

                if log_callback2:
                    log_callback2.emit(message)

                # CLI Progress
                else:
                    sys.stdout.write("\rCall FC_PROGRAM BLOCK "+ str(hex(offset)) +" successful: "  + str(retRegister[7]== 0x0)+"  finished " + str(int(offset *100 / size)) +" %")

            else:
                offset += writesize
                # Construct the message to send back to the gui
                message = f"only 0xff in block -> nothing to write. Finished {str(int(offset *100 / size))} %"

                if log_callback2:
                    log_callback2.emit(message)
                    
                # CLI Progress
                else:
                    sys.stdout.write("\r only 0xff in block -> nothing to write "+"  finished " + str(int(offset *100 / size)) +" % ")

        logger.info("Successfully Programmed %s :%s",hex(offset),retRegister[7] == 0x0)

        logger.info("\n********** write extFlash successful!! ***********\n", extra={"suppress_format": True})
        return 1

    if job == jobReadExtFlash:
        logger.info("\n*************  start read extFlash  *************\n", extra={"suppress_format": True})

        # Detect ECU type and configure
        chipType, flashSize, bootSectorType = detect_ecu_type_and_configure(ser)

        offset = 0x0
        readsize = BlockLength

        logger.info(f"Reading external flash of size {flashSize}kb...")
        # Use auto-detected flash size
        while offset < flashSize:
            if (flashSize - offset) < BlockLength:
                readsize = (flashSize - offset)
            
            success, blockData = GetBlockAtAddress(ser, ExtFlashAddress + offset, readsize)
            if success:
                file.write(bytes(blockData))
            else:
                logger.error("Read extFlash not successful!!")
                return 1

            offset += readsize

            # GUI Progress Bar Update
            percentage = int(offset * 100 / flashSize)
            if progress_callback:
                progress_callback(percentage)

            # Construct the message to send back to the GUI
            message = f"read at {hex(offset)} finished {percentage} %"
            if log_callback2:
                log_callback2.emit(message)

            # CLI Progress
            else:
                sys.stdout.write(f"\r  read at {hex(offset)} finished {percentage} % ")

        logger.info("", extra={"suppress_format": True}) #blank line
        logger.info(f"File has been saved to: {filename}")
        logger.info("\n*************  finish read extFlash  *************\n", extra={"suppress_format": True})
        return 1
    return 1   # != 0 ... exit

def PrintUsage():
    logger.info(" type -h for help\n Arguments in [] are optional:\n"+

          "\n-------------------- Display Hardware Information ----------------------\n"+
          "     bsl.py  38400     -hwinfo \n"+
          "------------------------------------------------------------------------\n"+
      
          "\n-------------------- Read Internal Rom or Flash (IROM, IFLASH) ---------\n"+
          "     bsl.py  baudrate  -readInt    size (hex or dec)     [filename]       \n"+
          " eg: bsl.py  38400     -readInt    0x8000                IntRom.ori\n"+
          "------------------------------------------------------------------------\n"+
          
          "\n-------------------- Read External Flash -------------------------------\n"+
          "     bsl.py  baudrate  -readextflash   size (hex or dec) [filename]  \n"+
          " eg: bsl.py  38400     -readextflash   0x100000          1024kb.ori\n"+         
          " eg: bsl.py  38400     -readextflash   0x80000           512kb.ori\n"+
          " eg: bsl.py  38400     -readextflash   0x40000           256kb.ori\n"+
          "------------------------------------------------------------------------\n"+
          
          "\n-------------------- Write External Flash ------------------------------\n"+
          "     bsl.py  baudrate  -writeextflash   filename       Ecu Type  \n"+
          "eg:  bsl.py  57600     -writeextflash   ca663045.bin   simk4x_i4\n"+
          "eg:  bsl.py  57600     -writeextflash   ca664019.bin   simk4x_v6\n"+
          "------------------------------------------------------------------------\n"+
          
          "\n-------------------- Baudrate ------------------------------------------\n"+
          " standard rates: 9600, 19200, 28800, 38400 (default: 57600)\n"
          "------------------------------------------------------------------------\n", extra={"suppress_format": True})


# Global Variables
jobHwInfo = 0
jobReadIntRom = 1
jobReadExtFlash = 2
jobWriteExtFlash = 3
size = 0x200
baud = 57600
portAddr4 = Port4Address8bit
directionPortAddress4 = DirectionPort4Address8bit
pinnum = 7

def run_bsl_loop(progress_callback=None, log_callback2=None):
    """Executes the main BSL loop."""
    global exit, usbpresent, state, printwait, ports, filename, job, size, baud, eetype, file
    try:
        while exit == 0:
            try:
                if state == 0:
                    raise RuntimeError("Unexpected state 0 in run_bsl_loop.")
                elif state == 10:
                    if printwait == 0:
                        logger.info("\nBootstrap Loader for Siemens SIMK4x ECU's \n********************************************\n", extra={"suppress_format": True})
                        logger.info("Waiting for K+Can or KKL Adapter (plug in USB if not done!!)")
                        printwait = 1
                    while usbpresent == 0:
                        time.sleep(1)
                        usbPort = serial.tools.list_ports.grep("USB Serial Port")
                        for port in usbPort:
                            ports.append(port)
                            usbpresent = 1
                            state = 11
                            printwait = 0

                elif state == 11:
                    comcounter = 0
                    if len(ports) > 1:
                        while comcounter < len(ports):
                            logger.info(f"num: {comcounter} : {ports[comcounter]}")
                            comcounter += 1
                        num = input("Select COM Port (position number): ")
                        try:
                            comcounter = int(num)
                            if comcounter >= len(ports) or comcounter < 0:
                                logger.warning("Invalid input")
                                usbpresent = 0
                                state = 10
                                ports = []
                        except ValueError:
                            logger.warning("Invalid input")
                            usbpresent = 0
                            state = 10
                            ports = []
                    if usbpresent == 1:
                        logger.info(f"Using {ports[comcounter].device}, baudrate {baud}")
                        ser = serial.Serial(ports[comcounter].device, baud, timeout=3)
                        ser.reset_input_buffer()
                        state = 20

                elif state == 20:
                    try:
                        exit = RunFunc(exit, ser, file, job, size, eetype, Port4Address8bit, DirectionPort4Address8bit, pinnum, progress_callback=progress_callback, log_callback2=log_callback2)
                    except Exception as e:
                        exit = -1
                        traceback.print_exc()
                        logger.error(f"Job execution failed: {e}")
            except Exception as e:
                exit = 1
                traceback.print_exc()
                logger.error(f"Exception in loop: {e}")
    finally:
        try:
            if file:
                file.close()
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup: {cleanup_error}")


# Main callable function
def execute_bsl(args, progress_callback=None, log_callback2=None):

    global exit, usbpresent, state, printwait, ports, filename, job, size, baud, eetype, file

    # reset vars before each run
    exit = 0
    usbpresent = 0
    state = 10  # Reset to the starting state
    printwait = 0
    ports = []
    file = None
    job = 0
    size = None  # Default to None for auto-detection
    eetype = None  # Default to None for auto-detection

    #logger.info(f"Received args in execute_bsl: {args}")
    try:
        if len(args) < 2:
            raise ValueError("Not enough arguments provided.")

        baud = int(args[0])
        opt1 = args[1]

        if "hwinfo" in opt1.lower():
            job = jobHwInfo
            state = 10

        elif "readint" in opt1.lower():
            size = int(args[2], 16 if args[2].startswith('0x') else 10)
            filename = args[3] if len(args) > 3 else "intRom.bin"
            file = open(filename, 'wb')
            job = jobReadIntRom
            state = 10

        elif "readextflash" in opt1.lower():
            if len(args) > 2:  # Check if size is provided
                size = int(args[2], 16 if args[2].startswith('0x') else 10)
            else:
                size = None  # Default to None if size is not provided
            filename = args[3] if len(args) > 3 else "extFlash.bin"
            file = open(filename, 'wb')
            job = jobReadExtFlash
            state = 10

        elif "writeextflash" in opt1.lower():
            if len(args) < 3:
                raise ValueError("Missing arguments for writeextflash command.")
            filename = args[2]
            eetype_str = args[3].lower() if len(args) > 3 else None
            eetype = "T_29FX00B_SIMK4X_I4" if eetype_str and "simk4x_i4" in eetype_str else (
            "T_29FX00B_SIMK4X_V6" if eetype_str and "simk4x_v6" in eetype_str else None
            )
            file = open(filename, 'rb')
            job = jobWriteExtFlash
            state = 10

        else:
            raise ValueError(f"Invalid command: {opt1}")

        # Call the reusable loop
        # Pass the progress_callback to the loop
        run_bsl_loop(progress_callback=progress_callback, log_callback2=log_callback2)

    except Exception as e:
        logger.error(f"Error occurred in execute_bsl: {e}")
        raise

# Run when the script is executed directly
if __name__ == "__main__":

    # Custom formatting logic for the console handler
    class ConsoleFormatter(logging.Formatter):
        def format(self, record):
            if not getattr(record, "suppress_format", False):
                if record.levelno == logging.INFO:
                    record.msg = f"[*] {record.msg}"
                elif record.levelno == logging.WARNING:
                    record.msg = f"[!] {record.msg}"
                elif record.levelno == logging.ERROR:
                    record.msg = f"[!] {record.msg}"
            return super().format(record)
        
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(ConsoleFormatter("%(message)s"))
    logger.addHandler(console_handler)

    if len(sys.argv) > 1:
        try:
            execute_bsl(sys.argv[1:])
        except Exception as e:
            logger.error(f"An error occurred: {e}")
    else:
        PrintUsage()
