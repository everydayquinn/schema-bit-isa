; probe_auto_label.s — verify auto-label derivation.
;
; Main calls a subroutine at 0x0700 via JSR. The sub at 0x0700 is NOT
; declared with a ; label directive. derive_calls_and_subs must:
;   - auto-promote 0x0700 to sub:probe_auto_label:auto_0x0700
;   - emit CALLS_SUB pointing at the auto-generated sub
;   - emit IN_SUB and RETURNS via the address-range walk
;
; Also exercises auto-main: 0x0600 has no label directive, so the derive
; layer must auto-promote the entry_addr to sub:probe_auto_label:main.

; org 0x0600
20 00 07     ; JSR $0700   ; calls an undeclared sub at 0x0700
00           ; BRK         ; halt main

; org 0x0700
; (no ; label directive — the derive layer must auto-promote)
E8           ; INX
60           ; RTS
