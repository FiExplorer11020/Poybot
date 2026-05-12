"""Allow ``python -m src.social`` to boot the social daemon.

Mirrors :mod:`src.microstructure.__main__`. The
:file:`polymarket-social.service` systemd unit invokes this shim.
"""

from __future__ import annotations

import asyncio

from src.social.daemon import main

if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
