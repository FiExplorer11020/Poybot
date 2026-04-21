import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch


def test_backtest_cli_writes_mock_report(tmp_path):
    output = tmp_path / "gate_report.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/backtest.py",
            "--fixture",
            "mock",
            "--out",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(output.read_text())
    assert result.returncode == 0
    assert output.exists()
    assert report["strategy_track"] == "leader_swing"
    assert Path(report["markdown_path"]).exists()


def test_backtest_cli_exposes_historical_options():
    result = subprocess.run(
        [sys.executable, "scripts/backtest.py", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--start" in result.stdout
    assert "--end" in result.stdout
    assert "--wallets" in result.stdout
    assert "--cache-dir" in result.stdout
    assert "--refresh" in result.stdout
    assert "--top-leaders" in result.stdout
    assert "--max-wallets" in result.stdout
    assert "--smoke-days" in result.stdout
    assert "--page-limit" in result.stdout
    assert "--max-pages" in result.stdout
    assert "--max-markets" in result.stdout
    assert "--max-tokens" in result.stdout


def test_run_backtest_with_loader_writes_report(tmp_path):
    import scripts.backtest as backtest_script

    loader = AsyncMock()
    markets, trades, books = backtest_script._mock_fixture()
    loader.load = AsyncMock(
        return_value=type(
            "Dataset",
            (),
            {
                "markets": markets,
                "trades": trades,
                "books": books,
            },
        )()
    )
    output = tmp_path / "historical_report.json"

    with patch.object(backtest_script, "_make_loader", return_value=loader):
        rc = backtest_script.main(
            [
                "--start",
                "2026-04-20",
                "--end",
                "2026-04-21",
                "--wallets",
                "0xleader",
                "--out",
                str(output),
            ]
        )

    report = json.loads(output.read_text())
    assert rc == 0
    assert report["strategy_track"] == "leader_swing"
    assert report["input_counts"] == {
        "markets": 1,
        "trades": 2,
        "books": 2,
        "candles": 0,
    }
    loader.load.assert_awaited_once()


def test_run_backtest_can_load_top_leaders_from_db(tmp_path):
    import scripts.backtest as backtest_script

    loader = AsyncMock()
    markets, trades, books = backtest_script._mock_fixture()
    loader.load = AsyncMock(
        return_value=type(
            "Dataset",
            (),
            {
                "markets": markets,
                "trades": trades,
                "books": books,
                "candles": [],
            },
        )()
    )
    output = tmp_path / "top_leaders_report.json"

    with (
        patch.object(backtest_script, "_make_loader", return_value=loader) as make_loader,
        patch.object(
            backtest_script,
            "_load_top_leader_wallets",
            new=AsyncMock(return_value=["0x1"]),
        ),
    ):
        rc = backtest_script.main(
            [
                "--start",
                "2026-04-13",
                "--end",
                "2026-04-20",
                "--top-leaders",
                "5",
                "--max-wallets",
                "1",
                "--out",
                str(output),
            ]
        )

    assert rc == 0
    make_loader.assert_called_once_with(
        str(backtest_script.Path("data_cache")),
        refresh=False,
        page_limit=200,
        max_pages=20,
        max_markets=None,
        max_tokens=None,
    )
    loader.load.assert_awaited_once()
    assert loader.load.await_args.kwargs["wallets"] == ["0x1"]
