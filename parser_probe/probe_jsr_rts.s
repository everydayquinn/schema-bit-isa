; probe_jsr_rts.s — smallest JSR/RTS shape for parser_6502.
;
; Main block at 0x0600 calls a one-instruction subroutine `inc_a`
; at 0x0700 via JSR; sub returns via RTS.
;
; Hand-computed expected facts live in test_parser_6502.py.
;
; Format reminder:
;   ; org 0xNNNN          — sets load address for following bytes
;   ; label NAME 0xNNNN   — declares a code label at NNNN (new directive)
;   <hex bytes>  ; <mnemonic + operand>

; org 0x0600
20 00 07     ; JSR $0700   ; calls sub:inc_a
00           ; BRK         ; halt main

; org 0x0700
; label inc_a 0x0700
E8           ; INX         ; tiny work payload (A unchanged, X+=1)
60           ; RTS         ; return to caller
