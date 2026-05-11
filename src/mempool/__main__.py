"""Allow ``python -m src.mempool`` to launch the mempool watcher daemon.

The :file:`polymarket-mempool.service` systemd unit invokes
``python -m src.mempool``; without a ``__main__`` module the import
would raise ``No module named src.mempool.__main__``. This is a thin
shim that re-exports :func:`src.mempool.main.main` and runs it under
``asyncio.run``, matching the existing ``src.observer.main`` /
``src.onchain.main`` convention but accessible at the package root.
"""

from __future__ import annotations

import asyncio

from src.mempool.main import main

if __name__ == "__main__":
    asyncio.run(main())
