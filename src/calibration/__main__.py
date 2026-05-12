"""Allow ``python -m src.calibration`` to launch the nightly calibration
daemon. Mirrors the conventions used by :mod:`src.mempool` and
:mod:`src.follower_volume`.
"""

from __future__ import annotations

import asyncio

from src.calibration.daemon import main

if __name__ == "__main__":
    asyncio.run(main())
