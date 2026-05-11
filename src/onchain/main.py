"""Entry point for the on-chain CLOB listener service.

Run as ``python -m src.onchain`` (or via the
``polymarket-onchain.service`` systemd unit). The process owns one
:class:`src.onchain.clob_listener.CLOBChainListener` instance for its
whole lifetime; SIGTERM triggers :meth:`CLOBChainListener.stop` and the
process exits cleanly once the loop has drained.

The :class:`src.rpc.client.RPCClient` is built defensively — if the RPC
module isn't ready yet (Wave-2 parallel work) we log and exit non-zero
so the supervisor (systemd) backs off rather than spinning.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from src.config import settings
from src.onchain.clob_listener import CLOBChainListener


async def _build_rpc_client():
    """Construct the RPCClient, surfacing a clear failure message if the
    rpc module's Wave-2 implementation isn't ready yet.

    Returns the constructed client or raises a RuntimeError documenting
    the missing piece. The supervisor (systemd) handles restart-on-fail.
    """
    try:
        from src.rpc.client import RPCClient
        from src.rpc.providers import RPCProvider  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "src.rpc client unavailable — Wave-2 RPC agent must ship "
            "before the onchain listener can boot"
        ) from exc

    # The provider list comes from settings; the actual pool builder is
    # owned by src.rpc (Wave-2 Agent A). We try the canonical builder
    # first and fall back to a constructor-with-empty-list which the
    # RPCClient should reject defensively.
    try:
        from src.rpc.providers import ProviderPool  # type: ignore[attr-defined]

        providers = ProviderPool.from_settings().providers  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            f"ProviderPool.from_settings() failed ({exc!r}); attempting "
            "bare RPCClient construction — this will likely raise"
        )
        providers = []

    return RPCClient(providers)


async def main() -> None:
    logger.info(
        "polymarket-onchain.service starting "
        f"contract={settings.POLYMARKET_CLOB_CONTRACT_ADDRESS}"
    )
    try:
        rpc_client = await _build_rpc_client()
    except Exception as exc:
        logger.error(f"polymarket-onchain: cannot start RPC client: {exc!r}")
        sys.exit(1)

    listener = CLOBChainListener(
        rpc_client=rpc_client,
        redis_url=settings.REDIS_URL,
    )

    stop_event = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        logger.info(f"polymarket-onchain: signal {signum} received, draining")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows / unusual loops — fall back to default handlers.
            pass

    await listener.start()
    try:
        await stop_event.wait()
    finally:
        await listener.stop()
        try:
            await rpc_client.close()
        except Exception:
            pass
        logger.info("polymarket-onchain: shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
