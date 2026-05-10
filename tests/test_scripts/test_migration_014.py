"""
Tests for migration 014_partial_indexes.sql (Phase 2 Task B).

These tests are pure-Python — they NEVER touch a real database. They assert
two invariants:

    1. The migration file is syntactically well-formed (parens balanced,
       statements terminated, the CONCURRENTLY apply-procedure header
       comment is present).
    2. Every index / constraint name introduced by migration 014 is unique
       across all migrations 001..014 — no collisions with existing
       schema objects.

Audit traceability: docs/audit/phase2/B_partial_indexes.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "docs" / "migrations"
MIGRATION_014 = MIGRATIONS_DIR / "014_partial_indexes.sql"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

# Match either `CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] <name>`
# or that same form on the next non-blank line after `CREATE` (we keep this
# tolerant of whitespace / line breaks between keywords).
_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# Match `CONSTRAINT <name>` only in DDL contexts that introduce a named
# constraint (ADD CONSTRAINT or `CONSTRAINT <name>` inside a CREATE TABLE).
# We match `(?:ADD\s+)?CONSTRAINT\s+<ident>\s+(CHECK|FOREIGN|UNIQUE|PRIMARY|REFERENCES)`
# so that bare `VALIDATE CONSTRAINT <name>` is skipped — those don't
# *introduce* a constraint name, they only reference one already added.
_CONSTRAINT_DEFINE_RE = re.compile(
    r"(?:ADD\s+)?CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:CHECK|FOREIGN|UNIQUE|PRIMARY|REFERENCES)",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* … */ block comments so that the regex
    scanners do not pick up names that appear only inside documentation
    comments (the 014 header references future index ideas in comments)."""
    no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", "", no_block)
    return no_line


def _extract_indexes(sql: str) -> list[str]:
    return _INDEX_RE.findall(_strip_sql_comments(sql))


def _extract_constraints(sql: str) -> list[str]:
    return _CONSTRAINT_DEFINE_RE.findall(_strip_sql_comments(sql))


def _migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert files, f"no migrations found under {MIGRATIONS_DIR}"
    return files


# --------------------------------------------------------------------------- #
# 1. File exists & is non-empty                                                #
# --------------------------------------------------------------------------- #


def test_migration_014_file_present():
    assert MIGRATION_014.is_file(), f"missing {MIGRATION_014}"
    assert MIGRATION_014.stat().st_size > 0, "migration 014 is empty"


# --------------------------------------------------------------------------- #
# 2. Syntactic sanity                                                          #
# --------------------------------------------------------------------------- #


def test_migration_014_parens_balanced():
    """Cheap structural check: open/close paren counts match. This catches
    the most common mistake from hand-edits without spinning up Postgres."""
    sql = MIGRATION_014.read_text()
    # Strip string literals so a stray paren inside a SQL string is ignored.
    stripped = re.sub(r"'(?:''|[^'])*'", "''", sql)
    opens = stripped.count("(")
    closes = stripped.count(")")
    assert opens == closes, f"paren mismatch: {opens} '(' vs {closes} ')'"


def test_migration_014_no_trailing_garbage():
    """Each top-level statement must end with ';'. The last non-comment
    line of the file should be either a ';'-terminated statement or a
    comment line. We scan for orphan keywords with no terminator."""
    sql = _strip_sql_comments(MIGRATION_014.read_text())
    # Last meaningful character (ignoring whitespace) must be ';'
    trimmed = sql.rstrip()
    assert trimmed.endswith(";"), (
        "migration 014 does not end on a ';'-terminated statement "
        f"(last 80 chars: {trimmed[-80:]!r})"
    )


def test_migration_014_declares_concurrently_apply_procedure():
    """The header comment MUST explain that this migration cannot be applied
    via the standard setup_db.py runner. Without that note an operator will
    pipe it through the runner and hit the 'CREATE INDEX CONCURRENTLY cannot
    run inside a transaction block' error."""
    sql = MIGRATION_014.read_text()
    must_contain_any = (
        "CREATE INDEX CONCURRENTLY",
        "cannot run inside",
        "APPLY PROCEDURE",
        "psql",
    )
    for needle in must_contain_any:
        assert needle in sql, f"migration 014 header missing reference to {needle!r}"


