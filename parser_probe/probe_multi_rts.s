; probe_multi_rts.s — verify derive_calls_and_subs on a sub with two RTS exits.
;
; Layout:
;   main at 0x0600:           JSR conditional_inc, then BRK
;   sub conditional_inc at 0x0700:
;     CPX #$00                ; compare X with 0
;     BEQ +2 ($0706)          ; if X==0, branch over INX+RTS to the early-exit RTS
;     INX                     ; otherwise, X += 1
;     RTS                     ; late return  (fall-through path)
;     RTS                     ; early return (BEQ target)
;
; What this pins:
;   - Both RTS instructions (0x0705 and 0x0706) must get RETURNS sub:conditional_inc.
;     The first-RTS-stop rule would emit RETURNS on 0x0705 only and miss 0x0706.
;     The address-range rule [STARTS_AT, next_STARTS_AT) emits RETURNS on both.
;   - Every instruction at addr in [0x0700, end-of-program) gets IN_SUB.
;   - Main-block instructions (0x0600, 0x0603) get NO IN_SUB (auto-main not yet
;     in this session's scope; see auto-main test below).

; org 0x0600
20 00 07         ; JSR $0700   ; call conditional_inc
00               ; BRK         ; halt main

; org 0x0700
; label conditional_inc 0x0700
E0 00            ; CPX #$00    ; compare X with 0
F0 02            ; BEQ $0706   ; branch over INX+RTS if X==0
E8               ; INX         ; X = X + 1
60               ; RTS         ; late return (fall-through path)
60               ; RTS         ; early return (BEQ target)
