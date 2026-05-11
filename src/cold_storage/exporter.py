"""Nightly Parquet exporter for the hot-tier tables.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.6.

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

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class ExportResult:
    """Per-table result returned by ``ColdExporter.run_nightly``.

    Fields:
        table: Source Postgres table name.
        target_path: Final Parquet file path.
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


class ColdExporter:
    """Nightly export of yesterday's hot+warm data to Parquet.

    Tables exported (from settings.COLD_EXPORT_TABLES):
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
            base_path: Root of the cold tree. None => read
                ``settings.COLD_EXPORT_BASE_PATH``.
            tables: Override the canonical table list. None => read
                ``settings.COLD_EXPORT_TABLES``.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.6
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    async def export_table(self, table: str, day: date) -> Path:
        """Export one day's worth of rows for one table.

        Algorithm (Wave 2):
          1. Build a parameterized SELECT bounded by the day:
             ``WHERE time >= $1 AND time < $2`` (or the table's
             canonical time column — different per table).
          2. Stream the result via asyncpg's ``cursor`` (avoid loading
             the whole partition into memory — at 10x scale that's
             multi-million rows).
          3. Write a Parquet file via ``pyarrow`` or ``polars``
             (lib choice deferred to Wave 2). Use ZSTD compression.
          4. Atomic rename: write to ``part-00000.parquet.tmp``, then
             ``os.replace`` to ``part-00000.parquet`` so a partial
             file never appears in the tree.

        Emits:
          * ``polybot_cold_export_rows_total{table}``
          * ``polybot_cold_export_bytes_total``
          * ``polybot_cold_export_duration_seconds{table}``

        Args:
            table: Source Postgres table.
            day: The date partition (yesterday in the common case).

        Returns:
            Path to the written Parquet file.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    async def run_nightly(self) -> dict[str, ExportResult]:
        """Export every table in settings.COLD_EXPORT_TABLES for
        yesterday's date.

        Per-table errors are caught and recorded on the ExportResult —
        one failed table does not abort the run. The caller (an
        APScheduler job) logs the dict and emits an alert if any
        ``error`` is non-None.

        Returns:
            Mapping ``{table_name: ExportResult}`` in iteration order.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    async def prune_older_than(self, retention_days: int) -> int:
        """Delete cold files older than the retention window.

        Drives the ``settings.COLD_RETENTION_DAYS`` policy. The default
        of 0 (set in Wave 2) means "keep everything"; cold storage is
        cheap enough that we lean conservative.

        Returns:
            Count of files deleted.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")
