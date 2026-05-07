"""
Composer — slice 2.

Reads chunks from the SQL catalog and assembles programs from them.

A program is a list of refs.  Each ref is either:

    ('LDA', 14)              -- literal mnemonic + operand
    ('LDA',)                 -- literal mnemonic, operand defaults to 0
    ('chunk_name', {params}) -- chunk reference with parameter dict
    ('chunk_name',)          -- chunk reference, no params

Disambiguation rule:
    if the head matches a known mnemonic (case-insensitive), it's literal.
    else, it's looked up in the chunks table (case-sensitive).
    else, ValueError.

Calls asm.assemble() at the end so the byte encoding stays consistent.
"""

from pathlib import Path

from asm import assemble

HERE              = Path(__file__).parent
CHUNKS_SCHEMA_SQL = (HERE / "chunks_schema.sql").read_text()


def ensure_schema(conn):
    """Apply chunks_schema.sql.  Safe to call multiple times — drops and recreates."""
    conn.executescript(CHUNKS_SCHEMA_SQL)
    conn.commit()


def _mnemonics(conn):
    return {r["mnemonic"].upper()
            for r in conn.execute("SELECT mnemonic FROM opcodes")}

def _chunk_names(conn):
    return {r["name"]
            for r in conn.execute("SELECT name FROM chunks")}


def expand_chunk(name, params, conn):
    """Expand one chunk into a flat list of (mnemonic, operand) tuples."""
    rows = conn.execute(
        "SELECT step, mnemonic, operand FROM chunk_body "
        "WHERE chunk_name=? ORDER BY step",
        (name,),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"chunk {name!r} not found or has empty body"
        )

    out = []
    for r in rows:
        op_text = r["operand"]
        if op_text is None or op_text == "":
            operand = 0
        elif op_text.startswith("$"):
            pname = op_text[1:]
            if pname not in params:
                raise ValueError(
                    f"chunk {name!r} step {r['step']}: missing param {pname!r} "
                    f"(got {sorted(params)})"
                )
            operand = params[pname]
        else:
            try:
                operand = int(op_text)
            except ValueError:
                raise ValueError(
                    f"chunk {name!r} step {r['step']}: "
                    f"operand {op_text!r} is neither int nor $param"
                )
        out.append((r["mnemonic"], operand))
    return out


def expand(refs, conn):
    """Expand a list of refs into a flat list of (mnemonic, operand) tuples."""
    mnem = _mnemonics(conn)
    chunks = _chunk_names(conn)

    flat = []
    for i, ref in enumerate(refs):
        if not isinstance(ref, (tuple, list)) or len(ref) not in (1, 2):
            raise ValueError(f"ref {i}: malformed {ref!r}")
        head = ref[0]
        rest = ref[1] if len(ref) == 2 else None

        if head.upper() in mnem:
            # literal mnemonic
            if isinstance(rest, dict):
                raise ValueError(
                    f"ref {i}: literal {head!r} got param dict, expected int operand"
                )
            operand = 0 if rest is None else rest
            flat.append((head.upper(), operand))
        elif head in chunks:
            # chunk reference
            if rest is not None and not isinstance(rest, dict):
                raise ValueError(
                    f"ref {i}: chunk {head!r} expects param dict, got {rest!r}"
                )
            params = rest or {}
            flat.extend(expand_chunk(head, params, conn))
        else:
            raise ValueError(
                f"ref {i}: {head!r} is neither a known mnemonic nor a chunk name "
                f"(known mnemonics: {sorted(mnem)}; chunks: {sorted(chunks)})"
            )
    return flat


def compose(refs, conn):
    """Top-level: refs -> flat list -> bytes."""
    flat = expand(refs, conn)
    return assemble(flat, conn)


# ------------------------------------------------------------------
# Helpers for inserting chunks (used by tests and by the miner).
# ------------------------------------------------------------------
def insert_chunk(conn, name, body, description=None, params=None, replace=False):
    """
    Insert a chunk.  body is a list of (mnemonic, operand) where operand
    can be an int or a string like '$paramname'.  Returns True if inserted,
    False if already exists and replace is False.
    """
    exists = conn.execute(
        "SELECT 1 FROM chunks WHERE name=?", (name,)
    ).fetchone()
    if exists:
        if not replace:
            return False
        conn.execute("DELETE FROM chunks WHERE name=?", (name,))

    import json
    conn.execute(
        "INSERT INTO chunks(name, description, params) VALUES (?, ?, ?)",
        (name, description, json.dumps(params or [])),
    )
    for step, (mnem, op) in enumerate(body):
        if isinstance(op, int):
            op_text = str(op)
        else:
            op_text = str(op)
        conn.execute(
            "INSERT INTO chunk_body(chunk_name, step, mnemonic, operand) "
            "VALUES (?, ?, ?, ?)",
            (name, step, mnem.upper(), op_text),
        )
    conn.commit()
    return True
