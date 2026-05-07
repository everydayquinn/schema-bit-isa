-- =============================================================
-- 4-bit CPU schema (SAP-1 style, extended)
--   data path     : 8-bit bus, 8-bit registers (A, B, OUT, IR)
--   address space : 4 bits  (16 RAM words)
--   instruction   : 4-bit opcode + 4-bit operand, packed in 8 bits
--
-- Layers in this schema:
--   1. control_lines     -- catalog of every wire in the control unit
--   2. mc_<instr>        -- one table per instruction, rows = T-states,
--                           columns = boolean control lines.
--                           This is the "control matrix" / hardwired ROM.
--   3. ram, registers    -- live machine state (mutated by Python)
--   4. state_log         -- append-only execution history
--
-- Extension layer (added 2026-05-02):
--   - Bitwise ALU ops    : AND / OR / XOR / NOT  (one-hot mode bits)
--   - Flags              : Z (zero flag), latched when `fi` fires
--   - Conditional jump   : JZ uses `jc` — j fires only if Z=1
-- =============================================================

PRAGMA foreign_keys = ON;

-- -------------------------------------------------------------
-- 1. Control line catalog (documentation; queryable)
-- -------------------------------------------------------------
DROP TABLE IF EXISTS control_lines;
CREATE TABLE control_lines (
    name        TEXT PRIMARY KEY,
    role        TEXT NOT NULL,          -- 'output' | 'input' | 'mode'
    target      TEXT,                   -- which component it touches
    description TEXT
);

INSERT INTO control_lines VALUES
('hlt',  'mode',   NULL,  'halt the clock'),
('mi',   'input',  'mar', 'load MAR from bus (low 4 bits)'),
('ri',   'input',  'ram', 'write bus into RAM[MAR]'),
('ro',   'output', 'ram', 'drive RAM[MAR] onto bus'),
('io',   'output', 'ir',  'drive IR operand (low 4 bits) onto bus'),
('ii',   'input',  'ir',  'load IR from bus'),
('ai',   'input',  'a',   'load A from bus'),
('ao',   'output', 'a',   'drive A onto bus'),
('eo',   'output', 'alu', 'drive ALU result onto bus'),
('su',   'mode',   'alu', 'ALU subtract mode'),
('andop','mode',   'alu', 'ALU AND mode'),
('orop', 'mode',   'alu', 'ALU OR mode'),
('xorop','mode',   'alu', 'ALU XOR mode'),
('notop','mode',   'alu', 'ALU NOT mode (unary on A)'),
('fi',   'input',  'z',   'latch Z flag from ALU (Z := alu == 0)'),
('bi',   'input',  'b',   'load B from bus'),
('oi',   'input',  'out', 'load OUT register from bus'),
('ce',   'mode',   'pc',  'increment PC at end of step'),
('co',   'output', 'pc',  'drive PC onto bus'),
('j',    'input',  'pc',  'load PC from bus (jump)'),
('jc',   'input',  'pc',  'load PC from bus only if Z=1 (conditional jump)');

-- -------------------------------------------------------------
-- 2. Microcode tables  (one per instruction)
--    Every cell is a boolean (0/1). A row is one T-state.
--    mc_fetch is shared by every instruction (T0-T1).
--    mc_<instr> holds the per-instruction tail (T2+).
--
--    Canonical column set (every mc_* table MUST have these):
--      t,
--      hlt, mi, ri, ro, io, ii, ai, ao,
--      eo, su, andop, orop, xorop, notop, fi,
--      bi, oi, ce, co, j, jc
-- -------------------------------------------------------------

-- Fetch  (shared front-end of every instruction)
DROP TABLE IF EXISTS mc_fetch;
CREATE TABLE mc_fetch (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0,
    mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0,
    ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0,
    ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0,
    ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0,
    su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0,
    orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0,
    notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0,
    oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0,
    co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0,
    jc    INTEGER NOT NULL DEFAULT 0
);
-- T0:  PC -> MAR
INSERT INTO mc_fetch (t, co, mi)        VALUES (0, 1, 1);
-- T1:  RAM[MAR] -> IR ; PC++
INSERT INTO mc_fetch (t, ro, ii, ce)    VALUES (1, 1, 1, 1);


-- NOP  (no execute steps)
DROP TABLE IF EXISTS mc_nop;
CREATE TABLE mc_nop (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);

