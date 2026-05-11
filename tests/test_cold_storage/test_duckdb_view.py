"""Unit tests for ``src.cold_storage.duckdb_view``.

Writes a synthetic Hive-partitioned Parquet tree under ``tmp_path`` and
verifies that ``DuckDBResearchView`` exposes it as queryable views.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.cold_storage.duckdb_view import DuckDBResearchView


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _write_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="snappy")


@pytest.fixture
def cold_tree(tmp_path: Path) -> Path:
    """Build a small two-day cold tree for trades_observed + decision_log."""
    base = tmp_path / "cold"
    _write_parquet(
        base / "trades_observed" / "year=2026" / "month=05" / "day=10" / "part-00000.parquet",
        [
            {"wallet_address": "0xA", "size_usdc": 100.0},
            {"wallet_address": "0xB", "size_usdc": 50.0},
        ],
    )
    _write_parquet(
        base / "trades_observed" / "year=2026" / "month=05" / "day=11" / "part-00000.parquet",
        [
            {"wallet_address": "0xA", "size_usdc": 200.0},
        ],
    )
    _write_parquet(
        base / "decision_log" / "year=2026" / "month=05" / "day=10" / "part-00000.parquet",
        [{"action": "follow"}, {"action": "fade"}],
    )
    return base


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestDuckDBResearchView:
    def test_connect_close_cycle(self, tmp_path: Path):
        view = DuckDBResearchView(cold_base_path=tmp_path)
        view.connect()
        assert view._con is not None
        view.close()
        assert view._con is None
        # Idempotent close.
        view.close()

    def test_register_views_returns_file_counts(self, cold_tree: Path):
        view = DuckDBResearchView(cold_base_path=cold_tree)
        view.connect()
        try:
            counts = view.register_all_views()
        finally:
            view.close()

        assert counts["trades_observed"] == 2
        assert counts["decision_log"] == 1
        # Tables with no Parquet files report 0.
        assert counts["book_quality_snapshots"] == 0

    def test_register_all_tables_alias(self, cold_tree: Path):
        view = DuckDBResearchView(cold_base_path=cold_tree)
        try:
            counts = view.register_all_tables()
        finally:
            view.close()
        assert counts["trades_observed"] == 2

    def test_query_returns_results(self, cold_tree: Path):
        view = DuckDBResearchView(cold_base_path=cold_tree)
        try:
            view.register_all_views()
            relation = view.query(
                "SELECT wallet_address, SUM(size_usdc) AS total "
                "FROM trades_observed GROUP BY wallet_address ORDER BY wallet_address"
            )
            rows = relation.fetchall()
        finally:
            view.close()

        # 0xA: 100 + 200 = 300, 0xB: 50
        assert rows == [("0xA", 300.0), ("0xB", 50.0)]

    def test_hive_partition_columns_visible(self, cold_tree: Path):
        """Hive partition keys (year/month/day) are exposed as columns."""
        view = DuckDBResearchView(cold_base_path=cold_tree)
        try:
            view.register_all_views()
            rows = view.query(
                "SELECT DISTINCT day FROM trades_observed ORDER BY day"
            ).fetchall()
        finally:
            view.close()
        days = [r[0] for r in rows]
        # DuckDB exposes the partition value as the same type the directory
        # name suggests; for `day=10` we expect 10 (int) or "10" (str).
        assert sorted(str(d) for d in days) == ["10", "11"]

    def test_empty_tree_returns_zero_counts_and_skips_registration(self, tmp_path: Path):
        """An empty cold tier yields zero counts everywhere — queries against
        unregistered tables raise (correct fail-loud behaviour)."""
        view = DuckDBResearchView(cold_base_path=tmp_path / "empty")
        try:
            counts = view.register_all_views()
        finally:
            view.close()
        assert all(c == 0 for c in counts.values())

    def test_context_manager(self, cold_tree: Path):
        """Context manager opens and closes the connection."""
        with DuckDBResearchView(cold_base_path=cold_tree) as view:
            assert view._con is not None
            view.register_all_views()
            n = view.query("SELECT COUNT(*) FROM trades_observed").fetchone()[0]
            assert n == 3
        assert view._con is None
