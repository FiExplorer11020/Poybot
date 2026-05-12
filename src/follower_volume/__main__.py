"""``python -m src.follower_volume`` entrypoint.

Used by ``infra/systemd/polymarket-follower-volume.service`` to boot
the Round 9 daemon. Defers to
:func:`src.follower_volume.daemon.main`.
"""
from __future__ import annotations

import asyncio

from src.follower_volume.daemon import main


if __name__ == "__main__":
    asyncio.run(main())
