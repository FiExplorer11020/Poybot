"""Instrumental-variable event detection pipelines.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.1.

This module wires the API/RPC-backed detectors plus the
:class:`InstrumentRegistry` orchestrator. The pure-SQL detectors
(:class:`RelatedMarketResolver`, :class:`LeaderGasQuirkDetector`,
:class:`APIOutageWindowDetector`) live in
:mod:`src.causal.instruments_sql` and are re-exported here for the
public API.

The base types (:class:`Detector`, :class:`InstrumentalEvent`) live in
:mod:`src.causal.instruments_base` so every detector file shares one
canonical contract.

Cadence guide (per spec § 3.1):

  * ``NewsEventDetector``       — every 5 min (operator schedules)
  * ``OracleUpdateDetector``    — real-time via R6 eth_subscribe('logs', ...)
  * ``RelatedMarketResolver``   — hourly batch (pure SQL on trades_observed)
  * ``LeaderGasQuirkDetector``  — weekly batch (pure SQL on mempool_observations)
  * ``APIOutageWindowDetector`` — on-alert (reads R6 coverage_reconciler output)

We deliberately keep the detectors as *separate classes* (not
methods of one god-class) so the operator can opt-out a specific
instrument when the methodology audit (spec § 6) flags a validity
concern — flip the detector list and the registry skips that path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from src.causal.instruments_base import Detector, InstrumentalEvent
from src.causal.instruments_sql import (
    APIOutageWindowDetector,
    LeaderGasQuirkDetector,
    RelatedMarketResolver,
)
from src.database.connection import get_db


# ---------------------------------------------------------------------------
# 1. NewsEventDetector
# ---------------------------------------------------------------------------


class NewsEventDetector(Detector):
    """NewsAPI-fed news event detector — R12 expands the event corpus to
    include high-confidence social signals.

    Pipeline (real path):
        1. Fetch top headlines via NewsAPI (injected ``http_session``).
        2. Run NER to extract entity mentions (operator-deliverable;
           we stub the NER step here as a callable so the methodology
           audit can swap in different NER backends).
        3. Match entities against candidate market subjects (markets
           whose ``question`` text contains the entity).
        4. **R12**: also sweep ``social_signals`` for high-confidence
           entry / exit signals (intent in {entry_signal, exit_signal}
           AND intent_confidence > ``min_social_confidence``) and emit
           one InstrumentalEvent per signal. Spec § 3.4 + R10
           instruments contract.
        5. Emit one InstrumentalEvent per matched market with a
           confidence score.

    The HTTP call is operator-deliverable infrastructure — we accept
    an injected ``http_session`` and a ``candidate_subjects`` map so
    the unit tests can run without a NewsAPI subscription. The
    :class:`FixtureNewsEventDetector` below is a deterministic stub
    for tests + smoke runs.
    """

    name = "news_event"
    event_type = "news"

    def __init__(
        self,
        http_session: Any | None = None,
        ner_extractor: Any | None = None,
        api_key: str | None = None,
        *,
        social_signal_lookback_s: int = 3600,
        min_social_confidence: float | None = None,
    ) -> None:
        self._http = http_session
        self._ner = ner_extractor
        self._api_key = api_key
        # R12 wiring — defaults to the periphery spec's threshold.
        self._social_lookback_s = int(social_signal_lookback_s)
        if min_social_confidence is None:
            try:
                from src.config import settings as _settings
                self._min_social_conf = float(
                    _settings.SOCIAL_NEWS_EVENT_MIN_CONFIDENCE
                )
            except Exception:  # pragma: no cover — defensive
                self._min_social_conf = 0.7
        else:
            self._min_social_conf = float(min_social_confidence)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        events: list[InstrumentalEvent] = []
        # R12 — social signals always contribute, regardless of whether
        # the NewsAPI side is wired. The social signals table is
        # populated by the social daemon (R12) and may carry useful
        # instrumental events even when the operator hasn't subscribed
        # to NewsAPI yet.
        events.extend(await self._fetch_social_signal_events(asof_ts))
        if self._http is None:
            logger.debug(
                "NewsEventDetector: no http_session injected; returning "
                "social-signal events only. Use FixtureNewsEventDetector "
                "for tests or wire a real aiohttp.ClientSession for the "
                "NewsAPI side."
            )
            return events
        # Real-path skeleton — kept minimal so the methodology audit
        # has a clean target. The operator wires the actual NewsAPI
        # endpoint + NER in a separate deliverable.
        headlines = await self._fetch_headlines(asof_ts)
        for h in headlines:
            entities = self._extract_entities(h)
            affected = await self._match_markets(entities, asof_ts)
            if not affected:
                continue
            events.append(
                InstrumentalEvent(
                    event_type=self.event_type,
                    event_time=h.get("published_at", asof_ts),
                    source="newsapi",
                    payload={
                        "headline": h.get("title", ""),
                        "entities": entities,
                    },
                    affected_market_ids=affected,
                    confidence=float(h.get("confidence", 0.8)),
                )
            )
        return events

    async def _fetch_social_signal_events(
        self, asof_ts: datetime
    ) -> list[InstrumentalEvent]:
        """Read high-confidence entry/exit signals from
        ``social_signals`` within ``social_signal_lookback_s`` of asof.
        Each row maps to one InstrumentalEvent — the affected markets
        are pulled from the ``parsed_market`` column (or left empty
        when the classifier couldn't extract one)."""
        floor = asof_ts.replace(microsecond=0)
        try:
            from datetime import timedelta as _td
            floor = asof_ts - _td(seconds=self._social_lookback_s)
        except Exception:  # pragma: no cover
            pass
        events: list[InstrumentalEvent] = []
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT signal_id, source, author_handle, posted_at,
                           intent, intent_confidence, parsed_market,
                           parsed_direction, text
                    FROM social_signals
                    WHERE posted_at >= $1
                      AND posted_at <= $2
                      AND intent IN ('entry_signal', 'exit_signal')
                      AND intent_confidence > $3
                    ORDER BY posted_at ASC
                    LIMIT 500
                    """,
                    floor,
                    asof_ts,
                    self._min_social_conf,
                )
        except Exception as exc:
            logger.debug(
                f"NewsEventDetector: social_signals read failed: {exc}"
            )
            return []
        for row in rows or []:
            parsed_market = row["parsed_market"]
            affected = [str(parsed_market)] if parsed_market else []
            events.append(
                InstrumentalEvent(
                    event_type=self.event_type,
                    event_time=row["posted_at"],
                    source=f"social:{row['source']}",
                    payload={
                        "signal_id": int(row["signal_id"]),
                        "author_handle": row["author_handle"],
                        "intent": row["intent"],
                        "parsed_direction": row["parsed_direction"],
                        "text_excerpt": (
                            row["text"][:280] if row["text"] else ""
                        ),
                    },
                    affected_market_ids=affected,
                    confidence=float(row["intent_confidence"]),
                )
            )
        return events

    async def _fetch_headlines(self, asof_ts: datetime) -> list[dict[str, Any]]:
        """Operator-deliverable: returns parsed NewsAPI headlines."""
        return []

    def _extract_entities(self, headline: dict[str, Any]) -> list[str]:
        if self._ner is not None:
            try:
                return list(self._ner(headline.get("title", "")))
            except Exception:
                return []
        return []

    async def _match_markets(
        self,
        entities: list[str],
        asof_ts: datetime,
    ) -> list[str]:
        """Match entities against current markets table."""
        if not entities:
            return []
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT market_id
                    FROM markets
                    WHERE active = TRUE
                      AND lower(question) LIKE ANY($1::text[])
                    LIMIT 100
                    """,
                    [f"%{e.lower()}%" for e in entities],
                )
            return [r["market_id"] for r in rows]
        except Exception as exc:
            logger.debug(f"NewsEventDetector: market match failed: {exc}")
            return []


