; OpenGK SIMK4x command kernel.
; Shared dispatcher for UART and CAN: addresses are 24-bit little-endian, block
; lengths are 16-bit, and register calls exchange R8..R15. RL5 holds the block
; XOR checksum; RH5 holds the current command. Send uses RH0; receive returns RL0.
; AUTOBAUD and WRITE_WORD_SEQUENCE were disabled in the bundled kernel and are
; intentionally omitted. The UART build reproduces the existing 394-byte asset.
; CAN uses symbolic I/O entry points; no binary-offset patching is required.

KernelCode SECTION CODE AT KERNEL_BASE
KernelStart PROC NEAR
KernelInit:
IF NOT CAN_TRANSPORT
    BCLR S0RIR                       ; clear receive interrupt bit
    BCLR S0TIR                       ; clear transmit interrupt bit
ENDIF
    MOVB RH0,#KERNEL_STARTED
    CALLR SendByte                   ; Announce that the command kernel is running.

CommandLoop:
    CALLR ReceiveByte
    MOVB RH5,RL0

    MOVB RH0,#ACK
    CALLR SendByte                   ; Acknowledge the command byte.
    MOVB RH0,#DONE                 ; Default completion status; writes can report a verification error.

; Command EINIT

CommandEinit:
    CMPB RH5,#C_EINIT
    JMPR cc_NE,CommandTest
    EINIT                            ; Finish CPU initialization.
    JMPR SendDone

; Command Test Communication

CommandTest:
    CMPB RH5,#C_TEST_COMM
    JMPR cc_NE,CommandReset
    JMPR SendDone

; Command Software Reset

CommandReset:
    CMPB RH5,#C_SOFTWARE_RESET
    JMPR cc_NE,CommandChecksum
    CALLR SendByte                   ; Acknowledge before resetting the CPU.
    SRST                             ; Does not return.

; Command 'GetChecksum'

CommandChecksum:
    CMPB RH5,#C_GET_CHECKSUM
    JMPR cc_NE,CommandWriteBlock
    MOVB RH0,RL5                      ; Return the last block's XOR checksum.
    CALLR SendByte                   ; send checksum
    MOVB RH0,#DONE
    JMPR SendDone

; Command Write Block to Memory

CommandWriteBlock:
    CMPB RH5,#C_WRITE_BLOCK
    JMPR cc_NE,CommandReadBlock
    MOVB RL5,#0                      ; Clear the block XOR checksum.
    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR ReceiveWord                ; receive 2 bytes length to R3

WriteBlockLoop:
    CMPD1 R3,#0                   ; Compare the previous count, then decrement it.
    JMPR cc_NE,WriteBlockByte
    JMPR SendDone

WriteBlockByte:
    CALLR ReceiveByte                ; receive data byte
    MOVB RL4,RL0
    XORB RL5,RL4                     ; Accumulate the block XOR checksum.
    CALLR MapAddress                 ; address R1,R2 to DPP2 and R6

    MOV R12,R6                       ; Select the containing word for a byte-preserving write.
    BCLR R12.0                       ; Align the word address.
    MOV R7,[R12]                     ; Read the word before replacing one byte.

    JB R6.0,WriteHighByte            ; Select the addressed byte.
    AND R7,#0FF00H                   ; Preserve the high byte.
    ORB RL7,RL4                      ; Replace the low byte.
    JMPR StoreByte
WriteHighByte:
    AND R7,#00FFH                    ; Preserve the low byte.
    ORB RH7,RL4                      ; Replace the high byte.
StoreByte:
    MOV [R12],R7                     ; Write the containing word.
    MOVB RH4,[R6]                    ; read back
    CMPB RH4,RL4                      ; Verify the addressed byte.
    JMPR cc_NE,WriteMismatch         ; Retain an error status if readback differs.
    JMPR WriteBlockLoop
WriteMismatch:
    MOVB RH0,#VERIFY_FAILED        ; Report a verification error when the block completes.
    JMPR WriteBlockLoop

; Command Read Block from Memory

CommandReadBlock:
    CMPB RH5,#C_READ_BLOCK
    JMPR cc_NE,CommandGo
    MOVB RL5,#0                      ; Clear the block XOR checksum.
    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR ReceiveWord                ; receive 2 bytes length to R3

ReadBlockLoop:
    CMPD1 R3,#0                   ; Compare the previous count, then decrement it.
    JMPR cc_NE,ReadBlockByte
    MOVB RH0,#DONE                 ; set transmit register
    JMPR SendDone

ReadBlockByte:
    CALLR MapAddress                 ; address R1,R2 to DPP2 and R6
    MOVB RH0,[R6]                    ; read from memory to RH0
    XORB RL5,RH0                     ; Accumulate the block XOR checksum.
    CALLR SendByte                   ; transmit read data
    JMPR ReadBlockLoop

; Command 'Go'

CommandGo:
    CMPB RH5,#C_GO
    JMPR cc_NE,CommandReadWord
    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR SendByte                   ; send acknowledge before JMP
    PUSH R2                          ; push R2 (as CSP)
    PUSH R1                          ; push R1 (as IP)
    RETS                             ; restore both CSP and IP from the system stack

