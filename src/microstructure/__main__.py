"""Allow ``python -m src.microstructure`` to boot the deriver daemon.

Mirrors :mod:`src.mempool.__main__`. The
:file:`polymarket-microstructure.service` systemd unit invokes
``python -m src.microstructure``; this shim re-exports
:func:`src.microstructure.daemon.main` and runs it under
``asyncio.run``.
"""

from __future__ import annotations

import asyncio

from src.microstructure.daemon import main

if __name__ == "__main__":
    asyncio.run(main())
