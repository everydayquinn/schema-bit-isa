"""
seed_substrate.py — register the substrate-only vocabulary for schema-bit-isa.

This is the slim seed: only the namespaces, predicates, and travelers needed
for cross-substrate facts about register machines (4-bit + 6502). The merged
project's gameplan / decision / contradiction layers are intentionally absent.

Run once before pointing any traveler at corkboard.db.
"""
from __future__ import annotations

from pathlib import Path

import corkboard as cb


HERE       = Path(__file__).parent
DEFAULT_DB = HERE / "corkboard.db"


NAMESPACES = [
    ("insn:", "instruction-level facts (one per decoded instruction, any ISA)",
     "insn:countdown:0x02"),
    ("prog:", "program-level facts (one per ingested program/ROM/binary)",
     "prog:countdown"),
    ("step:", "execution-step facts (one per executed instruction at runtime)",
     "step:countdown:000003"),
    ("sub:",  "subroutine-level facts (one per declared code label)",
     "sub:inc_a"),
]


# (name, domain, range, cardinality, definition, examples)
PREDICATES = [
    # ---- static-decode predicates (parser_6502 / cpu_4bit static side) ----
    ("AT_INSN",      "step", "ref",     "one",
     "the instruction (insn:* subject) executed at this step",
     ["step:countdown:000003 AT_INSN insn:countdown:0x02"]),
    ("AT_ADDRESS",   "insn", "literal", "one",
     "instruction's load address as 0xHHHH hex string",
     ["insn:countdown:0x02 AT_ADDRESS 0x02"]),
    ("HAS_MNEMONIC", "insn", "literal", "one",
     "decoded mnemonic in lowercase",
     ["insn:countdown:0x02 HAS_MNEMONIC sub"]),
    ("HAS_OPERANDS", "insn", "literal", "one",
     "operand string from disassembler; empty if none",
     ["insn:countdown:0x02 HAS_OPERANDS 15"]),
    ("HAS_SIZE",     "insn", "literal", "one",
     "instruction size in bytes as integer string",
     ["insn:countdown:0x02 HAS_SIZE 1"]),
    ("HAS_BYTES",    "insn", "literal", "one",
     "raw byte hex (lowercase, no separators)",
     ["insn:countdown:0x02 HAS_BYTES 3f"]),
    ("IN_PROGRAM",   "insn|sub", "prog", "one",
     "instruction or subroutine belongs to this program",
     ["insn:countdown:0x02 IN_PROGRAM prog:countdown",
      "sub:inc_a IN_PROGRAM prog:probe_jsr_rts"]),
    ("STARTS_AT",    "sub", "literal", "one",
     "subroutine starts at this load address (0xHHHH)",
     ["sub:inc_a STARTS_AT 0x0700"]),
    ("CALLS_SUB",    "insn", "ref", "one",
     "JSR instruction whose operand resolves to a declared label "
     "(subject = call site, object = sub:NAME)",
     ["insn:probe_jsr_rts:0x0600 CALLS_SUB sub:inc_a"]),
    ("IN_SUB",       "insn", "ref", "one",
     "instruction lies between a sub's STARTS_AT and its terminating RTS "
     "(walk-forward rule, single-RTS subs only this session)",
     ["insn:probe_jsr_rts:0x0700 IN_SUB sub:inc_a"]),
    ("RETURNS",      "insn", "ref", "one",
     "RTS that terminates the named subroutine",
     ["insn:probe_jsr_rts:0x0701 RETURNS sub:inc_a"]),
    ("HAS_MD5",      "prog", "literal", "one",
     "md5 of program source as hex",
     ["prog:countdown HAS_MD5 abc123..."]),
    ("INGESTED_AT",  "prog", "literal", "one",
     "ISO-8601 timestamp when this program was first populated",
     ["prog:countdown INGESTED_AT 2026-05-06T15:00:00.000"]),
    ("ENTRY_ADDR",   "prog", "literal", "one",
     "program's entry address as 0xHHHH hex string; derive layer reads "
     "this to auto-promote sub:<prog>:main when no explicit label is "
     "declared at the entry point",
     ["prog:probe_jsr_rts ENTRY_ADDR 0x0600"]),

    # ---- runtime predicates (cpu_4bit_traveler / sim_6502) ----
    ("STEP_SEQ",     "step", "literal", "one",
     "execution-order index, zero-padded to 6 digits",
     ["step:countdown:000003 STEP_SEQ 000003"]),
    ("STEP_AT_ADDR", "step", "literal", "one",
     "PC at the start of this step (hex address)",
     ["step:01_basic:000000 STEP_AT_ADDR 0x0600"]),
    ("DELTA",        "step", "literal", "one",
     "register/flag changes for this step (e.g. 'a=4->3,z=0->1')",
     ["step:countdown:000003 DELTA a=4->3,z=0->1"]),
    ("BRANCH",       "step", "literal", "one",
     "control-flow at end of step: 'linear' | 'taken:0xHHHH' | 'return' | 'halt'",
     ["step:countdown:000005 BRANCH taken:0x01"]),
    ("WRITES_REG",   "step", "literal", "many",
     "register written during this step",
     ["step:countdown:000003 WRITES_REG a"]),
    ("READS_REG",    "step", "literal", "many",
     "register read during this step",
     ["step:countdown:000003 READS_REG a"]),
    ("MEM_READ",     "step", "literal", "many",
     "memory read this step: 0xADDR=0xVAL (excludes opcode/operand fetch)",
     ["step:02_interrupt:000003 MEM_READ 0x0200=0x08"]),
    ("MEM_WRITE",    "step", "literal", "many",
     "memory write this step: 0xADDR=0xVAL",
     ["step:01_basic:000002 MEM_WRITE 0x0200=0x08"]),
    ("INTERRUPT",    "step", "literal", "one",
     "interrupt mark: irq | nmi | brk",
     ["step:01_basic:000003 INTERRUPT brk"]),
    ("CYCLES",       "step", "literal", "one",
     "cycle count consumed by this step",
     ["step:01_basic:000000 CYCLES 2"]),
    ("ENTRY_STATE",  "prog", "literal", "one",
     "initial register/flag state when a sim run starts",
     ["prog:01_basic ENTRY_STATE 'pc=0x0600, a=0 x=0 y=0 sp=0xff p=0x00'"]),
    ("TERMINATED",   "prog", "literal", "one",
     "termination reason: brk@0xHHHH | max-steps | fault@0xHHHH:<exception>",
     ["prog:01_basic TERMINATED brk@0x0606"]),
]


