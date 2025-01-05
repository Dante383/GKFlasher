import os
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

def forward_lookup(value):
    """
    Convert an input value to its line-swapped equivalent based on the SIMK43 2.0L mapping.

    The forward lookup table defines (DQ -> AD pins).

    Args:
        value (int): The input is a 16-bit word.

    Returns:
        int: The line-swapped equivalent value.
    """
    bin_to_sie_mapping = {
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

    binary_value = f"{value:016b}"
    swapped_binary = ['0'] * 16

    for dq_pin, ad_pin in bin_to_sie_mapping.items():
        swapped_binary[15 - ad_pin] = binary_value[15 - dq_pin]

    swapped_value = int(''.join(swapped_binary), 2)
    return swapped_value


def reverse_lookup(value):
    """
    Convert an input value to its unline-swapped equivalent based on the SIMK43 2.0L mapping.

    The reverse lookup table defines (DQ -> AD pins).

    Args:
        value (int): The input is a 16-bit word.

    Returns:
        int: The unline-swapped equivalent value.
    """
    sie_to_bin_mapping = {
        0: 15,  # DQ0 -> AD15
        1: 13,  # DQ1 -> AD13
        2: 11,  # DQ2 -> AD11
        3: 9,   # DQ3 -> AD9
        4: 0,   # DQ4 -> AD0
        5: 2,   # DQ5 -> AD2
        6: 4,   # DQ6 -> AD4
        7: 6,   # DQ7 -> AD6
        8: 14,  # DQ8 -> AD14
        9: 12,  # DQ9 -> AD12
        10: 10, # DQ10 -> AD10
        11: 8,  # DQ11 -> AD8
        12: 1,  # DQ12 -> AD1
        13: 3,  # DQ13 -> AD3
        14: 5,  # DQ14 -> AD5
        15: 7   # DQ15 -> AD7
    }

    binary_value = f"{value:016b}"
    swapped_binary = ['0'] * 16

    for dq_pin, ad_pin in sie_to_bin_mapping.items():
        swapped_binary[15 - ad_pin] = binary_value[15 - dq_pin]

    swapped_value = int(''.join(swapped_binary), 2)
    return swapped_value


def generate_sie(filename):
    logger.info(f"Reading {os.path.basename(filename)}")
    
    try:
        with open(filename, 'rb') as infile:
            payload = infile.read()
    except FileNotFoundError:
        logger.error(f"File not found!")
        return

    logger.info("Converting BIN to SIE...")
    output_filename = os.path.splitext(filename)[0] + ".sie"
    try:
        with open(output_filename, 'wb') as outfile:
            for i in range(0, len(payload), 2):
                # Read 16-bit word
                word = int.from_bytes(payload[i:i+2], byteorder='little', signed=False)
                
                # Convert using the provided function
                swapped_word = forward_lookup(word)
                
                # Write the swapped word to the output file
                outfile.write(swapped_word.to_bytes(2, byteorder='little'))
    
        logger.info(f"Done! Converted file saved as {output_filename}")
        return

    except Exception as e:
        logger.error(f"Error during conversion: {e}")
        return


def generate_bin(filename):
    logger.info(f"Reading {os.path.basename(filename)}")
    
    try:
        with open(filename, 'rb') as infile:
            payload = infile.read()
    except FileNotFoundError:
        logger.error(f"File not found!")
        return

    logger.info("Converting SIE to BIN...")
    output_filename = os.path.splitext(filename)[0] + ".bin"
    try:
        with open(output_filename, 'wb') as outfile:
            for i in range(0, len(payload), 2):
                # Read 16-bit word
                word = int.from_bytes(payload[i:i+2], byteorder='little', signed=False)
                
                # Convert using the provided function
                swapped_word = reverse_lookup(word)
                
                # Write the swapped word to the output file
                outfile.write(swapped_word.to_bytes(2, byteorder='little'))
    
        logger.info(f"Done! Converted file saved as {output_filename}")
        return

    except Exception as e:
        logger.error(f"Error during conversion: {e}")
        return


