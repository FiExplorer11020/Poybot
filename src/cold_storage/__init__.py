"""Tiered cold storage with Parquet archival (Round 6 / The Spine § 3.6).

Module shape:
  * :mod:`src.cold_storage.exporter`   — nightly Postgres-partition →
    Parquet exporter.
  * :mod:`src.cold_storage.duckdb_view` — DuckDB virtual-table view
    exposing the full cold history for research notebooks.

The hot tier (Postgres partitioned tables) stays as-is; cold is a
write-once Parquet tree on local disk, optionally synced to a Hetzner
Storage Box for off-host durability.
"""
