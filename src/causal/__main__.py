"""``python -m src.causal`` entrypoint.

Used by ``infra/systemd/polymarket-causal.service`` to boot the
Round 10 daemon. Defers to :func:`src.causal.daemon.main`.
"""

from __future__ import annotations

import asyncio

from src.causal.daemon import main


if __name__ == "__main__":
    asyncio.run(main())
