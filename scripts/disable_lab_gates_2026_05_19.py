"""Plan 2026-05-19 P0-2 — disable R7/R8/R9/R10 lab gates in production.

The 2026-05-18 live diagnostic showed all four lab gates were silently
ON in prod (causal_gating_enabled=true, volume_anticipation_enabled=true,
strategy_conditional_confidence_enabled=true, prefill_live_enabled=true),
violating the documented memory contract "V2 lab gated OFF, ne pas
migrer".

These gates downgrade follow_confidence (causal), gate prefill firing
(prefill_live), and apply per-strategy multipliers that aren't yet
validated by A/B testing. Disabling them restores the baseline
confidence_engine behaviour that was operating before the lab path was
silently enabled.

Idempotent — safe to run multiple times. Logs every change with the
prior value so an operator can reconstruct intent from logs.

Usage:
    python scripts/disable_lab_gates_2026_05_19.py            # apply
    python scripts/disable_lab_gates_2026_05_19.py --dry-run  # preview
    python scripts/disable_lab_gates_2026_05_19.py --rollback # re-enable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from loguru import logger

# The four lab gates to flip. The plan calls these P0-2 unblockers —
# they're independent boolean flags so the order does not matter.
LAB_GATES: tuple[str, ...] = (
    "causal_gating_enabled",
    "volume_anticipation_enabled",
    "strategy_conditional_confidence_enabled",
    "prefill_live_enabled",
)


async def _read_current() -> dict[str, Any]:
    """Read the live overrides snapshot via the runtime_config module."""
    from src.control.runtime_config import get_runtime_config

    cfg = get_runtime_config()
    return await cfg.effective()


async def _write_overrides(overrides: dict[str, Any], actor: str) -> dict[str, Any]:
    """Apply the overrides via the validated set_overrides path so the
    pub/sub notification fires and every service (engine, observer,
    paper_trader) sees the new value within 5s."""
    from src.control.runtime_config import get_runtime_config

    cfg = get_runtime_config()
    return await cfg.set_overrides(overrides, actor=actor)


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Disable / re-enable R7-R10 lab gates in runtime_config."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned changes without applying them.",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Re-enable the four gates (reverse of the default action).",
    )
    parser.add_argument(
        "--actor", default="plan_2026_05_19_p0_2",
        help="Identifier written to runtime_config:audit and pub/sub.",
    )
    args = parser.parse_args(argv)

    target_value = True if args.rollback else False

    current = await _read_current()
    plan: dict[str, Any] = {}
    for key in LAB_GATES:
        was = current.get(key)
        if was != target_value:
            plan[key] = target_value
            logger.info(
                f"plan: {key}: {was!r} → {target_value!r}"
            )
        else:
            logger.info(f"skip:  {key}: already {target_value!r}")

    if not plan:
        logger.info("nothing to do — all four gates already at target state.")
        return 0

    if args.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        logger.info("--dry-run: no writes performed.")
        return 0

    result = await _write_overrides(plan, actor=args.actor)
    logger.info(f"applied {len(plan)} overrides via {args.actor}")
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
