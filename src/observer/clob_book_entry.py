"""Thin shim so ``python -m src.observer.clob_book_entry`` boots the
CLOB Book L3 observer.

The systemd unit uses :mod:`src.observer.clob_book_main` directly, but
this shim mirrors :mod:`src.mempool.__main__` for operators who prefer
the ``-m <package>.<entry>`` form on CLI.
"""

from __future__ import annotations

import asyncio

from src.observer.clob_book_main import main

if __name__ == "__main__":
    asyncio.run(main())
