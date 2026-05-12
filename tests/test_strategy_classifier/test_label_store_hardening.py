"""R8 Wave-3 hardening tests for :mod:`src.strategy_classifier.labeling.label_store`.

These plug Cohen's κ edge-case + validation gaps in the original suite.

Covers:

* Cohen's κ on perfectly-disagreeing labellers (different deterministic
  classes) — must return 0 by Cohen's convention (no agreement beyond
  chance).
* Cohen's κ when one labeller has invalid / off-taxonomy labels — those
  rows are skipped, but the function does not crash.
* Cohen's κ matches scikit-learn's reference implementation on a mixed
  dataset (regression-test our hand-rolled formula).
* ``compute_inter_labeller_kappa`` n_overlap=1 returns nan (under-sample).
* ``label_set_size`` returns a count for every class even if some are
  empty (initialised to zero).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

import pytest

from src.strategy_classifier.labeling import LabelRow, StrategyLabelStore
from src.strategy_classifier.model import STRATEGY_CLASSES


class TestKappaEdgeCases:
    @pytest.mark.asyncio
    async def test_perfect_disagreement_returns_zero(self):
        """All wallets: a labels 'directional', b labels 'momentum'.

        Both labellers are deterministic but pick DIFFERENT classes.
        Cohen's κ convention: this is zero (no agreement beyond chance —
        actually negative-or-zero; p_e = 0 in this corner so the
        formula yields κ = (0 - 0) / (1 - 0) = 0).
        """
        wallets = [f"w{i}" for i in range(5)]
        rows_a = [
            {"wallet_address": w, "primary_strategy": "directional",
             "labelled_at": datetime.now()}
            for w in wallets
        ]
        rows_b = [
            {"wallet_address": w, "primary_strategy": "momentum",
             "labelled_at": datetime.now()}
            for w in wallets
        ]
        store = StrategyLabelStore()
        with patch.object(
            store, "_latest_label_per_wallet",
            new=AsyncMock(side_effect=lambda l: rows_a if l == "a" else rows_b),
        ):
            r = await store.compute_inter_labeller_kappa("a", "b")
        # Disagreement → κ = 0, p_o = 0.
        assert r["kappa"] == 0.0
        assert r["agreement_rate"] == 0.0
        assert r["n_overlap"] == 5

    @pytest.mark.asyncio
    async def test_kappa_matches_sklearn_on_mixed_dataset(self):
        """Regression-test our hand-rolled κ against sklearn's reference."""
        sklearn_metrics = pytest.importorskip("sklearn.metrics")
        labels_a = [
            "directional", "momentum", "directional", "info_leak",
            "directional", "contrarian", "momentum", "directional",
            "info_leak", "directional",
        ]
        labels_b = [
            "directional", "directional", "directional", "info_leak",
            "momentum", "contrarian", "momentum", "directional",
            "directional", "directional",
        ]
        rows_a = [
            {"wallet_address": f"w{i}", "primary_strategy": l,
             "labelled_at": datetime.now()}
            for i, l in enumerate(labels_a)
        ]
        rows_b = [
            {"wallet_address": f"w{i}", "primary_strategy": l,
             "labelled_at": datetime.now()}
            for i, l in enumerate(labels_b)
        ]
        store = StrategyLabelStore()
        with patch.object(
            store, "_latest_label_per_wallet",
            new=AsyncMock(side_effect=lambda l: rows_a if l == "a" else rows_b),
        ):
            r = await store.compute_inter_labeller_kappa("a", "b")
        ref = sklearn_metrics.cohen_kappa_score(labels_a, labels_b)
        # Tolerance accounts for our 4-dp rounding.
        assert abs(r["kappa"] - ref) < 1e-3

    @pytest.mark.asyncio
    async def test_off_taxonomy_label_skipped_not_crash(self):
        """A wallet with a non-taxonomy label string is skipped from the
        contingency matrix rather than crashing the κ."""
        wallets = ["w1", "w2", "w3", "w4"]
        labels_a = ["directional", "momentum", "directional", "info_leak"]
        # Labeller B includes an off-taxonomy label on w2.
        labels_b = ["directional", "garbage_class", "directional", "info_leak"]
        rows_a = [
            {"wallet_address": w, "primary_strategy": l,
             "labelled_at": datetime.now()}
            for w, l in zip(wallets, labels_a)
        ]
        rows_b = [
            {"wallet_address": w, "primary_strategy": l,
             "labelled_at": datetime.now()}
            for w, l in zip(wallets, labels_b)
        ]
        store = StrategyLabelStore()
        with patch.object(
            store, "_latest_label_per_wallet",
            new=AsyncMock(side_effect=lambda l: rows_a if l == "a" else rows_b),
        ):
            r = await store.compute_inter_labeller_kappa("a", "b")
        # w2 row skipped → effective overlap = 3 (after taxonomy filter).
        assert r["n_overlap"] == 3
        # Agreement on remaining 3 wallets: w1, w3, w4 → all match → p_o = 1.0
        assert r["agreement_rate"] == pytest.approx(1.0)
        # κ is 1.0 (p_o = 1.0, p_e < 1).
        assert r["kappa"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_n_overlap_one_returns_nan(self):
        """A single overlapping wallet is under-sample → κ is nan."""
        rows_a = [{"wallet_address": "w1", "primary_strategy": "directional",
                   "labelled_at": datetime.now()}]
        rows_b = [{"wallet_address": "w1", "primary_strategy": "directional",
                   "labelled_at": datetime.now()}]
        store = StrategyLabelStore()
        with patch.object(
            store, "_latest_label_per_wallet",
            new=AsyncMock(side_effect=lambda l: rows_a if l == "a" else rows_b),
        ):
            r = await store.compute_inter_labeller_kappa("a", "b")
        assert r["n_overlap"] == 1
        import math
        assert math.isnan(r["kappa"])


class TestLabelSetSize:
    @pytest.mark.asyncio
    async def test_label_set_size_includes_zero_classes(self):
        """Every taxonomy class gets a row in the output even if no labels
        exist for it (so the metric publishes a complete vector)."""
        store = StrategyLabelStore()
        # Mock get_labelled_set_for_training to return rows only for two classes.
        rows = [
            {"wallet_address": f"w{i}", "label_window_start": date(2026, 4, 1),
             "label_window_end": date(2026, 4, 30),
             "primary_strategy": "directional", "confidence": 0.9,
             "labeller": "op", "labelled_at": datetime.now(tz=timezone.utc),
             "rationale": None,
             "asof_ts": datetime(2026, 4, 30, tzinfo=timezone.utc)}
            for i in range(3)
        ] + [
            {"wallet_address": f"x{i}", "label_window_start": date(2026, 4, 1),
             "label_window_end": date(2026, 4, 30),
             "primary_strategy": "momentum", "confidence": 0.9,
             "labeller": "op", "labelled_at": datetime.now(tz=timezone.utc),
             "rationale": None,
             "asof_ts": datetime(2026, 4, 30, tzinfo=timezone.utc)}
            for i in range(2)
        ]

        async def _fake_assemble(primary_only: bool = True):
            return rows

        with patch.object(store, "get_labelled_set_for_training",
                          new=AsyncMock(side_effect=_fake_assemble)):
            out = await store.label_set_size()
        # Every class has an entry (zero for the missing ones).
        for cls in STRATEGY_CLASSES:
            assert cls in out
        assert out["directional"] == 3
        assert out["momentum"] == 2
        assert out["info_leak"] == 0
        assert out["arb_3way"] == 0


class TestLabelRowValidation:
    @pytest.mark.asyncio
    async def test_invalid_secondary_strategy_rejected(self):
        """A typo'd secondary_strategy must fail before the DB write."""
        store = StrategyLabelStore()
        row = LabelRow(
            wallet_address="0xabc",
            label_window_start=date(2026, 4, 1),
            label_window_end=date(2026, 4, 30),
            primary_strategy="directional",
            secondary_strategy="not_a_real_class",
            labeller="op_alice",
        )
        with pytest.raises(ValueError, match="secondary_strategy"):
            await store.insert_label(row)

    @pytest.mark.asyncio
    async def test_confidence_zero_accepted(self):
        """Boundary: confidence == 0.0 is valid (inclusive lower bound)."""
        store = StrategyLabelStore()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=99)

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch(
            "src.strategy_classifier.labeling.label_store.get_db",
            side_effect=_ctx,
        ):
            row = LabelRow(
                wallet_address="0xabc",
                label_window_start=date(2026, 4, 1),
                label_window_end=date(2026, 4, 30),
                primary_strategy="info_leak",
                labeller="op_alice",
                confidence=0.0,
            )
            label_id = await store.insert_label(row)
        assert label_id == 99
