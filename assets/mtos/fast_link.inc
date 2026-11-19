; Shared by the optional fast hello/loader paths. Caller saves R1-R3 and UART SFRs.
; MT75 is sent at entry baud; ACK A5 arrives after both sides switch to the selected rate.
FastStart PROC FAR
    BCLR S0RIE
    BCLR S0TIE
    ; Firmware may reset S0BG before calling MTOS. Match the host explicitly.
    BCLR S0R
    BCLR S0BRS
    MOV S0BG,#ENTRY_RELOAD
    BSET S0R
    MOVB RL1,#'M'
    CALLS SEG FastEcho,SOF FastEcho
    CMP R2,#0
    JMPR cc_EQ,FastAbort
    MOVB RL1,#'T'
    CALLS SEG FastEcho,SOF FastEcho
    CMP R2,#0
    JMPR cc_EQ,FastAbort
    MOVB RL1,#'7'
    CALLS SEG FastEcho,SOF FastEcho
    CMP R2,#0
    JMPR cc_EQ,FastAbort
    MOVB RL1,#'5'
    CALLS SEG FastEcho,SOF FastEcho
    CMP R2,#0
    JMPR cc_EQ,FastAbort
    BCLR S0BRS
    MOV S0BG,#BAUD_RELOAD
    MOV R2,#128
    MOV R3,#0
FastWait:
    SRVWDT
    JB S0RIR,FastReceived
    SUB R3,#1
    JMPR cc_NE,FastWait
    SUB R2,#1
    JMPR cc_NE,FastWait
FastAbort:
    MOV R2,#0
    RET
FastReceived:
    MOVB RL1,S0RBUF
    BCLR S0RIR
    CMPB RL1,#0A5H
    JMPR cc_NE,FastWait
    MOV R2,#1
    RET
FastStart ENDP

FastEcho PROC FAR
    MOVB RL2,S0RBUF
    BCLR S0RIR
    MOVB S0TBUF,RL1
FastTx:
    JNB S0TIR,FastTx
    BCLR S0TIR
    MOV R3,#0
FastEchoWait:
    SRVWDT
    JB S0RIR,FastEchoReceived
    SUB R3,#1
    JMPR cc_NE,FastEchoWait
    MOV R2,#0
    RET
FastEchoReceived:
    MOVB RL2,S0RBUF
    BCLR S0RIR
    CMPB RL2,RL1
    JMPR cc_NE,FastEchoWait
    MOV R2,#1
    RET
FastEcho ENDP
