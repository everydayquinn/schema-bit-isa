-- =============================================================
-- corkboard_schema.sql
--
-- Cork-board substrate for the project's gameplan, decisions, and
-- multi-traveler fact ingestion. Adopted from C_Compiler Schema/facts.db
-- with six discipline mechanisms baked in as schema constraints (not
-- conventions Claude can drift past):
--
--   1. predicates require mandatory definition + canonical examples
--   2. namespaces are registered; subjects must match a registered prefix
--   3. provenance is mandatory: traveler + captured_in_context + timestamp
--   4. no edits — only retraction with explicit retracts_id link
--   5. encoding tags inside notes_for_claude JSON
--   6. boot protocol reads vocab + namespaces FIRST (handled by helpers)
--
-- The cork-board is itself a fact-producing substrate. It uses the same
-- traveler/predicate/fact triple-store as the ISA travelers (cpu_4bit,
-- parser_6502, parser_jvm). Self-similar architecture is the point.
-- =============================================================

PRAGMA foreign_keys = ON;

-- -------------------------------------------------------------
-- 1. NAMESPACES — registered subject-prefix vocabulary
--    Mechanism #2: subjects must declare which namespace they belong to.
-- -------------------------------------------------------------
DROP TABLE IF EXISTS namespaces;
CREATE TABLE namespaces (
    prefix        TEXT PRIMARY KEY
                    CHECK (prefix GLOB '[a-z][a-z_0-9]*:'),
    definition    TEXT NOT NULL,
    example       TEXT NOT NULL,
    introduced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

-- -------------------------------------------------------------
-- 2. PREDICATES — bounded vocabulary with mandatory definitions
--    Mechanism #1: definition + examples are NOT NULL.
-- -------------------------------------------------------------
DROP TABLE IF EXISTS predicates;
CREATE TABLE predicates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    domain        TEXT NOT NULL,
    range         TEXT NOT NULL,
    cardinality   TEXT NOT NULL DEFAULT 'many'
                    CHECK (cardinality IN ('one','many')),
    definition    TEXT NOT NULL,        -- precise English statement of what this asserts
    examples      TEXT NOT NULL,        -- JSON array of canonical instances
    introduced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

-- -------------------------------------------------------------
-- 3. TRAVELERS — fact producers, with role classification
-- -------------------------------------------------------------
DROP TABLE IF EXISTS travelers;
CREATE TABLE travelers (
    name        TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL,
    role        TEXT NOT NULL
                  CHECK (role IN ('substrate','meta','external','human')),
    -- substrate: emits facts about an ISA / execution (cpu_4bit, parser_6502)
    -- meta: emits facts about decisions / gameplan / contradictions
    -- external: emits facts derived from outside this project (kairos_*)
    -- human: scrawn's manual annotations
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    retired_at  TEXT,
    source      TEXT,
    note        TEXT
);

-- -------------------------------------------------------------
-- 4. FACTS — the triple store, with full provenance + retraction
--    Mechanisms #3, #4, #5 baked in:
--      captured_in_context (JSON) — what was being discussed at capture
--      notes_for_claude (JSON)    — rich Claude-side annotations
--      retracts_id (FK self-ref)  — explicit link to the fact this retracts
-- -------------------------------------------------------------
DROP TABLE IF EXISTS facts;
CREATE TABLE facts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    traveler            TEXT    NOT NULL REFERENCES travelers(name) ON UPDATE CASCADE,
    subject             TEXT    NOT NULL
                          CHECK (length(subject) BETWEEN 3 AND 512
                             AND subject GLOB '[a-z]*:*'),
    predicate_id        INTEGER NOT NULL REFERENCES predicates(id) ON UPDATE CASCADE,
    object              TEXT    NOT NULL CHECK (length(object) < 65536),
    object_kind         TEXT    NOT NULL
                          CHECK (object_kind IN ('ref','literal','json')),
    captured_in_context TEXT,             -- JSON: question, prior fact, alternatives in play
    notes_for_claude    TEXT,             -- JSON: rich annotations, evidence, encodings
    retracts_id         INTEGER REFERENCES facts(id),  -- this fact supersedes that one
    retracts_reason     TEXT,                          -- why
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    retracted_at        TEXT,
    retracted_by_id     INTEGER REFERENCES facts(id)
);

-- Trigger: enforce that subject matches a registered namespace.
DROP TRIGGER IF EXISTS fact_namespace_check;
CREATE TRIGGER fact_namespace_check BEFORE INSERT ON facts
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM namespaces WHERE NEW.subject GLOB (prefix || '*')
    ) THEN RAISE(ABORT, 'fact subject does not match any registered namespace prefix')
    END;
END;

