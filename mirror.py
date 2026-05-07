"""
Mirror / projection layer.

Reads the immutable execution archive (state_log) and rebuilds derived
tables that reshape it into more useful shapes.  These tables are NOT
the source of truth — they can be dropped and rebuilt from state_log
at any time without losing information.

Naming convention:  d_<name>  ("d" = derived).

Projections built here:
  d_instruction_trace   one row per executed instruction (T-states collapsed)
  d_memory_access       every RAM read and every RAM write
  d_instruction_freq    how many times each instruction ran
"""

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB   = HERE / "cpu.db"

PROJECTIONS = ("d_instruction_trace", "d_memory_access", "d_instruction_freq")


def rebuild(conn):
    """Drop every derived table and recompute from state_log + ram + opcodes."""
    for t in PROJECTIONS:
        conn.execute(f"DROP TABLE IF EXISTS {t}")

    # ---------------------------------------------------------------
    # d_instruction_trace : one row per cycle
    # ---------------------------------------------------------------
    conn.execute("""
        CREATE TABLE d_instruction_trace (
            cycle        INTEGER PRIMARY KEY,
            mnemonic     TEXT,
            operand      INTEGER,
            pc_before    INTEGER, pc_after    INTEGER,
            a_before     INTEGER, a_after     INTEGER,
            out_before   INTEGER, out_after   INTEGER,
            halted_after INTEGER,
            t_states     INTEGER          -- how many micro-steps the instruction took
        )
    """)
    conn.execute("""
        WITH cycle_last AS (
            SELECT cycle, MAX(step) AS last_step
            FROM   state_log
            GROUP  BY cycle
        ),
        cycle_state AS (
            SELECT s.cycle, s.pc, s.a, s.out, s.ir, s.halted
            FROM   state_log s
            JOIN   cycle_last cl ON s.step = cl.last_step
        ),
        cycle_count AS (
            SELECT cycle, COUNT(*) AS n FROM state_log GROUP BY cycle
        )
        INSERT INTO d_instruction_trace
        SELECT
            cs.cycle,
            COALESCE(o.mnemonic, '???')                                 AS mnemonic,
            cs.ir & 15                                                  AS operand,
            COALESCE(LAG(cs.pc)  OVER (ORDER BY cs.cycle), 0)           AS pc_before,
            cs.pc                                                        AS pc_after,
            COALESCE(LAG(cs.a)   OVER (ORDER BY cs.cycle), 0)           AS a_before,
            cs.a                                                         AS a_after,
            COALESCE(LAG(cs.out) OVER (ORDER BY cs.cycle), 0)           AS out_before,
            cs.out                                                       AS out_after,
            cs.halted                                                    AS halted_after,
            cc.n                                                         AS t_states
        FROM   cycle_state cs
        LEFT   JOIN opcodes o    ON o.opcode = (cs.ir >> 4) & 15
        JOIN   cycle_count cc    ON cc.cycle = cs.cycle
        ORDER  BY cs.cycle
    """)

    # ---------------------------------------------------------------
    # d_memory_access : one row per read or write
    #   signals is comma-separated tokens; 'ri' and 'ro' are unique
    #   substrings, so LIKE %ri% / %ro% is safe.
    # ---------------------------------------------------------------
    conn.execute("""
        CREATE TABLE d_memory_access (
            step      INTEGER PRIMARY KEY,
            cycle     INTEGER,
            instr     TEXT,
            t         INTEGER,
            direction TEXT,             -- 'read' or 'write'
            addr      INTEGER,
            value     INTEGER,
            signals   TEXT
        )
    """)
    conn.execute("""
        INSERT INTO d_memory_access
        SELECT
            step, cycle, instr, t,
            CASE
                WHEN signals LIKE '%ri%' THEN 'write'
                WHEN signals LIKE '%ro%' THEN 'read'
            END                              AS direction,
            mar                              AS addr,
            bus                              AS value,
            signals
        FROM state_log
        WHERE signals LIKE '%ri%' OR signals LIKE '%ro%'
        ORDER BY step
    """)

    # ---------------------------------------------------------------
    # d_instruction_freq : count of executed instructions
    # ---------------------------------------------------------------
    conn.execute("""
        CREATE TABLE d_instruction_freq (
            mnemonic TEXT PRIMARY KEY,
            count    INTEGER NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO d_instruction_freq
        SELECT mnemonic, COUNT(*) AS count
        FROM   d_instruction_trace
        GROUP  BY mnemonic
        ORDER  BY count DESC
    """)

    conn.commit()


def show(conn):
    print("\nd_instruction_trace:")
    print(f"{'cyc':>3} {'instr':<5} {'op':>2}  "
          f"{'pc_b':>4}->{'pc_a':<4} {'a_b':>3}->{'a_a':<3} "
          f"{'out_b':>5}->{'out_a':<5} {'hlt':>3} {'Ts':>2}")
    for r in conn.execute("SELECT * FROM d_instruction_trace"):
        print(f"{r['cycle']:>3} {r['mnemonic']:<5} {r['operand']:>2}  "
              f"{r['pc_before']:>4}->{r['pc_after']:<4} "
              f"{r['a_before']:>3}->{r['a_after']:<3} "
              f"{r['out_before']:>5}->{r['out_after']:<5} "
              f"{r['halted_after']:>3} {r['t_states']:>2}")

    print("\nd_memory_access:")
    print(f"{'step':>4} {'cyc':>3} {'dir':<5} {'addr':>4} {'val':>4}  via")
    for r in conn.execute("SELECT * FROM d_memory_access"):
        print(f"{r['step']:>4} {r['cycle']:>3} {r['direction']:<5} "
              f"{r['addr']:>4} {r['value']:>4}  {r['instr']} t{r['t']}")

    print("\nd_instruction_freq:")
    for r in conn.execute("SELECT * FROM d_instruction_freq"):
        print(f"  {r['mnemonic']:<5} {r['count']}")


if __name__ == "__main__":
    if not DB.exists():
        print(f"no database at {DB} — run cpu.py first", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rebuild(conn)
    show(conn)
