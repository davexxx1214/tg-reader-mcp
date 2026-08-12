import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from query_stock_prices import _fetch_alpaca_snapshots  # noqa: E402
from _config import get_ntaco_strategy_config  # noqa: E402
from run_analysis_trade_pipeline import (  # noqa: E402
    _execute_trade_plan,
    _load_prices,
    validate_run_options,
)
from taco_strategy import (  # noqa: E402
    FACTOR_WEIGHTS,
    build_rebalance_plan,
    calculate_ntaco_signal,
    target_exposure_for_ntaco,
    target_weights_for_exposure,
)


def taco_row(day: str, pressure: float):
    contributions = {key: pressure * weight for key, weight in FACTOR_WEIGHTS.items()}
    return {"date": day, "value": pressure, "contributions": contributions}


class NormalizedTacoSignalTests(unittest.TestCase):
    def test_config_defaults_match_100_buy_20_sell_strategy(self):
        parsed = get_ntaco_strategy_config({})
        self.assertEqual(parsed["symbol"], "QQQ")
        self.assertEqual(parsed["lower_threshold"], 0.30)
        self.assertEqual(parsed["upper_threshold"], 0.49)
        self.assertEqual(parsed["buy_exposure"], 1.0)
        self.assertEqual(parsed["sell_fraction"], 0.20)
        self.assertEqual(parsed["normalization_lookback"], 42)
        self.assertEqual(parsed["transaction_cost_bps"], 5.0)
        self.assertNotIn("jin10_db", parsed)

    def test_legacy_strategy_parameters_do_not_leak_into_ntaco(self):
        parsed = get_ntaco_strategy_config(
            {"taco_strategy": {"buy_threshold": -4.0, "transaction_cost_bps": 10.0}}
        )
        self.assertEqual(parsed["lower_threshold"], 0.30)
        self.assertEqual(parsed["upper_threshold"], 0.49)
        self.assertEqual(parsed["transaction_cost_bps"], 5.0)

    def test_signal_uses_only_prior_rows_and_buys_100_percent(self):
        result = calculate_ntaco_signal(
            [
                taco_row("2026-06-08", 1.0),
                taco_row("2026-06-09", 2.0),
                taco_row("2026-06-10", 3.0),
                taco_row("2026-06-11", -100.0),
            ],
            execution_date="2026-06-11",
            prior_exposure=0.8,
            lower_threshold=0.30,
            upper_threshold=0.49,
            normalization_lookback=42,
            max_data_age_days=7,
        )

        self.assertEqual(result["signal_date"], "2026-06-10")
        self.assertAlmostEqual(result["ntaco"], 100.0)
        self.assertEqual(result["exposure"], 1.0)
        self.assertEqual(result["action"], "raise_to_100")

    def test_low_signal_sells_only_20_percent_from_full_position(self):
        self.assertEqual(target_exposure_for_ntaco(20.0, 1.0), (0.8, "trim_to_80"))

    def test_low_signal_never_buys_from_cash(self):
        self.assertEqual(target_exposure_for_ntaco(20.0, 0.0), (0.0, "hold_cash"))

    def test_middle_band_holds_prior_target(self):
        self.assertEqual(target_exposure_for_ntaco(40.0, 0.8), (0.8, "hold"))

    def test_stale_taco_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "TACO data is stale"):
            calculate_ntaco_signal(
                [taco_row("2026-06-01", 1.0), taco_row("2026-06-02", 2.0)],
                execution_date="2026-06-11",
                prior_exposure=0.0,
                max_data_age_days=3,
            )


class TacoAllocationTests(unittest.TestCase):
    def test_target_weights_accept_80_and_100_percent_qqq(self):
        self.assertEqual(target_weights_for_exposure("QQQ", 1.0), {"QQQ": 1.0})
        self.assertEqual(target_weights_for_exposure("QQQ", 0.8), {"QQQ": 0.8})
        self.assertEqual(target_weights_for_exposure("QQQ", 0.0), {})

    def test_rebalance_sells_other_assets_and_buys_qqq(self):
        plan = build_rebalance_plan(
            account={"equity": 100_000.0, "cash": 100_000.0},
            positions=[{"symbol": "NVDA", "qty": 20.0, "current_price": 100.0}],
            prices={"NVDA": 100.0, "QQQ": 500.0},
            target_weights={"QQQ": 0.8},
        )
        by_symbol = {(item["action"], item["symbol"]): item for item in plan}
        self.assertEqual(by_symbol[("sell", "NVDA")]["qty"], 20)
        self.assertEqual(by_symbol[("buy", "QQQ")]["qty"], 160)


class AlpacaSnapshotCompatibilityTests(unittest.TestCase):
    @patch("query_stock_prices._alpaca_headers", return_value={"key": "value"})
    @patch("query_stock_prices.requests.get")
    def test_direct_symbol_map_response_is_parsed(self, get_mock, _headers_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "QQQ": {
                "latestTrade": {"p": 600.0},
                "latestQuote": {"ap": 600.1, "bp": 599.9},
                "dailyBar": {"c": 600.0, "v": 1000},
                "prevDailyBar": {"c": 590.0},
            }
        }
        get_mock.return_value = response
        quotes = _fetch_alpaca_snapshots(["QQQ"], timeout_seconds=5)
        self.assertEqual(quotes["QQQ"]["price"], 600.0)


class PipelineSafetyTests(unittest.TestCase):
    def test_non_finite_factor_is_rejected(self):
        bad = taco_row("2026-06-09", 1.0)
        bad["contributions"]["vix"] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            calculate_ntaco_signal(
                [taco_row("2026-06-08", 0.0), bad],
                execution_date="2026-06-10",
                prior_exposure=1.0,
            )

    def test_execution_rejects_stale_local_account_snapshot(self):
        with self.assertRaisesRegex(ValueError, "fresh Alpaca account snapshot"):
            validate_run_options(execute_trades=True, skip_account_refresh=True)

    @patch("run_analysis_trade_pipeline._fetch_alpaca_snapshots", return_value={})
    def test_live_execution_requires_live_qqq_quote(self, _snapshot_mock):
        with self.assertRaisesRegex(RuntimeError, "live Alpaca quote"):
            _load_prices("QQQ", [], require_live=True)

    @patch("run_analysis_trade_pipeline.subprocess.run")
    def test_execution_stops_when_previous_order_is_not_filled(self, run_mock):
        run_mock.return_value = Mock(
            returncode=0,
            stdout='{"status": "new", "symbol": "NVDA"}',
            stderr="",
        )
        results = _execute_trade_plan(
            [
                {"action": "sell", "symbol": "NVDA", "qty": 10},
                {"action": "buy", "symbol": "QQQ", "qty": 10},
            ]
        )
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(results[0]["trade"]["status"], "new")


if __name__ == "__main__":
    unittest.main()
