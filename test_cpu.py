"""
Test suite for the 4-bit CPU.

Two layers:
  1. Static checks on the microcode tables (no bus contention, etc.)
  2. Behavioral tests that load a program, run it, and assert the
     final register / memory state.

Each test records: name, claim, expected, actual, pass/fail.
Results are printed AND written to TEST_REPORT.md so there is
documented proof of every check.
"""

import sqlite3
import traceback
from datetime import datetime
from pathlib import Path

from cpu    import CPU, CONTROL_LINES
from mirror import rebuild as mirror_rebuild

HERE       = Path(__file__).parent
SCHEMA_SQL = (HERE / "schema.sql").read_text()
TEST_DB    = HERE / "test_cpu.db"
REPORT     = HERE / "TEST_REPORT.md"

OUTPUT_LINES = ('co','ro','io','ao','eo')               # bus drivers — must be one-hot
PC_LINES     = ('ce','j','jc')                          # PC writers — mutually exclusive
ALU_MODES    = ('su','andop','orop','xorop','notop')    # ALU function selectors — one-hot
A_CONFLICT   = ('ai','ao')                              # can't load+drive A in same step
B_CONFLICT   = ('bi',)                                  # B is input-only, just sanity


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------
def fresh_db():
    if TEST_DB.exists():
        TEST_DB.unlink()
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn

def load_program(conn, words):
    """Replace RAM contents with `words` (list of ints, 0-255), pad to 16."""
    conn.execute("DELETE FROM ram")
    padded = list(words) + [0] * (16 - len(words))
    for addr, val in enumerate(padded[:16]):
        conn.execute("INSERT INTO ram(addr,value) VALUES(?,?)", (addr, val & 0xFF))
    conn.commit()

def microcode_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name LIKE 'mc\\_%' ESCAPE '\\'"
    ).fetchall()
    return [r["name"] for r in rows]


# ------------------------------------------------------------------
# test registry
# ------------------------------------------------------------------
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


# ==================================================================
# STATIC microcode checks
# ==================================================================
def static_checks():
    print("\n[static microcode checks]")
    conn = fresh_db()
    tables = microcode_tables(conn)

    # 1. every microcode table has the canonical column set
    expected_cols = {"t"} | set(CONTROL_LINES)
    def cols(t):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}

    run("schema: all mc_* tables share canonical columns",
        "every microcode table has the same columns as mc_fetch",
        lambda: (
            {tbl: True for tbl in tables},
            {tbl: cols(tbl) == expected_cols for tbl in tables},
        ))

    # 2. one-hot bus drivers: at every row of every mc table, sum(outputs) <= 1
    violations = []
    for tbl in tables:
        for row in conn.execute(f"SELECT * FROM {tbl}"):
            n = sum(row[c] for c in OUTPUT_LINES)
            if n > 1:
                violations.append((tbl, row["t"], n,
                    [c for c in OUTPUT_LINES if row[c]]))
    run("bus: at most one output drives the bus per T-state",
        "no row in any mc_* table has >1 output line asserted",
        lambda: ([], violations))

    # 3. PC: ce / j / jc are mutually exclusive (any pair is a contention)
    pc_violations = []
    for tbl in tables:
        for row in conn.execute(f"SELECT * FROM {tbl}"):
            n = sum(row[c] for c in PC_LINES)
            if n > 1:
                pc_violations.append((tbl, row["t"], n,
                    [c for c in PC_LINES if row[c]]))
    run("pc: at most one PC writer (ce / j / jc) per T-state",
        "no row asserts more than one of ce, j, jc",
        lambda: ([], pc_violations))

    # 3b. ALU: at most one mode bit (su / andop / orop / xorop / notop)
    alu_violations = []
    for tbl in tables:
        for row in conn.execute(f"SELECT * FROM {tbl}"):
            n = sum(row[c] for c in ALU_MODES)
            if n > 1:
                alu_violations.append((tbl, row["t"], n,
                    [c for c in ALU_MODES if row[c]]))
    run("alu: at most one ALU mode bit per T-state",
        "no row asserts more than one of su, andop, orop, xorop, notop",
        lambda: ([], alu_violations))

    # 4. A register: ai (load A) and ao (drive A) are mutually exclusive
    a_violations = []
    for tbl in tables:
        for row in conn.execute(f"SELECT * FROM {tbl}"):
            if row["ai"] and row["ao"]:
                a_violations.append((tbl, row["t"]))
    run("regA: ai and ao are never both asserted",
        "no row both loads AND drives the A register",
        lambda: ([], a_violations))

    # 5. every opcode in `opcodes` has a real microcode table
    missing = []
    for r in conn.execute("SELECT mnemonic, mc_table FROM opcodes"):
        n = conn.execute(
            "SELECT count(*) c FROM sqlite_master WHERE type='table' AND name=?",
            (r["mc_table"],)).fetchone()["c"]
        if n == 0:
            missing.append((r["mnemonic"], r["mc_table"]))
    run("opcodes: every opcode references an existing mc_<instr> table",
        "no dangling opcode -> microcode pointer",
        lambda: ([], missing))

    conn.close()


