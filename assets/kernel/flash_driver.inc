; OpenGK AM29Fx bottom-boot sector flash driver, shared by I4 and V6 builds.
; Data/command constants and status-bit tests select the board wiring.
; both variants reproduce the bundled driver binaries.
; Helpers occupy 0xF600; the register-call entry is fixed at 0xF640.
; Flash writes use CS1 (0x40xxxx), reads use CS0 (0x80xxxx). Program addresses and
; sizes must be word-aligned. Existing status/error behavior is preserved.
FC_PROG EQU 0
FC_ERASE EQU 1
FC_BLANKCHECK EQU 034H
FC_GETSTATE EQU 6
ERASE_TIMEOUTCYCLES EQU 040H
E_ERASE_FAILED EQU 031H
E_UNKNOWN_FC EQU 1
E_INVALID_SECTOR EQU 033H

IF CROSSED_DATA_LINES
FLASH_UNLOCK1 EQU 02244H
FLASH_UNLOCK2 EQU 08811H
FLASH_RESET EQU 055H
FLASH_PROGRAM EQU 044H
FLASH_DQ7_MASK EQU 040H
FLASH_ERASE_SETUP EQU 040H
FLASH_ERASE_SECTOR EQU 5
FLASH_AUTOSELECT EQU 041H
ELSE
FLASH_UNLOCK1 EQU 0AAH
FLASH_UNLOCK2 EQU 055H
FLASH_RESET EQU 0F0H
FLASH_PROGRAM EQU 0A0H
FLASH_DQ7_MASK EQU 080H
FLASH_ERASE_SETUP EQU 080H
FLASH_ERASE_SECTOR EQU 030H
FLASH_AUTOSELECT EQU 090H
ENDIF

FlashHelpers SECTION CODE AT 0F600H

; Send the AMD unlock sequence through the flash write mapping.
UnlockSequence PROC FAR

    MOV R3,#0555H
    SHL R3,#1
    MOV R5,#FLASH_UNLOCK1
    EXTS #040H,#1
    MOV [R3],R5

    MOV R3,#02AAH
    SHL R3,#1
    MOV R5,#FLASH_UNLOCK2
    EXTS #040H,#1
    MOV [R3],R5

    RET

UnlockSequence ENDP

; Return the flash chip to normal array reads after an operation or error.
ResetFlash PROC FAR
    MOV R6,#0000H
    MOV R7,#FLASH_RESET
    EXTS #040H,#1
    MOV [R6],R7

    RET
ResetFlash ENDP

FlashHelpers ENDS

FlashDriver SECTION CODE AT 0F640H
; R8 selects the operation; preserve R0..R7 and return status in R15.
; Program: R9 length, R11:R10 RAM source, R14:R13 flash destination.
; Erase: R14 sector number. Autoselect: R14 word index, result in R9.
FlashEntry PROC FAR

    PUSH R0
    PUSH R1
    PUSH R2
    PUSH R3
    PUSH R4
    PUSH R5
    PUSH R6
    PUSH R7

ProgramCommand:
    CMP R8,#(FC_PROG)
    JMP cc_NE,EraseCommand

ProgramWord:

    CALL UnlockSequence

    MOV R3,#0555H
    SHL R3,#1
    MOV R5,#FLASH_PROGRAM
    EXTS #040H,#1
    MOV [R3],R5

    EXTS R11,#1
    MOV R5,[R10]
    MOV R7,R5

    ADD R10,#2
    ADDC R11,#0

    MOV R2,R14
    AND R2,#03FH
    OR R2,#040H
    EXTS R2,#1
    MOV [R13],R5

WaitProgram:
    EXTS R14,#1
    MOV R4,[R13]
    MOV R6,R4
    AND R5,#FLASH_DQ7_MASK
    AND R4,#FLASH_DQ7_MASK
    CMP R5,R4
    JMP CC_NE,CheckProgramError

VerifyProgram:
    EXTS R14,#1
    MOV R4,[R13]
    CMP R7,R4
    JMP CC_NE,ProgramFailed

    ADD R13,#2
    ADDC R14,#0

    SUB R9,#2
    CMP R9,#0
    JMP cc_NE,ProgramWord

    MOV R15,#0

    JMP cc_uc,RestoreRegisters

CheckProgramError:

IF CROSSED_DATA_LINES
    JNB R6.2,WaitProgram
ELSE
    JNB R6.4,WaitProgram
ENDIF

    EXTS R14,#1
    MOV R4,[R13]
    AND R4,#FLASH_DQ7_MASK
    CMP R5,R4
    JMP CC_EQ,VerifyProgram

ProgramFailed:
    CALL ResetFlash

    MOV R15,#1
    MOV R14,R2
    JMP cc_uc,RestoreRegisters

