"""``python -m src.strategy_classifier`` entrypoint.

Used by ``infra/systemd/polymarket-strategy-classifier.service`` to
boot the Round 8 daemon. Defers to :func:`src.strategy_classifier.daemon.main`.
"""
from __future__ import annotations

import asyncio

from src.strategy_classifier.daemon import main


if __name__ == "__main__":
    asyncio.run(main())