TRAVELERS = [
    ("cpu_4bit",
     "the in-house 4-bit CPU; emits facts from state_log",
     "substrate",
     "cpu_4bit_traveler.py",
     "Re-emits each T-state row from state_log as substrate-layer facts."),
    ("parser_6502",
     "static decode of 6502 lessons via py65 disassembler",
     "substrate",
     "parser_6502.py",
     "Same predicate vocabulary as cpu_4bit. Different word size, different ISA."),
    ("sim_6502",
     "runtime sim of 6502 lessons via py65 (with optional IRQ injection)",
     "substrate",
     "sim_6502.py",
     "Per-step register diff, memory read/write capture, interrupt marks."),
]


def main():
    conn = cb.bootstrap(DEFAULT_DB)
    for prefix, defn, ex in NAMESPACES:
        cb.register_namespace(conn, prefix, defn, ex)
    for name, domain, range_, card, defn, exs in PREDICATES:
        cb.register_predicate(conn, name, domain, range_, card, defn, exs)
    for name, purpose, role, source, note in TRAVELERS:
        cb.register_traveler(conn, name, purpose, role, source, note)
    conn.commit()

    n_ns   = conn.execute("SELECT COUNT(*) FROM namespaces").fetchone()[0]
    n_pred = conn.execute("SELECT COUNT(*) FROM predicates").fetchone()[0]
    n_trav = conn.execute("SELECT COUNT(*) FROM travelers").fetchone()[0]
    print(f"namespaces: {n_ns} | predicates: {n_pred} | travelers: {n_trav}")


if __name__ == "__main__":
    main()
