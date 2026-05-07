"""
parser_6502.py — static-decode 6502 lesson .s files into corkboard.db.

Ports populate_6502.py from /home/scrawn/C_Compiler Schema/ — static
disassembler that emits one fact per instruction under traveler='parser_6502'.
Same predicate vocabulary as cpu_4bit traveler — substrate-independence
across register-machine ISAs becomes a SQL query.

Lesson format (kit_6502_lessons/*.s):
    Lines starting with `;` are comments (or directives like `; org 0xNNNN`).
    Non-comment lines have `<hex bytes>  ; <mnemonic + operand>`.
    `; org 0xNNNN` resets the load address for subsequent bytes.

Predicates emitted (substrate vocabulary, shared with cpu_4bit):
    insn:<prog>:0xHHHH    AT_ADDRESS, HAS_MNEMONIC, HAS_OPERANDS,
                          HAS_BYTES, HAS_SIZE, IN_PROGRAM
    prog:<prog>           HAS_MD5, INGESTED_AT

Usage:
    python3 parser_6502.py <lesson.s> [corkboard.db]
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

from py65.devices.mpu6502 import MPU
from py65.disassembler import Disassembler

import corkboard as cb

HERE      = Path(__file__).parent
DEFAULT_DB = HERE / "corkboard.db"


HEX_BYTE_RE = re.compile(r'^[0-9A-Fa-f]{2}$')
ORG_RE      = re.compile(r';\s*org\s+0x([0-9A-Fa-f]+)', re.IGNORECASE)


def parse_lesson(path: Path) -> tuple[list[tuple[int, bytes, str]], int]:
    """Parse a lesson .s into [(addr, bytes, comment), ...] + entry_addr.

    `; org 0xNNNN` directives reset the cursor. Each non-comment line
    contributes one instruction's bytes (concatenated hex tokens until ';').
    """
    addr = 0x0600  # default load (py65 monitor convention)
    entry = None
    out: list[tuple[int, bytes, str]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = ORG_RE.search(line)
        if m and line.lstrip().startswith(";"):
            addr = int(m.group(1), 16)
            continue
        if line.lstrip().startswith(";"):
            continue
        if ";" in line:
            byte_part, comment = line.split(";", 1)
            comment = comment.strip()
        else:
            byte_part, comment = line, ""
        tokens = byte_part.split()
        bts = []
        for tok in tokens:
            if HEX_BYTE_RE.match(tok):
                bts.append(int(tok, 16))
            else:
                sys.exit(f"{path}:{raw_line!r}: bad hex token {tok!r}")
        if not bts:
            continue
        out.append((addr, bytes(bts), comment))
        if entry is None and addr < 0xFF00:  # entry = first non-vector load
            entry = addr
        addr += len(bts)
    if entry is None:
        entry = out[0][0] if out else 0x0600
    return out, entry


def disassemble(insn_addr: int, insn_bytes: bytes,
                full_memory: list[int]) -> tuple[str, str, int]:
    """Return (mnemonic, op_str, size) using py65's disassembler."""
    mpu = MPU()
    mpu.memory = list(full_memory)
    d = Disassembler(mpu)
    size, dis = d.instruction_at(insn_addr)
    parts = dis.split(None, 1)
    mnemonic = parts[0]
    op_str   = parts[1] if len(parts) > 1 else ""
    return mnemonic, op_str, size


def ensure_traveler(conn) -> None:
    """Register parser_6502 traveler if not already present."""
    cb.register_traveler(conn, "parser_6502",
        "static decode of 6502 lesson .s files via py65 disassembler",
        "substrate",
        source="parser_6502.py (ported from /home/scrawn/C_Compiler Schema/populate_6502.py)",
        note="Asserts mechanically from py65 disassembly. Same predicate vocabulary as cpu_4bit; cross-substrate query works on (cpu_4bit, parser_6502) without modification.")


def populate(conn, lesson_path: Path) -> dict:
    """Parse + disassemble + emit facts. Returns counts dict for verification."""
    if not lesson_path.exists():
        raise FileNotFoundError(f"no lesson at {lesson_path}")

    ensure_traveler(conn)

    prog_name = lesson_path.stem
    md5 = hashlib.md5(lesson_path.read_bytes()).hexdigest()
    ts  = datetime.now().isoformat(timespec="milliseconds")

    insns, entry_addr = parse_lesson(lesson_path)
    if not insns:
        raise ValueError(f"no instructions parsed from {lesson_path}")

    # Build a 64KB memory image for the disassembler to look up context
    mem = [0] * 0x10000
    for addr, bts, _ in insns:
        for i, b in enumerate(bts):
            mem[addr + i] = b

    ctx = {
        "session_marker":   "session_6_2026-05-04",
        "via":              "parser_6502.py — Day 2 substrate port from Kairos",
        "lesson":           str(lesson_path),
        "md5":              md5,
        "byte_length":      sum(len(b) for _, b, _ in insns),
        "entry_addr":       f"0x{entry_addr:04x}",
    }

    prog_subj = f"prog:{prog_name}"
    cb.emit(conn, "parser_6502", prog_subj, "HAS_MD5",     md5, captured_in_context=ctx)
    cb.emit(conn, "parser_6502", prog_subj, "INGESTED_AT", ts,  captured_in_context=ctx)

    n_insns = 0
    for addr, bts, _comment in insns:
        # Skip vector regions for instruction emission; they're data, not code
        if addr >= 0xFF00:
            continue
        mnemonic, op_str, size = disassemble(addr, bts, mem)
        if size != len(bts):
            raise ValueError(
                f"size mismatch at 0x{addr:04x}: declared {len(bts)} bytes, "
                f"disassembler says {size}-byte {mnemonic}"
            )
        insn_subj = f"insn:{prog_name}:0x{addr:04x}"
        cb.emit(conn, "parser_6502", insn_subj, "IN_PROGRAM",   prog_subj, object_kind="ref", captured_in_context=ctx)
        cb.emit(conn, "parser_6502", insn_subj, "AT_ADDRESS",   f"0x{addr:04x}",        captured_in_context=ctx)
        cb.emit(conn, "parser_6502", insn_subj, "HAS_MNEMONIC", mnemonic.lower(),       captured_in_context=ctx)
        cb.emit(conn, "parser_6502", insn_subj, "HAS_OPERANDS", op_str,                 captured_in_context=ctx)
        cb.emit(conn, "parser_6502", insn_subj, "HAS_SIZE",     str(size),              captured_in_context=ctx)
        cb.emit(conn, "parser_6502", insn_subj, "HAS_BYTES",    bts.hex(),              captured_in_context=ctx)
        n_insns += 1

    return {
        "program":      prog_name,
        "lesson_path":  str(lesson_path),
        "instructions": n_insns,
        "md5":          md5,
        "entry_addr":   entry_addr,
    }


def main():
    if len(sys.argv) < 2:
        sys.exit(f"usage: python3 {sys.argv[0]} <lesson.s> [corkboard.db]")
    lesson = Path(sys.argv[1])
    db     = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB

    conn = cb.bootstrap(db)
    info = populate(conn, lesson)
    conn.commit()

    n_live = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE traveler='parser_6502' AND retracted_at IS NULL"
    ).fetchone()[0]

    print(f"lesson:        {info['lesson_path']}")
    print(f"program:       {info['program']}  ({info['instructions']} insns)")
    print(f"md5:           {info['md5'][:16]}...")
    print(f"entry addr:    0x{info['entry_addr']:04x}")
    print(f"parser_6502 facts (live): {n_live}")
    conn.close()


if __name__ == "__main__":
    main()
