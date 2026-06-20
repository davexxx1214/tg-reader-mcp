import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_taco_jin10_qqq import run_backtest_rows  # noqa: E402


class TacoBacktestTests(unittest.TestCase):
    def test_cash_signal_avoids_next_day_qqq_loss(self):
        price_rows = [
            {"date": "2026-06-10", "open": 100.0, "close": 100.0},
            {"date": "2026-06-11", "open": 100.0, "close": 90.0},
        ]
        taco_rows = [
            {"date": "2026-06-08", "value": 0.0},
            {"date": "2026-06-09", "value": 0.0},
            {"date": "2026-06-10", "value": 0.0},
        ]
        result = run_backtest_rows(
            price_rows=price_rows,
            taco_rows=taco_rows,
            jin10_rows=[],
            start_date="2026-06-11",
            end_date="2026-06-11",
            strategy_config={
                "smoothing_days": 3,
                "news_half_life_days": 2,
                "risk_beta": -3.0,
                "relief_beta": 5.0,
                "buy_threshold": -4.0,
                "max_data_age_days": 7,
                "require_fresh_news": True,
                "transaction_cost_bps": 10.0,
            },
        )
        self.assertAlmostEqual(result["daily"][0]["strategy_return"], 0.0)
        self.assertAlmostEqual(result["daily"][0]["qqq_return"], -0.1)
        self.assertEqual(result["daily"][0]["target_qqq"], 0.0)


if __name__ == "__main__":
    unittest.main()
