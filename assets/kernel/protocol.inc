; OpenGK SIMK4x loader/command protocol constants, shared by both transports.
; Command bytes, acknowledgements and little-endian parameters match the existing
; Python Kernel interface. Changing these values requires a protocol update.
LOADER_STARTED EQU 01H
KERNEL_STARTED EQU 03H
ACK EQU 0AAH
DONE EQU 0EAH
VERIFY_FAILED EQU 011H
C_EINIT EQU 031H
C_TEST_COMM EQU 093H
C_SOFTWARE_RESET EQU 032H
C_GET_CHECKSUM EQU 033H
C_WRITE_BLOCK EQU 084H
C_READ_BLOCK EQU 085H
C_GO EQU 041H
C_READ_WORD EQU 0CDH
C_WRITE_WORD EQU 082H
C_CALL EQU 09FH