# ==================================================================
# BEHAVIORAL tests
# ==================================================================
def behavior_checks():
    print("\n[behavioral tests]")

    # ---- ADD ------------------------------------------------------
    def t_add():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x2F,  # ADD 15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            3, 4,
        ])
        cpu = CPU(conn); cpu.run()
        return 7, cpu.out
    run("ADD: 3 + 4 = 7",
        "LDA loads from memory, ADD adds, OUT latches A into output",
        t_add)

    # ---- SUB ------------------------------------------------------
    def t_sub():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x3F,  # SUB 15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            9, 5,
        ])
        cpu = CPU(conn); cpu.run()
        return 4, cpu.out
    run("SUB: 9 - 5 = 4",
        "SUB asserts the SU line so the ALU computes A - B",
        t_sub)

    # ---- ADD overflow (8-bit wrap) -------------------------------
    def t_add_overflow():
        # data >255 won't fit in RAM, so use 200 + 100 = 300 -> 44 (300 & 0xFF)
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x2F,  # ADD 15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            200, 100,
        ])
        cpu = CPU(conn); cpu.run()
        return (200 + 100) & 0xFF, cpu.out
    run("ADD: 200 + 100 wraps to 44 (8-bit overflow)",
        "ALU is masked to 8 bits, so 300 % 256 == 44",
        t_add_overflow)

    # ---- SUB underflow -------------------------------------------
    def t_sub_underflow():
        conn = fresh_db()
        load_program(conn, [
            0x1E, 0x3F, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,0,0,
            5, 9,
        ])
        cpu = CPU(conn); cpu.run()
        return (5 - 9) & 0xFF, cpu.out
    run("SUB: 5 - 9 wraps to 252 (two's-complement)",
        "ALU subtraction masked to 8 bits gives 0xFC",
        t_sub_underflow)

    # ---- STA round-trip ------------------------------------------
    def t_sta_roundtrip():
        # LDA 14 (=42); STA 13; LDA 13; OUT; HLT  -> OUT should be 42
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x4D,  # STA 13
            0x1D,  # LDA 13
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,
            42,    # addr 13 (will be overwritten by STA)
            42,    # addr 14
            0,
        ])
        # NOTE: addr 13 starts at 42 too, but to prove round-trip, zero it:
        conn.execute("UPDATE ram SET value=0 WHERE addr=13")
        conn.commit()
        cpu = CPU(conn); cpu.run()
        # Verify both: OUT is 42 AND ram[13] is now 42
        ram13 = conn.execute("SELECT value FROM ram WHERE addr=13").fetchone()["value"]
        return (42, 42), (cpu.out, ram13)
    run("STA: round-trip A -> mem[13] -> A -> OUT",
        "STA writes A into RAM, subsequent LDA reads it back",
        t_sta_roundtrip)

    # ---- JMP ------------------------------------------------------
    def t_jmp():
        # addr 0: JMP 3   -- skip past poison at 1,2
        # addr 1: ADD 15  -- would corrupt A (poison)
        # addr 2: HLT     -- would halt early (poison)
        # addr 3: LDA 14  -- A = 99
        # addr 4: OUT
        # addr 5: HLT
        conn = fresh_db()
        load_program(conn, [
            0x53,        # JMP 3
            0x2F,        # ADD 15  (must be skipped)
            0xF0,        # HLT     (must be skipped)
            0x1E,        # LDA 14
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,
            99, 1,
        ])
        cpu = CPU(conn); cpu.run()
        return 99, cpu.out
    run("JMP: control flow skips poisoned instructions",
        "JMP loads PC from operand, bypassing intermediate code",
        t_jmp)

    # ---- HLT halts -----------------------------------------------
    def t_hlt():
        conn = fresh_db()
        load_program(conn, [0xF0])  # HLT immediately
        cpu = CPU(conn); cpu.run(max_cycles=10)
        return (True, 1), (cpu.halted, cpu.cycle)
    run("HLT: halts the clock after exactly one cycle",
        "HLT sets the halted flag; run loop exits before cycle 2",
        t_hlt)

    # ---- NOP is a no-op ------------------------------------------
    def t_nop():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x00,  # NOP
            0x00,  # NOP
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,
            55, 0,
        ])
        cpu = CPU(conn); cpu.run()
        return 55, cpu.out
    run("NOP: does not disturb registers",
        "NOP has zero execute T-states, A survives across two NOPs",
        t_nop)

    # ---- AND ------------------------------------------------------
    def t_and():
        # 0xCC & 0xAA = 0x88 ; OUT
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14    ; A = 0xCC
            0x7F,  # AND 15    ; A &= 0xAA
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            0xCC, 0xAA,
        ])
        cpu = CPU(conn); cpu.run()
        return 0xCC & 0xAA, cpu.out
    run("AND: 0xCC & 0xAA = 0x88",
        "AND asserts andop so the ALU produces A & B",
        t_and)

    # ---- OR -------------------------------------------------------
    def t_or():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x8F,  # OR  15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            0x0F, 0xF0,
        ])
        cpu = CPU(conn); cpu.run()
        return 0x0F | 0xF0, cpu.out
    run("OR: 0x0F | 0xF0 = 0xFF",
        "OR asserts orop so the ALU produces A | B",
        t_or)

    # ---- XOR ------------------------------------------------------
    def t_xor():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x9F,  # XOR 15
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            0xC3, 0xA5,
        ])
        cpu = CPU(conn); cpu.run()
        return 0xC3 ^ 0xA5, cpu.out
    run("XOR: 0xC3 ^ 0xA5 = 0x66",
        "XOR asserts xorop so the ALU produces A ^ B",
        t_xor)

    # ---- NOT (unary) ----------------------------------------------
    def t_not():
        # LDA 14 (=0x05); NOT; OUT; HLT  -> ~0x05 & 0xFF = 0xFA
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0xA0,  # NOT (operand ignored)
            0x60,  # OUT
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,
            0x05, 0,
        ])
        cpu = CPU(conn); cpu.run()
        return (~0x05) & 0xFF, cpu.out
    run("NOT: ~0x05 = 0xFA",
        "NOT asserts notop, ignores B, produces ~A & 0xFF",
        t_not)

    # ---- Z flag set when ALU result == 0 -------------------------
    def t_z_set_on_zero():
        # SUB equal values -> ALU = 0 -> fi -> Z = 1
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x3F,  # SUB 15
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,0,
            7, 7,
        ])
        cpu = CPU(conn); cpu.run()
        return 1, cpu.z
    run("Z: SUB of equal values sets Z=1",
        "fi latches Z := (alu == 0) at the SUB execute T-state",
        t_z_set_on_zero)

    # ---- Z flag clear when ALU result != 0 -----------------------
    def t_z_clear_on_nonzero():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14
            0x2F,  # ADD 15  -> 1+1 = 2, Z=0
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,0,
            1, 1,
        ])
        cpu = CPU(conn); cpu.run()
        return 0, cpu.z
    run("Z: ADD with nonzero result clears Z to 0",
        "fi sets Z=0 when the ALU output is nonzero",
        t_z_clear_on_nonzero)

    # ---- JZ taken (Z=1) ------------------------------------------
    def t_jz_taken():
        # SUB equal -> Z=1; JZ 5 should jump over poison.
        # 0: LDA 14   ; A=7
        # 1: SUB 15   ; A=0, Z=1
        # 2: JZ 5     ; jump
        # 3: ADD 13   ; poison (would set A=99)
        # 4: HLT      ; poison
        # 5: LDA 12   ; A=42
        # 6: OUT
        # 7: HLT
        conn = fresh_db()
        load_program(conn, [
            0x1E,        # LDA 14
            0x3F,        # SUB 15
            0xB5,        # JZ 5
            0x2D,        # ADD 13 (poison)
            0xF0,        # HLT     (poison)
            0x1C,        # LDA 12
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,
            42, 99,
            7, 7,
        ])
        cpu = CPU(conn); cpu.run()
        return 42, cpu.out
    run("JZ: taken when Z=1 (skips poisoned code)",
        "jc loads PC from operand only when Z=1; SUB-of-equals sets Z",
        t_jz_taken)

    # ---- JZ not taken (Z=0) --------------------------------------
    def t_jz_not_taken():
        # ADD nonzero -> Z=0; JZ should fall through.
        # 0: LDA 14    ; A=3
        # 1: ADD 15    ; A=7, Z=0
        # 2: JZ 5      ; must NOT jump
        # 3: LDA 12    ; A=99
        # 4: OUT
        # 5: HLT       ; would-be jump target (also halt for safety)
        conn = fresh_db()
        load_program(conn, [
            0x1E,        # LDA 14
            0x2F,        # ADD 15
            0xB5,        # JZ 5
            0x1C,        # LDA 12
            0x60,        # OUT
            0xF0,        # HLT
            0,0,0,0,0,0,
            99, 0,
            3, 4,
        ])
        cpu = CPU(conn); cpu.run()
        return 99, cpu.out
    run("JZ: falls through when Z=0",
        "jc must NOT load PC when Z=0; ADD with nonzero result keeps Z=0",
        t_jz_not_taken)

    # ---- Z persists across non-fi instructions -------------------
    def t_z_persists():
        # SUB equal -> Z=1; LDA (no fi) must leave Z=1.
        conn = fresh_db()
        load_program(conn, [
            0x1E,        # LDA 14   ; A=4
            0x3F,        # SUB 15   ; A=0, Z=1
            0x1D,        # LDA 13   ; A=99 (LDA does not touch fi)
            0xF0,        # HLT
            0,0,0,0,0,0,0,0,0,
            99,          # addr 13
            4,           # addr 14
            4,           # addr 15
        ])
        cpu = CPU(conn); cpu.run()
        return (99, 1), (cpu.a, cpu.z)
    run("Z: persists across instructions that don't latch fi",
        "Z is only updated when fi is asserted; LDA leaves it alone",
        t_z_persists)

    # ---- AND zero result also sets Z -----------------------------
    def t_and_zero_sets_z():
        conn = fresh_db()
        load_program(conn, [
            0x1E,  # LDA 14   ; A = 0xF0
            0x7F,  # AND 15   ; A &= 0x0F -> 0x00, Z=1
            0xF0,  # HLT
            0,0,0,0,0,0,0,0,0,0,0,
            0xF0, 0x0F,
        ])
        cpu = CPU(conn); cpu.run()
        return (0, 1), (cpu.a, cpu.z)
    run("AND: zero result also sets Z=1",
        "AND asserts fi, so a zero result latches Z=1 just like SUB",
        t_and_zero_sets_z)

    # ---- max_cycles guard ----------------------------------------
    def t_runaway_guard():
        # JMP 0 forever; max_cycles must stop it
        conn = fresh_db()
        load_program(conn, [0x50])  # JMP 0
        cpu = CPU(conn); cpu.run(max_cycles=20)
        return (False, 20), (cpu.halted, cpu.cycle)
    run("runaway: max_cycles halts an infinite loop",
        "JMP 0 loops forever; orchestrator must enforce a ceiling",
        t_runaway_guard)

    # ---- state_log monotonicity ----------------------------------
    def t_state_log_monotonic():
        conn = fresh_db()
        load_program(conn, [0x1E,0x2F,0x60,0xF0,0,0,0,0,0,0,0,0,0,0,3,4])
        cpu = CPU(conn); cpu.run()
        rows = conn.execute(
            "SELECT step, cycle, t FROM state_log ORDER BY step"
        ).fetchall()
        # within each cycle, t must be non-decreasing
        bad = []
        last = {}
        for r in rows:
            c, t = r["cycle"], r["t"]
            if c in last and t < last[c]:
                bad.append((c, last[c], t))
            last[c] = t
        return [], bad
    run("state_log: T-states are non-decreasing within a cycle",
        "execution history is recorded in time order",
        t_state_log_monotonic)


