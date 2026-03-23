from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.monthly_report_service import (  # noqa: E402
    example_monthly_payload,
    generate_monthly_report_pdf,
    load_monthly_report_payload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a monthly Polymarket trading performance PDF report."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional JSON file containing monthly report data.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/poybot-monthly-performance-mars-2026.pdf"),
        help="Destination PDF path.",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        help="Override the bot name displayed in the report header.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    payload = load_monthly_report_payload(args.input) if args.input else example_monthly_payload()
    if args.bot_name:
        payload.bot_name = args.bot_name

    destination = generate_monthly_report_pdf(payload, args.output)
    print(f"Monthly report generated at {destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
