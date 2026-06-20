import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from query_stock_prices import _fetch_alpaca_snapshots  # noqa: E402
from _config import get_taco_strategy_config  # noqa: E402
from run_analysis_trade_pipeline import _execute_trade_plan, _load_prices, validate_run_options  # noqa: E402
from taco_strategy import (  # noqa: E402
    build_rebalance_plan,
    calculate_taco_jin10_signal,
    score_jin10_message,
    target_weights_for_exposure,
)


class TacoJin10SignalTests(unittest.TestCase):
    def test_config_defaults_match_deployed_strategy(self):
        parsed = get_taco_strategy_config({})
        self.assertEqual(parsed["symbol"], "QQQ")
        self.assertEqual(parsed["buy_threshold"], -4.0)
        self.assertEqual(parsed["smoothing_days"], 3)
        self.assertEqual(parsed["news_half_life_days"], 2)
        self.assertEqual(parsed["risk_beta"], -3.0)
        self.assertEqual(parsed["relief_beta"], 5.0)
        self.assertTrue(parsed["require_fresh_news"])

    def test_keyword_scoring_matches_research_weights(self):
        risk, relief = score_jin10_message("★★ 美国宣布新关税，同时恢复谈判并讨论停火")
        self.assertAlmostEqual(risk, 1.5)
        self.assertAlmostEqual(relief, 3.3)

    def test_signal_uses_only_dates_before_execution(self):
        taco_rows = [
            {"date": "2026-06-08", "value": -6.0},
            {"date": "2026-06-09", "value": -5.0},
            {"date": "2026-06-10", "value": -4.0},
            {"date": "2026-06-11", "value": 100.0},
        ]
        jin10_rows = [
            {"message_id": 1, "date_hk": "2026-06-10T09:00:00+08:00", "text": "美国宣布新关税"},
            {"message_id": 2, "date_hk": "2026-06-11T09:00:00+08:00", "text": "达成停火协议"},
        ]

        result = calculate_taco_jin10_signal(
            taco_rows,
            jin10_rows,
            execution_date="2026-06-11",
            smoothing_days=3,
            news_half_life_days=2,
            risk_beta=-3.0,
            relief_beta=5.0,
            buy_threshold=-4.0,
            max_data_age_days=7,
        )

        self.assertEqual(result["signal_date"], "2026-06-10")
        self.assertAlmostEqual(result["smoothed_taco"], -5.0)
        self.assertAlmostEqual(result["risk_intensity"], 0.9)
        self.assertAlmostEqual(result["relief_intensity"], 0.0)
        self.assertAlmostEqual(result["combined_signal"], -7.7)
        self.assertEqual(result["exposure"], 1.0)
        self.assertEqual(result["regime"], "long_qqq")

    def test_signal_above_threshold_moves_to_cash(self):
        result = calculate_taco_jin10_signal(
            [
                {"date": "2026-06-08", "value": 0.0},
                {"date": "2026-06-09", "value": 0.0},
                {"date": "2026-06-10", "value": 0.0},
            ],
            [],
            execution_date="2026-06-11",
            smoothing_days=3,
            news_half_life_days=2,
            risk_beta=-3.0,
            relief_beta=5.0,
            buy_threshold=-4.0,
            max_data_age_days=7,
        )
        self.assertEqual(result["exposure"], 0.0)
        self.assertEqual(result["regime"], "cash")

    def test_stale_jin10_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Jin10 data is stale"):
            calculate_taco_jin10_signal(
                [{"date": "2026-06-10", "value": -5.0}],
                [{"message_id": 1, "date_hk": "2026-06-01T09:00:00+08:00", "text": "关税"}],
                execution_date="2026-06-11",
                max_data_age_days=3,
                require_fresh_news=True,
            )


class TacoAllocationTests(unittest.TestCase):
    def test_target_weights_are_qqq_or_cash_only(self):
        self.assertEqual(target_weights_for_exposure("QQQ", 1.0), {"QQQ": 1.0})
        self.assertEqual(target_weights_for_exposure("QQQ", 0.0), {})

    def test_rebalance_sells_other_assets_and_buys_qqq(self):
        plan = build_rebalance_plan(
            account={"equity": 100_000.0, "cash": 100_000.0},
            positions=[{"symbol": "NVDA", "qty": 20.0, "current_price": 100.0}],
            prices={"NVDA": 100.0, "QQQ": 500.0},
            target_weights={"QQQ": 1.0},
        )
        by_symbol = {(item["action"], item["symbol"]): item for item in plan}
        self.assertEqual(by_symbol[("sell", "NVDA")]["qty"], 20)
        self.assertEqual(by_symbol[("buy", "QQQ")]["qty"], 200)


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
