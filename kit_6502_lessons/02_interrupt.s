; 02_interrupt.s — S159 lesson 2 — IRQ injection sandbox.
;
; What this teaches:
;   - IRQ vector at 0xFFFE/F points at our handler
;   - Main loop runs LDA / STA repeatedly while waiting
;   - When sim_6502.py injects IRQ at --irq-at-step N, the handler runs:
;     handler stores 0xFF to mem[0x02FF] and RTIs back to the main loop
;   - INTERRUPT fact emitted at the step where mpu.irq() fired
;   - MEM_WRITE facts show both the main-loop store AND the handler store
;
; Memory map:
;   0x0600  main-loop entry
;   0x0700  IRQ handler
;   0xFFFE  IRQ vector lo = 0x00
;   0xFFFF  IRQ vector hi = 0x07   (handler at 0x0700)
;
; Run:
;   make q-6502-irq INSN=02_interrupt
;   (or: python3 sim_6502.py kit_6502_lessons/02_interrupt.s --irq-at-step 4)

; org 0x0600 — main loop
A9 11         ; LDA #$11    ; A = 0x11
8D 00 02      ; STA $0200   ; mem[0x0200] = 0x11
A9 22         ; LDA #$22    ; A = 0x22
8D 01 02      ; STA $0201   ; mem[0x0201] = 0x22
A9 33         ; LDA #$33    ; A = 0x33   <-- IRQ injected around here in sandbox
8D 02 02      ; STA $0202   ; mem[0x0202] = 0x33
00            ; BRK         ; halt main loop

; org 0x0700 — IRQ handler
A9 FF         ; LDA #$FF    ; handler signals it ran by writing 0xFF
8D FF 02      ; STA $02FF   ; mem[0x02FF] = 0xFF (handler-fired marker)
40            ; RTI         ; return from interrupt

; org 0xFFFE — IRQ vector (16-bit, low byte first per 6502 little-endian)
00 07         ; .word $0700 ; IRQ vector → handler at 0x0700
