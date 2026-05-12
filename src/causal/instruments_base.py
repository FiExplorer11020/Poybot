"""Base types for the R10 instrument-detection pipeline.

Audit reference: docs/ROUND_10_CAUSAL_INFERENCE.md § 3.1.

Split out of ``src/causal/instruments.py`` so the detector
implementations in ``instruments.py`` and ``instruments_sql.py`` share
one canonical :class:`Detector` ABC and one :class:`InstrumentalEvent`
dataclass. Keeps each implementation file under the 500-LOC project
limit and lets the methodology-audit reviewer focus on the contract
in this file alone.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class InstrumentalEvent:
    """One row in the ``instrumental_events`` table.

    The detectors all return ``list[InstrumentalEvent]``; the registry
    writes them en bloc.
    """

    event_type: str  # news|oracle_update|api_outage|funding|gas_quirk
    event_time: datetime
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    affected_market_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def affected_csv(self, max_chars: int = 2000) -> Optional[str]:
        """Serialise the affected_market_ids list to the VARCHAR(2000)
        column. Returns None when the list is empty.

        Truncation strategy: keep complete market_id entries up to the
        budget. The full list is also preserved in ``payload`` so the
        2SLS first stage can recover it from JSONB if needed.
        """
        if not self.affected_market_ids:
            return None
        out: list[str] = []
        used = 0
        for mid in self.affected_market_ids:
            piece = mid if not out else "," + mid
            if used + len(piece) > max_chars:
                break
            out.append(piece)
            used += len(piece)
        return "".join(out) if out else None


class Detector(abc.ABC):
    """Abstract base for instrument detectors.

    Concrete subclasses implement ``detect(asof_ts)`` returning a list
    of ``InstrumentalEvent``s. The registry calls them in order; any
    detector that raises is logged and skipped (the rest still run).
    """

    #: Name used for logging + metrics labels.
    name: str = "detector"
    #: ``event_type`` written to ``instrumental_events.event_type``.
    event_type: str = "unknown"

    @abc.abstractmethod
    async def detect(self, asof_ts: datetime) -> list[InstrumentalEvent]:
        """Find new instrumental events as of ``asof_ts``.

        Returns
        -------
        list[InstrumentalEvent]
            New events. The detector should NOT return events older
            than the last successful run — that's the registry's job
            via the inserted_at column.
        """
        raise NotImplementedError


__all__ = ["Detector", "InstrumentalEvent"]
