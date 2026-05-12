"""Per-bucket rollup writer for :mod:`microstructure_features` —
Round 11 § 3.2.

Consumes the :meth:`MicrostructureFeatureDeriver.flush_bucket` snapshot
and writes ONE row per (market_id, token_id) into the
``microstructure_features`` table. Idempotent via the (market_id,
token_id, bucket_ts) primary key + ``ON CONFLICT DO UPDATE`` — a stale
re-flush never inflates the row count.

The default bucket size is ``settings.MICROSTRUCTURE_ROLLUP_BUCKET_S``
(60 s). The bucket boundary alignment is the responsibility of the
daemon — this module only writes whatever bucket_ts it's told.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.microstructure.derivers import (
    IcebergBucket,
    OFIBucket,
    SpoofBucket,
)

try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        microstructure_features_emitted_total,
    )
except Exception:  # pragma: no cover

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

    microstructure_features_emitted_total = _NoOpLabel()  # type: ignore[assignment]


class MicrostructureRollup:
    """Writes per-bucket rollup rows to ``microstructure_features``.

    Construction takes an optional ``bucket_s`` override; the daemon
    passes the configured value so an operator can shorten/lengthen the
    bucket without touching code.
    """

    def __init__(self, *, bucket_s: int | None = None) -> None:
        self.bucket_s = int(
            bucket_s if bucket_s is not None else settings.MICROSTRUCTURE_ROLLUP_BUCKET_S
        )

    async def flush(
        self,
        bucket_ts: datetime,
        snapshot: dict,
        *,
        conn: Any | None = None,
    ) -> int:
        """Write the snapshot to ``microstructure_features``.

        ``snapshot`` is the output of
        :meth:`MicrostructureFeatureDeriver.flush_bucket` — a dict with
        ``iceberg``, ``spoof``, and ``ofi`` sub-dicts keyed by
        (market_id, token_id).

        Returns the number of rows written.
        """
        if bucket_ts.tzinfo is None:
            bucket_ts = bucket_ts.replace(tzinfo=timezone.utc)
        # Union of (market, token) keys across all three detectors so
        # we emit one row even when only one detector fired.
        keys: set[tuple[str, str]] = set()
        keys.update(snapshot.get("iceberg", {}).keys())
        keys.update(snapshot.get("spoof", {}).keys())
        keys.update(snapshot.get("ofi", {}).keys())
        if not keys:
            return 0

        rows = []
        for key in keys:
            iceberg: IcebergBucket = snapshot.get("iceberg", {}).get(
                key, IcebergBucket()
            )
            spoof: SpoofBucket = snapshot.get("spoof", {}).get(
                key, SpoofBucket()
            )
            ofi: OFIBucket | None = snapshot.get("ofi", {}).get(key)
            ofi_summary = ofi.summary() if ofi is not None else None
            ofi_mean, ofi_max, ofi_min, ofi_std = (
                ofi_summary if ofi_summary is not None else (None, None, None, None)
            )
            rows.append(
                (
                    key[0],  # market_id
                    key[1],  # token_id
                    bucket_ts,
                    int(iceberg.count) or None,
                    float(iceberg.total_size) if iceberg.total_size else None,
                    int(spoof.count) or None,
                    float(spoof.total_size) if spoof.total_size else None,
                    ofi_mean,
                    ofi_max,
                    ofi_min,
                    ofi_std,
                )
            )

        sql = """
            INSERT INTO microstructure_features
                (market_id, token_id, bucket_ts,
                 iceberg_orders_count, iceberg_total_size,
                 spoof_orders_count, spoof_total_size,
                 ofi_mean, ofi_max, ofi_min, ofi_std)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (market_id, token_id, bucket_ts) DO UPDATE
                SET iceberg_orders_count = EXCLUDED.iceberg_orders_count,
                    iceberg_total_size   = EXCLUDED.iceberg_total_size,
                    spoof_orders_count   = EXCLUDED.spoof_orders_count,
                    spoof_total_size     = EXCLUDED.spoof_total_size,
                    ofi_mean             = EXCLUDED.ofi_mean,
                    ofi_max              = EXCLUDED.ofi_max,
                    ofi_min              = EXCLUDED.ofi_min,
                    ofi_std              = EXCLUDED.ofi_std
        """
        try:
            if conn is not None:
                await conn.executemany(sql, rows)
            else:
                async with get_db() as conn_:
                    await conn_.executemany(sql, rows)
        except Exception as exc:
            logger.warning(
                f"MicrostructureRollup flush failed (n={len(rows)}): {exc}"
            )
            return 0

        try:
            microstructure_features_emitted_total.inc(len(rows))
        except Exception:  # pragma: no cover
            pass
        return len(rows)
