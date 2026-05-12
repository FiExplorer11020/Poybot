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

    # Build the provider list directly from settings — there is no
    # `ProviderPool.from_settings()` classmethod in the codebase. The
    # priorities string in `RPC_PROVIDER_PRIORITIES` decides the order.
    from src.rpc.providers import RPCProvider

    providers: list[RPCProvider] = []
    priority_order = [
        p.strip() for p in (settings.RPC_PROVIDER_PRIORITIES or "").split(",") if p.strip()
    ]

    def _ws_from_https(url: str) -> str | None:
        """Derive WSS URL from an HTTPS RPC URL (Alchemy/QuickNode pattern).

        Both providers expose the same path under wss:// — we just need
        the scheme swap. Returns None for non-https inputs so we don't
        invent broken URLs.
        """
        if not url:
            return None
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return None

    for i, name in enumerate(priority_order):
        if name == "local_erigon" and settings.LOCAL_ERIGON_RPC_URL:
            providers.append(
                RPCProvider(
                    name="local_erigon",
                    url=settings.LOCAL_ERIGON_RPC_URL,
                    ws_url=settings.LOCAL_ERIGON_WS_URL or _ws_from_https(settings.LOCAL_ERIGON_RPC_URL),
                    priority=i,
                )
            )
        elif name == "alchemy" and settings.ALCHEMY_RPC_URL:
            providers.append(
                RPCProvider(
                    name="alchemy",
                    url=settings.ALCHEMY_RPC_URL,
                    ws_url=_ws_from_https(settings.ALCHEMY_RPC_URL),
                    api_key=settings.ALCHEMY_RPC_API_KEY,
                    priority=i,
                )
            )
        elif name == "quicknode" and settings.QUICKNODE_RPC_URL:
            providers.append(
                RPCProvider(
                    name="quicknode",
                    url=settings.QUICKNODE_RPC_URL,
                    ws_url=_ws_from_https(settings.QUICKNODE_RPC_URL),
                    api_key=settings.QUICKNODE_RPC_API_KEY,
                    priority=i,
                )
            )

    if not providers:
        logger.warning(
            "polymarket-onchain: no RPC providers configured in .env. "
            "Set ALCHEMY_RPC_URL or LOCAL_ERIGON_RPC_URL. Listener will idle."
        )

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