-- LDA addr   :  A <- RAM[addr]
DROP TABLE IF EXISTS mc_lda;
CREATE TABLE mc_lda (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_lda (t, io, mi)          VALUES (2, 1, 1);   -- IR.op -> MAR
INSERT INTO mc_lda (t, ro, ai)          VALUES (3, 1, 1);   -- RAM[MAR] -> A

-- ADD addr   :  A <- A + RAM[addr]   (sets Z)
DROP TABLE IF EXISTS mc_add;
CREATE TABLE mc_add (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_add (t, io, mi)          VALUES (2, 1, 1);
INSERT INTO mc_add (t, ro, bi)          VALUES (3, 1, 1);   -- RAM[MAR] -> B
INSERT INTO mc_add (t, eo, ai, fi)      VALUES (4, 1, 1, 1);-- A+B -> A; latch Z

-- SUB addr   :  A <- A - RAM[addr]   (sets Z)
DROP TABLE IF EXISTS mc_sub;
CREATE TABLE mc_sub (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_sub (t, io, mi)              VALUES (2, 1, 1);
INSERT INTO mc_sub (t, ro, bi)              VALUES (3, 1, 1);
INSERT INTO mc_sub (t, eo, ai, su, fi)      VALUES (4, 1, 1, 1, 1);

-- STA addr   :  RAM[addr] <- A
DROP TABLE IF EXISTS mc_sta;
CREATE TABLE mc_sta (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_sta (t, io, mi)          VALUES (2, 1, 1);
INSERT INTO mc_sta (t, ao, ri)          VALUES (3, 1, 1);

-- JMP addr   :  PC <- addr   (unconditional)
DROP TABLE IF EXISTS mc_jmp;
CREATE TABLE mc_jmp (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_jmp (t, io, j)           VALUES (2, 1, 1);

-- OUT        :  OUT <- A
DROP TABLE IF EXISTS mc_out;
CREATE TABLE mc_out (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_out (t, ao, oi)          VALUES (2, 1, 1);

-- HLT        :  stop the clock
DROP TABLE IF EXISTS mc_hlt;
CREATE TABLE mc_hlt (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_hlt (t, hlt)             VALUES (2, 1);

-- AND addr   :  A <- A & RAM[addr]   (sets Z)
DROP TABLE IF EXISTS mc_and;
CREATE TABLE mc_and (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_and (t, io, mi)                  VALUES (2, 1, 1);
INSERT INTO mc_and (t, ro, bi)                  VALUES (3, 1, 1);
INSERT INTO mc_and (t, eo, ai, andop, fi)       VALUES (4, 1, 1, 1, 1);

-- OR  addr   :  A <- A | RAM[addr]   (sets Z)
DROP TABLE IF EXISTS mc_or;
CREATE TABLE mc_or (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_or (t, io, mi)                   VALUES (2, 1, 1);
INSERT INTO mc_or (t, ro, bi)                   VALUES (3, 1, 1);
INSERT INTO mc_or (t, eo, ai, orop, fi)         VALUES (4, 1, 1, 1, 1);

-- XOR addr   :  A <- A ^ RAM[addr]   (sets Z)
DROP TABLE IF EXISTS mc_xor;
CREATE TABLE mc_xor (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_xor (t, io, mi)                  VALUES (2, 1, 1);
INSERT INTO mc_xor (t, ro, bi)                  VALUES (3, 1, 1);
INSERT INTO mc_xor (t, eo, ai, xorop, fi)       VALUES (4, 1, 1, 1, 1);

-- NOT        :  A <- ~A             (unary, sets Z; operand ignored)
DROP TABLE IF EXISTS mc_not;
CREATE TABLE mc_not (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_not (t, eo, ai, notop, fi)       VALUES (2, 1, 1, 1, 1);

-- JZ  addr   :  if Z=1, PC <- addr   (else fall through)
DROP TABLE IF EXISTS mc_jz;
CREATE TABLE mc_jz (
    t     INTEGER PRIMARY KEY,
    hlt   INTEGER NOT NULL DEFAULT 0, mi    INTEGER NOT NULL DEFAULT 0,
    ri    INTEGER NOT NULL DEFAULT 0, ro    INTEGER NOT NULL DEFAULT 0,
    io    INTEGER NOT NULL DEFAULT 0, ii    INTEGER NOT NULL DEFAULT 0,
    ai    INTEGER NOT NULL DEFAULT 0, ao    INTEGER NOT NULL DEFAULT 0,
    eo    INTEGER NOT NULL DEFAULT 0, su    INTEGER NOT NULL DEFAULT 0,
    andop INTEGER NOT NULL DEFAULT 0, orop  INTEGER NOT NULL DEFAULT 0,
    xorop INTEGER NOT NULL DEFAULT 0, notop INTEGER NOT NULL DEFAULT 0,
    fi    INTEGER NOT NULL DEFAULT 0,
    bi    INTEGER NOT NULL DEFAULT 0, oi    INTEGER NOT NULL DEFAULT 0,
    ce    INTEGER NOT NULL DEFAULT 0, co    INTEGER NOT NULL DEFAULT 0,
    j     INTEGER NOT NULL DEFAULT 0, jc    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO mc_jz (t, io, jc)           VALUES (2, 1, 1);


-- Opcode -> microcode-table-name lookup
DROP TABLE IF EXISTS opcodes;
CREATE TABLE opcodes (
    opcode  INTEGER PRIMARY KEY,        -- 0..15
    mnemonic TEXT NOT NULL,
    mc_table TEXT NOT NULL
);
INSERT INTO opcodes VALUES
(0x0,'NOP','mc_nop'),
(0x1,'LDA','mc_lda'),
(0x2,'ADD','mc_add'),
(0x3,'SUB','mc_sub'),
(0x4,'STA','mc_sta'),
(0x5,'JMP','mc_jmp'),
(0x6,'OUT','mc_out'),
(0x7,'AND','mc_and'),
(0x8,'OR', 'mc_or'),
(0x9,'XOR','mc_xor'),
(0xA,'NOT','mc_not'),
(0xB,'JZ', 'mc_jz'),
(0xF,'HLT','mc_hlt');

-- -------------------------------------------------------------
-- 3. Live machine state
-- -------------------------------------------------------------
DROP TABLE IF EXISTS ram;
CREATE TABLE ram (
    addr  INTEGER PRIMARY KEY CHECK (addr BETWEEN 0 AND 15),
    value INTEGER NOT NULL    CHECK (value BETWEEN 0 AND 255)
);

DROP TABLE IF EXISTS registers;
CREATE TABLE registers (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT INTO registers VALUES
('pc',0),('mar',0),('ir',0),
('a',0),('b',0),('alu',0),
('bus',0),('out',0),('halted',0),
('z',0);

-- -------------------------------------------------------------
-- 4. Append-only execution history
--    Every T-state of every cycle gets one row.
-- -------------------------------------------------------------
DROP TABLE IF EXISTS state_log;
CREATE TABLE state_log (
    step    INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle   INTEGER NOT NULL,            -- which fetch-execute cycle
    instr   TEXT    NOT NULL,            -- 'fetch' or mnemonic
    t       INTEGER NOT NULL,            -- T-state within the cycle
    pc      INTEGER, mar INTEGER, ir  INTEGER,
    a       INTEGER, b   INTEGER, alu INTEGER,
    bus     INTEGER, out INTEGER, halted INTEGER,
    z       INTEGER,
    signals TEXT                          -- comma-separated active lines
);

-- -------------------------------------------------------------
-- 5. Views (perspectives onto the same truth)
-- -------------------------------------------------------------
DROP VIEW IF EXISTS v_register_timeline;
CREATE VIEW v_register_timeline AS
SELECT step, cycle, instr, t, pc, a, b, alu, out, z, signals
FROM   state_log ORDER BY step;

DROP VIEW IF EXISTS v_memory_writes;
CREATE VIEW v_memory_writes AS
SELECT step, cycle, instr, t, mar AS addr, bus AS value, signals
FROM   state_log
WHERE  signals LIKE '%ri%';

DROP VIEW IF EXISTS v_disassembly;
CREATE VIEW v_disassembly AS
SELECT r.addr,
       printf('%02X', r.value)                                  AS hex,
       o.mnemonic,
       (r.value & 0x0F)                                         AS operand
FROM   ram r
LEFT JOIN opcodes o ON o.opcode = (r.value >> 4) & 0x0F
ORDER BY r.addr;

-- -------------------------------------------------------------
-- 6. Sample program  :  3 + 4 -> OUT
--    addr 0 : LDA 14
--    addr 1 : ADD 15
--    addr 2 : OUT
--    addr 3 : HLT
--    addr 14: 3
--    addr 15: 4
-- -------------------------------------------------------------
INSERT INTO ram (addr,value) VALUES
( 0, 0x1E),  -- LDA 14
( 1, 0x2F),  -- ADD 15
( 2, 0x60),  -- OUT
( 3, 0xF0),  -- HLT
( 4, 0x00),( 5, 0x00),( 6, 0x00),( 7, 0x00),
( 8, 0x00),( 9, 0x00),(10, 0x00),(11, 0x00),
(12, 0x00),(13, 0x00),
(14, 0x03),  -- data: 3
(15, 0x04);  -- data: 4
