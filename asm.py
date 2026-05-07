"""
Assembler — slice 1.

Pure function: list of (mnemonic, operand) -> list of 8-bit bytes.

No CPU dependency, no chunks, no SQL writes.  Reads only the `opcodes`
table from a connection so the mnemonic set stays in sync with whatever
schema.sql defines.

Usage:
    bytes_out = assemble(
        [('LDA', 14), ('ADD', 15), ('OUT',), ('HLT',)],
        conn,
    )
    # -> [0x1E, 0x2F, 0x60, 0xF0]

Each entry may be:
    (mnemonic,)              -- operand defaults to 0
    (mnemonic, operand)      -- operand must be 0..15

Raises ValueError for unknown mnemonics or out-of-range operands.
"""


def _opcode_map(conn):
    return {
        r["mnemonic"].upper(): r["opcode"]
        for r in conn.execute("SELECT mnemonic, opcode FROM opcodes")
    }


def assemble(instructions, conn):
    ops = _opcode_map(conn)
    out = []
    for i, entry in enumerate(instructions):
        if not isinstance(entry, (tuple, list)) or len(entry) not in (1, 2):
            raise ValueError(
                f"entry {i}: expected (mnemonic,) or (mnemonic, operand), got {entry!r}"
            )
        mnem = entry[0].upper()
        operand = entry[1] if len(entry) == 2 else 0

        if mnem not in ops:
            raise ValueError(
                f"entry {i}: unknown mnemonic {mnem!r} "
                f"(known: {sorted(ops)})"
            )
        if not isinstance(operand, int) or not (0 <= operand <= 15):
            raise ValueError(
                f"entry {i}: operand {operand!r} out of 4-bit range (0..15) for {mnem}"
            )

        out.append(((ops[mnem] & 0x0F) << 4) | (operand & 0x0F))
    return out
