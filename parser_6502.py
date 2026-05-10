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
                          CALLS_SUB (JSR -> sub:NAME when operand resolves
                                     to a declared label)
                          IN_SUB    (insn lies between sub start and its RTS)
                          RETURNS   (RTS -> sub:NAME for the sub it terminates)
    sub:<name>            IN_PROGRAM, STARTS_AT
    prog:<prog>           HAS_MD5, INGESTED_AT

Lesson directives:
    ; org 0xNNNN          — sets the load-address cursor for following bytes
    ; label NAME 0xNNNN   — declares a code label NAME at NNNN; subsequent
                            facts use sub:NAME as the subject for sub-level
                            edges and as the object for CALLS_SUB / IN_SUB /
                            RETURNS edges.

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
LABEL_RE    = re.compile(r';\s*label\s+([A-Za-z_][A-Za-z_0-9]*)\s+0x([0-9A-Fa-f]+)',
                         re.IGNORECASE)
JSR_OPERAND_RE = re.compile(r'^\$([0-9A-Fa-f]+)$')


def parse_lesson(path: Path) -> tuple[list[tuple[int, bytes, str]], int, dict[int, str]]:
    """Parse a lesson .s into ([(addr, bytes, comment), ...], entry_addr, labels).

    `; org 0xNNNN` directives reset the cursor.
    `; label NAME 0xNNNN` directives declare a code label at NNNN.
    Each non-comment line contributes one instruction's bytes
    (concatenated hex tokens until ';').
    """
    addr = 0x0600  # default load (py65 monitor convention)
    entry = None
    out: list[tuple[int, bytes, str]] = []
    labels: dict[int, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith(";"):
            m_label = LABEL_RE.search(line)
            if m_label:
                lbl_name = m_label.group(1)
                lbl_addr = int(m_label.group(2), 16)
                if lbl_addr in labels and labels[lbl_addr] != lbl_name:
                    sys.exit(f"{path}: label collision at 0x{lbl_addr:04x}: "
                             f"{labels[lbl_addr]!r} vs {lbl_name!r}")
                labels[lbl_addr] = lbl_name
                continue
            m_org = ORG_RE.search(line)
            if m_org:
                addr = int(m_org.group(1), 16)
                continue
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
    return out, entry, labels


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


def derive_calls_and_subs(conn, prog_subj: str, ctx: dict | None = None) -> dict:
    """Derive sub-discovery and call/return/membership edges for one program.

    Pipeline (each step reads what previous steps emitted):

      1. Collect all explicitly-declared subs (subjects with `IN_PROGRAM
         prog_subj` and a `STARTS_AT` fact).
      2. Auto-promote: read the program's `ENTRY_ADDR`. If no declared sub
         starts at that address, emit a synthetic `sub:<prog>:main` with
         `IN_PROGRAM` and `STARTS_AT` facts.
      3. Auto-label: scan decoded instructions for JSR whose target
         resolves to an in-program address with no matching sub. For each
         such target, emit `sub:<prog>:auto_0xHHHH` with `IN_PROGRAM` and
         `STARTS_AT` facts.
      4. Walk: with the full sub list sorted by start address, every
         instruction at addr in [STARTS_AT(sub), STARTS_AT(next sub) or
         end-of-space) gets `IN_SUB sub:NAME`. Every RTS in that range
         also gets `RETURNS sub:NAME`. Multi-RTS subs work correctly.
      5. CALLS_SUB: every JSR whose operand resolves to a sub's start
         address (now including auto-promoted and auto-labeled subs)
         gets `CALLS_SUB sub:NAME`.

    Naming convention: explicit labels stay bare (`sub:inc_a`); auto-
    generated subs are program-namespaced (`sub:<prog>:main`,
    `sub:<prog>:auto_0xHHHH`) so multiple programs in the same DB don't
    collide.

    Idempotency: emitting the same edge twice is harmless under
    cardinality='one' (the second is a no-op via the predicate's
    uniqueness rule).
    """
    ctx = dict(ctx or {})
    ctx.setdefault("via_derive", "derive_calls_and_subs")

    prog_name = prog_subj.split(":", 1)[1] if ":" in prog_subj else prog_subj

    def _hex_to_int(hex_str: str) -> int:
        s = hex_str.lower()
        if s.startswith("0x"):
            s = s[2:]
        return int(s, 16)

    # ----- 1. Collect explicitly-declared subs --------------------
    subs: list[tuple[int, str]] = []
    for r in conn.execute(
        "SELECT s.subject AS sub_subj, sa.object AS start_hex "
        "  FROM v_facts_live s "
        "  JOIN v_facts_live sa "
        "    ON sa.traveler = s.traveler "
        "   AND sa.subject  = s.subject "
        "   AND sa.predicate = 'STARTS_AT' "
        " WHERE s.traveler  = 'parser_6502' "
        "   AND s.predicate = 'IN_PROGRAM' "
        "   AND s.object    = ? "
        "   AND s.subject   LIKE 'sub:%'",
        (prog_subj,),
    ):
        subs.append((_hex_to_int(r["start_hex"]), r["sub_subj"]))

    # ----- collect decoded instructions ---------------------------
    insns: list[tuple[int, str, str, str]] = []
    for r in conn.execute(
        "SELECT i.subject AS insn_subj, "
        "       a.object  AS addr_hex, "
        "       m.object  AS mnem, "
        "       o.object  AS ops "
        "  FROM v_facts_live i "
        "  JOIN v_facts_live a ON a.traveler='parser_6502' AND a.subject=i.subject AND a.predicate='AT_ADDRESS' "
        "  JOIN v_facts_live m ON m.traveler='parser_6502' AND m.subject=i.subject AND m.predicate='HAS_MNEMONIC' "
        "  JOIN v_facts_live o ON o.traveler='parser_6502' AND o.subject=i.subject AND o.predicate='HAS_OPERANDS' "
        " WHERE i.traveler  = 'parser_6502' "
        "   AND i.predicate = 'IN_PROGRAM' "
        "   AND i.object    = ? "
        "   AND i.subject   LIKE 'insn:%'",
        (prog_subj,),
    ):
        insns.append((_hex_to_int(r["addr_hex"]), r["insn_subj"],
                      r["mnem"], r["ops"] or ""))
    insns.sort()

    insn_addr_set = {addr for addr, _, _, _ in insns}
    declared_addrs = {start for start, _ in subs}
    counts = {"IN_SUB": 0, "RETURNS": 0, "CALLS_SUB": 0,
              "auto_main": 0, "auto_label": 0}

    # ----- 2. Auto-main: entry_addr -> sub:<prog>:main ------------
    entry_row = conn.execute(
        "SELECT object FROM v_facts_live "
        " WHERE traveler='parser_6502' AND predicate='ENTRY_ADDR' AND subject=?",
        (prog_subj,),
    ).fetchone()
    if entry_row is not None:
        entry_addr = _hex_to_int(entry_row["object"])
        if entry_addr in insn_addr_set and entry_addr not in declared_addrs:
            main_subj = f"sub:{prog_name}:main"
            cb.emit(conn, "parser_6502", main_subj, "IN_PROGRAM", prog_subj,
                    object_kind="ref", captured_in_context=ctx)
            cb.emit(conn, "parser_6502", main_subj, "STARTS_AT",
                    f"0x{entry_addr:04x}", captured_in_context=ctx)
            subs.append((entry_addr, main_subj))
            declared_addrs.add(entry_addr)
            counts["auto_main"] = 1

    # ----- 3. Auto-label: JSR targets with no matching sub --------
    jsr_targets_needing_label: set[int] = set()
    for _addr, _isubj, mnem, ops in insns:
        if mnem != "jsr":
            continue
        m = JSR_OPERAND_RE.match(ops.strip())
        if not m:
            continue
        target = int(m.group(1), 16)
        if target in insn_addr_set and target not in declared_addrs:
            jsr_targets_needing_label.add(target)

    for target in sorted(jsr_targets_needing_label):
        auto_subj = f"sub:{prog_name}:auto_0x{target:04x}"
        cb.emit(conn, "parser_6502", auto_subj, "IN_PROGRAM", prog_subj,
                object_kind="ref", captured_in_context=ctx)
        cb.emit(conn, "parser_6502", auto_subj, "STARTS_AT",
                f"0x{target:04x}", captured_in_context=ctx)
        subs.append((target, auto_subj))
        declared_addrs.add(target)
        counts["auto_label"] += 1

    subs.sort()

    # ----- 4. IN_SUB + RETURNS via address-range walk -------------
    for i, (start, sub_subj) in enumerate(subs):
        end = subs[i + 1][0] if i + 1 < len(subs) else 0x10000
        for addr, isubj, mnem, _ops in insns:
            if start <= addr < end:
                cb.emit(conn, "parser_6502", isubj, "IN_SUB", sub_subj,
                        object_kind="ref", captured_in_context=ctx)
                counts["IN_SUB"] += 1
                if mnem == "rts":
                    cb.emit(conn, "parser_6502", isubj, "RETURNS", sub_subj,
                            object_kind="ref", captured_in_context=ctx)
                    counts["RETURNS"] += 1

    # ----- 5. CALLS_SUB: JSR target resolves to any sub's start ---
    sub_at_addr = {start: subj for start, subj in subs}
    for _addr, isubj, mnem, ops in insns:
        if mnem != "jsr":
            continue
        m = JSR_OPERAND_RE.match(ops.strip())
        if not m:
            continue
        target = int(m.group(1), 16)
        if target in sub_at_addr:
            cb.emit(conn, "parser_6502", isubj, "CALLS_SUB",
                    sub_at_addr[target], object_kind="ref",
                    captured_in_context=ctx)
            counts["CALLS_SUB"] += 1

    return counts


def populate(conn, lesson_path: Path) -> dict:
    """Parse + disassemble + emit facts. Returns counts dict for verification."""
    if not lesson_path.exists():
        raise FileNotFoundError(f"no lesson at {lesson_path}")

    ensure_traveler(conn)

    prog_name = lesson_path.stem
    md5 = hashlib.md5(lesson_path.read_bytes()).hexdigest()
    ts  = datetime.now().isoformat(timespec="milliseconds")

    insns, entry_addr, labels = parse_lesson(lesson_path)
    if not insns:
        raise ValueError(f"no instructions parsed from {lesson_path}")

    # Build a 64KB memory image for the disassembler to look up context
    mem = [0] * 0x10000
    for addr, bts, _ in insns:
        for i, b in enumerate(bts):
            mem[addr + i] = b

    ctx = {
        "via":              "parser_6502.py",
        "lesson":           str(lesson_path),
        "md5":              md5,
        "byte_length":      sum(len(b) for _, b, _ in insns),
        "entry_addr":       f"0x{entry_addr:04x}",
        "label_count":      len(labels),
    }

    prog_subj = f"prog:{prog_name}"
    cb.emit(conn, "parser_6502", prog_subj, "HAS_MD5",     md5, captured_in_context=ctx)
    cb.emit(conn, "parser_6502", prog_subj, "INGESTED_AT", ts,  captured_in_context=ctx)
    cb.emit(conn, "parser_6502", prog_subj, "ENTRY_ADDR",  f"0x{entry_addr:04x}",
            captured_in_context=ctx)

    # Sub-level facts: one IN_PROGRAM and one STARTS_AT per declared label.
    for lbl_addr in sorted(labels):
        lbl_name  = labels[lbl_addr]
        sub_subj  = f"sub:{lbl_name}"
        cb.emit(conn, "parser_6502", sub_subj, "IN_PROGRAM", prog_subj,
                object_kind="ref", captured_in_context=ctx)
        cb.emit(conn, "parser_6502", sub_subj, "STARTS_AT",  f"0x{lbl_addr:04x}",
                captured_in_context=ctx)

    # Disassemble + collect insn metadata before emitting, so JSR resolution
    # and IN_SUB walking can see the full picture.
    decoded: list[dict] = []
    for addr, bts, _comment in insns:
        if addr >= 0xFF00:  # vectors are data, not code
            continue
        mnemonic, op_str, size = disassemble(addr, bts, mem)
        if size != len(bts):
            raise ValueError(
                f"size mismatch at 0x{addr:04x}: declared {len(bts)} bytes, "
                f"disassembler says {size}-byte {mnemonic}"
            )
        decoded.append({
            "addr":     addr,
            "bytes":    bts,
            "mnemonic": mnemonic.lower(),
            "op_str":   op_str,
            "size":     size,
            "subject":  f"insn:{prog_name}:0x{addr:04x}",
        })

    decoded.sort(key=lambda d: d["addr"])

    # Base per-instruction facts.
    for d in decoded:
        s = d["subject"]
        cb.emit(conn, "parser_6502", s, "IN_PROGRAM",   prog_subj,            object_kind="ref", captured_in_context=ctx)
        cb.emit(conn, "parser_6502", s, "AT_ADDRESS",   f"0x{d['addr']:04x}", captured_in_context=ctx)
        cb.emit(conn, "parser_6502", s, "HAS_MNEMONIC", d["mnemonic"],        captured_in_context=ctx)
        cb.emit(conn, "parser_6502", s, "HAS_OPERANDS", d["op_str"],          captured_in_context=ctx)
        cb.emit(conn, "parser_6502", s, "HAS_SIZE",     str(d["size"]),       captured_in_context=ctx)
        cb.emit(conn, "parser_6502", s, "HAS_BYTES",    d["bytes"].hex(),     captured_in_context=ctx)

    derive_calls_and_subs(conn, prog_subj, ctx=ctx)

    return {
        "program":      prog_name,
        "lesson_path":  str(lesson_path),
        "instructions": len(decoded),
        "labels":       len(labels),
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
    print(f"program:       {info['program']}  ({info['instructions']} insns, {info['labels']} labels)")
    print(f"md5:           {info['md5'][:16]}...")
    print(f"entry addr:    0x{info['entry_addr']:04x}")
    print(f"parser_6502 facts (live): {n_live}")
    conn.close()


if __name__ == "__main__":
    main()
