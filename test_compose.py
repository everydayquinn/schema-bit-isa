"""
Tests for compose.py — slice 2.

Verification: byte-exact equality with hand-coded bytes / asm.py output.
"""

import sqlite3
import traceback
from datetime import datetime
from pathlib import Path

from asm     import assemble
from compose import compose, expand, insert_chunk, ensure_schema

HERE       = Path(__file__).parent
SCHEMA_SQL = (HERE / "schema.sql").read_text()
TEST_DB    = HERE / "test_compose.db"
REPORT     = HERE / "COMPOSE_TEST_REPORT.md"


def fresh_db():
    if TEST_DB.exists():
        TEST_DB.unlink()
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)            # CPU schema (opcodes, ram, …)
    ensure_schema(conn)                       # chunks tables
    conn.commit()
    return conn


RESULTS = []
def record(name, claim, expected, actual, ok, err=None):
    RESULTS.append({"name":name, "claim":claim,
                    "expected":expected, "actual":actual,
                    "ok":ok, "err":err})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
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
    try:
        return ("did not raise", fn())
    except exc_type:
        return ("raised", exc_type.__name__)
    except Exception as e:
        return ("raised", type(e).__name__)


# ==================================================================
def behaviour():
    print("[compose.py tests]")
    conn = fresh_db()

    # ---- empty catalog: literals-only matches asm.py exactly ------
    run("empty catalog: literals-only compose == asm.assemble",
        "with no chunks defined, compose is a transparent passthrough to asm",
        lambda: (
            assemble([('LDA',14),('ADD',15),('OUT',),('HLT',)], conn),
            compose ([('LDA',14),('ADD',15),('OUT',),('HLT',)], conn),
        ))

    # ---- ADD program via hand-coded reference --------------------
    run("ADD program literal: matches the bytes from test_cpu.py",
        "compose handles literal mnemonics same as the proven asm path",
        lambda: (
            [0x1E, 0x2F, 0x60, 0xF0],
            compose([('LDA',14),('ADD',15),('OUT',),('HLT',)], conn),
        ))

    # ---- single-instruction chunk (no params) --------------------
    insert_chunk(conn, "halt",   [('HLT', 0)], description="stop")
    insert_chunk(conn, "output", [('OUT', 0)], description="A -> OUT")

    run("zero-param chunk: ('halt',) expands to [HLT]",
        "a 1-step zero-param chunk produces one byte equal to its instruction",
        lambda: (
            [0xF0],
            compose([('halt',)], conn),
        ))

    run("zero-param chunk: ('output',) expands to [OUT]",
        "OUT byte 0x60 equals halt-less compose call",
        lambda: ([0x60], compose([('output',)], conn)))

    # ---- single-param chunk --------------------------------------
    insert_chunk(conn, "load",
                 [('LDA', '$addr')], params=['addr'],
                 description="A <- mem[addr]")
    insert_chunk(conn, "add_at",
                 [('ADD', '$addr')], params=['addr'])

    run("single-param chunk: load(addr=14) -> [0x1E]",
        "$addr is substituted from the params dict",
        lambda: ([0x1E], compose([('load', {'addr': 14})], conn)))

    # ---- multi-step chunk with one param -------------------------
    insert_chunk(conn, "load_and_output",
                 [('LDA', '$addr'), ('OUT', 0)], params=['addr'])
    run("multi-step chunk: load_and_output(addr=14) -> [LDA 14, OUT]",
        "ordered chunk_body rows expand in step order",
        lambda: ([0x1E, 0x60], compose([('load_and_output', {'addr':14})], conn)))

    # ---- two-param chunk -----------------------------------------
    insert_chunk(conn, "add_two",
                 [('LDA', '$a'), ('ADD', '$b')], params=['a','b'])
    run("two-param chunk: add_two(a=14,b=15) -> [LDA 14, ADD 15]",
        "multiple parameters resolve independently",
        lambda: ([0x1E, 0x2F], compose([('add_two', {'a':14,'b':15})], conn)))

    # ---- pivotal test: ADD program from chunks only --------------
    run("pivotal: ADD program from chunks only matches hand-coded bytes",
        "compose([add_two,output,halt]) == [0x1E,0x2F,0x60,0xF0]",
        lambda: (
            [0x1E, 0x2F, 0x60, 0xF0],
            compose([
                ('add_two', {'a':14,'b':15}),
                ('output',),
                ('halt',),
            ], conn),
        ))

    # ---- mixed literals and chunks -------------------------------
    run("mixed: literal + chunk in same program",
        "compose handles both ref styles in one list",
        lambda: (
            [0x1E, 0x2F, 0x60, 0xF0],
            compose([
                ('LDA', 14),
                ('add_at', {'addr': 15}),
                ('output',),
                ('HLT',),
            ], conn),
        ))

    # ---- expand returns the flat (mnem, operand) list ------------
    run("expand: returns flat (mnemonic, operand) tuples for asm.py",
        "expand is the same data asm.assemble would consume",
        lambda: (
            [('LDA', 14), ('ADD', 15), ('OUT', 0), ('HLT', 0)],
            expand([
                ('add_two', {'a':14,'b':15}),
                ('output',),
                ('halt',),
            ], conn),
        ))

    # ---- error cases --------------------------------------------
    run("error: unknown name (neither mnemonic nor chunk) raises",
        "garbage refs are rejected up-front",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: compose([('xyzzy',)], conn))))

    run("error: chunk missing required param raises",
        "callers must supply every $param the chunk uses",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: compose([('add_two', {'a':1})], conn))))

    run("error: literal mnemonic given param dict raises",
        "passing {a:1} to ('LDA', ...) is a structural error",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: compose([('LDA', {'addr':14})], conn))))

    run("error: bad operand from chunk surfaces from asm",
        "asm.py's 4-bit range check still fires on chunk-produced operands",
        lambda: (("raised","ValueError"),
                 expect_raises(lambda: compose([('add_two', {'a':99,'b':1})], conn))))

    # ---- byte-exact agreement on every program from test_cpu.py --
    programs = [
        ("ADD",        [('LDA',14),('ADD',15),('OUT',),('HLT',)],
                       [0x1E, 0x2F, 0x60, 0xF0]),
        ("SUB",        [('LDA',14),('SUB',15),('OUT',),('HLT',)],
                       [0x1E, 0x3F, 0x60, 0xF0]),
        ("STA-trip",   [('LDA',14),('STA',13),('LDA',13),('OUT',),('HLT',)],
                       [0x1E, 0x4D, 0x1D, 0x60, 0xF0]),
        ("JMP",        [('JMP',3),('ADD',15),('HLT',),('LDA',14),('OUT',),('HLT',)],
                       [0x53, 0x2F, 0xF0, 0x1E, 0x60, 0xF0]),
    ]
    for label, refs, expected in programs:
        run(f"parity: {label} program via compose == hand-coded bytes",
            "compose -> asm pipeline reproduces every test_cpu.py program",
            lambda refs=refs, expected=expected:
                (expected, compose(refs, conn)))

    conn.close()


# ==================================================================
def write_report():
    n = len(RESULTS); p = sum(1 for r in RESULTS if r["ok"]); f = n - p
    lines = [
        f"# compose.py test report  (slice 2)",
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
            f"{'PASS' if r['ok'] else '**FAIL**'} |")
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
