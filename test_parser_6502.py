"""
test_parser_6502.py — verification suite for parser_6502's static facts.

Indexes parser_probe/probe_jsr_rts.s into a fresh DB and asserts
hand-computed expected fact tables. The probe pins:
  - the smallest JSR/RTS shape (main-block JSR + one-instruction sub)
  - the new ; label NAME 0xHHHH directive
  - sub-level facts (IN_PROGRAM, STARTS_AT)
  - call-site facts (CALLS_SUB)
  - sub-membership and return facts (IN_SUB, RETURNS)

Hand-computed total: 32 live facts. Each sub-test below verifies a slice
of that count by content, not just count, so a regression that swaps a
predicate or subject is still caught.

Each test records: name, claim, expected, actual, pass/fail.
Results print AND write to PARSER_6502_TEST_REPORT.md.
"""
from __future__ import annotations

import sqlite3
import traceback
from datetime import datetime
from pathlib import Path

import corkboard as cb
import parser_6502
import seed_substrate


HERE         = Path(__file__).parent
PROBE_JSR    = HERE / "parser_probe" / "probe_jsr_rts.s"
PROBE_MULTI  = HERE / "parser_probe" / "probe_multi_rts.s"
PROBE_AUTO   = HERE / "parser_probe" / "probe_auto_label.s"
TEST_DB      = HERE / "test_parser_6502.db"
REPORT       = HERE / "PARSER_6502_TEST_REPORT.md"

PROG         = "probe_jsr_rts"
SUB          = "sub:inc_a"

PROG_M       = "probe_multi_rts"
SUB_M        = "sub:conditional_inc"


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------
def fresh_indexed_db() -> sqlite3.Connection:
    """Wipe TEST_DB, bootstrap, seed substrate vocab, run parser_6502
    over BOTH probe programs into the same DB so multi-program queries
    can be exercised."""
    if TEST_DB.exists():
        TEST_DB.unlink()
    conn = cb.bootstrap(TEST_DB)
    for prefix, defn, ex in seed_substrate.NAMESPACES:
        cb.register_namespace(conn, prefix, defn, ex)
    for name, dom, rng, card, defn, exs in seed_substrate.PREDICATES:
        cb.register_predicate(conn, name, dom, rng, card, defn, exs)
    for name, purpose, role, source, note in seed_substrate.TRAVELERS:
        cb.register_traveler(conn, name, purpose, role, source, note)
    parser_6502.populate(conn, PROBE_JSR)
    parser_6502.populate(conn, PROBE_MULTI)
    parser_6502.populate(conn, PROBE_AUTO)
    conn.commit()
    return conn


def facts_for(conn, subject: str) -> dict[str, set[str]]:
    """Return {predicate: {object, ...}} for a given subject."""
    out: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT predicate, object FROM v_facts_live "
        "WHERE traveler='parser_6502' AND subject=?",
        (subject,),
    ):
        out.setdefault(r["predicate"], set()).add(r["object"])
    return out


# ------------------------------------------------------------------
# test registry
# ------------------------------------------------------------------
RESULTS = []
def record(name, claim, expected, actual, ok, err=None):
    RESULTS.append({"name": name, "claim": claim,
                    "expected": expected, "actual": actual,
                    "ok": ok, "err": err})
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}")
    if not ok and err:
        print("        " + err.replace("\n", "\n        "))


def run(name, claim, fn):
    try:
        expected, actual = fn()
        record(name, claim, expected, actual, expected == actual)
    except Exception:
        record(name, claim, "(no exception)", "(exception)", False,
               traceback.format_exc())


