import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_ntaco_qqq import run_backtest_rows  # noqa: E402
from taco_strategy import FACTOR_WEIGHTS  # noqa: E402


def taco_row(day: str, pressure: float):
    return {
        "date": day,
        "value": pressure,
        "contributions": {key: pressure * weight for key, weight in FACTOR_WEIGHTS.items()},
    }


class TacoBacktestTests(unittest.TestCase):
    def test_low_signal_trims_full_position_to_80_percent(self):
        price_rows = [
            {"date": "2026-06-09", "open": 100.0, "close": 100.0},
            {"date": "2026-06-10", "open": 100.0, "close": 100.0},
            {"date": "2026-06-11", "open": 100.0, "close": 100.0},
            {"date": "2026-06-12", "open": 100.0, "close": 90.0},
        ]
        result = run_backtest_rows(
            price_rows=price_rows,
            taco_rows=[
                taco_row("2026-06-09", 1.0),
                taco_row("2026-06-10", 2.0),
                taco_row("2026-06-11", 0.0),
            ],
            start_date="2026-06-10",
            end_date="2026-06-12",
            strategy_config={
                "lower_threshold": 0.30,
                "upper_threshold": 0.49,
                "buy_exposure": 1.0,
                "sell_fraction": 0.20,
                "normalization_lookback": 42,
                "max_data_age_days": 7,
                "transaction_cost_bps": 0.0,
            },
        )

        final = result["daily"][-1]
        self.assertEqual(final["target_qqq"], 0.8)
        self.assertAlmostEqual(final["strategy_return"], -0.08)
        self.assertAlmostEqual(final["qqq_return"], -0.10)


if __name__ == "__main__":
    unittest.main()
