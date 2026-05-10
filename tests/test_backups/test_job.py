"""
Tests for src.backups.job (S4.12).

Strategy:
    * Substitute a `FakeR2Client` for `R2Client` so we can assert on
      the put/list/delete sequence without boto3.
    * Monkeypatch `run_pg_dump` to "create" the local dump file
      synchronously — no postgres-client needed.
    * End-to-end test verifies the happy path *and* the failure paths
      (pg_dump error, upload error, retention sweep error).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from unittest.mock import MagicMock

import pytest

from src.backups import job as job_module
from src.backups.dumper import PgDumpError
from src.backups.job import (
    BackupResult,
    _build_object_key,
    _decide_retention,
    _parse_ts_from_key,
    make_backup_job,
    run_backup_once,
)
from src.backups.r2_client import R2Error, R2Object


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #


def test_build_object_key_groups_by_year_month():
    ts = datetime(2026, 4, 27, 5, 30, 0, tzinfo=timezone.utc)
    key = _build_object_key(prefix="postgres/", ts=ts)
    assert key == "postgres/2026/04/2026-04-27T05-30-00Z.dump"


def test_build_object_key_normalises_prefix_without_trailing_slash():
    ts = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    assert (
        _build_object_key(prefix="postgres", ts=ts)
        == "postgres/2026/01/2026-01-05T00-00-00Z.dump"
    )


def test_build_object_key_with_empty_prefix():
    ts = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    assert (
        _build_object_key(prefix="", ts=ts)
        == "2026/01/2026-01-05T00-00-00Z.dump"
    )


def test_parse_ts_from_key_roundtrip():
    ts = datetime(2026, 4, 27, 5, 30, 0, tzinfo=timezone.utc)
    key = _build_object_key(prefix="postgres/", ts=ts)
    parsed = _parse_ts_from_key(key)
    assert parsed == ts


def test_parse_ts_from_key_invalid_returns_none():
    assert _parse_ts_from_key("postgres/random.txt") is None
    assert _parse_ts_from_key("postgres/2026-04-27.dump") is None  # missing time
    assert _parse_ts_from_key("") is None


# --------------------------------------------------------------------------- #
# _decide_retention                                                            #
# --------------------------------------------------------------------------- #


def _obj(key: str, last_mod: datetime, size: int = 100) -> R2Object:
    return R2Object(key=key, size=size, last_modified=last_mod)


def test_decide_retention_uses_embedded_ts_first():
    """The key encodes a 2026-04-27 timestamp; LastModified disagrees
    (2020). We should trust the key."""
    ts_in_key = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
    drift = datetime(2020, 1, 1, tzinfo=timezone.utc)
    objs = [_obj("postgres/2026/04/2026-04-27T05-00-00Z.dump", last_mod=drift)]
    decision = _decide_retention(objs, daily=1, weekly=0, monthly=0, weekly_dow=6)
    assert decision.keep == ["postgres/2026/04/2026-04-27T05-00-00Z.dump"]


def test_decide_retention_falls_back_to_last_modified():
    """Key has no parseable timestamp — we use LastModified."""
    objs = [
        _obj("legacy/foo.dump", last_mod=datetime(2026, 4, 27, tzinfo=timezone.utc)),
        _obj("legacy/bar.dump", last_mod=datetime(2026, 4, 26, tzinfo=timezone.utc)),
    ]
    decision = _decide_retention(objs, daily=1, weekly=0, monthly=0, weekly_dow=6)
    assert decision.keep == ["legacy/foo.dump"]
    assert decision.delete == ["legacy/bar.dump"]


def test_decide_retention_naive_datetime_normalised_to_utc():
    """Boto3 *should* hand back tz-aware datetimes, but if a backend
    returns a naive one we must not crash on the weekday() call."""
    naive = datetime(2026, 4, 27, 5, 0)  # naive
    objs = [_obj("legacy/x.dump", last_mod=naive)]
    decision = _decide_retention(objs, daily=1, weekly=0, monthly=0, weekly_dow=6)
    assert decision.keep == ["legacy/x.dump"]


# --------------------------------------------------------------------------- #
# Fake R2 client + run_backup_once                                             #
# --------------------------------------------------------------------------- #


class FakeR2Client:
    """Stand-in that records every call. Stores upload payloads in
    memory so list_objects can return what we just uploaded."""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, int]] = []
        self.delete_calls: list[list[str]] = []
        self._existing: list[R2Object] = []
        # Allow tests to seed list_objects responses or trigger errors.
        self.list_error: Exception | None = None
        self.put_error: Exception | None = None
        self.delete_error: Exception | None = None

    def seed_existing(self, objs: Iterable[R2Object]) -> None:
        self._existing.extend(objs)

    def put_file(self, *, local_path: Path, key: str) -> int:
        if self.put_error:
            raise self.put_error
        size = Path(local_path).stat().st_size
        # Surface the new object so the immediately-following retention
        # sweep sees it (mirrors real R2 read-after-write semantics).
        self._existing.append(
            R2Object(
                key=key,
                size=size,
                last_modified=datetime.now(timezone.utc),
            )
        )
        self.put_calls.append((key, size))
        return size

    def list_objects(self, *, prefix: str = "") -> Iterator[R2Object]:
        if self.list_error:
            raise self.list_error
        for o in self._existing:
            if o.key.startswith(prefix):
                yield o

    def delete_objects(self, *, keys: Iterable[str]) -> int:
        keys = list(keys)
        if self.delete_error:
            raise self.delete_error
        self.delete_calls.append(list(keys))
        self._existing = [o for o in self._existing if o.key not in set(keys)]
        return len(keys)


@pytest.fixture
def stub_pg_dump(monkeypatch):
    """Patch run_pg_dump to write a fake archive."""

    def fake_dump(*, dsn, output_path, timeout_s, pg_dump_binary=None):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"PGDMP fake bytes" * 64)
        return Path(output_path)

    monkeypatch.setattr(job_module, "run_pg_dump", fake_dump)
    return fake_dump


def test_run_backup_once_happy_path_uploads_and_prunes(stub_pg_dump, tmp_path):
    """Seed 10 daily backups; configure GFS=2/0/0; expect new upload +
    8 deletes (10 old - keep 2 most recent including the brand new one
    means 9 to delete, but daily=2 keeps 2 newest including the just-uploaded → 8 deletes of older)."""
    r2 = FakeR2Client()
    # 10 days of older backups: April 1..10 of 2026 at 05:00 UTC.
    for d in range(1, 11):
        ts = datetime(2026, 4, d, 5, 0, tzinfo=timezone.utc)
        r2.seed_existing(
            [
                R2Object(
                    key=f"postgres/2026/04/2026-04-{d:02d}T05-00-00Z.dump",
                    size=10_000,
                    last_modified=ts,
                )
            ]
        )

    # Run on April 11 at 05:00 → should upload that timestamped key.
    now = datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc)
    result = run_backup_once(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=tmp_path / "scratch",
        daily=2,
        weekly=0,
        monthly=0,
        weekly_dow=6,
        pg_dump_timeout_s=60,
        now=now,
    )

    assert result.uploaded_key == "postgres/2026/04/2026-04-11T05-00-00Z.dump"
    assert result.uploaded_bytes > 0
    # We keep the new upload + the most recent prior (April 10) = 2 daily slots.
    assert result.kept_keys == [
        "postgres/2026/04/2026-04-11T05-00-00Z.dump",
        "postgres/2026/04/2026-04-10T05-00-00Z.dump",
    ]
    assert len(result.deleted_keys) == 9
    # Local scratch file cleaned up.
    assert not any(p.suffix == ".dump" for p in (tmp_path / "scratch").iterdir())


def test_run_backup_once_first_ever_run_no_deletes(stub_pg_dump, tmp_path):
    """Empty bucket — uploads, retention has nothing to do."""
    r2 = FakeR2Client()
    now = datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc)
    result = run_backup_once(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=tmp_path / "scratch",
        daily=7,
        weekly=4,
        monthly=3,
        weekly_dow=6,
        pg_dump_timeout_s=60,
        now=now,
    )
    assert result.uploaded_key == "postgres/2026/04/2026-04-11T05-00-00Z.dump"
    assert result.deleted_keys == []
    assert result.kept_keys == ["postgres/2026/04/2026-04-11T05-00-00Z.dump"]
    assert r2.delete_calls == []


def test_run_backup_once_pg_dump_failure_propagates(monkeypatch, tmp_path):
    """If pg_dump fails we must NOT touch R2. The exception should
    bubble up so APScheduler logs it and the next cron tick retries."""

    def failing_dump(*, dsn, output_path, timeout_s, pg_dump_binary=None):
        raise PgDumpError("pg_dump exited 1: connection refused")

    monkeypatch.setattr(job_module, "run_pg_dump", failing_dump)

    r2 = FakeR2Client()
    with pytest.raises(PgDumpError, match="connection refused"):
        run_backup_once(
            dsn="postgresql://u:p@h/d",
            r2_client=r2,
            prefix="postgres/",
            scratch_dir=tmp_path / "scratch",
            daily=7,
            weekly=4,
            monthly=3,
            weekly_dow=6,
            pg_dump_timeout_s=60,
            now=datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc),
        )
    assert r2.put_calls == []
    assert r2.delete_calls == []


def test_run_backup_once_upload_failure_keeps_local_file(stub_pg_dump, tmp_path):
    """Upload failed → local dump must remain on disk so the next run
    can decide what to do with it. No retention sweep should run."""
    r2 = FakeR2Client()
    r2.put_error = R2Error("upload failed: signature mismatch")

    scratch = tmp_path / "scratch"
    with pytest.raises(R2Error, match="signature mismatch"):
        run_backup_once(
            dsn="postgresql://u:p@h/d",
            r2_client=r2,
            prefix="postgres/",
            scratch_dir=scratch,
            daily=7,
            weekly=4,
            monthly=3,
            weekly_dow=6,
            pg_dump_timeout_s=60,
            now=datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc),
        )
    # The dump file should still be sitting in scratch.
    leftover = list(scratch.glob("*.dump"))
    assert len(leftover) == 1
    assert leftover[0].stat().st_size > 0
    assert r2.delete_calls == []


def test_run_backup_once_retention_failure_does_not_fail_run(
    stub_pg_dump, tmp_path
):
    """delete_objects errored out — but the new backup is already
    persisted, so we count the run as a success."""
    r2 = FakeR2Client()
    r2.delete_error = R2Error("AccessDenied")
    # Seed one old object so retention has something to delete.
    r2.seed_existing(
        [
            R2Object(
                key="postgres/2026/04/2026-04-09T05-00-00Z.dump",
                size=1_000,
                last_modified=datetime(2026, 4, 9, 5, 0, tzinfo=timezone.utc),
            )
        ]
    )

    result = run_backup_once(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=tmp_path / "scratch",
        daily=1,
        weekly=0,
        monthly=0,
        weekly_dow=6,
        pg_dump_timeout_s=60,
        now=datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc),
    )
    # Upload succeeded.
    assert result.uploaded_key == "postgres/2026/04/2026-04-11T05-00-00Z.dump"
    # Retention sweep failed → deleted_keys empty even though we tried.
    assert result.deleted_keys == []


def test_run_backup_once_creates_scratch_dir(stub_pg_dump, tmp_path):
    nonexistent = tmp_path / "deep" / "nested" / "scratch"
    assert not nonexistent.exists()
    r2 = FakeR2Client()
    run_backup_once(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=nonexistent,
        daily=1,
        weekly=0,
        monthly=0,
        weekly_dow=6,
        pg_dump_timeout_s=60,
        now=datetime(2026, 4, 11, 5, 0, tzinfo=timezone.utc),
    )
    assert nonexistent.exists()


# --------------------------------------------------------------------------- #
# make_backup_job                                                              #
# --------------------------------------------------------------------------- #


def test_make_backup_job_returns_awaitable_and_runs(stub_pg_dump, tmp_path):
    r2 = FakeR2Client()
    coro_factory = make_backup_job(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=str(tmp_path / "scratch"),
        daily=7,
        weekly=4,
        monthly=3,
        weekly_dow=6,
        pg_dump_timeout_s=60,
    )
    # Call the factory — produces a coroutine.
    coro = coro_factory()
    asyncio.get_event_loop().run_until_complete(coro)
    # And it actually called r2.
    assert len(r2.put_calls) == 1


def test_make_backup_job_swallows_exceptions(monkeypatch, tmp_path):
    """The async job wrapper must NOT raise — APScheduler should keep
    firing even after a failed run. Exceptions go to the logs only."""

    def failing_dump(*, dsn, output_path, timeout_s, pg_dump_binary=None):
        raise PgDumpError("ka-boom")

    monkeypatch.setattr(job_module, "run_pg_dump", failing_dump)

    r2 = FakeR2Client()
    coro_factory = make_backup_job(
        dsn="postgresql://u:p@h/d",
        r2_client=r2,
        prefix="postgres/",
        scratch_dir=str(tmp_path / "scratch"),
        daily=1,
        weekly=0,
        monthly=0,
        weekly_dow=6,
        pg_dump_timeout_s=60,
    )
    # Should NOT raise.
    asyncio.get_event_loop().run_until_complete(coro_factory())
    assert r2.put_calls == []


def test_backup_result_dataclass():
    res = BackupResult(
        uploaded_key="k",
        uploaded_bytes=100,
        deleted_keys=["a", "b"],
        kept_keys=["k"],
    )
    assert res.skipped is False
    assert res.skipped_reason == ""