; Erase one sector and check it is blank. The counter bounds the ready poll.
EraseCommand:
    CMP R8,#(FC_ERASE)
    JMP cc_NE,BlankCheckCommand

    CALL SectorAddress
    MOV R13,R2
    CALL UnlockSequence

    MOV R3,#0555H
    SHL R3,#1
    MOV R5,#FLASH_ERASE_SETUP
    EXTS #040H,#1
    MOV [R3],R5

    CALL UnlockSequence

    MOV R6,R2
    AND R6,#01FH
    OR R6,#040H
    MOV R5,#FLASH_ERASE_SECTOR
    EXTS R6,#1
    MOV [R1],R5

    MOV R15,#0
    MOV R14,#0
WaitErase:
    ADD R15,#1
    ADDC R14,#0
    CMP R14,#(ERASE_TIMEOUTCYCLES)
    JMP cc_NE,CheckEraseReady
    MOV R15,#(E_ERASE_FAILED)
    JMP cc_uc,RestoreRegisters

CheckEraseReady:
    EXTS R2,#1
    MOV R5,[R1]
IF CROSSED_DATA_LINES
; I4 AD6 carries flash DQ7 (erase ready). AD5 carries DQ14, not a ready flag.
; ST labs bugfix for M29Fx chips.
    JNB R5.6,WaitErase
ELSE
    JNB R5.7,WaitErase
ENDIF

BlankCheckSector:
    EXTS R2,#1
    MOV R5,[R4]
    CMP R5,#0FFFFH
    JMP CC_NE,EraseFailed
    CMP R4,R1
    JMP CC_EQ,EraseSucceeded
    SUB R4,#2
    JMP BlankCheckSector

EraseSucceeded:
    MOV R15,#0
    JMP cc_uc,RestoreRegisters

EraseFailed:
    CALL ResetFlash

    MOV R15,#1
    JMP cc_uc,RestoreRegisters

; Legacy whole-array blank check, retained for binary compatibility.
BlankCheckCommand:
    CMP R8,#(FC_BLANKCHECK)
    JMP cc_NE,IdentifyCommand

    MOV R10,#8
    MOV R11,#0000H
BlankCheckArray:
    EXTS R10,#1
    MOV R5,[R11]
    CMP R5,#0FFFFH
    JMP CC_NE,ArrayNotBlank
    ADD R11,#2
    CMP R11,#0000H
    JMP CC_NE,BlankCheckArray
    CMPI1 R10,#000FH
    JMP CC_NE,BlankCheckArray

    MOV R15,#0
    JMP cc_uc,RestoreRegisters

ArrayNotBlank:
    MOV R15,#1
    JMP cc_uc,RestoreRegisters

; Read manufacturer/device identification in AMD autoselect mode.
IdentifyCommand:
    CMP R8,#(FC_GETSTATE)
    JMP cc_NE,UnsupportedCommand

    MOV R8,#1

    CALL UnlockSequence

    MOV R3,#0555H
    SHL R3,#1
    MOV R5,#FLASH_AUTOSELECT
    EXTS #040H,#1
    MOV [R3],R5
    SHL R14,#1
    EXTS #080H,#1
    MOV R9,[R14]

    CALL ResetFlash

    MOV R15,#0
    JMP cc_uc,RestoreRegisters

UnsupportedCommand:
    MOV R15,#(E_UNKNOWN_FC)
    JMP cc_uc,RestoreRegisters

RestoreRegisters:
    POP R7
    POP R6
    POP R5
    POP R4
    POP R3
    POP R2
    POP R1
    POP R0
    RETS

FlashEntry ENDP

; Resolve bottom-boot sector geometry, rejecting indexes above 18.
SectorAddress PROC NEAR
    CMP R14,#0
    JMP cc_NE,Sector1
    MOV R1,#0H
    MOV R2,#80H
    MOV R4,#3FFEH
    RET

Sector1:
    CMP R14,#1
    JMP cc_NE,Sector2
    MOV R1,#4000H
    MOV R2,#80H
    MOV R4,#5FFEH
    RET

Sector2:
    CMP R14,#2
    JMP cc_NE,Sector3
    MOV R1,#6000H
    MOV R2,#80H
    MOV R4,#7FFEH
    RET

Sector3:
    CMP R14,#3
    JMP cc_NE,Sector4
    MOV R1,#8000H
    MOV R2,#80H
    MOV R4,#0FFFEH
    RET

Sector4:
    CMP R14,#18
    JMP cc_UGT,InvalidSector
    MOV R1,#0000H
    MOV R2,R14
    ADD R2,#07DH
    MOV R4,#0FFFEH
    RET

InvalidSector:
    MOV R15,#(E_INVALID_SECTOR)
    RET

SectorAddress ENDP

FlashDriver ENDS
