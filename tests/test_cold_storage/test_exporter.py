"""Unit tests for ``src.cold_storage.exporter``.

All Postgres I/O is mocked via patching ``src.cold_storage.exporter.get_db``.
Parquet files are written to a ``tmp_path`` and read back with
``pyarrow.parquet`` to verify the round-trip.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pyarrow.parquet as pq
import pytest

from src.cold_storage.exporter import (
    TABLE_TIME_COLUMNS,
    ColdExporter,
    ExportResult,
    _partition_dir,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _mock_get_db(fetch_return: list[dict]):
    """Patch ``src.cold_storage.exporter.get_db`` with a fake connection
    whose ``fetch`` returns ``fetch_return``."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[dict(r) for r in fetch_return])

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.cold_storage.exporter.get_db", side_effect=_ctx), conn


def _sample_trade(idx: int) -> dict:
    """A row that mirrors trades_observed."""
    return {
        "id": idx,
        "time": datetime(2026, 5, 10, 12, 0, idx, tzinfo=timezone.utc),
        "market_id": f"m{idx}",
        "token_id": f"t{idx}",
        "wallet_address": "0xwallet",
        "side": "buy",
        "price": Decimal("0.5500"),
        "size_usdc": Decimal("123.45"),
        "source": "websocket",
        "is_leader": True,
    }


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestPartitionPath:
    def test_hive_partition_layout(self, tmp_path: Path):
        """Output path uses Hive-style year=/month=/day= partitioning."""
        p = _partition_dir(tmp_path, "trades_observed", date(2026, 5, 10))
        assert p == tmp_path / "trades_observed" / "year=2026" / "month=05" / "day=10"


