"""
4-bit CPU orchestrator.

SQL is the source of truth:
  - mc_<instr> tables are the hardwired control matrix (read-only at runtime)
  - ram, registers hold live state (mutated each T-state)
  - state_log is the append-only execution history

Python is the clock + bus + ALU. It does NOT decide what to do; it asks the
microcode tables what to do, then drives the wires accordingly.
"""

import sqlite3
import sys
from pathlib import Path

HERE      = Path(__file__).parent
SCHEMA    = HERE / "schema.sql"
DB        = HERE / "cpu.db"

CONTROL_LINES = ['hlt','mi','ri','ro','io','ii','ai','ao',
                 'eo','su','andop','orop','xorop','notop','fi',
                 'bi','oi','ce','co','j','jc']


# ------------------------------------------------------------------
# DB bootstrap
# ------------------------------------------------------------------
def build_db():
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    return conn


# ------------------------------------------------------------------
# CPU
# ------------------------------------------------------------------
class CPU:
    def __init__(self, conn):
        self.db = conn
        self.db.row_factory = sqlite3.Row
        self.pc = self.mar = self.ir = 0
        self.a  = self.b   = self.alu = 0
        self.bus = self.out = 0
        self.z = 0
        self.halted = False
        self.cycle  = 0

    # ---- RAM helpers -------------------------------------------------
    def ram_read(self, addr):
        r = self.db.execute(
            "SELECT value FROM ram WHERE addr=?", (addr & 0xF,)
        ).fetchone()
        return r["value"] if r else 0

    def ram_write(self, addr, value):
        self.db.execute(
            "INSERT INTO ram(addr,value) VALUES(?,?) "
            "ON CONFLICT(addr) DO UPDATE SET value=excluded.value",
            (addr & 0xF, value & 0xFF),
        )

    # ---- one T-state -------------------------------------------------
    def fire(self, sig):
        """Drive one row of microcode onto the bus and latch inputs."""
        # ALU is combinational: result tracks A, B, and the active mode bit.
        # Mode bits are mutually exclusive (enforced by static check).
        if   sig["notop"]: self.alu = (~self.a)        & 0xFF
        elif sig["andop"]: self.alu = (self.a & self.b) & 0xFF
        elif sig["orop"]:  self.alu = (self.a | self.b) & 0xFF
        elif sig["xorop"]: self.alu = (self.a ^ self.b) & 0xFF
        elif sig["su"]:    self.alu = (self.a - self.b) & 0xFF
        else:              self.alu = (self.a + self.b) & 0xFF

        # 1. exactly one output line should drive the bus
        if   sig["co"]: self.bus = self.pc
        elif sig["ro"]: self.bus = self.ram_read(self.mar)
        elif sig["io"]: self.bus = self.ir & 0x0F     # operand only
        elif sig["ao"]: self.bus = self.a
        elif sig["eo"]: self.bus = self.alu

        # 2. inputs latch from the bus
        if sig["mi"]: self.mar = self.bus & 0x0F
        if sig["ri"]: self.ram_write(self.mar, self.bus)
        if sig["ii"]: self.ir  = self.bus & 0xFF
        if sig["ai"]: self.a   = self.bus & 0xFF
        if sig["bi"]: self.b   = self.bus & 0xFF
        if sig["oi"]: self.out = self.bus & 0xFF
        if sig["j"]:  self.pc  = self.bus & 0x0F
        if sig["jc"] and self.z:
            self.pc = self.bus & 0x0F

        # 2b. flags: latch Z from the ALU output when fi fires
        if sig["fi"]:
            self.z = 1 if self.alu == 0 else 0

        # 3. PC counter / halt
        if sig["ce"]:  self.pc = (self.pc + 1) & 0x0F
        if sig["hlt"]: self.halted = True

    # ---- log a snapshot --------------------------------------------
    def log(self, instr, sig):
        active = ",".join(k for k in CONTROL_LINES if sig[k])
        self.db.execute(
            """INSERT INTO state_log
               (cycle, instr, t, pc, mar, ir, a, b, alu, bus, out, halted, z, signals)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.cycle, instr, sig["t"],
             self.pc, self.mar, self.ir,
             self.a,  self.b,   self.alu,
             self.bus, self.out, int(self.halted), self.z, active),
        )

    # ---- one fetch-execute cycle -----------------------------------
    def run_cycle(self):
        # T0 .. T1  (shared fetch)
        for row in self.db.execute("SELECT * FROM mc_fetch ORDER BY t"):
            sig = dict(row)
            self.fire(sig)
            self.log("fetch", sig)

        # decode
        opcode = (self.ir >> 4) & 0x0F
        info = self.db.execute(
            "SELECT mnemonic, mc_table FROM opcodes WHERE opcode=?", (opcode,)
        ).fetchone()
        if info is None:
            raise RuntimeError(f"unknown opcode 0x{opcode:X} at PC={self.pc}")
        mnemonic, mc_table = info["mnemonic"], info["mc_table"]

        # T2 .. Tn  (per-instruction tail)
        for row in self.db.execute(f"SELECT * FROM {mc_table} ORDER BY t"):
            sig = dict(row)
            self.fire(sig)
            self.log(mnemonic, sig)

        self.cycle += 1

    # ---- mirror live registers into the registers table ------------
    def sync_registers(self):
        for name, val in (
            ("pc",self.pc),("mar",self.mar),("ir",self.ir),
            ("a",self.a),("b",self.b),("alu",self.alu),
            ("bus",self.bus),("out",self.out),("halted",int(self.halted)),
            ("z",self.z),
        ):
            self.db.execute("UPDATE registers SET value=? WHERE name=?", (val, name))

    def run(self, max_cycles=64):
        while not self.halted and self.cycle < max_cycles:
            self.run_cycle()
        self.sync_registers()
        self.db.commit()


# ------------------------------------------------------------------
# pretty-printers
# ------------------------------------------------------------------
def show_registers(conn):
    print("registers:")
    for r in conn.execute("SELECT name, value FROM registers"):
        print(f"  {r['name']:>6} = {r['value']:#04x}  ({r['value']})")

def show_log(conn, limit=40):
    print("\nexecution log:")
    print(f"{'step':>4} {'cyc':>3} {'instr':<6} {'t':>2}  "
          f"{'pc':>2} {'mar':>3} {'ir':>4} {'a':>3} {'b':>3} {'alu':>3} "
          f"{'bus':>3} {'out':>3}  signals")
    q = conn.execute("SELECT * FROM state_log ORDER BY step LIMIT ?", (limit,))
    for r in q:
        print(f"{r['step']:>4} {r['cycle']:>3} {r['instr']:<6} {r['t']:>2}  "
              f"{r['pc']:>2} {r['mar']:>3} {r['ir']:>#04x} "
              f"{r['a']:>3} {r['b']:>3} {r['alu']:>3} "
              f"{r['bus']:>3} {r['out']:>3}  {r['signals']}")

def show_disasm(conn):
    print("\ndisassembly:")
    for r in conn.execute("SELECT * FROM v_disassembly"):
        mn = r["mnemonic"] or "DATA"
        print(f"  {r['addr']:>2}: {r['hex']}   {mn:<5} {r['operand']}")


# ------------------------------------------------------------------
# entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    import mirror

    conn = build_db()
    show_disasm(conn)

    cpu = CPU(conn)
    cpu.run()

    mirror.rebuild(conn)             # rebuild derived projections
    show_registers(conn)
    show_log(conn, limit=64)
    mirror.show(conn)

    print(f"\nfinal OUT register = {cpu.out}  (expected 7)")
