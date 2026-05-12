"""Allow ``python -m src.cross_market`` to boot the cross-market daemon.

Mirrors :mod:`src.microstructure.__main__`. The
:file:`polymarket-crossmarket.service` systemd unit invokes this shim.
"""

from __future__ import annotations

import asyncio

from src.cross_market.daemon import main

if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
