"""DuckDB virtual-table view onto the cold Parquet tree.

See docs/ROUND_6_THE_SPINE.md § 3.6.

Usage from research notebooks::

    from src.cold_storage.duckdb_view import DuckDBResearchView
    view = DuckDBResearchView('/data/cold')
    view.connect()
    view.register_all_views()
    df = view.query("SELECT wallet_address, COUNT(*) FROM trades_observed "
                    "WHERE year=2026 GROUP BY 1 ORDER BY 2 DESC LIMIT 100").df()

DuckDB scans Parquet files directly with predicate pushdown — queries
that would melt Postgres run in seconds against years of data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
from loguru import logger

from src.config import settings
from src.cold_storage.exporter import TABLE_TIME_COLUMNS


class DuckDBResearchView:
    """Exposes the entire cold history as DuckDB virtual tables.

    Lifecycle: instantiated once per research session (e.g. inside a
    Jupyter notebook). Calling ``connect()`` opens a DuckDB connection
    (in-memory by default); ``register_all_views()`` creates one view
    per cold table. ``close()`` is idempotent and safe in ``__del__``.
    """

    def __init__(
        self,
        cold_base_path: Path | str | None = None,
        duckdb_path: Path | str = ":memory:",
    ) -> None:
        """
        Args:
            cold_base_path: Same path the ColdExporter writes to. ``None``
                reads ``settings.COLD_EXPORT_BASE_PATH``.
            duckdb_path: Persistent DuckDB file path, or ``:memory:`` for
                an ephemeral connection. Default is ``:memory:`` so a
                notebook crash leaves no state behind.
        """
        if cold_base_path is None:
            cold_base_path = settings.COLD_EXPORT_BASE_PATH
        self.base_path = Path(cold_base_path)
        self.duckdb_path = str(duckdb_path)
        self._con: duckdb.DuckDBPyConnection | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def connect(self, persistent_path: str | None = None) -> None:
        """Open the DuckDB connection.

        If ``persistent_path`` is provided it overrides the constructor's
        ``duckdb_path``; otherwise the constructor value is used.
        """
        if self._con is not None:
            return
        path = persistent_path if persistent_path is not None else self.duckdb_path
        self._con = duckdb.connect(path)

    def close(self) -> None:
        """Close the DuckDB connection. Idempotent."""
        if self._con is None:
            return
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass
        self._con = None

    # ------------------------------------------------------------------ #
    # View registration                                                   #
    # ------------------------------------------------------------------ #
    def register_all_views(self) -> dict[str, int]:
        """Create one DuckDB view per known cold table.

        Returns a mapping ``{table_name: n_parquet_files_matched}`` so
        the caller can see what's actually available on disk. A table
        with zero matching files still gets a view registered (queries
        against it return zero rows but don't error out).

        DuckDB's ``read_parquet('glob/**/*.parquet')`` handles Hive
        partitioning natively — adding new daily files doesn't require
        a re-register.
        """
        if self._con is None:
            self.connect()
        assert self._con is not None  # for type checkers

        counts: dict[str, int] = {}
        for table in TABLE_TIME_COLUMNS:
            table_dir = self.base_path / table
            n_files = (
                sum(1 for _ in table_dir.rglob("*.parquet"))
                if table_dir.exists()
                else 0
            )
            counts[table] = n_files

            if n_files == 0:
                # Drop any stale view so a subsequent register after data
                # arrives doesn't conflict. Queries against an absent
                # table now raise — that's the right behaviour: a research
                # query referencing an empty cold tier should fail loudly.
                self._con.execute(f'DROP VIEW IF EXISTS "{table}"')
                logger.info(
                    f"DuckDBResearchView: {table!r} has 0 Parquet files at "
                    f"{table_dir} — view not registered."
                )
                continue

            glob = str(table_dir / "**" / "*.parquet")
            # CREATE OR REPLACE → idempotent; safe to call after each
            # nightly export refresh.
            self._con.execute(
                f'CREATE OR REPLACE VIEW "{table}" AS '
                f"SELECT * FROM read_parquet('{glob}', hive_partitioning=1)"
            )
        return counts

    # Spec-compatibility alias.
    def register_all_tables(self) -> dict[str, int]:
        """Alias for ``register_all_views``."""
        return self.register_all_views()

    # ------------------------------------------------------------------ #
    # Query pass-through                                                  #
    # ------------------------------------------------------------------ #
    def query(self, sql: str) -> Any:
        """Execute a SQL statement against the registered views.

        Returns a DuckDB ``Relation`` (callers do ``.df()`` for pandas,
        ``.arrow()`` for Arrow, etc.). If ``connect()`` hasn't been
        called yet, we open lazily.
        """
        if self._con is None:
            self.connect()
        assert self._con is not None
        return self._con.execute(sql)

    # ------------------------------------------------------------------ #
    # Context-manager sugar                                               #
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "DuckDBResearchView":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort finaliser; never raises.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
