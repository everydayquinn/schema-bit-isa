"""
Tests for asm.py — slice 1.

Verification strategy: exact byte-for-byte equality.

The reference bytes come straight from test_cpu.py — programs that
already pass all 20 CPU tests.  If asm.py produces any different byte
for the same mnemonic+operand, this test fails.
"""

import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

from asm import assemble

HERE       = Path(__file__).parent
SCHEMA_SQL = (HERE / "schema.sql").read_text()
TEST_DB    = HERE / "test_asm.db"
REPORT     = HERE / "ASM_TEST_REPORT.md"


def fresh_db():
    if TEST_DB.exists():
        TEST_DB.unlink()
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


RESULTS = []
def record(name, claim, expected, actual, ok, err=None):
    RESULTS.append({
        "name": name, "claim": claim,
        "expected": expected, "actual": actual,
        "ok": ok, "err": err,
    })
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}")
    if not ok and err:
        print("        " + err.replace("\n", "\n        "))

def run(name, claim, fn):
    try:
        expected, actual = fn()
        record(name, claim, expected, actual, expected == actual)
    except AssertionError as e:
        record(name, claim, "(assertion)", "(failed)", False, str(e))
    except Exception:
        record(name, claim, "(no exception)", "(exception)", False, traceback.format_exc())

def expect_raises(fn, exc_type=ValueError):
    """Returns ('raised', exc_type) if fn() raises exc_type, else ('did not raise', actual)."""
    try:
        result = fn()
        return ("did not raise", result)
    except exc_type:
        return ("raised", exc_type.__name__)
    except Exception as e:
        return ("raised", type(e).__name__)


# ==================================================================
def behaviour():
    print("[asm.py tests]")
    conn = fresh_db()

    # ---- exact-byte parity with the ADD program in test_cpu.py ----
    run("ADD program: assembles to the bytes that already pass test_cpu.py",
        "LDA 14, ADD 15, OUT, HLT -> [0x1E, 0x2F, 0x60, 0xF0]",
        lambda: (
            [0x1E, 0x2F, 0x60, 0xF0],
            assemble([('LDA',14),('ADD',15),('OUT',),('HLT',)], conn),
        ))

    # ---- SUB program ---------------------------------------------
    run("SUB program: matches test_cpu.py SUB bytes",
        "LDA 14, SUB 15, OUT, HLT -> [0x1E, 0x3F, 0x60, 0xF0]",
        lambda: (
            [0x1E, 0x3F, 0x60, 0xF0],
            assemble([('LDA',14),('SUB',15),('OUT',),('HLT',)], conn),
        ))

    # ---- STA round-trip program ----------------------------------
    run("STA round-trip program: matches test_cpu.py bytes",
        "LDA 14, STA 13, LDA 13, OUT, HLT -> [0x1E,0x4D,0x1D,0x60,0xF0]",
        lambda: (
            [0x1E, 0x4D, 0x1D, 0x60, 0xF0],
            assemble([('LDA',14),('STA',13),('LDA',13),('OUT',),('HLT',)], conn),
        ))

    # ---- JMP program ---------------------------------------------
    run("JMP program: matches test_cpu.py JMP bytes",
        "JMP 3, ADD 15, HLT, LDA 14, OUT, HLT -> [0x53,0x2F,0xF0,0x1E,0x60,0xF0]",
        lambda: (
            [0x53, 0x2F, 0xF0, 0x1E, 0x60, 0xF0],
            assemble([('JMP',3),('ADD',15),('HLT',),('LDA',14),('OUT',),('HLT',)], conn),
        ))

    # ---- NOP encodes as 0x00 -------------------------------------
    run("NOP program: NOP encodes as 0x00",
        "NOP -> [0x00]",
        lambda: ([0x00], assemble([('NOP',)], conn)))

    # ---- explicit operand 0 == implicit operand ------------------
    run("operand: ('OUT',) and ('OUT',0) produce the same byte",
        "missing operand defaults to 0",
        lambda: (
            assemble([('OUT', 0)], conn),
            assemble([('OUT',)], conn),
        ))

    # ---- mnemonic is case-insensitive ----------------------------
    run("case: lowercase mnemonics work",
        "'lda' assembles the same as 'LDA'",
        lambda: (
            assemble([('LDA', 7)], conn),
            assemble([('lda', 7)], conn),
        ))

    # ---- every opcode round-trips at every operand ---------------
    def t_full_grid():
        bad = []
        rows = list(conn.execute("SELECT mnemonic, opcode FROM opcodes"))
        for r in rows:
            for op in range(16):
                got = assemble([(r["mnemonic"], op)], conn)[0]
                want = ((r["opcode"] & 0x0F) << 4) | op
                if got != want:
                    bad.append((r["mnemonic"], op, got, want))
        return [], bad
    run("full grid: every (mnemonic, operand) pair encodes correctly",
        "(opcode<<4)|operand for every mnemonic in opcodes table",
        t_full_grid)

    # ---- error cases ---------------------------------------------
    run("error: unknown mnemonic raises ValueError",
        "asm rejects mnemonics not in the opcodes table",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: assemble([('XYZ', 0)], conn))))

    run("error: operand > 15 raises ValueError",
        "operand must fit in 4 bits",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: assemble([('LDA', 16)], conn))))

    run("error: negative operand raises ValueError",
        "operand must be non-negative",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: assemble([('LDA', -1)], conn))))

    run("error: malformed entry raises ValueError",
        "entries must be 1- or 2-tuples",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: assemble(['LDA'], conn))))

    conn.close()


# ==================================================================
def write_report():
    n = len(RESULTS)
    p = sum(1 for r in RESULTS if r["ok"])
    f = n - p
    lines = [
        f"# asm.py test report  (slice 1)",
        f"",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        f"",
        f"**{p}/{n} passed**" + ("" if f == 0 else f"  &nbsp;|&nbsp;  **{f} FAILED**"),
        f"",
        f"| # | test | claim | expected | actual | result |",
        f"|---|------|-------|----------|--------|--------|",
    ]
    for i, r in enumerate(RESULTS, 1):
        exp = repr(r["expected"]).replace("|","\\|")
        act = repr(r["actual"]).replace("|","\\|")
        lines.append(
            f"| {i} | `{r['name']}` | {r['claim']} | `{exp}` | `{act}` | "
            f"{'PASS' if r['ok'] else '**FAIL**'} |"
        )
    if f:
        lines += ["", "## failures", ""]
        for r in RESULTS:
            if not r["ok"]:
                lines += [f"### {r['name']}", "", "```", r["err"] or "", "```", ""]
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"\nreport: {REPORT}")
    print(f"summary: {p}/{n} passed" + ("" if f == 0 else f", {f} FAILED"))
    return f == 0


if __name__ == "__main__":
    behaviour()
    ok = write_report()
    raise SystemExit(0 if ok else 1)