; Command Read Word

CommandReadWord:
    CMPB RH5,#C_READ_WORD
    JMPR cc_NE,CommandWriteWord
    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR MapAddress                 ; address R1,R2 to DPP2 and R6
    MOV R3,[R6]                      ; read from Memory to R3
    MOVB RH0,RL3
    CALLR SendByte
    MOVB RH0,RH3
    CALLR SendByte
    MOVB RH0,#DONE                 ; set transmit register
    JMPR SendDone

; Command Write Word

CommandWriteWord:
    CMPB RH5,#C_WRITE_WORD
    JMPR cc_NE,CommandCall

    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR ReceiveWord                ; receive 2 bytes word to R3
    CALLR MapAddress                 ; address R1,R2 to DPP2 and R6
    MOV [R6],R3                      ; write from R3 to memory

SendDone:
    CALLR SendByte                   ; transmit acknowledge ACK2
NextCommand:
    JMPR CommandLoop

; SendByte: send the contents of RH0

SendByte:
IF CAN_TRANSPORT
    JMPS 0,CAN_TX
ELSE
    MOVB S0TBUF,RH0
WaitTxEcho:
    JNB S0RIR,WaitTxEcho
    BCLR S0RIR
    BCLR S0TIR
    RET
ENDIF

; Receive a byte into RL0. UART waits service the watchdog.
ReceiveByte:
IF CAN_TRANSPORT
    JMPS 0,CAN_RX
ELSE
    SRVWDT
    JNB S0RIR,ReceiveByte
    BCLR S0RIR
    MOVB RL0,S0RBUF
    RET
ENDIF

CommandCall:
    CMPB RH5,#C_CALL
    JMPR cc_NE,NextCommand

    CALLR ReceiveAddress             ; receive 3 bytes address R1/R2
    CALLR ReceiveRegisters           ; Receive all eight argument registers, R8..R15.

    CALLR CallTrampoline             ; obtain the return IP for the register-call trampoline

CallTrampoline:
    POP R7                           ; store IP in R7
    ADD R7,#(CallReturn-CallTrampoline) ; Compute the return position symbolically.
    PUSH CSP                         ; push return position
    PUSH R7
    PUSH R2                          ; push destination
    PUSH R1
    RETS                             ; restore both CSP and IP from the system stack

CallReturn:
    CALLR SendRegisters              ; return position
    MOVB RH0,#DONE
    JMPR SendDone

; ReceiveAddress: receive 3 bytes address to R1 and RL2

ReceiveAddress:                      ; R1=A0-A15
    CALLR ReceiveByte                ; receive data byte
    MOVB RL1,RL0
ReceiveAddressHigh:
    CALLR ReceiveByte                ; receive data byte
    MOVB RH1,RL0
ReceiveSegment:                      ; RL2 contains address bits 16..23.
    CALLR ReceiveByte                ; receive data byte
    MOVB RL2,RL0
    MOVB RH2,#0
    RET
; ReceiveWord: receive 2 bytes length to R3

ReceiveWord:                         ; R3=Length
    CALLR ReceiveByte                ; receive data byte
    MOVB RL3,RL0
ReceiveWordHigh:
    CALLR ReceiveByte                ; receive data byte
    MOVB RH3,RL0
    RET
; MapAddress: set DPP2 and R6, according to address registers R1 and R2

MapAddress:
    MOV R6,R1                        ; R6 contains A0-A15
    MOV R7,R2                        ; R7 contains A16-A23
    SHR R6,#14                       ; R6 Bits 0-1 are A14-A15
    SHL R7,#2                        ; R7 Bits 2-9 are A16-A23
    OR R7,R6                         ; R7 contains A14-A23
    MOV DPP2,R7                      ; DPP2 contains A14-A23
    MOV R6,R1                        ; R6 contains A0-A15, increment R1
    ADD R1,#1
    ADDC R2,#0H                      ; increment segment R2 if necessary
    AND R6,#3FFFH                    ; R6 Bits 0-13 contain A0-A13
    OR R6,#8000H                     ; R6 Bits 14-15 contain 10=DPP2

    RET
; ReceiveRegisters: receive R8-R15

ReceiveRegisters:
    MOV R6,CP
    ADD R6,#16
    MOV R7,#0
ReceiveRegistersLoop:
    CALLR ReceiveByte                ; receive data byte
    MOVB [R6],RL0
    ADD R6,#1
    CMPI1 R7,#15
    JMPR cc_NE,ReceiveRegistersLoop
    RET
; SendRegisters: send R8-R15

SendRegisters:
    MOV R6,CP
    ADD R6,#16
    MOV R7,#0
SendRegistersLoop:
    MOVB RH0,[R6]
    CALLR SendByte
    ADD R6,#1
    CMPI1 R7,#15
    JMPR cc_NE,SendRegistersLoop
    RET
KernelEnd:
KernelStart ENDP
KernelCode ENDS