# ------------------------------------------------------------------
# tests
# ------------------------------------------------------------------
def all_checks():
    conn = fresh_indexed_db()

    # ---- total live fact count (probe_jsr_rts only) ------------
    def t_total():
        n = conn.execute(
            "SELECT COUNT(*) FROM facts "
            "WHERE traveler='parser_6502' AND retracted_at IS NULL "
            "  AND (subject LIKE ? OR subject = ? OR subject = ? OR subject = ?)",
            (f"insn:{PROG}:%", f"prog:{PROG}", SUB, f"sub:{PROG}:main"),
        ).fetchone()[0]
        return 37, n
    run("count: probe_jsr_rts emits exactly 37 live facts",
        "3 prog + 2 sub:inc_a + 2 sub:main (auto) + 24 base + 4 sub edges + 2 IN_SUB(main)",
        t_total)

    # ---- program-level -----------------------------------------
    def t_prog():
        f = facts_for(conn, f"prog:{PROG}")
        return ({"HAS_MD5", "INGESTED_AT", "ENTRY_ADDR"}, set(f.keys()))
    run("prog: program subject has HAS_MD5, INGESTED_AT, ENTRY_ADDR",
        "three program-level facts per ingest (ENTRY_ADDR feeds auto-main)",
        t_prog)

    def t_prog_entry():
        f = facts_for(conn, f"prog:{PROG}")
        return ({"0x0600"}, f.get("ENTRY_ADDR", set()))
    run("prog: ENTRY_ADDR == 0x0600 (first non-vector load)",
        "default org for lesson .s files; what auto-main keys off",
        t_prog_entry)

    # ---- sub-level ---------------------------------------------
    def t_sub_predicates():
        f = facts_for(conn, SUB)
        return ({"IN_PROGRAM", "STARTS_AT"}, set(f.keys()))
    run("sub: sub:inc_a has IN_PROGRAM and STARTS_AT",
        "; label inc_a 0x0700 produces both sub-level edges",
        t_sub_predicates)

    def t_sub_starts_at():
        f = facts_for(conn, SUB)
        return ({"0x0700"}, f.get("STARTS_AT", set()))
    run("sub: STARTS_AT object equals declared address",
        "label directive's hex address is captured verbatim",
        t_sub_starts_at)

    def t_sub_in_program():
        f = facts_for(conn, SUB)
        return ({f"prog:{PROG}"}, f.get("IN_PROGRAM", set()))
    run("sub: sub:inc_a IN_PROGRAM points at prog:probe_jsr_rts",
        "subroutine entity is contained in the indexed program",
        t_sub_in_program)

    # ---- JSR call site (0x0600) --------------------------------
    def t_jsr():
        f = facts_for(conn, f"insn:{PROG}:0x0600")
        expected = {
            "AT_ADDRESS":   {"0x0600"},
            "HAS_BYTES":    {"200007"},
            "HAS_MNEMONIC": {"jsr"},
            "HAS_OPERANDS": {"$0700"},
            "HAS_SIZE":     {"3"},
            "IN_PROGRAM":   {f"prog:{PROG}"},
            "CALLS_SUB":    {SUB},
            "IN_SUB":       {f"sub:{PROG}:main"},
        }
        return expected, f
    run("jsr: 0x0600 emits 6 base + CALLS_SUB sub:inc_a + IN_SUB sub:main",
        "JSR target resolves to sub:inc_a; JSR itself lives in auto-main",
        t_jsr)

    # ---- BRK (0x0603) — main-block now lives in sub:main -------
    def t_brk():
        f = facts_for(conn, f"insn:{PROG}:0x0603")
        expected = {
            "AT_ADDRESS":   {"0x0603"},
            "HAS_BYTES":    {"00"},
            "HAS_MNEMONIC": {"brk"},
            "HAS_OPERANDS": {""},
            "HAS_SIZE":     {"1"},
            "IN_PROGRAM":   {f"prog:{PROG}"},
            "IN_SUB":       {f"sub:{PROG}:main"},
        }
        return expected, f
    run("brk: 0x0603 emits 6 base + IN_SUB sub:main (no CALLS_SUB / RETURNS)",
        "auto-main makes BRK a member of sub:<prog>:main",
        t_brk)

    # ---- auto-main sub entity --------------------------------
    def t_auto_main():
        f = facts_for(conn, f"sub:{PROG}:main")
        expected = {
            "IN_PROGRAM": {f"prog:{PROG}"},
            "STARTS_AT":  {"0x0600"},
        }
        return expected, f
    run("auto-main: sub:probe_jsr_rts:main has IN_PROGRAM + STARTS_AT 0x0600",
        "no ; label declared at entry_addr -> derive layer auto-promotes",
        t_auto_main)

    # ---- INX (0x0700) — inside sub but not the terminator ------
    def t_inx():
        f = facts_for(conn, f"insn:{PROG}:0x0700")
        expected = {
            "AT_ADDRESS":   {"0x0700"},
            "HAS_BYTES":    {"e8"},
            "HAS_MNEMONIC": {"inx"},
            "HAS_OPERANDS": {""},
            "HAS_SIZE":     {"1"},
            "IN_PROGRAM":   {f"prog:{PROG}"},
            "IN_SUB":       {SUB},
        }
        return expected, f
    run("inx: 0x0700 emits 6 base facts + IN_SUB (no RETURNS)",
        "non-terminator instructions inside a sub get IN_SUB only",
        t_inx)

    # ---- RTS (0x0701) — both IN_SUB and RETURNS ----------------
    def t_rts():
        f = facts_for(conn, f"insn:{PROG}:0x0701")
        expected = {
            "AT_ADDRESS":   {"0x0701"},
            "HAS_BYTES":    {"60"},
            "HAS_MNEMONIC": {"rts"},
            "HAS_OPERANDS": {""},
            "HAS_SIZE":     {"1"},
            "IN_PROGRAM":   {f"prog:{PROG}"},
            "IN_SUB":       {SUB},
            "RETURNS":      {SUB},
        }
        return expected, f
    run("rts: 0x0701 emits 6 base facts + IN_SUB + RETURNS",
        "RTS terminating a labeled sub gets both edges",
        t_rts)

    # ---- only the JSR in probe_jsr_rts emits CALLS_SUB ---------
    def t_calls_sub_uniqueness():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='CALLS_SUB' "
            "  AND subject LIKE ?",
            (f"insn:{PROG}:%",),
        ).fetchall()
        return ([f"insn:{PROG}:0x0600"], [r["subject"] for r in rows])
    run("calls_sub: only the JSR instruction in probe_jsr_rts emits CALLS_SUB",
        "no other mnemonic produces a call-edge",
        t_calls_sub_uniqueness)

    # ---- only the RTS in probe_jsr_rts emits RETURNS -----------
    def t_returns_uniqueness():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='RETURNS' "
            "  AND subject LIKE ?",
            (f"insn:{PROG}:%",),
        ).fetchall()
        return ([f"insn:{PROG}:0x0701"], [r["subject"] for r in rows])
    run("returns: only the RTS instruction in probe_jsr_rts emits RETURNS",
        "no other mnemonic terminates a sub",
        t_returns_uniqueness)

    # ---- IN_SUB scoped to sub:inc_a only -----------------------
    def t_in_sub_inc_a():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='IN_SUB' "
            "  AND subject LIKE ? AND object = ? "
            "ORDER BY subject",
            (f"insn:{PROG}:%", SUB),
        ).fetchall()
        expected = [f"insn:{PROG}:0x0700", f"insn:{PROG}:0x0701"]
        return expected, [r["subject"] for r in rows]
    run("in_sub: only INX and RTS are members of sub:inc_a",
        "address-range rule [STARTS_AT, next_STARTS_AT) covers 0x0700..0x0701",
        t_in_sub_inc_a)

    # ---- IN_SUB scoped to sub:probe_jsr_rts:main ---------------
    def t_in_sub_main():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='IN_SUB' "
            "  AND subject LIKE ? AND object = ? "
            "ORDER BY subject",
            (f"insn:{PROG}:%", f"sub:{PROG}:main"),
        ).fetchall()
        expected = [f"insn:{PROG}:0x0600", f"insn:{PROG}:0x0603"]
        return expected, [r["subject"] for r in rows]
    run("in_sub: JSR and BRK are members of auto sub:probe_jsr_rts:main",
        "main-block range [0x0600, 0x0700) covers JSR and BRK only",
        t_in_sub_main)

    # ==================================================================
    # MULTI-RTS PROBE (probe_multi_rts.s)
    # ==================================================================
    # Pins the multi-RTS shape: a sub with two RTS exits. The first-RTS-
    # stop rule (session 1 inline walk) would mis-tag this; the range
    # rule in derive_calls_and_subs (session 1 refactor) handles it.

    # ---- total fact count -------------------------------------
    def t_multi_total():
        n = conn.execute(
            "SELECT COUNT(*) FROM facts "
            "WHERE traveler='parser_6502' AND retracted_at IS NULL "
            "  AND (subject LIKE ? OR subject = ? OR subject = ? OR subject = ?)",
            (f"insn:{PROG_M}:%", f"prog:{PROG_M}", SUB_M, f"sub:{PROG_M}:main"),
        ).fetchone()[0]
        return 59, n
    run("multi count: probe_multi_rts emits exactly 59 live facts",
        "3 prog + 2 sub:conditional_inc + 2 sub:main + 42 base + 1 CALLS_SUB + 7 IN_SUB + 2 RETURNS",
        t_multi_total)

    # ---- sub-level facts --------------------------------------
    def t_multi_sub():
        f = facts_for(conn, SUB_M)
        expected = {
            "IN_PROGRAM": {f"prog:{PROG_M}"},
            "STARTS_AT":  {"0x0700"},
        }
        return expected, f
    run("multi sub: sub:conditional_inc has IN_PROGRAM + STARTS_AT 0x0700",
        "; label conditional_inc 0x0700 produces both sub-level edges",
        t_multi_sub)

    # ---- the late-return RTS (0x0705) -------------------------
    def t_multi_rts_late():
        f = facts_for(conn, f"insn:{PROG_M}:0x0705")
        expected = {
            "AT_ADDRESS":   {"0x0705"},
            "HAS_BYTES":    {"60"},
            "HAS_MNEMONIC": {"rts"},
            "HAS_OPERANDS": {""},
            "HAS_SIZE":     {"1"},
            "IN_PROGRAM":   {f"prog:{PROG_M}"},
            "IN_SUB":       {SUB_M},
            "RETURNS":      {SUB_M},
        }
        return expected, f
    run("multi rts-late: 0x0705 emits 6 base + IN_SUB + RETURNS",
        "fall-through RTS gets both edges",
        t_multi_rts_late)

    # ---- the early-return RTS (0x0706) ------------------------
    def t_multi_rts_early():
        f = facts_for(conn, f"insn:{PROG_M}:0x0706")
        expected = {
            "AT_ADDRESS":   {"0x0706"},
            "HAS_BYTES":    {"60"},
            "HAS_MNEMONIC": {"rts"},
            "HAS_OPERANDS": {""},
            "HAS_SIZE":     {"1"},
            "IN_PROGRAM":   {f"prog:{PROG_M}"},
            "IN_SUB":       {SUB_M},
            "RETURNS":      {SUB_M},
        }
        return expected, f
    run("multi rts-early: 0x0706 emits 6 base + IN_SUB + RETURNS (multi-RTS works)",
        "BEQ-target RTS is also tagged; first-RTS-stop bug would miss this",
        t_multi_rts_early)

    # ---- RETURNS scoped to probe_multi_rts must include BOTH RTSes
    def t_multi_returns_set():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='RETURNS' "
            "  AND subject LIKE ? "
            "ORDER BY subject",
            (f"insn:{PROG_M}:%",),
        ).fetchall()
        expected = [f"insn:{PROG_M}:0x0705", f"insn:{PROG_M}:0x0706"]
        return expected, [r["subject"] for r in rows]
    run("multi returns: both RTSes in sub:conditional_inc emit RETURNS",
        "address-range rule (not first-RTS-stop) is what makes this work",
        t_multi_returns_set)

    # ---- IN_SUB membership scoped to sub:conditional_inc ------
    def t_multi_in_sub_cond():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='IN_SUB' "
            "  AND subject LIKE ? AND object = ? "
            "ORDER BY subject",
            (f"insn:{PROG_M}:%", SUB_M),
        ).fetchall()
        expected = [f"insn:{PROG_M}:0x0700",  # CPX
                    f"insn:{PROG_M}:0x0702",  # BEQ
                    f"insn:{PROG_M}:0x0704",  # INX
                    f"insn:{PROG_M}:0x0705",  # RTS (late)
                    f"insn:{PROG_M}:0x0706"]  # RTS (early)
        return expected, [r["subject"] for r in rows]
    run("multi in_sub: every insn at addr >= 0x0700 is in sub:conditional_inc",
        "address-range walk [STARTS_AT, end-of-program) includes all 5 instructions",
        t_multi_in_sub_cond)

    # ---- IN_SUB membership scoped to sub:probe_multi_rts:main -
    def t_multi_in_sub_main():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='IN_SUB' "
            "  AND subject LIKE ? AND object = ? "
            "ORDER BY subject",
            (f"insn:{PROG_M}:%", f"sub:{PROG_M}:main"),
        ).fetchall()
        expected = [f"insn:{PROG_M}:0x0600", f"insn:{PROG_M}:0x0603"]
        return expected, [r["subject"] for r in rows]
    run("multi in_sub: JSR and BRK are members of auto sub:probe_multi_rts:main",
        "main-block range [0x0600, 0x0700) covers exactly these two",
        t_multi_in_sub_main)

    # ---- auto-main entity ------------------------------------
    def t_multi_auto_main():
        f = facts_for(conn, f"sub:{PROG_M}:main")
        expected = {
            "IN_PROGRAM": {f"prog:{PROG_M}"},
            "STARTS_AT":  {"0x0600"},
        }
        return expected, f
    run("multi auto-main: sub:probe_multi_rts:main exists and STARTS_AT 0x0600",
        "auto-main fires identically across probes; not probe-specific",
        t_multi_auto_main)

    # ==================================================================
    # AUTO-LABEL PROBE (probe_auto_label.s)
    # ==================================================================
    # JSR target at 0x0700 has no ; label directive. derive layer must
    # synthesize sub:<prog>:auto_0x0700 with full membership + CALLS_SUB.

    PROG_A = "probe_auto_label"
    SUB_A  = f"sub:{PROG_A}:auto_0x0700"

    def t_auto_total():
        n = conn.execute(
            "SELECT COUNT(*) FROM facts "
            "WHERE traveler='parser_6502' AND retracted_at IS NULL "
            "  AND (subject LIKE ? OR subject = ? OR subject = ? OR subject = ?)",
            (f"insn:{PROG_A}:%", f"prog:{PROG_A}", f"sub:{PROG_A}:main", SUB_A),
        ).fetchone()[0]
        return 37, n
    run("auto count: probe_auto_label emits exactly 37 live facts",
        "3 prog + 2 sub:main + 2 sub:auto_0x0700 + 24 base + 1 CALLS_SUB + 4 IN_SUB + 1 RETURNS",
        t_auto_total)

    def t_auto_label_entity():
        f = facts_for(conn, SUB_A)
        expected = {
            "IN_PROGRAM": {f"prog:{PROG_A}"},
            "STARTS_AT":  {"0x0700"},
        }
        return expected, f
    run("auto-label: sub:probe_auto_label:auto_0x0700 has IN_PROGRAM + STARTS_AT",
        "JSR target with no declared label gets auto-synthesized sub entity",
        t_auto_label_entity)

    def t_auto_jsr_calls_sub():
        f = facts_for(conn, f"insn:{PROG_A}:0x0600")
        return ({SUB_A}, f.get("CALLS_SUB", set()))
    run("auto-label: JSR 0x0600 CALLS_SUB points at the auto-labeled sub",
        "auto-promotion plus CALLS_SUB resolution unify in one pass",
        t_auto_jsr_calls_sub)

    def t_auto_rts_returns():
        f = facts_for(conn, f"insn:{PROG_A}:0x0701")
        return ({SUB_A}, f.get("RETURNS", set()))
    run("auto-label: RTS 0x0701 RETURNS to the auto-labeled sub",
        "RTS within the auto-sub's address range is tagged correctly",
        t_auto_rts_returns)

    def t_auto_in_sub_membership():
        rows = conn.execute(
            "SELECT subject FROM v_facts_live "
            "WHERE traveler='parser_6502' AND predicate='IN_SUB' "
            "  AND subject LIKE ? AND object = ? "
            "ORDER BY subject",
            (f"insn:{PROG_A}:%", SUB_A),
        ).fetchall()
        expected = [f"insn:{PROG_A}:0x0700", f"insn:{PROG_A}:0x0701"]
        return expected, [r["subject"] for r in rows]
    run("auto-label: INX and RTS are members of sub:auto_0x0700",
        "address-range walk treats auto-labeled subs the same as explicit ones",
        t_auto_in_sub_membership)


# ------------------------------------------------------------------
# REPORT
# ------------------------------------------------------------------
def write_report():
    n = len(RESULTS)
    p = sum(1 for r in RESULTS if r["ok"])
    f = n - p
    lines = [
        "# parser_6502 test report",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"**{p}/{n} passed**" + ("" if f == 0 else f"  &nbsp;|&nbsp;  **{f} FAILED**"),
        "",
        "| # | test | claim | expected | actual | result |",
        "|---|------|-------|----------|--------|--------|",
    ]
    for i, r in enumerate(RESULTS, 1):
        exp = repr(r["expected"]).replace("|", "\\|")
        act = repr(r["actual"]).replace("|", "\\|")
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


def main():
    print("\n[parser_6502 probe checks]")
    all_checks()
    ok = write_report()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
