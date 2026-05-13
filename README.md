# schema-bit-isa

Two register machines, same predicate vocabulary in SQL.

The 4-bit CPU lives in [schema-bit-cpu](https://github.com/everydayquinn/schema-bit-cpu) as a self-contained artifact. This repo holds the *same* 4-bit code alongside a 6502 (driven by [py65](https://github.com/mnaberez/py65)) so you can see the pattern travel across instruction-set architectures.

The duplication is explicit and intentional — same artifact, two relations. `schema-bit-cpu` is *the CPU alone*. `schema-bit-isa` is *the CPU as one entry in a register-machine substrate library*. Two ways of looking at the same thing.

## What this repository does

Records execution traces from two different instruction sets into the same SQLite database: an in-house 4-bit register machine (re-emits its own `state_log`) and a 6502 (driven by py65, observed both statically via disassembly and dynamically via per-step execution with optional IRQ injection).

## What it produces

A SQLite database (`corkboard.db`) with rows under three source labels:

- `cpu_4bit` — 4-bit CPU execution steps written into the shared table shape
- `parser_6502` — static disassembly entries (mnemonic, address, program, bytes)
- `sim_6502` — per-step runtime entries (instruction at address, register writes, memory reads/writes, branches, interrupts)

Rows use shared column names: `traveler`, `predicate`, `subject`, `object`.

## What it explores

Comparison of instruction sets: writing execution traces from a custom register machine and a real 6502 into the same SQLite table shape so the same SQL queries return rows from both. Execution trace normalization for cross-ISA comparison.

## Relation to other repositories

This repository is independent. It does not depend on or execute other repositories.

The other repos in this account — `schema-bit-cpu`, `schema-bit-jvm`, `schema-bit-graph`, `macro-schema-dsl` — are independent experiments that also store some view of computation in SQLite. The similarity is limited to:

- shared use of SQLite as the storage format
- overlap in column names where the same concept happens to fit (e.g. `predicate`, `subject`, `object`, `traveler`)

## What's in here

**The 4-bit CPU.** 13 opcodes, 21 control lines, microcode in SQL tables. Mirrored from `schema-bit-cpu`; full description is in that repo's README.

**A 6502 static parser** (`parser_6502.py`). Reads `.s` lesson files via py65's disassembler. Emits facts at the same granularity the 4-bit emits: `HAS_MNEMONIC`, `AT_ADDRESS`, `IN_PROGRAM`, `HAS_BYTES`.

**A 6502 runtime simulator** (`sim_6502.py`). Executes the same lesson files, optionally injects an IRQ at a chosen step. Emits per-step facts: `STEP_AT_ADDR`, `WRITES_REG`, `MEM_READ`, `MEM_WRITE`, `BRANCH`, `INTERRUPT`.

**A bridge from the 4-bit CPU into the same fact-store** (`cpu_4bit_traveler.py`). Re-emits the CPU's `state_log` rows as substrate facts using the same predicates the 6502 parser uses.

**A slim cork-board substrate** (`corkboard.py` + `corkboard_schema.sql` + `seed_substrate.py`) — predicate vocabulary, namespace gating, retraction discipline. The 4-bit CPU and the 6502 both emit into the same `corkboard.db`.

## The cross-substrate query

```sql
SELECT traveler, predicate, COUNT(*) AS n
FROM v_facts_live
WHERE predicate IN ('HAS_MNEMONIC', 'BRANCH', 'MEM_WRITE')
  AND traveler IN ('cpu_4bit', 'parser_6502', 'sim_6502')
GROUP BY traveler, predicate
ORDER BY traveler, predicate;
```

```
traveler     predicate     n
cpu_4bit     BRANCH        17
cpu_4bit     HAS_MNEMONIC   6
parser_6502  HAS_MNEMONIC   4
sim_6502     BRANCH        11
sim_6502     MEM_WRITE      4
```

The query has no idea what 4-bit means or what 6502 means. It just asks the database. The same predicate fires for both ISA shapes.

## Reproducing it

```bash
git clone https://github.com/everydayquinn/schema-bit-isa
cd schema-bit-isa
pip install py65

python verify_cpu_4bit.py                                           # CPU integrity (13 edge programs)
python seed_substrate.py                                            # register vocabulary in corkboard.db
python cpu_4bit_traveler.py countdown                               # 4-bit emits substrate facts
python parser_6502.py kit_6502_lessons/01_basic.s                   # 6502 static
python sim_6502.py kit_6502_lessons/02_interrupt.s --irq-at-step 3 --scenario 02_irq3  # 6502 runtime + IRQ
```

Then run the cross-substrate query above against `corkboard.db`.

## What this proves and what it doesn't

**Proves:** the predicate vocabulary I picked for the 4-bit CPU (`HAS_MNEMONIC`, `BRANCH`, `WRITES_REG`, `MEM_WRITE`, `AT_ADDRESS`, `INTERRUPT`, `CYCLES`) is generic enough to absorb facts from a real, externally-defined ISA (the 6502) without modification. Same SQL queries return rows for both. That isn't asserted; it's queried.

**Doesn't prove:** that the 6502 corpus here is exhaustive (two lesson files, ~14 instructions). The point of this repo is the *shape* — that the predicates are reusable across ISAs — not the depth of the 6502 coverage. Larger 6502 programs (e.g. Klaus Dormann's functional test) would extend coverage; the predicate set wouldn't have to change.

## Related

- [schema-bit-cpu](https://github.com/everydayquinn/schema-bit-cpu) — the 4-bit CPU as a standalone artifact, with its full analysis stack and dispatcher. The 4-bit code in this repo is the same code.
- [schema-bit-graph](https://github.com/everydayquinn/schema-bit-graph) — Java source indexer + JVM bytecode parser + runtime tracer using the same predicate-vocabulary pattern, applied to a real Java codebase.

## Contact

[github.com/everydayquinn](https://github.com/everydayquinn) — backend / data engineering / contract roles.
