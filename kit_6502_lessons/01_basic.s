; 01_basic.s — S159 lesson 1 — minimal 6502 program.
;
; Format: each non-comment line is `<hex bytes>  ; <mnemonic + operand>`.
; populate_6502.py parses both columns:
;   - bytes column is the canonical machine code
;   - comment column is human readable (verified against py65 disassembler)
;
; org = 0x0600 (default load address; py65 monitor convention)
;
; What this teaches:
;   - LDA #imm  (immediate load into accumulator)
;   - ADC #imm  (add with carry; reads carry flag implicitly)
;   - STA addr  (store accumulator to absolute address)
;   - BRK       (software break — halts the run, sets B flag, jumps to IRQ vec)

; org 0x0600
A9 05         ; LDA #$05    ; A = 5
69 03         ; ADC #$03    ; A = A + 3 + C = 8 (C cleared at boot)
8D 00 02      ; STA $0200   ; mem[0x0200] = 8
00            ; BRK         ; halt
