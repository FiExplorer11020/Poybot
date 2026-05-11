"""DuckDB virtual-table view onto the cold Parquet tree.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.6.

Usage from research notebooks::

    import duckdb
    con = duckdb.connect('research.duckdb')
    con.execute('CREATE VIEW trades AS SELECT * FROM '
                '"/data/cold/trades_observed/**/*.parquet"')
    df = con.execute('SELECT wallet_address, COUNT(*) FROM trades '
                     'WHERE year=2026 GROUP BY 1 ORDER BY 2 DESC '
                     'LIMIT 100').df()

No need to load into pandas. DuckDB scans the Parquet files directly
with predicate pushdown. Queries that would melt Postgres run in
seconds against years of data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class DuckDBResearchView:
    """Exposes the entire cold history as DuckDB virtual tables.

    Lifecycle: instantiated once per research session (e.g. inside a
    Jupyter notebook). Wave 2 may persist the DuckDB database file
    across sessions so the view definitions don't have to be recreated
    on every notebook boot.
    """

    def __init__(
        self,
        cold_base_path: Path | str,
        duckdb_path: Path | str = ":memory:",
    ) -> None:
        """
        Args:
            cold_base_path: Same path the ColdExporter writes to. Wave
                2 reads ``settings.COLD_EXPORT_BASE_PATH`` if a caller
                doesn't supply one.
            duckdb_path: Persistent DuckDB file path, or ``:memory:``
                for an ephemeral connection. Default is ``:memory:``
                so a notebook crash leaves no state behind.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.6
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    def register_all_views(self) -> list[str]:
        """Create one DuckDB view per cold table.

        Naming: the view name matches the source table name minus the
        ``polymarket_`` prefix where present (none today; placeholder
        for future tables). The view definition is::

            CREATE VIEW <table> AS
            SELECT * FROM '<base>/<table>/**/*.parquet';

        DuckDB resolves the glob lazily — adding new daily files
        doesn't require a view re-register.

        Returns:
            List of view names registered. Wave 2 uses this for the
            "what's available?" notebook-side helper.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    def query(self, sql: str) -> Any:
        """Execute a SQL query, returning whatever DuckDB returns.

        Thin pass-through; included so notebooks don't have to grab
        the underlying ``con`` attribute. Default return is a DuckDB
        ``Relation``; callers do ``.df()`` for pandas, ``.arrow()``
        for Arrow, etc.

        Args:
            sql: A SQL statement against the registered views.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    def close(self) -> None:
        """Close the DuckDB connection.

        Wave 2: idempotent; safe to call from a notebook cleanup cell
        or a ``__del__`` finaliser.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.6")

    def __enter__(self) -> "DuckDBResearchView":
        """Sync context-manager sugar for notebook usage."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
