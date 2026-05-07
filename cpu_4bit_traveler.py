"""
cpu_4bit_traveler.py — emit 4-bit CPU execution as facts.

Runs a 4-bit program on the existing CPU substrate (cpu.py + schema.sql),
then re-emits each cycle of state_log as a fact under traveler='cpu_4bit'
in corkboard.db. The same predicate vocabulary used here will be used by
parser_6502 (Day 2) and parser_jvm (Day 3) — that's the substrate-
independence claim made operational.

Predicates emitted (substrate layer):
    insn:<prog>:0xHH    AT_ADDRESS, HAS_MNEMONIC, HAS_OPERANDS,
                        HAS_BYTES, HAS_SIZE, IN_PROGRAM
    prog:<prog>         HAS_MD5, INGESTED_AT
    step:<prog>:NNNNNN  AT_INSN, STEP_SEQ, DELTA, BRANCH,
                        WRITES_REG (×N), READS_REG (×N)

The DELTA is computed as the diff between this cycle's end-state and the
previous cycle's end-state. BRANCH is 'linear' | 'taken:0xHH' | 'halt',
derived from PC behaviour and hlt signal.

Usage:
    python3 cpu_4bit_traveler.py                  # runs countdown demo
    python3 cpu_4bit_traveler.py <prog_name>      # by registered name
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import corkboard as cb
from cpu import CPU

HERE        = Path(__file__).parent
SCHEMA_FILE = HERE / "schema.sql"
CPU_DB      = HERE / "cpu.db"
CB_DB       = HERE / "corkboard.db"


# ------------------------------------------------------------------
# Programs in the canonical "4-bit CPU programs" library
# ------------------------------------------------------------------
PROGRAMS = {
    "countdown": {
        "description": "Counts 5 down to 0 using SUB + JZ; first runtime-decision program on this CPU.",
        "bytes": [
            0x1E,  # 0: LDA 14    ; A = 5
            0x3F,  # 1: SUB 15    ; A -= 1; sets Z when A==0
            0xB4,  # 2: JZ  4     ; if Z, go to 4
            0x51,  # 3: JMP 1     ; loop back
            0x60,  # 4: OUT       ; OUT = A (=0)
            0xF0,  # 5: HLT
            0,0,0,0,0,0,0,0,
            5, 1,  # 14: data 5; 15: data 1
        ],
    },
    "add": {
        "description": "3 + 4 = 7. Smallest demonstrative program.",
        "bytes": [
            0x1E,  # LDA 14
            0x2F,  # ADD 15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            3, 4,
        ],
    },
}


# Map opcode nybble → mnemonic (mirrors schema.sql opcodes table)
OP_MNEMONIC = {
    0x0: "nop", 0x1: "lda", 0x2: "add", 0x3: "sub", 0x4: "sta",
    0x5: "jmp", 0x6: "out", 0x7: "and", 0x8: "or",  0x9: "xor",
    0xA: "not", 0xB: "jz",  0xF: "hlt",
}


# Registers we track for DELTA / WRITES_REG (4-bit CPU has these)
TRACKED_REGS = ("pc", "a", "b", "alu", "bus", "out", "halted", "z")


def fresh_cpu_db():
    """Apply schema fresh to cpu.db (ensures clean state for the traveler run)."""
    if CPU_DB.exists():
        CPU_DB.unlink()
    conn = sqlite3.connect(CPU_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_FILE.read_text())
    return conn


def load_program(conn, words: list[int]) -> None:
    conn.execute("DELETE FROM ram")
    padded = list(words) + [0] * (16 - len(words))
    for addr, val in enumerate(padded[:16]):
        conn.execute("INSERT INTO ram(addr,value) VALUES(?,?)", (addr, val & 0xFF))
    conn.commit()


def run_program(prog_name: str, prog_bytes: list[int], max_cycles: int = 256):
    """Run prog_bytes on a fresh 4-bit CPU; return (state_log_rows, end_state)."""
    conn = fresh_cpu_db()
    load_program(conn, prog_bytes)
    cpu = CPU(conn)
    cpu.run(max_cycles=max_cycles)

    rows = list(conn.execute("""
        SELECT step, cycle, instr, t, pc, mar, ir, a, b, alu, bus, out, halted, z, signals
        FROM state_log ORDER BY step
    """).fetchall())
    end = {
        "halted": cpu.halted, "cycle": cpu.cycle,
        "out": cpu.out, "a": cpu.a, "b": cpu.b, "z": cpu.z,
    }
    conn.close()
    return rows, end


# ------------------------------------------------------------------
# Per-cycle fact derivation
# ------------------------------------------------------------------
def per_cycle_summary(rows):
    """Group state_log rows by cycle, return list of dicts:
        {cycle, mnemonic, fetch_pc, ir_byte, end_state, taken_branch, halted}
    'fetch_pc' is the PC at the fetch T0 row of the cycle = the instruction's
    address. 'ir_byte' is the IR latched after fetch T1 = the instruction byte.
    'taken_branch' is the destination address if PC was loaded from bus
    (j or jc fired), else None.
    """
    by_cycle = {}
    for r in rows:
        by_cycle.setdefault(r["cycle"], []).append(r)

    out = []
    for cycle in sorted(by_cycle):
        crows = sorted(by_cycle[cycle], key=lambda r: r["t"])
        # fetch T0: PC->MAR (signals contain 'co,mi'); state.pc here is the
        # PC value as printed at end of fire(). For T0, PC has just been
        # driven to bus; CE happens at T1 so pc is still the fetch address.
        # Actually, fire() at T0 runs co,mi which doesn't change PC. So
        # crows[0]["pc"] IS the instruction's address.
        fetch_pc = crows[0]["pc"]
        # IR is latched at T1 (ro,ii,ce). After T1 fires, ir holds the byte
        # and pc has been incremented. crows[1]["ir"] is the instruction byte.
        ir_byte = crows[1]["ir"] if len(crows) > 1 else crows[0]["ir"]

        # Mnemonic: instr column on the execute T-state rows (not 'fetch')
        # First non-fetch row's instr is the canonical mnemonic.
        mnemonic = None
        for r in crows:
            if r["instr"] != "fetch":
                mnemonic = r["instr"].lower()
                break
        if mnemonic is None:
            # All-fetch cycle — shouldn't happen, but fall back to opcode decode
            mnemonic = OP_MNEMONIC.get((ir_byte >> 4) & 0xF, "?")

        # End state: last row's register snapshot
        end_state = {k: crows[-1][k] for k in TRACKED_REGS}

        # Branch detection: did j or jc actually load PC from bus?
        # Look at execute rows' signals for 'j,' or 'jc,' (then z=1 case).
        taken_branch = None
        halted = False
        for r in crows:
            sigs = (r["signals"] or "").split(",")
            if "j" in sigs:
                # Unconditional jump — bus value loaded into PC
                taken_branch = r["bus"] & 0xF
            elif "jc" in sigs and r["z"]:
                taken_branch = r["bus"] & 0xF
            if "hlt" in sigs:
                halted = True

        out.append({
            "cycle": cycle,
            "mnemonic": mnemonic,
            "fetch_pc": fetch_pc,
            "ir_byte": ir_byte,
            "end_state": end_state,
            "taken_branch": taken_branch,
            "halted": halted,
        })
    return out


def make_delta(prev_state, curr_state) -> str:
    """Condensed register-change string: 'a=5->4,z=0->1'. Empty if no change."""
    if prev_state is None:
        return ",".join(f"{k}={curr_state[k]}" for k in TRACKED_REGS if curr_state[k])
    parts = []
    for k in TRACKED_REGS:
        if prev_state[k] != curr_state[k]:
            parts.append(f"{k}={prev_state[k]}->{curr_state[k]}")
    return ",".join(parts)


def written_regs(prev_state, curr_state) -> list[str]:
    if prev_state is None:
        return [k for k in TRACKED_REGS if curr_state[k]]
    return [k for k in TRACKED_REGS if prev_state[k] != curr_state[k]]


def operand_of(ir_byte: int) -> int:
    return ir_byte & 0x0F


# ------------------------------------------------------------------
# Emission
# ------------------------------------------------------------------
def emit_program(conn, prog_name: str, prog_bytes: list[int],
                 description: str | None = None) -> dict:
    """Run the program on the 4-bit CPU; emit all facts under cpu_4bit traveler.
    Returns counts dict for verification."""

    rows, end = run_program(prog_name, prog_bytes)
    cycles = per_cycle_summary(rows)

    md5 = hashlib.md5(bytes(prog_bytes).hex().encode()).hexdigest()
    ts = datetime.now().isoformat(timespec="milliseconds")

    prog_subj = f"prog:{prog_name}"

    ctx = {
        "session_marker": "session_6_2026-05-04",
        "via":            "cpu_4bit_traveler.py — Day 1 substrate-traveler establishment",
        "program":        prog_name,
        "byte_length":    len(prog_bytes),
        "halted":         end["halted"],
        "cycle_count":    end["cycle"],
    }

    # ---- prog facts
    cb.emit(conn, "cpu_4bit", prog_subj, "HAS_MD5", md5,
            captured_in_context=ctx)
    cb.emit(conn, "cpu_4bit", prog_subj, "INGESTED_AT", ts,
            captured_in_context=ctx)

    # ---- per-instruction facts (one row per UNIQUE address executed)
    seen_insns = set()
    for c in cycles:
        addr = c["fetch_pc"]
        if addr in seen_insns:
            continue
        seen_insns.add(addr)
        insn_subj = f"insn:{prog_name}:0x{addr:02x}"
        cb.emit(conn, "cpu_4bit", insn_subj, "IN_PROGRAM", prog_subj, object_kind="ref",
                captured_in_context=ctx)
        cb.emit(conn, "cpu_4bit", insn_subj, "AT_ADDRESS", f"0x{addr:02x}",
                captured_in_context=ctx)
        cb.emit(conn, "cpu_4bit", insn_subj, "HAS_MNEMONIC", c["mnemonic"],
                captured_in_context=ctx)
        operand = operand_of(c["ir_byte"])
        cb.emit(conn, "cpu_4bit", insn_subj, "HAS_OPERANDS", str(operand),
                captured_in_context=ctx,
                notes_for_claude={"note": "4-bit operand; 0..15 only"})
        cb.emit(conn, "cpu_4bit", insn_subj, "HAS_SIZE", "1",
                captured_in_context=ctx,
                notes_for_claude={"note": "every 4-bit CPU instruction is exactly 1 byte (4-bit op + 4-bit operand)"})
        cb.emit(conn, "cpu_4bit", insn_subj, "HAS_BYTES", f"{c['ir_byte']:02x}",
                captured_in_context=ctx)

    # ---- per-step facts (one row per cycle)
    prev_state = None
    for seq, c in enumerate(cycles):
        step_subj = f"step:{prog_name}:{seq:06d}"
        insn_subj = f"insn:{prog_name}:0x{c['fetch_pc']:02x}"

        cb.emit(conn, "cpu_4bit", step_subj, "STEP_SEQ", f"{seq:06d}",
                captured_in_context=ctx)
        cb.emit(conn, "cpu_4bit", step_subj, "AT_INSN", insn_subj, object_kind="ref",
                captured_in_context=ctx)

        delta = make_delta(prev_state, c["end_state"])
        cb.emit(conn, "cpu_4bit", step_subj, "DELTA", delta or "(no-change)",
                captured_in_context=ctx,
                notes_for_claude={
                    "encoding": [{"type": "prose", "value": "register-change string, comma-separated"}],
                    "tracked_regs": list(TRACKED_REGS),
                })

        # Branch
        if c["halted"]:
            cb.emit(conn, "cpu_4bit", step_subj, "BRANCH", "halt", captured_in_context=ctx)
        elif c["taken_branch"] is not None:
            cb.emit(conn, "cpu_4bit", step_subj, "BRANCH", f"taken:0x{c['taken_branch']:02x}",
                    captured_in_context=ctx)
        else:
            cb.emit(conn, "cpu_4bit", step_subj, "BRANCH", "linear", captured_in_context=ctx)

        # WRITES_REG (one fact per register changed this cycle)
        for reg in written_regs(prev_state, c["end_state"]):
            cb.emit(conn, "cpu_4bit", step_subj, "WRITES_REG", reg, captured_in_context=ctx)

        prev_state = c["end_state"]

    return {
        "program": prog_name,
        "cycles": len(cycles),
        "unique_insns": len(seen_insns),
        "halted": end["halted"],
        "final_out": end["out"],
        "final_a": end["a"],
    }


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main():
    prog_name = sys.argv[1] if len(sys.argv) > 1 else "countdown"
    if prog_name not in PROGRAMS:
        sys.exit(f"unknown program {prog_name!r}; known: {sorted(PROGRAMS)}")

    prog = PROGRAMS[prog_name]

    conn = cb.bootstrap(CB_DB)
    counts = emit_program(conn, prog_name, prog["bytes"], prog["description"])
    conn.commit()

    # Verify
    n_facts = conn.execute(
        "SELECT COUNT(*) AS n FROM v_facts_live WHERE traveler='cpu_4bit'"
    ).fetchone()["n"]

    print(f"program:        {counts['program']}")
    print(f"cycles:         {counts['cycles']}")
    print(f"unique insns:   {counts['unique_insns']}")
    print(f"halted:         {counts['halted']}")
    print(f"final OUT:      {counts['final_out']}")
    print(f"final A:        {counts['final_a']}")
    print(f"cpu_4bit facts (live): {n_facts}")
    conn.close()


if __name__ == "__main__":
    main()
