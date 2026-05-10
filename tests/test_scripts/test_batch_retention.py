"""
Unit tests for the retention sweep in `scripts/batch_runner.py`
(Phase 0 Task D — audit R-6).

We stub asyncpg out via a fake connection that records the SQL it sees and
returns scripted answers, so the tests stay pure-Python and never touch a
real database. The retention sweep should:

    1. Be a no-op when RETENTION_ENABLED is not set (default).
    2. In --dry-run mode, count rows but never DELETE — regardless of the gate.
    3. When RETENTION_ENABLED=true, perform batched DELETEs that terminate
       when a round returns less than batch_size rows.
    4. Respect per-table env overrides (RETENTION_<TABLE>_DAYS).
    5. Be per-table-independent: a failure on one policy doesn't kill the
       rest of the sweep.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import pytest

from scripts import batch_runner


# --------------------------------------------------------------------------- #
# Fake asyncpg connection + get_db context manager                             #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """Records every execute/fetchval call. Behaviour is driven by the
    enclosing _FakeDB so we can script different responses per call."""

    def __init__(self, parent: "_FakeDB") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._record_execute(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._parent._record_fetchval(sql, args)


class _FakeDB:
    """Drives a sequence of canned answers and remembers what was called."""

    def __init__(
        self,
        *,
        execute_results: list[str] | None = None,
        fetchval_results: list[int] | None = None,
    ) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self._execute_results = list(execute_results or [])
        self._fetchval_results = list(fetchval_results or [])

    def _record_execute(self, sql: str, args: tuple[Any, ...]) -> str:
        self.execute_calls.append((sql, args))
        if self._execute_results:
            return self._execute_results.pop(0)
        return "DELETE 0"

    def _record_fetchval(self, sql: str, args: tuple[Any, ...]) -> Any:
        self.fetchval_calls.append((sql, args))
        if self._fetchval_results:
            return self._fetchval_results.pop(0)
        return 0

    @contextlib.asynccontextmanager
    async def get_db(self):
        yield _FakeConn(self)


@pytest.fixture
def fake_db(monkeypatch):
    """Patch `batch_runner.get_db` with a fresh _FakeDB and yield it."""
    db = _FakeDB()
    monkeypatch.setattr(batch_runner, "get_db", db.get_db)
    return db


@pytest.fixture(autouse=True)
def _clear_retention_env(monkeypatch):
    """Strip every retention-related env var so tests are deterministic."""
    monkeypatch.delenv("RETENTION_ENABLED", raising=False)
    for policy in batch_runner.RETENTION_POLICIES:
        monkeypatch.delenv(batch_runner._retention_env_var(policy.table), raising=False)


# --------------------------------------------------------------------------- #
# 1. Disabled by default                                                       #
# --------------------------------------------------------------------------- #


async def test_retention_disabled_by_default_is_noop(fake_db):
    """No env vars set → sweep must not touch the DB at all."""
    await batch_runner.step_apply_retention_policies(dry_run=False)
    assert fake_db.execute_calls == []
    assert fake_db.fetchval_calls == []


async def test_retention_enabled_falsey_strings_are_noop(fake_db, monkeypatch):
    """RETENTION_ENABLED=false / 0 / "" must all be treated as off."""
    for value in ("false", "0", "no", "", "FALSE"):
        monkeypatch.setenv("RETENTION_ENABLED", value)
        await batch_runner.step_apply_retention_policies(dry_run=False)
    assert fake_db.execute_calls == []
    assert fake_db.fetchval_calls == []


# --------------------------------------------------------------------------- #
# 2. Dry-run mode: bypass gate, only COUNT                                     #
# --------------------------------------------------------------------------- #


async def test_dry_run_bypasses_gate_and_only_counts(fake_db, monkeypatch):
    """Dry-run must work even when RETENTION_ENABLED is unset, and must
    issue exactly one fetchval (COUNT) per policy and zero DELETE calls."""
    # Pre-populate fetchval results — one per policy, arbitrary counts
    fake_db._fetchval_results = [42] * len(batch_runner.RETENTION_POLICIES)

    await batch_runner.step_apply_retention_policies(dry_run=True)

    # One COUNT per policy
    assert len(fake_db.fetchval_calls) == len(batch_runner.RETENTION_POLICIES)
    # Zero DELETEs
    assert fake_db.execute_calls == []
    # Every COUNT should target SELECT COUNT(*)
    for sql, _args in fake_db.fetchval_calls:
        assert "SELECT COUNT(*)" in sql
        assert "DELETE" not in sql


# --------------------------------------------------------------------------- #
# 3. Enabled mode: batched DELETE that terminates                              #
# --------------------------------------------------------------------------- #


async def test_enabled_runs_delete_for_each_policy(fake_db, monkeypatch):
    """RETENTION_ENABLED=true → one or more DELETE per policy.
    With every DELETE returning < batch_size rows, the loop exits after
    a single round per policy."""
    monkeypatch.setenv("RETENTION_ENABLED", "true")
    fake_db._execute_results = ["DELETE 5"] * len(batch_runner.RETENTION_POLICIES)

    await batch_runner.step_apply_retention_policies(dry_run=False)

    # No COUNT queries when not dry-running
    assert fake_db.fetchval_calls == []
    # Exactly one DELETE per policy (5 < default batch_size of 10_000)
    assert len(fake_db.execute_calls) == len(batch_runner.RETENTION_POLICIES)
    for sql, _args in fake_db.execute_calls:
        assert "DELETE FROM" in sql
        assert "ctid IN" in sql  # batched-delete idiom


async def test_batched_delete_loop_terminates_when_round_returns_less_than_batch_size(
    monkeypatch,
):
    """Per-policy: simulate two full rounds then a short round; the loop
    must stop after the short round."""
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    db = _FakeDB(
        execute_results=[
            "DELETE 100",  # full batch
            "DELETE 100",  # full batch
            "DELETE 7",    # short batch → loop must exit
        ],
    )
    monkeypatch.setattr(batch_runner, "get_db", db.get_db)

    deleted = await batch_runner._apply_one_retention_policy(
        policy, dry_run=False, batch_size=100, max_batches=100
    )

    assert deleted == 207
    assert len(db.execute_calls) == 3


async def test_batched_delete_loop_respects_max_batches_safety_cap(monkeypatch):
    """If the DB keeps returning full batches (pathological case), the loop
    must still bound itself by max_batches and NOT spin forever."""
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    db = _FakeDB(execute_results=["DELETE 100"] * 50)
    monkeypatch.setattr(batch_runner, "get_db", db.get_db)

    deleted = await batch_runner._apply_one_retention_policy(
        policy, dry_run=False, batch_size=100, max_batches=5
    )

    assert deleted == 500
    assert len(db.execute_calls) == 5


# --------------------------------------------------------------------------- #
# 4. Env overrides                                                             #
# --------------------------------------------------------------------------- #


def test_resolve_retention_days_uses_default_when_unset():
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    assert batch_runner._resolve_retention_days(policy) == 90


def test_resolve_retention_days_uses_env_override(monkeypatch):
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    monkeypatch.setenv("RETENTION_DECISION_LOG_DAYS", "30")
    assert batch_runner._resolve_retention_days(policy) == 30


def test_resolve_retention_days_falls_back_on_garbage(monkeypatch):
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    monkeypatch.setenv("RETENTION_DECISION_LOG_DAYS", "not-a-number")
    assert batch_runner._resolve_retention_days(policy) == 90


def test_resolve_retention_days_falls_back_on_zero(monkeypatch):
    """A 0-or-negative override would mean "delete everything right now" —
    almost certainly an operator mistake. Fall back to default instead."""
    policy = batch_runner.RetentionPolicy("decision_log", "time", 90)
    monkeypatch.setenv("RETENTION_DECISION_LOG_DAYS", "0")
    assert batch_runner._resolve_retention_days(policy) == 90
    monkeypatch.setenv("RETENTION_DECISION_LOG_DAYS", "-5")
    assert batch_runner._resolve_retention_days(policy) == 90


# --------------------------------------------------------------------------- #
# 5. Per-table independence                                                    #
# --------------------------------------------------------------------------- #


async def test_failing_policy_does_not_kill_the_run(fake_db, monkeypatch):
    """If one policy raises, the sweep should log and continue with the rest."""
    monkeypatch.setenv("RETENTION_ENABLED", "true")

    calls: list[str] = []

    async def fake_apply(policy, *, dry_run, batch_size=10_000, max_batches=10_000):
        calls.append(policy.table)
        if policy.table == "book_quality_snapshots":
            raise RuntimeError("simulated boom")
        return 0

    with patch.object(batch_runner, "_apply_one_retention_policy", side_effect=fake_apply):
        await batch_runner.step_apply_retention_policies(dry_run=False)

    # Every policy was attempted, despite the second one raising.
    assert [p.table for p in batch_runner.RETENTION_POLICIES] == calls


# --------------------------------------------------------------------------- #
# 6. CLI flag plumbing                                                         #
# --------------------------------------------------------------------------- #


def test_cli_dry_run_flag_is_parsed():
    args = batch_runner._parse_cli_args(["--dry-run"])
    assert args.dry_run is True


def test_cli_default_is_not_dry_run():
    args = batch_runner._parse_cli_args([])
    assert args.dry_run is False


# --------------------------------------------------------------------------- #
# 7. Policy registry sanity                                                    #
# --------------------------------------------------------------------------- #


def test_retention_policies_cover_all_audit_r6_tables():
    """R-6 in docs/audit/01_data_inventory.md lists nine tables; make sure
    the registry covers each one."""
    expected = {
        "decision_log",
        "book_quality_snapshots",
        "portfolio_equity",
        "decision_state_transitions",
        "live_orders",
        "signal_audits",
        "fee_snapshots",
        "system_control_audit",
        "risk_config_history",
    }
    actual = {p.table for p in batch_runner.RETENTION_POLICIES}
    assert actual == expected


def test_retention_policies_have_positive_defaults():
    for policy in batch_runner.RETENTION_POLICIES:
        assert policy.default_days > 0, f"{policy.table} has non-positive default"