class FixtureNewsEventDetector(Detector):
    """Deterministic fixture-backed news detector for tests + smoke runs.

    Reads events from a JSON file::

        [
          {
            "event_time": "2026-05-12T10:00:00+00:00",
            "headline": "X collapse",
            "affected_market_ids": ["mkt-1", "mkt-2"],
            "confidence": 0.9
          },
          ...
        ]

    All entries with ``event_time <= asof_ts`` are returned on each call;
    the caller is responsible for deduping against ``instrumental_events``
    (the natural dedupe is via inserted_at + payload contents).
    """

    name = "news_event_fixture"
    event_type = "news"

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
            entries = json.loads(raw)
        except Exception as exc:
            logger.warning(
                f"FixtureNewsEventDetector: failed to read {self._path}: {exc}"
            )
            return []
        out: list[InstrumentalEvent] = []
        for entry in entries:
            try:
                t = entry.get("event_time")
                if isinstance(t, str):
                    ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
                elif isinstance(t, datetime):
                    ts = t
                else:
                    continue
                if ts > asof_ts:
                    continue
                out.append(
                    InstrumentalEvent(
                        event_type=self.event_type,
                        event_time=ts,
                        source="newsapi_fixture",
                        payload={
                            "headline": entry.get("headline", ""),
                            "entities": entry.get("entities", []),
                        },
                        affected_market_ids=list(
                            entry.get("affected_market_ids", [])
                        ),
                        confidence=float(entry.get("confidence", 1.0)),
                    )
                )
            except Exception as exc:
                logger.debug(
                    f"FixtureNewsEventDetector: skipping bad entry {entry}: {exc}"
                )
        return out


# ---------------------------------------------------------------------------
# 2. OracleUpdateDetector
# ---------------------------------------------------------------------------


