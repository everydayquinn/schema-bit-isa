"""
sim_6502.py — execute a 6502 lesson under py65, emit runtime facts.

Ports sim_6502.py from /home/scrawn/C_Compiler Schema/ — runtime traveler
that emits per-step facts under traveler='sim_6502'. Same predicate
vocabulary as cpu_4bit (AT_INSN, DELTA, BRANCH, STEP_SEQ) plus the
runtime-substrate set (ENTRY_STATE, STEP_AT_ADDR, TERMINATED, MEM_READ,
MEM_WRITE, INTERRUPT, CYCLES).

Three travelers running on three distinct ISA shapes (4-bit register,
8-bit register, eventually JVM stack), all queryable with one SQL query.

Usage:
    python3 sim_6502.py <lesson.s> [options]

Options:
    --scenario NAME       label for this run; defaults to lesson stem
    --irq-at-step N       call mpu.irq() AFTER step N (synthetic step)
    --nmi-at-step N       call mpu.nmi() AFTER step N
    --input ADDR=VAL      memory-mapped input written before run; repeatable
    --max-steps N         step cap (default 200)
    --db PATH             corkboard.db path (default ./corkboard.db)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from py65.devices.mpu6502 import MPU
from py65.disassembler import Disassembler
from py65.memory import ObservableMemory

import corkboard as cb
from parser_6502 import parse_lesson, ensure_traveler as ensure_parser_traveler

HERE       = Path(__file__).parent
DEFAULT_DB = HERE / "corkboard.db"


FLAG_BITS = [
    ("c", 0x01), ("z", 0x02), ("i", 0x04), ("d", 0x08),
    ("b", 0x10), ("u", 0x20), ("v", 0x40), ("n", 0x80),
]


def snapshot(mpu: MPU) -> dict:
    return {"a": mpu.a, "x": mpu.x, "y": mpu.y, "sp": mpu.sp, "pc": mpu.pc, "p": mpu.p}


def fmt_delta(before: dict, after: dict, suppress_pc: bool = True) -> str:
    """Condensed register-change string. PC change usually suppressed
    because it's implicit in BRANCH; un-suppressed for IRQ/NMI steps."""
    parts = []
    for r in ("a", "x", "y", "sp"):
        if before[r] != after[r]:
            parts.append(f"{r}:0x{before[r]:02x}->0x{after[r]:02x}")
    if not suppress_pc and before["pc"] != after["pc"]:
        parts.append(f"pc:0x{before['pc']:04x}->0x{after['pc']:04x}")
    if before["p"] != after["p"]:
        for fname, mask in FLAG_BITS:
            b = 1 if (before["p"] & mask) else 0
            a = 1 if (after["p"]  & mask) else 0
            if b != a:
                parts.append(f"p.{fname}:{b}->{a}")
    return ", ".join(parts)


def fmt_branch(prev_pc: int, prev_size: int, next_pc: int, mnemonic: str) -> str:
    linear = prev_pc + prev_size
    if mnemonic.lower() in ("rti", "rts"):
        return "return"
    if next_pc == linear:
        return "linear"
    return f"taken:0x{next_pc:04x}"


def ensure_sim_traveler(conn) -> None:
    cb.register_traveler(conn, "sim_6502",
        "py65 6502 execution trace; supports IRQ/NMI injection",
        "substrate",
        source="sim_6502.py (ported from /home/scrawn/C_Compiler Schema/sim_6502.py)",
        note="Hooks ObservableMemory + per-step register diff. Same predicate vocabulary as cpu_4bit and parser_6502; runtime traveler complementing parser_6502's static disassembly.")


def retract_scenario(conn, scenario: str):
    """Mark all prior facts for this scenario retracted before re-running.
    Idempotent: re-running sim_6502 for the same scenario produces a single
    consistent snapshot, not accumulated history."""
    conn.execute(
        "UPDATE facts SET retracted_at=strftime('%Y-%m-%dT%H:%M:%f','now') "
        "WHERE traveler='sim_6502' AND retracted_at IS NULL "
        "  AND (subject=? OR subject LIKE ?)",
        (f"prog:{scenario}", f"step:{scenario}:%"),
    )


def parse_input_arg(s: str) -> tuple[int, int]:
    addr_s, val_s = s.split("=", 1)
    return int(addr_s, 0), int(val_s, 0)


