"""
External verification of the 4-bit CPU.

NOT a pytest. This file encodes hand-computed expectations for programs the
existing test_cpu.py does NOT cover, runs them on cpu.CPU, and prints a per-
program PASS/FAIL with the actual end-state values shown next to the expected
ones. The goal is "I can read this output and decide whether the CPU is doing
what a CPU should do," not "tests pass."

If a row says FAIL, that is real divergence from a hand-derived expectation.

Programs are loaded directly into RAM via the same mechanism cpu_4bit_traveler
uses; no test infrastructure between the program bytes and the CPU.

Run:  python3 verify_cpu_4bit.py
"""

import sqlite3
from pathlib import Path

import cpu as cpu_mod


HERE   = Path(__file__).parent
SCHEMA = HERE / "schema.sql"


def fresh_db() -> sqlite3.Connection:
    db_path = HERE / "cpu.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def load(conn: sqlite3.Connection, words: list[int]) -> None:
    conn.execute("DELETE FROM ram")
    padded = list(words) + [0] * (16 - len(words))
    for addr, val in enumerate(padded[:16]):
        conn.execute("INSERT INTO ram(addr,value) VALUES(?,?)", (addr, val & 0xFF))
    conn.commit()


def run(words: list[int], max_cycles: int = 64):
    conn = fresh_db()
    load(conn, words)
    cpu = cpu_mod.CPU(conn)
    cpu.run(max_cycles=max_cycles)
    return cpu


def check(label: str, cpu, expected: dict) -> bool:
    actual = {
        "a":      cpu.a,
        "b":      cpu.b,
        "out":    cpu.out,
        "z":      cpu.z,
        "pc":     cpu.pc,
        "halted": cpu.halted,
        "cycles": cpu.cycle,
    }
    fields = list(expected.keys())
    ok = all(actual[k] == expected[k] for k in fields)
    verdict = "PASS" if ok else "FAIL"
    print(f"\n[{verdict}] {label}")
    for k in fields:
        marker = " " if actual[k] == expected[k] else "*"
        print(f"   {marker} {k:<7} expected {expected[k]!r:<10}  actual {actual[k]!r}")
    return ok


# ---------------------------------------------------------------------------
# Programs (each tuple is a 16-word RAM image)
# Opcodes:
#   0x0 NOP  0x1 LDA  0x2 ADD  0x3 SUB  0x4 STA  0x5 JMP  0x6 OUT
#   0x7 AND  0x8 OR   0x9 XOR  0xA NOT  0xB JZ   0xF HLT
# ---------------------------------------------------------------------------