class TestColdExporter:
    @pytest.mark.asyncio
    async def test_export_table_writes_parquet_with_schema(self, tmp_path: Path):
        """export_table writes a Parquet file readable by pyarrow."""
        records = [_sample_trade(i) for i in range(3)]
        patcher, _conn = _mock_get_db(records)

        with patcher:
            exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
            result = await exporter.export_table("trades_observed", date(2026, 5, 10))

        assert result.error is None
        assert result.rows_exported == 3
        assert result.bytes_written > 0
        assert result.target_path.exists()
        assert result.target_path.name == "part-00000.parquet"

        # Round-trip through pyarrow.
        table = pq.read_table(result.target_path)
        assert table.num_rows == 3
        col_names = set(table.schema.names)
        assert "market_id" in col_names
        assert "wallet_address" in col_names
        assert "time" in col_names

    @pytest.mark.asyncio
    async def test_export_table_empty_writes_empty_parquet(self, tmp_path: Path):
        """An empty result still writes a (zero-row) Parquet file."""
        patcher, _conn = _mock_get_db([])

        with patcher:
            exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
            result = await exporter.export_table("trades_observed", date(2026, 5, 10))

        assert result.error is None
        assert result.rows_exported == 0
        assert result.target_path.exists(), "empty day must still produce a file"
        table = pq.read_table(result.target_path)
        assert table.num_rows == 0

    @pytest.mark.asyncio
    async def test_export_yesterday_iterates_all_tables(self, tmp_path: Path):
        """run_nightly visits every configured table."""
        records = [_sample_trade(0)]
        patcher, _conn = _mock_get_db(records)

        with patcher:
            exporter = ColdExporter(
                base_path=tmp_path,
                tables=["trades_observed", "decision_log"],
            )
            results = await exporter.run_nightly()

        assert set(results.keys()) == {"trades_observed", "decision_log"}
        for r in results.values():
            assert isinstance(r, ExportResult)
            assert r.error is None

    @pytest.mark.asyncio
    async def test_one_table_failure_doesnt_block_others(self, tmp_path: Path):
        """A failing table records the error on the ExportResult and the
        rest of the sweep still runs."""
        good_records = [_sample_trade(0)]

        call_count = {"n": 0}

        async def fake_fetch(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated DB outage")
            return [dict(r) for r in good_records]

        conn = AsyncMock()
        conn.fetch = fake_fetch

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch("src.cold_storage.exporter.get_db", side_effect=_ctx):
            exporter = ColdExporter(
                base_path=tmp_path,
                tables=["trades_observed", "decision_log"],
            )
            results = await exporter.run_nightly()

        assert results["trades_observed"].error is not None
        assert isinstance(results["trades_observed"].error, RuntimeError)
        assert results["decision_log"].error is None
        assert results["decision_log"].rows_exported == 1

    @pytest.mark.asyncio
    async def test_export_range_backfills_multiple_days(self, tmp_path: Path):
        """export_range covers ``[start, end]`` inclusive."""
        records = [_sample_trade(0)]
        patcher, _conn = _mock_get_db(records)

        with patcher:
            exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
            out = await exporter.export_range(
                "trades_observed", date(2026, 5, 1), date(2026, 5, 3)
            )

        assert len(out) == 3
        days = {r.target_path.parent.name for r in out}
        assert days == {"day=01", "day=02", "day=03"}

    @pytest.mark.asyncio
    async def test_export_range_rejects_inverted_range(self, tmp_path: Path):
        exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
        with pytest.raises(ValueError):
            await exporter.export_range(
                "trades_observed", date(2026, 5, 10), date(2026, 5, 1)
            )

    @pytest.mark.asyncio
    async def test_cleanup_old_exports_respects_retention(self, tmp_path: Path):
        """Files older than retention_days are removed, fresher ones kept."""
        import os
        import time

        old_dir = tmp_path / "trades_observed" / "year=2026" / "month=01" / "day=01"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "part-00000.parquet"
        old_file.write_bytes(b"dummy")
        # Set mtime to 30 days ago.
        past = time.time() - (30 * 86400)
        os.utime(old_file, (past, past))

        new_dir = tmp_path / "trades_observed" / "year=2026" / "month=05" / "day=10"
        new_dir.mkdir(parents=True)
        new_file = new_dir / "part-00000.parquet"
        new_file.write_bytes(b"dummy")

        exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
        removed = await exporter.cleanup_old_exports(retention_days=7)

        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    @pytest.mark.asyncio
    async def test_cleanup_zero_retention_keeps_everything(self, tmp_path: Path):
        """retention_days <= 0 means keep everything (default policy)."""
        d = tmp_path / "trades_observed" / "year=2026" / "month=01" / "day=01"
        d.mkdir(parents=True)
        f = d / "part-00000.parquet"
        f.write_bytes(b"dummy")

        exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
        removed = await exporter.cleanup_old_exports(retention_days=0)

        assert removed == 0
        assert f.exists()

    @pytest.mark.asyncio
    async def test_metrics_emitted_on_success(self, tmp_path: Path):
        """Counters tick up after a successful export."""
        records = [_sample_trade(i) for i in range(2)]
        patcher, _conn = _mock_get_db(records)

        from src.monitoring import metrics as m

        before_rows = m.cold_export_rows_total.labels(table="trades_observed")._value.get()
        before_bytes = m.cold_export_bytes_total._value.get()

        with patcher:
            exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
            result = await exporter.export_table("trades_observed", date(2026, 5, 10))

        after_rows = m.cold_export_rows_total.labels(table="trades_observed")._value.get()
        after_bytes = m.cold_export_bytes_total._value.get()

        assert after_rows - before_rows == 2
        assert after_bytes - before_bytes == result.bytes_written

    def test_unknown_table_rejected_at_construction(self):
        """A table missing from TABLE_TIME_COLUMNS is rejected upfront."""
        with pytest.raises(ValueError, match="TABLE_TIME_COLUMNS"):
            ColdExporter(tables=["not_a_real_table"])

    def test_table_time_columns_contains_canonical_set(self):
        """Sanity check on the canonical map (guards against accidental drift)."""
        canonical = {
            "trades_observed",
            "book_quality_snapshots",
            "orderbook_features_minute",
            "decision_log",
            "positions_reconstructed",
            "paper_trades",
        }
        assert canonical.issubset(TABLE_TIME_COLUMNS.keys())

    @pytest.mark.asyncio
    async def test_export_table_passes_correct_time_window(self, tmp_path: Path):
        """The SELECT must bind day's UTC midnight and the next day's midnight."""
        captured: dict = {}

        async def fake_fetch(sql, start, end):
            captured["sql"] = sql
            captured["start"] = start
            captured["end"] = end
            return []

        conn = AsyncMock()
        conn.fetch = fake_fetch

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch("src.cold_storage.exporter.get_db", side_effect=_ctx):
            exporter = ColdExporter(base_path=tmp_path, tables=["trades_observed"])
            await exporter.export_table("trades_observed", date(2026, 5, 10))

        assert captured["start"] == datetime(2026, 5, 10, tzinfo=timezone.utc)
        assert captured["end"] == datetime(2026, 5, 11, tzinfo=timezone.utc)
        assert "trades_observed" in captured["sql"]
        # time column for trades_observed is `time`
        assert " time " in captured["sql"] or '"time"' in captured["sql"]