def emit_step(conn, ctx, step_subj, insn_subj, seq, pc, delta, branch,
              cycles, writes, reads, interrupt=None):
    """Emit the canonical step-level facts."""
    cb.emit(conn, "sim_6502", step_subj, "STEP_SEQ",     f"{seq:06d}",       captured_in_context=ctx)
    cb.emit(conn, "sim_6502", step_subj, "STEP_AT_ADDR", f"0x{pc:04x}",      captured_in_context=ctx)
    if insn_subj is not None:
        cb.emit(conn, "sim_6502", step_subj, "AT_INSN", insn_subj, object_kind="ref", captured_in_context=ctx)
    cb.emit(conn, "sim_6502", step_subj, "DELTA",        delta or "(no-change)", captured_in_context=ctx)
    cb.emit(conn, "sim_6502", step_subj, "BRANCH",       branch,             captured_in_context=ctx)
    cb.emit(conn, "sim_6502", step_subj, "CYCLES",       str(cycles),        captured_in_context=ctx)
    if interrupt:
        cb.emit(conn, "sim_6502", step_subj, "INTERRUPT", interrupt,         captured_in_context=ctx)
    for addr, val in writes:
        cb.emit(conn, "sim_6502", step_subj, "MEM_WRITE",
                f"0x{addr:04x}=0x{val:02x}", captured_in_context=ctx)
    for addr, val in reads:
        cb.emit(conn, "sim_6502", step_subj, "MEM_READ",
                f"0x{addr:04x}=0x{val:02x}", captured_in_context=ctx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson")
    ap.add_argument("--scenario")
    ap.add_argument("--irq-at-step", type=int, default=-1)
    ap.add_argument("--nmi-at-step", type=int, default=-1)
    ap.add_argument("--input", action="append", default=[],
                    help="ADDR=VAL memory-mapped input; repeatable")
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    lesson = Path(args.lesson)
    if not lesson.exists():
        sys.exit(f"no lesson at {lesson}")

    conn = cb.bootstrap(Path(args.db))
    ensure_sim_traveler(conn)
    ensure_parser_traveler(conn)  # so AT_INSN refs to insn:* are present even
                                   # if parser_6502 hasn't been run separately

    scenario = args.scenario or lesson.stem
    insns, entry_addr = parse_lesson(lesson)

    # Build memory + load all bytes (including vector regions)
    mpu = MPU()
    mpu.memory = ObservableMemory()
    for addr, bts, _ in insns:
        for i, b in enumerate(bts):
            mpu.memory[addr + i] = b
    mpu.pc = entry_addr

    inputs = [parse_input_arg(s) for s in args.input]
    for addr, val in inputs:
        mpu.memory[addr] = val

    # Hook writes and reads. Read callback must NOT go through mpu.memory[addr]
    # (that re-enters __getitem__ and recurses); use the underlying list at
    # mpu.memory._subject[addr]. This was a real bug in the original sim_6502
    # — see C_Compiler Schema/NEXT_SESSION.md ("mem-read-fix" was the S160
    # deliverable).
    step_writes: list[tuple[int, int]] = []
    step_reads:  list[tuple[int, int]] = []
    def on_write(addr, val):
        step_writes.append((addr, val))
    def on_read(addr):
        step_reads.append((addr, mpu.memory._subject[addr]))
        return None
    mpu.memory.subscribe_to_write(range(0x0000, 0x10000), on_write)
    mpu.memory.subscribe_to_read(range(0x0000, 0x10000), on_read)

    retract_scenario(conn, scenario)

    ctx = {
        "session_marker": "session_6_2026-05-04",
        "via":            "sim_6502.py — Day 2 runtime substrate from Kairos",
        "lesson":         str(lesson),
        "scenario":       scenario,
        "irq_at":         args.irq_at_step,
        "nmi_at":         args.nmi_at_step,
        "max_steps":      args.max_steps,
    }

    prog_subj = f"prog:{scenario}"
    entry_desc = (f"pc=0x{entry_addr:04x}, a=0 x=0 y=0 sp=0xff p=0x00, "
                  f"inputs={inputs!r}, irq_at={args.irq_at_step}, nmi_at={args.nmi_at_step}")
    cb.emit(conn, "sim_6502", prog_subj, "ENTRY_STATE", entry_desc, captured_in_context=ctx)

    disasm = Disassembler(mpu)
    seq = 0
    termination = "max-steps"
    while seq < args.max_steps:
        pc_before = mpu.pc
        size, dis = disasm.instruction_at(pc_before)
        mnemonic = dis.split()[0] if dis else ""

        # Detect BRK before execution — it would jump to the IRQ vector,
        # but for unprepared programs we want clean termination semantics.
        if mpu.memory[pc_before] == 0x00:
            step_writes.clear()
            step_reads.clear()
            step_subj = f"step:{scenario}:{seq:06d}"
            insn_subj = f"insn:{scenario}:0x{pc_before:04x}"
            # Real 6502 BRK consumes 7 cycles even though we short-circuit termination.
            emit_step(conn, ctx, step_subj, insn_subj, seq, pc_before,
                      delta="", branch="return", cycles=7,
                      writes=[], reads=[], interrupt="brk")
            termination = f"brk@0x{pc_before:04x}"
            seq += 1
            break

        before = snapshot(mpu)
        step_writes.clear()
        step_reads.clear()
        cycles_before = mpu.processorCycles
        try:
            mpu.step()
        except Exception as e:
            termination = f"fault@0x{pc_before:04x}:{type(e).__name__}"
            break
        after = snapshot(mpu)
        cycles = mpu.processorCycles - cycles_before

        step_subj = f"step:{scenario}:{seq:06d}"
        insn_subj = f"insn:{scenario}:0x{pc_before:04x}"

        # Filter MEM_READ to data reads only — exclude opcode/operand fetches
        # (instruction bytes at [pc_before, pc_before+size)).
        data_reads = [(a, v) for (a, v) in step_reads
                      if not (pc_before <= a < pc_before + size)]

        emit_step(conn, ctx, step_subj, insn_subj, seq, pc_before,
                  delta=fmt_delta(before, after),
                  branch=fmt_branch(pc_before, size, after["pc"], mnemonic),
                  cycles=cycles,
                  writes=list(step_writes),
                  reads=data_reads)

        # IRQ / NMI injection AFTER this step's facts are written
        if seq == args.irq_at_step:
            seq += 1
            step_reads.clear()
            inj_before = snapshot(mpu)
            mpu.irq()
            inj_after = snapshot(mpu)
            inj_subj = f"step:{scenario}:{seq:06d}"
            emit_step(conn, ctx, inj_subj, None, seq, inj_after['pc'],
                      delta=fmt_delta(inj_before, inj_after, suppress_pc=False),
                      branch=f"taken:0x{inj_after['pc']:04x}",
                      cycles=7,
                      writes=[],
                      reads=[(a, v) for (a, v) in step_reads])
            cb.emit(conn, "sim_6502", inj_subj, "INTERRUPT", "irq", captured_in_context=ctx)
        elif seq == args.nmi_at_step:
            seq += 1
            step_reads.clear()
            inj_before = snapshot(mpu)
            mpu.nmi()
            inj_after = snapshot(mpu)
            inj_subj = f"step:{scenario}:{seq:06d}"
            emit_step(conn, ctx, inj_subj, None, seq, inj_after['pc'],
                      delta=fmt_delta(inj_before, inj_after, suppress_pc=False),
                      branch=f"taken:0x{inj_after['pc']:04x}",
                      cycles=7,
                      writes=[],
                      reads=[(a, v) for (a, v) in step_reads])
            cb.emit(conn, "sim_6502", inj_subj, "INTERRUPT", "nmi", captured_in_context=ctx)

        seq += 1

    cb.emit(conn, "sim_6502", prog_subj, "TERMINATED", termination, captured_in_context=ctx)
    conn.commit()

    n_live = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE traveler='sim_6502' AND retracted_at IS NULL "
        "  AND (subject=? OR subject LIKE ?)",
        (prog_subj, f"step:{scenario}:%"),
    ).fetchone()[0]
    conn.close()

    print(f"lesson:    {lesson}")
    print(f"scenario:  {scenario}")
    print(f"steps:     {seq}")
    print(f"end:       {termination}")
    print(f"sim_6502 facts (live, this scenario): {n_live}")


if __name__ == "__main__":
    main()
