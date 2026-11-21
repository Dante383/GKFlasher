; Shared MTOS handoff to the OpenGK RAM kernel. Interrupts stay disabled;
; the watchdog remains active. Stack/register-bank placement keeps flash-driver
; RAM (0xF600) and the transfer buffer (0xFC00) clear.
    BCLR IEN
    NOP                         ; an interrupt can still arrive just after BCLR
    MOV WDTCON,#0001H
    SRVWDT
    MOV STKOV,#0F200H           ; widen bounds before moving the stack
    MOV STKUN,#0FDFEH
    MOV SP,#0F600H
    NOP                         ; do not use SP immediately after changing it
    MOV STKOV,#0F300H
    MOV STKUN,#0F600H
    MOV CP,#0F200H
    NOP                         ; nor registers immediately after changing CP