# ==================================================================
# PROJECTION (mirror layer) consistency checks
# ==================================================================
def projection_checks():
    print("\n[projection consistency tests]")

    # ---- d_instruction_trace row count == cycle count -----------
    def t_trace_row_count():
        conn = fresh_db()
        load_program(conn, [0x1E,0x2F,0x60,0xF0,0,0,0,0,0,0,0,0,0,0,3,4])
        cpu = CPU(conn); cpu.run()
        mirror_rebuild(conn)
        cycles = conn.execute(
            "SELECT MAX(cycle)+1 AS n FROM state_log").fetchone()["n"]
        traced = conn.execute(
            "SELECT COUNT(*) AS n FROM d_instruction_trace").fetchone()["n"]
        return cycles, traced
    run("trace: one row per executed cycle",
        "d_instruction_trace has exactly one row for each cycle in state_log",
        t_trace_row_count)

    # ---- d_memory_access count matches ri+ro firings ------------
    def t_mem_access_count():
        conn = fresh_db()
        load_program(conn, [
            0x1E, 0x4D, 0x1D, 0x60, 0xF0,
            0,0,0,0,0,0,0,0,
            0, 42, 0,
        ])
        conn.execute("UPDATE ram SET value=0 WHERE addr=13")
        conn.commit()
        cpu = CPU(conn); cpu.run()
        mirror_rebuild(conn)
        # count ri / ro events in raw log
        raw = conn.execute("""
            SELECT COUNT(*) c FROM state_log
            WHERE signals LIKE '%ri%' OR signals LIKE '%ro%'
        """).fetchone()["c"]
        derived = conn.execute(
            "SELECT COUNT(*) c FROM d_memory_access").fetchone()["c"]
        return raw, derived
    run("memory: derived access count == raw ri/ro events",
        "every RAM read or write in the log appears once in d_memory_access",
        t_mem_access_count)

    # ---- mnemonic frequencies sum to total instruction count ----
    def t_freq_sum():
        conn = fresh_db()
        load_program(conn, [0x1E,0x2F,0x60,0xF0,0,0,0,0,0,0,0,0,0,0,3,4])
        cpu = CPU(conn); cpu.run()
        mirror_rebuild(conn)
        traced = conn.execute(
            "SELECT COUNT(*) AS n FROM d_instruction_trace").fetchone()["n"]
        summed = conn.execute(
            "SELECT COALESCE(SUM(count),0) AS n FROM d_instruction_freq"
        ).fetchone()["n"]
        return traced, summed
    run("freq: sum of d_instruction_freq == row count of d_instruction_trace",
        "every traced instruction is counted exactly once in the frequency table",
        t_freq_sum)

    # ---- spot-check OUT delta on the OUT instruction -----------
    def t_out_delta():
        conn = fresh_db()
        load_program(conn, [0x1E,0x2F,0x60,0xF0,0,0,0,0,0,0,0,0,0,0,3,4])
        cpu = CPU(conn); cpu.run()
        mirror_rebuild(conn)
        row = conn.execute(
            "SELECT out_before, out_after FROM d_instruction_trace WHERE mnemonic='OUT'"
        ).fetchone()
        return (0, 7), (row["out_before"], row["out_after"])
    run("trace: OUT cycle shows 0 -> 7 transition",
        "before/after snapshot in d_instruction_trace matches expected program effect",
        t_out_delta)

    # ---- projections are reproducible ----------------------------
    def t_idempotent():
        conn = fresh_db()
        load_program(conn, [0x1E,0x2F,0x60,0xF0,0,0,0,0,0,0,0,0,0,0,3,4])
        cpu = CPU(conn); cpu.run()
        mirror_rebuild(conn)
        first = conn.execute(
            "SELECT cycle, mnemonic, operand, a_after, out_after "
            "FROM d_instruction_trace ORDER BY cycle"
        ).fetchall()
        first = [tuple(r) for r in first]
        mirror_rebuild(conn)            # rebuild again
        second = conn.execute(
            "SELECT cycle, mnemonic, operand, a_after, out_after "
            "FROM d_instruction_trace ORDER BY cycle"
        ).fetchall()
        second = [tuple(r) for r in second]
        return first, second
    run("idempotent: rebuilding projections produces the same tables",
        "the mirror layer is a pure function of the event log",
        t_idempotent)


# ==================================================================
# REPORT
# ==================================================================
def write_report():
    n = len(RESULTS)
    p = sum(1 for r in RESULTS if r["ok"])
    f = n - p
    lines = [
        f"# CPU test report",
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


# ==================================================================
if __name__ == "__main__":
    static_checks()
    behavior_checks()
    projection_checks()
    ok = write_report()
    raise SystemExit(0 if ok else 1)