PROGRAMS = [
    # ---- 1. Underflow: 5 - 9. Expect A = 0xFC (252), Z = 0 ----
    (
        "underflow: 5 - 9 -> A=0xFC=252, Z=0",
        [
            0x1E,        # 0: LDA 14   ; A = 5
            0x3F,        # 1: SUB 15   ; A = (5 - 9) & 0xFF = 0xFC
            0x60,        # 2: OUT      ; OUT = A
            0xF0,        # 3: HLT
            0,0,0,0,0,0,0,0,0,0,
            5, 9,        # 14: 5; 15: 9
        ],
        {"a": 0xFC, "out": 0xFC, "z": 0, "halted": True},
    ),

    # ---- 2. Subtract to zero sets Z. 9 - 9 -> A=0, Z=1 ----
    (
        "sub-to-zero: 9 - 9 -> A=0, Z=1",
        [
            0x1E,        # LDA 14
            0x3F,        # SUB 15
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,0,0,
            9, 9,
        ],
        {"a": 0, "out": 0, "z": 1, "halted": True},
    ),

    # ---- 3. AND zero with 0xFF -> A=0, Z=1 ----
    (
        "AND: 0x00 & 0xFF -> A=0, Z=1",
        [
            0x1E,        # LDA 14   ; A = 0
            0x7F,        # AND 15   ; A = A & mem[15] = 0 & 0xFF = 0
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,0,0,
            0x00, 0xFF,
        ],
        {"a": 0, "out": 0, "z": 1, "halted": True},
    ),

    # ---- 4. AND non-zero -> Z=0 ----
    (
        "AND: 0x0F & 0xF0 -> A=0, Z=1 (no shared bits)",
        [
            0x1E, 0x7F, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,0,0,
            0x0F, 0xF0,
        ],
        {"a": 0x00, "out": 0x00, "z": 1, "halted": True},
    ),

    # ---- 5. XOR self -> 0, Z=1 ----
    (
        "XOR: 0xFF ^ 0xFF -> A=0, Z=1",
        [
            0x1E, 0x9F, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,0,0,
            0xFF, 0xFF,
        ],
        {"a": 0x00, "out": 0x00, "z": 1, "halted": True},
    ),

    # ---- 6. NOT 0 -> 0xFF, Z=0 ----
    # NOT is unary on A (no operand fetch). We load 0 via LDA 14, then NOT.
    (
        "NOT: ~0 -> A=0xFF, Z=0",
        [
            0x1E,        # LDA 14   ; A = 0
            0xA0,        # NOT      ; A = ~A = 0xFF
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,0,0,
            0x00, 0,
        ],
        {"a": 0xFF, "out": 0xFF, "z": 0, "halted": True},
    ),

    # ---- 7. NOT 0xFF -> 0, Z=1 ----
    (
        "NOT: ~0xFF -> A=0, Z=1",
        [
            0x1E, 0xA0, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,0,0,
            0xFF, 0,
        ],
        {"a": 0x00, "out": 0x00, "z": 1, "halted": True},
    ),

    # ---- 7a. OR: 0x0F | 0xF0 -> 0xFF, Z=0 ----
    (
        "OR: 0x0F | 0xF0 -> A=0xFF, Z=0",
        [
            0x1E,        # LDA 14   ; A = 0x0F
            0x8F,        # OR  15   ; A = A | 0xF0 = 0xFF
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,0,0,
            0x0F, 0xF0,
        ],
        {"a": 0xFF, "out": 0xFF, "z": 0, "halted": True},
    ),

    # ---- 7b. OR: 0x00 | 0x00 -> 0, Z=1 ----
    (
        "OR: 0x00 | 0x00 -> A=0, Z=1",
        [
            0x1E, 0x8F, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,0,0,
            0x00, 0x00,
        ],
        {"a": 0x00, "out": 0x00, "z": 1, "halted": True},
    ),

    # ---- 7c. STA writes A to RAM[operand]. To prove the write actually
    # landed in RAM (not just in A), clobber A, then reload from the same
    # address. If reload returns the stored value, STA + LDA both work.
    # Layout: instructions at 0..5, zeros at 6..13, data at 14..15.
    (
        "STA: store A=0xAA to RAM[12], clobber, reload, OUT must be 0xAA",
        [
            0x1E,                      # 0: LDA 14   ; A = RAM[14] = 0xAA
            0x4C,                      # 1: STA 12   ; RAM[12] := 0xAA
            0x1D,                      # 2: LDA 13   ; A := RAM[13] = 0 (clobber)
            0x1C,                      # 3: LDA 12   ; A := RAM[12] (must be 0xAA)
            0x60,                      # 4: OUT      ; OUT := A
            0xF0,                      # 5: HLT
            0, 0, 0, 0, 0, 0, 0, 0,    # 6..13: padding (RAM[12], RAM[13] start at 0)
            0xAA, 0x00,                # 14: 0xAA   15: 0
        ],
        {"a": 0xAA, "out": 0xAA, "halted": True},
    ),

    # ---- 8. JZ NOT taken when Z=0 ----
    # ADD 1+2=3 (Z=0), then JZ 7. JZ should NOT branch; fall through to OUT 3 HLT.
    # If JZ wrongly takes when Z=0, we'd jump to addr 7 and run zeros (NOPs)
    # forever, hitting max_cycles without halting.
    (
        "JZ-not-taken: Z=0 must fall through, not branch",
        [
            0x1E,        # 0: LDA 14   ; A = 1
            0x2F,        # 1: ADD 15   ; A = 3, Z=0
            0xB7,        # 2: JZ 7     ; should NOT jump
            0x60,        # 3: OUT      ; OUT = 3
            0xF0,        # 4: HLT
            0,0,0,0,0,0,0,0,0,
            1, 2,        # 14: 1; 15: 2
        ],
        {"a": 3, "out": 3, "z": 0, "halted": True, "pc": 5},
    ),

    # ---- 9. JZ taken when Z=1 ----
    (
        "JZ-taken: Z=1 must branch",
        [
            0x1E,        # 0: LDA 14   ; A=0
            0x3F,        # 1: SUB 15   ; A=0-0=0, Z=1
            0xB5,        # 2: JZ 5     ; jump to 5
            0x60,        # 3: OUT      ; should be SKIPPED (would put 0 in OUT anyway, so use a guard)
            0xF0,        # 4: HLT      ; should be SKIPPED
            0x1E,        # 5: LDA 14   ; A=0 (re-load to confirm we landed here)
            0x60,        # 6: OUT
            0xF0,        # 7: HLT
            0,0,0,0,0,0,
            0, 0,
        ],
        # If JZ fails to take, PC walks 3->4 and halts at PC=5 with OUT=0 (wrong path).
        # If JZ takes correctly, we land at 5, run LDA/OUT/HLT, halt at PC=8.
        {"halted": True, "pc": 8},
    ),

    # ---- 10. JMP forward, then HLT -- never falls through ----
    # JMP to 5, HLT at 5. If JMP failed, we'd run garbage at PC=1.
    (
        "JMP forward: PC must follow operand, not increment",
        [
            0x55,        # 0: JMP 5
            0xF0,        # 1: HLT (decoy — should NOT execute)
            0,0,0,
            0xF0,        # 5: HLT (real landing)
            0,0,0,0,0,0,0,0,0,0,
        ],
        {"halted": True, "pc": 6},
    ),
]


def main() -> int:
    print("=" * 60)
    print("4-bit CPU external verification")
    print("=" * 60)
    failures = []
    for label, words, expected in PROGRAMS:
        cpu = run(words, max_cycles=64)
        ok = check(label, cpu, expected)
        if not ok:
            failures.append(label)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAIL of {len(PROGRAMS)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {len(PROGRAMS)} edge programs match hand-computed expectations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
