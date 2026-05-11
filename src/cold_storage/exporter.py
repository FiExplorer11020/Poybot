"""Nightly Parquet exporter for the hot-tier tables.

See docs/ROUND_6_THE_SPINE.md § 3.6.

Output structure (local disk on box-1, optionally synced to a Hetzner
Storage Box afterwards)::

    /data/cold/
      ├── trades_observed/
      │     └── year=2026/month=05/day=11/part-00000.parquet
      ├── orderbook_features_minute/
      │     └── year=2026/month=05/day=11/part-00000.parquet
      ...

Hive-style partitioning so DuckDB's predicate pushdown can skip whole
days during scans (see ``src.cold_storage.duckdb_view``).
"""

from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.monitoring import metrics as _metrics

# --------------------------------------------------------------------------- #
# Canonical (table → time column) map.                                        #
# --------------------------------------------------------------------------- #
# Edit here whenever a new table is added to ``settings.COLD_EXPORT_TABLES``. #
# Anything in ``COLD_EXPORT_TABLES`` not present here is rejected at runtime  #
# (we refuse to guess — wrong column choice silently exports zero rows).     #
TABLE_TIME_COLUMNS: dict[str, str] = {
    "trades_observed": "time",
    "book_quality_snapshots": "observed_at",
    "orderbook_features_minute": "bucket_ts",
    "decision_log": "time",
    "positions_reconstructed": "open_time",
    "paper_trades": "opened_at",
}


@dataclass
class ExportResult:
    """Per-table result returned by ``ColdExporter.run_nightly``.

    Fields:
        table: Source Postgres table name.
        target_path: Final Parquet file path (or the expected one when
            the export errored before writing).
        rows_exported: Count of rows written to Parquet.
        bytes_written: File size after compression.
        duration_s: Wall time of this single-table export.
        error: Exception caught during export (None on success). One
            table's failure must not block the others — the nightly
            cron logs the error and proceeds.
    """

    table: str
    target_path: Path
    rows_exported: int
    bytes_written: int
    duration_s: float
    error: Exception | None = None


