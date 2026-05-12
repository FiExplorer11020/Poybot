"""SQL-backed CRUD for the ``strategy_labels`` table.

Round 8 (The Lens) — § 3.2 of the spec.

The label store is **append-only** with full audit trail (labeller +
labelled_at + rationale). Every operator label is one row. To "fix" a
label, insert a new row with a later ``labelled_at``; the latest row
per (wallet, window) wins.

Cohen's κ on the 20-wallet validation set is implemented here too —
it's the primary inter-labeller-agreement signal the spec mandates
(target κ ≥ 0.7, gate before training).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
from loguru import logger

from src.database.connection import get_db
from src.strategy_classifier.model import STRATEGY_CLASSES


@dataclass
class LabelRow:
    """One row in ``strategy_labels``. Mirrors the migration 026 schema."""

    wallet_address: str
    label_window_start: date
    label_window_end: date
    primary_strategy: str
    labeller: str
    confidence: float = 1.0
    secondary_strategy: str | None = None
    rationale: str | None = None
    labelled_at: datetime | None = None  # defaulted server-side


class StrategyLabelStore:
    """Async CRUD over ``strategy_labels``.

    All public methods are coroutines (async per project convention).
    Reads use ``get_db()`` context manager and parameterised SQL — never
    string-interpolated, never sync.
    """

    # ------------------------------------------------------------------ #
    # Insert                                                             #
    # ------------------------------------------------------------------ #

    async def insert_label(self, row: LabelRow) -> int:
        """Append one label row. Returns the generated ``label_id``.

        Raises ``ValueError`` for invalid strategy names so a typo'd
        notebook entry fails loud rather than ending up in the DB and
        poisoning training. The DB CHECK constraint enforces this too
        but we prefer a fast Python error with the offending string in
        the message.
        """
        if row.primary_strategy not in STRATEGY_CLASSES:
            raise ValueError(
                f"primary_strategy={row.primary_strategy!r} not in "
                f"{STRATEGY_CLASSES!r}"
            )
        if (
            row.secondary_strategy is not None
            and row.secondary_strategy not in STRATEGY_CLASSES
        ):
            raise ValueError(
                f"secondary_strategy={row.secondary_strategy!r} not in "
                f"{STRATEGY_CLASSES!r}"
            )
        if not (0.0 <= row.confidence <= 1.0):
            raise ValueError(f"confidence={row.confidence} outside [0, 1]")
        if row.label_window_end < row.label_window_start:
            raise ValueError(
                "label_window_end must be >= label_window_start; "
                f"got {row.label_window_start} -> {row.label_window_end}"
            )

        async with get_db() as conn:
            label_id = await conn.fetchval(
                """
                INSERT INTO strategy_labels
                    (wallet_address, label_window_start, label_window_end,
                     primary_strategy, secondary_strategy, confidence,
                     labeller, labelled_at, rationale)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING label_id
                """,
                row.wallet_address,
                row.label_window_start,
                row.label_window_end,
                row.primary_strategy,
                row.secondary_strategy,
                float(row.confidence),
                row.labeller,
                row.labelled_at or datetime.now(tz=timezone.utc),
                row.rationale,
            )
        logger.info(
            f"strategy_labels: inserted label_id={label_id} "
            f"wallet={row.wallet_address} primary={row.primary_strategy} "
            f"labeller={row.labeller}"
        )
        return int(label_id)

    # ------------------------------------------------------------------ #
    # Read                                                               #
    # ------------------------------------------------------------------ #

    async def get_labels_for_wallet(
        self,
        wallet_address: str,
        labeller: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all label rows for one wallet, most-recent first.

        If ``labeller`` is supplied, restrict to that labeller (used by
        the κ computation).
        """
        async with get_db() as conn:
            if labeller:
                rows = await conn.fetch(
                    """
                    SELECT label_id, wallet_address, label_window_start,
                           label_window_end, primary_strategy,
                           secondary_strategy, confidence, labeller,
                           labelled_at, rationale
                    FROM strategy_labels
                    WHERE wallet_address = $1 AND labeller = $2
                    ORDER BY labelled_at DESC
                    """,
                    wallet_address,
                    labeller,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT label_id, wallet_address, label_window_start,
                           label_window_end, primary_strategy,
                           secondary_strategy, confidence, labeller,
                           labelled_at, rationale
                    FROM strategy_labels
                    WHERE wallet_address = $1
                    ORDER BY labelled_at DESC
                    """,
                    wallet_address,
                )
        return [dict(r) for r in rows]

    async def get_labelled_set_for_training(
        self,
        primary_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the latest label per (wallet, window) for use as the
        training set.

        Output rows: ``{wallet_address, label_window_start,
        label_window_end, primary_strategy, confidence, asof_ts}``.

        ``asof_ts`` is set to ``label_window_end`` at midnight UTC, which
        is the contract the feature extractor expects (per spec § 3.1).

        ``primary_only=False`` would extend to include secondary labels
        as a soft target; v1 keeps it simple — single-label
        multi-class.
        """
        async with get_db() as conn:
            # DISTINCT ON (wallet, window) ORDER BY labelled_at DESC picks
            # the latest label per cell — append-only contract honored.
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (wallet_address, label_window_start, label_window_end)
                    wallet_address, label_window_start, label_window_end,
                    primary_strategy, secondary_strategy, confidence,
                    labeller, labelled_at, rationale
                FROM strategy_labels
                ORDER BY wallet_address, label_window_start, label_window_end,
                         labelled_at DESC
                """
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            # asof_ts := end-of-window midnight UTC. Spec § 3.1.
            window_end = row["label_window_end"]
            asof_ts = datetime(
                window_end.year, window_end.month, window_end.day,
                tzinfo=timezone.utc,
            )
            row["asof_ts"] = asof_ts
            if primary_only:
                row.pop("secondary_strategy", None)
            out.append(row)
        return out

    async def label_set_size(self) -> dict[str, int]:
        """Return ``{strategy_class: count}`` over the latest label per cell.

        Used by the daemon to emit
        ``polybot_strategy_label_set_size{strategy=...}``.
        """
        rows = await self.get_labelled_set_for_training(primary_only=True)
        out: dict[str, int] = {s: 0 for s in STRATEGY_CLASSES}
        for r in rows:
            out[r["primary_strategy"]] = out.get(r["primary_strategy"], 0) + 1
        return out

    # ------------------------------------------------------------------ #
    # Inter-labeller agreement                                           #
    # ------------------------------------------------------------------ #

    async def compute_inter_labeller_kappa(
        self,
        labeller_a: str,
        labeller_b: str,
    ) -> dict[str, Any]:
        """Cohen's κ between two labellers on the shared 20-wallet
        validation set (or whatever overlap exists — works on any
        non-empty intersection).

        Returns ``{"kappa": float, "n_overlap": int, "agreement_rate":
        float, "labels": [...]}``. ``kappa`` is ``np.nan`` when n_overlap
        is too small (< 2) to compute meaningfully.

        Cohen's κ definition:

            κ = (p_o - p_e) / (1 - p_e)

        where ``p_o`` = observed agreement and ``p_e`` = expected
        agreement under independence. κ = 1 means perfect agreement,
        κ = 0 means agreement at chance, κ < 0 means worse than chance.

        Implementation note: we DON'T import scikit-learn here — the
        math is small and we want this module to import cleanly even
        in stripped environments where the heavy deps are deferred.
        """
        labels_a_rows = await self._latest_label_per_wallet(labeller_a)
        labels_b_rows = await self._latest_label_per_wallet(labeller_b)
        a_map = {r["wallet_address"]: r["primary_strategy"] for r in labels_a_rows}
        b_map = {r["wallet_address"]: r["primary_strategy"] for r in labels_b_rows}
        overlap = sorted(set(a_map.keys()) & set(b_map.keys()))
        n = len(overlap)
        if n < 2:
            return {
                "kappa": float("nan"),
                "n_overlap": n,
                "agreement_rate": float("nan"),
                "labeller_a": labeller_a,
                "labeller_b": labeller_b,
                "labels": [],
            }

        # Build contingency on STRATEGY_CLASSES ordering.
        k = len(STRATEGY_CLASSES)
        idx = {s: i for i, s in enumerate(STRATEGY_CLASSES)}
        cm = np.zeros((k, k), dtype=float)
        for wallet in overlap:
            i = idx.get(a_map[wallet])
            j = idx.get(b_map[wallet])
            if i is None or j is None:
                # Defensive: someone labelled with a string outside our taxonomy.
                # Skip the row rather than crash the κ.
                continue
            cm[i, j] += 1.0

        total = float(cm.sum())
        if total < 2.0:
            return {
                "kappa": float("nan"),
                "n_overlap": int(total),
                "agreement_rate": float("nan"),
                "labeller_a": labeller_a,
                "labeller_b": labeller_b,
                "labels": [],
            }

        p_o = float(np.trace(cm)) / total
        # Expected agreement: sum_i (row_i/total) * (col_i/total)
        row_marg = cm.sum(axis=1) / total
        col_marg = cm.sum(axis=0) / total
        p_e = float(np.sum(row_marg * col_marg))
        if abs(1.0 - p_e) < 1e-12:
            # Both labellers always picked the same class. κ is degenerate;
            # the convention is κ = 1.0 iff p_o == 1.0, else 0.0.
            kappa = 1.0 if p_o >= 0.9999 else 0.0
        else:
            kappa = (p_o - p_e) / (1.0 - p_e)

        return {
            "kappa": round(float(kappa), 4),
            "n_overlap": int(total),
            "agreement_rate": round(p_o, 4),
            "labeller_a": labeller_a,
            "labeller_b": labeller_b,
            "labels": [
                {"wallet": w, "a": a_map[w], "b": b_map[w]} for w in overlap
            ],
        }

    async def _latest_label_per_wallet(self, labeller: str) -> list[dict[str, Any]]:
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (wallet_address)
                    wallet_address, primary_strategy, labelled_at
                FROM strategy_labels
                WHERE labeller = $1
                ORDER BY wallet_address, labelled_at DESC
                """,
                labeller,
            )
        return [dict(r) for r in rows]
