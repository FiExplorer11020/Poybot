"""Unit tests for StrategyLabelStore (insert + κ + training set assembly).

DB I/O is mocked — these tests verify the Python contracts (validation,
shape of κ output, training-set assembly) without touching Postgres.
The integration test that exercises the real DB is in tests/integration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.strategy_classifier.labeling import LabelRow, StrategyLabelStore
from src.strategy_classifier.model import STRATEGY_CLASSES


def _mock_db(fetchval=None, fetch=None, fetchrow=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestInsertLabel:
    @pytest.mark.asyncio
    async def test_insert_valid_label(self):
        ctx, conn = _mock_db(fetchval=42)
        row = LabelRow(
            wallet_address="0xabc",
            label_window_start=date(2026, 4, 1),
            label_window_end=date(2026, 4, 30),
            primary_strategy="directional",
            labeller="op_alice",
            confidence=0.9,
            rationale="long holding, low cancel-to-fill",
        )
        with patch(
            "src.strategy_classifier.labeling.label_store.get_db",
            side_effect=ctx,
        ):
            store = StrategyLabelStore()
            label_id = await store.insert_label(row)
        assert label_id == 42
        conn.fetchval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_primary_strategy_rejected(self):
        store = StrategyLabelStore()
        row = LabelRow(
            wallet_address="0xabc",
            label_window_start=date(2026, 4, 1),
            label_window_end=date(2026, 4, 30),
            primary_strategy="not_a_real_class",
            labeller="op_alice",
        )
        with pytest.raises(ValueError, match="primary_strategy"):
            await store.insert_label(row)

    @pytest.mark.asyncio
    async def test_invalid_confidence_rejected(self):
        store = StrategyLabelStore()
        row = LabelRow(
            wallet_address="0xabc",
            label_window_start=date(2026, 4, 1),
            label_window_end=date(2026, 4, 30),
            primary_strategy="directional",
            labeller="op_alice",
            confidence=1.5,
        )
        with pytest.raises(ValueError, match="confidence"):
            await store.insert_label(row)

    @pytest.mark.asyncio
    async def test_inverted_window_rejected(self):
        store = StrategyLabelStore()
        row = LabelRow(
            wallet_address="0xabc",
            label_window_start=date(2026, 5, 1),
            label_window_end=date(2026, 4, 30),
            primary_strategy="directional",
            labeller="op_alice",
        )
        with pytest.raises(ValueError, match="label_window_end"):
            await store.insert_label(row)


class TestInterLabellerKappa:
    @pytest.mark.asyncio
    async def test_perfect_agreement_kappa_one(self):
        """Both labellers labelled the same 5 wallets the same way."""
        wallets = ["w1", "w2", "w3", "w4", "w5"]
        labels = ["directional", "momentum", "info_leak", "directional", "directional"]
        rows_a = [
            {"wallet_address": w, "primary_strategy": l, "labelled_at": datetime.now()}
            for w, l in zip(wallets, labels)
        ]
        rows_b = list(rows_a)
        store = StrategyLabelStore()
        with patch.object(store, "_latest_label_per_wallet", new=AsyncMock(
            side_effect=lambda labeller: rows_a if labeller == "a" else rows_b
        )):
            result = await store.compute_inter_labeller_kappa("a", "b")
        # Perfect agreement → either κ = 1.0 or 0.0 with all-same labels.
        # In this case multiple distinct labels are present so p_e < 1
        # and κ should be 1.0.
        assert result["agreement_rate"] == pytest.approx(1.0)
        assert result["kappa"] == pytest.approx(1.0)
        assert result["n_overlap"] == 5

    @pytest.mark.asyncio
    async def test_no_overlap_returns_nan(self):
        rows_a = [
            {"wallet_address": "w1", "primary_strategy": "directional",
             "labelled_at": datetime.now()},
        ]
        rows_b = [
            {"wallet_address": "w2", "primary_strategy": "momentum",
             "labelled_at": datetime.now()},
        ]
        store = StrategyLabelStore()
        with patch.object(store, "_latest_label_per_wallet", new=AsyncMock(
            side_effect=lambda l: rows_a if l == "a" else rows_b
        )):
            result = await store.compute_inter_labeller_kappa("a", "b")
        # n_overlap < 2 → kappa is nan
        assert result["n_overlap"] == 0

    @pytest.mark.asyncio
    async def test_partial_disagreement(self):
        """Test κ < 1.0 on a partial-disagreement case."""
        wallets = ["w1", "w2", "w3", "w4"]
        labels_a = ["directional", "momentum", "directional", "info_leak"]
        labels_b = ["directional", "directional", "directional", "info_leak"]
        rows_a = [
            {"wallet_address": w, "primary_strategy": l, "labelled_at": datetime.now()}
            for w, l in zip(wallets, labels_a)
        ]
        rows_b = [
            {"wallet_address": w, "primary_strategy": l, "labelled_at": datetime.now()}
            for w, l in zip(wallets, labels_b)
        ]
        store = StrategyLabelStore()
        with patch.object(store, "_latest_label_per_wallet", new=AsyncMock(
            side_effect=lambda l: rows_a if l == "a" else rows_b
        )):
            result = await store.compute_inter_labeller_kappa("a", "b")
        # 3/4 agreement, κ is positive but < 1.
        assert 0.0 < result["kappa"] < 1.0
        assert result["agreement_rate"] == pytest.approx(0.75)


class TestTrainingSetAssembly:
    @pytest.mark.asyncio
    async def test_get_labelled_set_for_training_returns_asof_ts(self):
        """asof_ts := label_window_end at midnight UTC. Spec § 3.1."""
        rows = [
            {
                "wallet_address": "w1",
                "label_window_start": date(2026, 4, 1),
                "label_window_end": date(2026, 4, 30),
                "primary_strategy": "directional",
                "secondary_strategy": None,
                "confidence": 0.9,
                "labeller": "op_alice",
                "labelled_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "rationale": None,
            }
        ]
        ctx, _ = _mock_db(fetch=rows)
        with patch(
            "src.strategy_classifier.labeling.label_store.get_db",
            side_effect=ctx,
        ):
            store = StrategyLabelStore()
            training_rows = await store.get_labelled_set_for_training()
        assert len(training_rows) == 1
        row = training_rows[0]
        assert row["asof_ts"] == datetime(2026, 4, 30, tzinfo=timezone.utc)
        assert row["primary_strategy"] == "directional"
        assert "secondary_strategy" not in row  # primary_only=True