-- Trigger: when a new fact has retracts_id, mark the old one retracted.
DROP TRIGGER IF EXISTS fact_apply_retraction;
CREATE TRIGGER fact_apply_retraction AFTER INSERT ON facts
WHEN NEW.retracts_id IS NOT NULL
BEGIN
    UPDATE facts
       SET retracted_at = NEW.created_at,
           retracted_by_id = NEW.id
     WHERE id = NEW.retracts_id AND retracted_at IS NULL;
END;

-- Indexes (mirrors Kairos facts.db + retraction-link index)
DROP INDEX IF EXISTS fact_unique_live;
CREATE UNIQUE INDEX fact_unique_live
    ON facts(traveler, subject, predicate_id, object) WHERE retracted_at IS NULL;
CREATE INDEX fact_tpos ON facts(traveler, predicate_id, object, subject) WHERE retracted_at IS NULL;
CREATE INDEX fact_tosp ON facts(traveler, object, subject, predicate_id) WHERE retracted_at IS NULL;
CREATE INDEX fact_spo  ON facts(subject, predicate_id, object) WHERE retracted_at IS NULL;
CREATE INDEX fact_retracts ON facts(retracts_id) WHERE retracts_id IS NOT NULL;

-- -------------------------------------------------------------
-- VIEWS
-- -------------------------------------------------------------

-- v_facts_live: the canonical read-side. Joins predicates + travelers
-- and filters retracted facts. Includes notes_for_claude so the boot
-- protocol surfaces nuance, not just the bare triple.
DROP VIEW IF EXISTS v_facts_live;
CREATE VIEW v_facts_live AS
SELECT f.id, f.traveler, t.role AS traveler_role,
       f.subject, p.name AS predicate, f.object, f.object_kind,
       f.captured_in_context, f.notes_for_claude, f.created_at
  FROM facts f
  JOIN predicates p ON p.id = f.predicate_id
  JOIN travelers  t ON t.name = f.traveler
 WHERE f.retracted_at IS NULL;

-- v_contradictions: surface where two travelers disagree on the same
-- subject/predicate. The discipline payoff for mechanism #3 (provenance).
-- The cork-board's purpose is to make these visible, not to resolve them.
DROP VIEW IF EXISTS v_contradictions;
CREATE VIEW v_contradictions AS
SELECT a.subject, p.name AS predicate,
       a.traveler AS traveler_a, a.object AS object_a, a.id AS fact_a_id,
       b.traveler AS traveler_b, b.object AS object_b, b.id AS fact_b_id
  FROM facts a
  JOIN facts b ON a.subject = b.subject
              AND a.predicate_id = b.predicate_id
  JOIN predicates p ON p.id = a.predicate_id
 WHERE a.traveler < b.traveler
   AND a.object <> b.object
   AND a.retracted_at IS NULL
   AND b.retracted_at IS NULL;

-- v_plan_today: the gameplan view. Only includes subjects with a FOR_DAY
-- fact (genuine deliverables — excludes contradiction-target subjects like
-- 'plan:5day:day2:deliverable' which is just where competing CLAIMS land).
DROP VIEW IF EXISTS v_plan_today;
CREATE VIEW v_plan_today AS
SELECT
    p.subject AS deliverable,
    (SELECT object FROM v_facts_live
        WHERE subject=p.subject AND predicate='ROADMAP_TITLE') AS title,
    p.day,
    (SELECT object FROM v_facts_live
        WHERE subject=p.subject AND predicate='STATUS')        AS status,
    (SELECT object FROM v_facts_live
        WHERE subject=p.subject AND predicate='CUT_REASON')    AS cut_reason,
    (SELECT object FROM v_facts_live
        WHERE subject=p.subject AND predicate='IS_BUFFER')     AS is_buffer
  FROM (
    SELECT subject, object AS day FROM v_facts_live WHERE predicate='FOR_DAY'
  ) p
 ORDER BY day, deliverable;

-- v_pinned: items explicitly pinned (do not propose, do not build)
DROP VIEW IF EXISTS v_pinned;
CREATE VIEW v_pinned AS
SELECT subject, object AS reason, traveler
  FROM v_facts_live
 WHERE predicate='PINNED_REASON';

-- v_trajectory: items named for the README's trajectory section, not built
DROP VIEW IF EXISTS v_trajectory;
CREATE VIEW v_trajectory AS
SELECT subject, object AS description, traveler
  FROM v_facts_live
 WHERE predicate='GOES_IN_TRAJECTORY';

-- v_competing_plans: the three competing 6-day gameplans side-by-side
-- (session-5 lock, web-Claude's, claude-terminal's). Boot reads this to
-- ground each session in what's contested.
DROP VIEW IF EXISTS v_competing_plans;
CREATE VIEW v_competing_plans AS
SELECT subject, traveler, predicate, object, captured_in_context, notes_for_claude
  FROM v_facts_live
 WHERE subject GLOB 'plan:5day:*' OR subject GLOB 'plan:6day:*'
 ORDER BY subject, traveler;