def _parse_table_list(raw: str | list[str] | None) -> list[str]:
    """Accept either a comma-separated string (Settings) or a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [t.strip() for t in raw if t and t.strip()]
    return [t.strip() for t in raw.split(",") if t and t.strip()]


def _partition_dir(base: Path, table: str, day: date) -> Path:
    """Return the Hive-partitioned directory for ``(table, day)``."""
    return (
        Path(base)
        / table
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
    )


def _python_value_to_parquet(v: Any) -> Any:
    """Normalise asyncpg-returned types for pyarrow.

    pyarrow handles ``Decimal``, ``datetime``, ``bool``, ``int``, ``float``,
    ``str``, ``bytes``, ``dict`` (JSON), ``list`` and ``None`` natively. We
    only need to coerce edge cases — ``asyncpg.Range`` and ``UUID`` come
    through as objects pyarrow refuses; cast them to strings so the export
    survives an unexpected column type.
    """
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str, bytes, Decimal, datetime)):
        return v
    if isinstance(v, (list, tuple)):
        return [_python_value_to_parquet(x) for x in v]
    if isinstance(v, dict):
        return v
    # Catch-all: stringify exotic asyncpg types (UUID, Range, etc.).
    return str(v)


def _records_to_arrow_table(records: list[dict[str, Any]]) -> pa.Table:
    """Convert a list of dict rows into a pyarrow Table.

    pyarrow infers a sensible schema from the Python values, including
    Decimal128 for ``Decimal`` and timestamp[us, tz=UTC] for aware
    datetimes. Empty input → empty Table with no schema (caller wraps
    that case to write a zero-row Parquet against an inferred schema).
    """
    return pa.Table.from_pylist(records)


class ColdExporter:
    """Nightly export of yesterday's hot+warm data to Parquet.

    Tables exported (from ``settings.COLD_EXPORT_TABLES``):
      * trades_observed (yesterday's partition)
      * book_quality_snapshots
      * orderbook_features_minute
      * decision_log
      * positions_reconstructed

    Runs from the engine's APScheduler at ~04:30 UTC (after the 03:00
    nightly batch and 04:00 redis cleanup).
    """

    def __init__(
        self,
        base_path: Path | str | None = None,
        tables: list[str] | None = None,
    ) -> None:
        """
        Args:
            base_path: Root of the cold tree. ``None`` => read
                ``settings.COLD_EXPORT_BASE_PATH``.
            tables: Override the canonical table list. ``None`` => read
                ``settings.COLD_EXPORT_TABLES``.
        """
        self.base_path = Path(base_path) if base_path is not None else Path(
            settings.COLD_EXPORT_BASE_PATH
        )
        if tables is None:
            self.tables = _parse_table_list(settings.COLD_EXPORT_TABLES)
        else:
            self.tables = list(tables)
        unknown = [t for t in self.tables if t not in TABLE_TIME_COLUMNS]
        if unknown:
            # Refuse to start with mis-configured tables — wrong time column
            # would silently export zero rows.
            raise ValueError(
                f"ColdExporter: tables {unknown!r} not in TABLE_TIME_COLUMNS map. "
                "Edit src/cold_storage/exporter.py TABLE_TIME_COLUMNS to add them."
            )

    # --------------------------------------------------------------- #
    # Single-day, single-table export                                  #
    # --------------------------------------------------------------- #
    async def export_table(self, table: str, day: date) -> ExportResult:
        """Export one day's worth of rows for one table.

        Algorithm:
          1. Bounded SELECT ``WHERE <time_col> >= day AND <time_col> < day+1d``.
          2. Build a pyarrow Table, write a single Parquet file (snappy).
          3. Atomic rename: ``.tmp`` -> ``part-00000.parquet``.
          4. Emit metrics.
        """
        if table not in TABLE_TIME_COLUMNS:
            raise ValueError(
                f"Unknown cold-export table {table!r} — add it to TABLE_TIME_COLUMNS."
            )
        time_col = TABLE_TIME_COLUMNS[table]
        t0 = _time.time()

        # Bound the day as an aware-UTC datetime range so the index hits.
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)

        target_dir = _partition_dir(self.base_path, table, day)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "part-00000.parquet"
        tmp_path = target_dir / "part-00000.parquet.tmp"

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    f"SELECT * FROM {table} "
                    f"WHERE {time_col} >= $1 AND {time_col} < $2 "
                    f"ORDER BY {time_col}",
                    start,
                    end,
                )

            # asyncpg.Record → dict, normalising exotic types.
            records: list[dict[str, Any]] = []
            for row in rows:
                records.append(
                    {k: _python_value_to_parquet(v) for k, v in dict(row).items()}
                )

            if records:
                arrow_tbl = _records_to_arrow_table(records)
            else:
                # Empty day: write a zero-row Parquet so DuckDB still sees a
                # file (a missing day looks the same as a never-exported day
                # otherwise — the operator can't tell apart). An empty Table
                # with no schema would fail pq.write_table, so we use a
                # nominal placeholder column.
                arrow_tbl = pa.table({"_empty": pa.array([], type=pa.null())})

            pq.write_table(arrow_tbl, tmp_path, compression="snappy")
            os.replace(tmp_path, target_path)

            bytes_written = target_path.stat().st_size
            rows_exported = len(records)
            duration = _time.time() - t0

            # Metrics
            try:
                _metrics.cold_export_rows_total.labels(table=table).inc(rows_exported)
                _metrics.cold_export_bytes_total.inc(bytes_written)
                _metrics.cold_export_duration_seconds.labels(table=table).observe(duration)
            except Exception:  # noqa: BLE001 — metrics must never break export
                pass

            logger.info(
                f"cold_export[{table}]: {rows_exported} rows / {bytes_written} bytes "
                f"-> {target_path} ({duration:.2f}s)"
            )
            return ExportResult(
                table=table,
                target_path=target_path,
                rows_exported=rows_exported,
                bytes_written=bytes_written,
                duration_s=duration,
                error=None,
            )
        except Exception as e:  # noqa: BLE001 — per-table isolation
            # Best-effort cleanup of the half-written tmp file.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:  # noqa: BLE001
                pass
            duration = _time.time() - t0
            logger.exception(f"cold_export[{table}]: failed after {duration:.2f}s — {e}")
            return ExportResult(
                table=table,
                target_path=target_path,
                rows_exported=0,
                bytes_written=0,
                duration_s=duration,
                error=e,
            )

    # --------------------------------------------------------------- #
    # Nightly entry point                                              #
    # --------------------------------------------------------------- #
    async def run_nightly(self) -> dict[str, ExportResult]:
        """Export every configured table for yesterday's date.

        Per-table errors are caught and recorded on the ExportResult —
        one failed table does not abort the run. Returns a mapping
        ``{table_name: ExportResult}`` in iteration order.
        """
        return await self.export_yesterday()

    async def export_yesterday(self) -> dict[str, ExportResult]:
        """Alias for ``run_nightly`` — kept for spec parity."""
        yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date()
        results: dict[str, ExportResult] = {}
        for table in self.tables:
            results[table] = await self.export_table(table, yesterday)
        return results

    # --------------------------------------------------------------- #
    # Backfill helper                                                  #
    # --------------------------------------------------------------- #
    async def export_range(
        self,
        table: str,
        start: date,
        end: date,
    ) -> list[ExportResult]:
        """Export ``[start, end]`` inclusive for one table.

        Used once at deploy time to seed the cold tier with historical
        data. Dates iterate forward; one failure doesn't abort the rest.
        """
        if start > end:
            raise ValueError(f"export_range: start {start} > end {end}")
        out: list[ExportResult] = []
        cursor = start
        while cursor <= end:
            out.append(await self.export_table(table, cursor))
            cursor = cursor + timedelta(days=1)
        return out

    # --------------------------------------------------------------- #
    # Retention                                                        #
    # --------------------------------------------------------------- #
    async def prune_older_than(self, retention_days: int) -> int:
        """Spec-compatible name. Delegates to ``cleanup_old_exports``."""
        return await self.cleanup_old_exports(retention_days=retention_days)

    async def cleanup_old_exports(self, retention_days: int | None = None) -> int:
        """Delete Parquet files older than ``retention_days``.

        Default reads ``settings.COLD_RETENTION_DAYS``. A retention of 0
        means "keep everything" (cold storage is cheap; default policy
        is to lean conservative).

        Returns the count of files removed (does not remove empty
        directories — DuckDB doesn't care, and the next export refills
        them).
        """
        days = retention_days if retention_days is not None else settings.COLD_RETENTION_DAYS
        if days <= 0:
            return 0
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        cutoff_epoch = cutoff.timestamp()

        removed = 0
        if not self.base_path.exists():
            return 0
        for parquet in self.base_path.rglob("*.parquet"):
            try:
                if parquet.stat().st_mtime < cutoff_epoch:
                    parquet.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning(f"cleanup_old_exports: failed to remove {parquet}: {e}")
        if removed:
            logger.info(
                f"cleanup_old_exports: removed {removed} Parquet file(s) older than "
                f"{days}d (cutoff={cutoff.isoformat()})"
            )
        return removed