def test_migration_014_uses_concurrently_for_every_create_index():
    """All non-trivial indexes added here are on hot dashboard tables; every
    one must be CONCURRENTLY so we never hold ACCESS EXCLUSIVE."""
    sql = _strip_sql_comments(MIGRATION_014.read_text())
    # Pull each `CREATE [UNIQUE] INDEX ...` statement up to the next ';'
    statements = re.findall(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*?;",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert statements, "no CREATE INDEX statements found in migration 014"
    for stmt in statements:
        assert re.search(r"\bCONCURRENTLY\b", stmt, re.IGNORECASE), (
            "migration 014 has a CREATE INDEX without CONCURRENTLY; this would "
            f"hold ACCESS EXCLUSIVE on a hot table:\n{stmt.strip()}"
        )


# --------------------------------------------------------------------------- #
# 3. Index / constraint name uniqueness across all migrations                  #
# --------------------------------------------------------------------------- #


def test_migration_014_index_names_do_not_collide():
    """Every index introduced by 014 must NOT appear in any migration
    001..013. (Indexes use `IF NOT EXISTS`, so a name clash would silently
    skip — but that's worse than a hard fail, because the existing index
    might have different predicates.)"""
    new_indexes = set(_extract_indexes(MIGRATION_014.read_text()))
    assert new_indexes, "migration 014 declares no indexes (regex broken?)"

    prior_indexes: set[str] = set()
    for fp in _migration_files():
        if fp.name == MIGRATION_014.name:
            continue
        prior_indexes.update(_extract_indexes(fp.read_text()))

    collisions = new_indexes & prior_indexes
    assert not collisions, (
        f"migration 014 reuses index name(s) from earlier migrations: {sorted(collisions)}"
    )


def test_migration_014_constraint_names_do_not_collide():
    """Same idempotency safety net for CHECK + FK constraint names."""
    new_constraints = set(_extract_constraints(MIGRATION_014.read_text()))
    assert new_constraints, "migration 014 declares no named constraints (regex broken?)"

    prior_constraints: set[str] = set()
    for fp in _migration_files():
        if fp.name == MIGRATION_014.name:
            continue
        prior_constraints.update(_extract_constraints(fp.read_text()))

    collisions = new_constraints & prior_constraints
    assert not collisions, (
        f"migration 014 reuses constraint name(s) from earlier migrations: "
        f"{sorted(collisions)}"
    )


# --------------------------------------------------------------------------- #
# 4. Expected objects are present (regression guard)                           #
# --------------------------------------------------------------------------- #
# These are NOT a duplicate of the collision check above — they ensure the
# audit traceability matrix in B_partial_indexes.md stays in sync with the
# SQL. If someone accidentally deletes one of these in a rebase, the test
# fails and the audit doc has to be updated explicitly.


_EXPECTED_INDEXES = {
    "idx_paper_trades_v1_active_opened",
    "idx_paper_trades_v1_active_closed",
    "idx_decision_log_v1_active_time",
    "idx_positions_reconstructed_v1_active_opened",
    "idx_signal_audits_decision_id",
}

_EXPECTED_CONSTRAINTS = {
    # FK
    "fk_signal_audits_decision_id",
    # CHECKs
    "ck_paper_trades_direction",
    "ck_paper_trades_status",
    "ck_paper_trades_strategy",
    "ck_positions_reconstructed_direction",
    "ck_positions_reconstructed_close_method",
    "ck_decision_log_action",
    "ck_decision_log_outcome",
}


@pytest.mark.parametrize("name", sorted(_EXPECTED_INDEXES))
def test_migration_014_declares_expected_index(name: str):
    actual = set(_extract_indexes(MIGRATION_014.read_text()))
    assert name in actual, (
        f"migration 014 is missing expected index {name!r}; actual={sorted(actual)}"
    )


@pytest.mark.parametrize("name", sorted(_EXPECTED_CONSTRAINTS))
def test_migration_014_declares_expected_constraint(name: str):
    actual = set(_extract_constraints(MIGRATION_014.read_text()))
    assert name in actual, (
        f"migration 014 is missing expected constraint {name!r}; actual={sorted(actual)}"
    )


# --------------------------------------------------------------------------- #
# 5. Helper self-tests — confirm the regexes do what we claim                  #
# --------------------------------------------------------------------------- #


def test_helper_extract_indexes_smoke():
    sample = """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS my_idx_a ON t (c) WHERE x;
        CREATE UNIQUE INDEX IF NOT EXISTS my_idx_b ON t (c);
        CREATE INDEX my_idx_c ON t (c);
        -- CREATE INDEX commented_idx ON t (c);
    """
    found = _extract_indexes(sample)
    assert found == ["my_idx_a", "my_idx_b", "my_idx_c"]


def test_helper_extract_constraints_smoke():
    sample = """
        ALTER TABLE t ADD CONSTRAINT my_ck CHECK (c IN ('a','b')) NOT VALID;
        ALTER TABLE t ADD CONSTRAINT my_fk FOREIGN KEY (c) REFERENCES u (id);
        ALTER TABLE t VALIDATE CONSTRAINT my_ck;        -- must NOT be picked up
        CREATE TABLE z (id INT, CONSTRAINT z_uq UNIQUE (id));
    """
    found = _extract_constraints(sample)
    # Order of regex matches in the source — VALIDATE is excluded.
    assert "my_ck" in found
    assert "my_fk" in found
    assert "z_uq" in found
    assert "my_ck" == found[0]  # first one
    # VALIDATE CONSTRAINT must not introduce a duplicate match
    assert found.count("my_ck") == 1