class OracleUpdateDetector(Detector):
    """Oracle / UMA update detector.

    Wires R6 ``eth_subscribe('logs', ...)`` to detect when an oracle
    posts a resolution price or proposal. Operator-friendly: the
    :class:`src.rpc.client.RPCClient` is injected so tests can mock it.

    For the R10 code pass we implement the *batch* path: the daemon
    sweeps recent oracle logs each cycle. A future iteration can move
    this to a real-time eth_subscribe loop (mirrors the R6 chain
    listener pattern).
    """

    name = "oracle_update"
    event_type = "oracle_update"

    def __init__(
        self,
        rpc_client: Any | None = None,
        oracle_address: str = "",
        lookback_blocks: int = 1_000,
    ) -> None:
        self._rpc = rpc_client
        self._oracle = oracle_address
        self._lookback = int(lookback_blocks)

    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        if self._rpc is None or not self._oracle:
            return []
        try:
            current_block = await self._rpc.call("eth_blockNumber", [])
        except Exception as exc:
            logger.debug(f"OracleUpdateDetector: head fetch failed: {exc}")
            return []
        if isinstance(current_block, str):
            try:
                head = int(current_block, 16)
            except ValueError:
                return []
        else:
            head = int(current_block)
        from_block = max(0, head - self._lookback)
        try:
            logs = await self._rpc.call(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(from_block),
                        "toBlock": "latest",
                        "address": self._oracle,
                    }
                ],
            )
        except Exception as exc:
            logger.debug(f"OracleUpdateDetector: eth_getLogs failed: {exc}")
            return []
        events: list[InstrumentalEvent] = []
        for entry in logs or []:
            try:
                events.append(
                    InstrumentalEvent(
                        event_type=self.event_type,
                        event_time=asof_ts,  # caller may post-process
                        source="oracle_logs",
                        payload={
                            "tx_hash": entry.get("transactionHash"),
                            "block": entry.get("blockNumber"),
                            "topics": entry.get("topics", []),
                        },
                        affected_market_ids=[],
                        confidence=1.0,
                    )
                )
            except Exception as exc:
                logger.debug(
                    f"OracleUpdateDetector: skipping bad log entry: {exc}"
                )
        return events


# ---------------------------------------------------------------------------
# Registry orchestrator
# ---------------------------------------------------------------------------


class InstrumentRegistry:
    """Run every registered detector and persist results.

    Lifecycle: instantiate once per daemon; add detectors via
    ``register(detector)``; call ``run_one_pass(asof_ts)`` per cycle.
    Each detector that raises is logged + skipped; the rest still run.
    """

    def __init__(self, detectors: list[Detector] | None = None) -> None:
        self._detectors: list[Detector] = list(detectors) if detectors else []

    def register(self, detector: Detector) -> None:
        self._detectors.append(detector)

    @property
    def detectors(self) -> list[Detector]:
        return list(self._detectors)

    async def run_one_pass(
        self,
        asof_ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Invoke every detector, persist the unified result.

        Returns a small summary dict for logging:
            {detector_name: {events_detected, events_persisted, error}}
        """
        if asof_ts is None:
            asof_ts = datetime.now(tz=timezone.utc)
        summary: dict[str, Any] = {"asof_ts": asof_ts.isoformat(), "by_detector": {}}
        for det in self._detectors:
            entry: dict[str, Any] = {
                "events_detected": 0,
                "events_persisted": 0,
                "error": None,
            }
            try:
                events = await det.detect(asof_ts)
                entry["events_detected"] = len(events)
                if events:
                    written = await self._persist(events)
                    entry["events_persisted"] = written
            except Exception as exc:  # pragma: no cover — top-level
                entry["error"] = str(exc)
                logger.warning(
                    f"InstrumentRegistry: detector {det.name} raised: {exc}"
                )
            summary["by_detector"][det.name] = entry
        return summary

    async def _persist(self, events: list[InstrumentalEvent]) -> int:
        """Insert events en bloc; returns the count actually written."""
        if not events:
            return 0
        written = 0
        try:
            async with get_db() as conn:
                for ev in events:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO instrumental_events (
                                event_type, event_time, affected_market_ids,
                                payload_json, source, confidence
                            )
                            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                            """,
                            ev.event_type,
                            ev.event_time,
                            ev.affected_csv(),
                            json.dumps(ev.payload),
                            ev.source,
                            float(ev.confidence),
                        )
                        written += 1
                    except Exception as exc:
                        logger.debug(
                            f"InstrumentRegistry: persist failed for "
                            f"{ev.event_type}/{ev.event_time}: {exc}"
                        )
        except Exception as exc:
            logger.warning(f"InstrumentRegistry: DB connect failed: {exc}")
        return written


__all__ = [
    "APIOutageWindowDetector",
    "Detector",
    "FixtureNewsEventDetector",
    "InstrumentRegistry",
    "InstrumentalEvent",
    "LeaderGasQuirkDetector",
    "NewsEventDetector",
    "OracleUpdateDetector",
    "RelatedMarketResolver",
]
