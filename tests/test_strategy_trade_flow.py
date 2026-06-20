import json
import sys
import sqlite3
import unittest
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _config import get_market_gate_config, get_risk_config, get_strategy_config  # noqa: E402
from order_builder import build_trade_plan  # noqa: E402
from query_alpaca_account import persist_account_snapshot  # noqa: E402
from risk_guard import apply_risk_guard  # noqa: E402
from run_analysis_trade_pipeline import _select_fundamentals_sync_symbols  # noqa: E402
from strategy_engine import run_strategies  # noqa: E402


class ConfigParsingTests(unittest.TestCase):
    def test_strategy_config_defaults_and_clamp(self):
        cfg = {"strategy": {"enabled": "true", "name": "w_bottom_breakout", "min_confidence": 9}}
        parsed = get_strategy_config(cfg)
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["name"], "w_bottom_breakout")
        self.assertEqual(parsed["min_confidence"], 1.0)

    def test_risk_config_defaults(self):
        parsed = get_risk_config({})
        self.assertGreaterEqual(parsed["max_position_pct"], 0.0)
        self.assertGreaterEqual(parsed["max_positions"], 1)
        self.assertGreaterEqual(parsed["max_trade_notional"], 0.0)

    def test_market_gate_config_defaults_and_parsing(self):
        parsed = get_market_gate_config({})
        self.assertEqual(parsed["benchmark_tickers"], ["QQQ", "SPY"])
        self.assertEqual(parsed["threshold"], -0.05)

        cfg = {"market_gate": {"benchmark_tickers": "spy, qqq, dia", "threshold": "0.1"}}
        parsed = get_market_gate_config(cfg)
        self.assertEqual(parsed["benchmark_tickers"], ["SPY", "QQQ", "DIA"])
        self.assertEqual(parsed["threshold"], 0.1)


class StrategyEngineTests(unittest.TestCase):
    def test_run_strategies_generates_signals(self):
        context = {
            "ranking": [
                {"ticker": "NVDA", "momentum_score": 0.30},
                {"ticker": "AAPL", "momentum_score": -0.2},
            ],
            "selected_top_tickers": ["NVDA", "AAPL"],
            "market_gate_score": 0.2,
            "quotes": [
                {"symbol": "NVDA", "price": 100.0, "technical": {"recommend_all": 0.3}},
                {"symbol": "AAPL", "price": 50.0, "technical": {"recommend_all": 0.1}},
            ],
        }
        result = run_strategies(["news_momentum", "market_gate_trend"], context, min_confidence=0.5)
        self.assertIn("signals_accepted", result)
        self.assertGreaterEqual(len(result["signals_accepted"]), 1)

    def test_run_w_bottom_breakout_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stock_daily.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE stock_daily (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY(symbol, trade_date)
                )
                """
            )

            # Build a simple W-bottom-like series.
            prices = [100.0] * 120
            prices[20] = 82.0
            prices[45] = 100.0
            prices[70] = 84.0
            prices[95] = 98.0
            prices[119] = 99.5
            for i, close in enumerate(prices):
                dt = f"2025-01-{(i % 28) + 1:02d}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_daily(symbol, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("TEST", f"{dt}-{i:03d}", close * 0.99, close * 1.01, close * 0.98, close, 1000000),
                )
            conn.commit()
            conn.close()

            context = {
                "universe_tickers": ["TEST"],
                "history_db_path": str(db_path),
                "strategy_prefilter_top_k": 10,
                "quotes": [{"symbol": "TEST", "price": 99.5, "technical": {"recommend_all": 0.2}}],
            }
            result = run_strategies(["w_bottom_breakout"], context, min_confidence=0.0)
            self.assertIn("signals_all", result)
            self.assertGreaterEqual(len(result["signals_all"]), 1)
            self.assertEqual(result["signals_all"][0]["strategy"], "w_bottom_breakout")

    def test_run_autoresearch_trend_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stock_daily.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE stock_daily (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY(symbol, trade_date)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE fundamentals_quarterly (
                    symbol TEXT NOT NULL,
                    fiscal_date_ending TEXT NOT NULL,
                    revenue REAL,
                    free_cashflow REAL,
                    total_shareholder_equity REAL,
                    long_term_debt REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE fundamentals_overview_daily (
                    symbol TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    pe_ratio REAL,
                    beta REAL,
                    profit_margin REAL,
                    roe_ttm REAL,
                    roa_ttm REAL
                )
                """
            )

            prices = [100.0 + i * 0.35 for i in range(210)]
            prices[-1] = prices[-2] * 1.015
            for i, close in enumerate(prices):
                volume = 1_000_000 if i < len(prices) - 1 else 1_700_000
                conn.execute(
                    """
                    INSERT INTO stock_daily(symbol, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "TEST",
                        f"2025-01-01-{i:03d}",
                        close * 0.99,
                        close * 1.01,
                        close * 0.98,
                        close,
                        volume,
                    ),
                )

            quarterly_rows = [
                ("2025-12-31", 2200.0, 360.0, 1700.0, 120.0),
                ("2025-09-30", 2050.0, 320.0, 1660.0, 125.0),
                ("2025-06-30", 1900.0, 300.0, 1600.0, 130.0),
                ("2025-03-31", 1800.0, 280.0, 1550.0, 132.0),
                ("2024-12-31", 1600.0, 220.0, 1500.0, 140.0),
            ]
            for fiscal_date, revenue, fcf, equity, debt in quarterly_rows:
                conn.execute(
                    """
                    INSERT INTO fundamentals_quarterly(symbol, fiscal_date_ending, revenue, free_cashflow, total_shareholder_equity, long_term_debt)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("TEST", fiscal_date, revenue, fcf, equity, debt),
                )

            conn.execute(
                """
                INSERT INTO fundamentals_overview_daily(symbol, as_of_date, pe_ratio, beta, profit_margin, roe_ttm, roa_ttm)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("TEST", "2026-03-31", 22.0, 1.05, 0.20, 0.28, 0.14),
            )
            conn.commit()
            conn.close()

            context = {
                "universe_tickers": ["TEST"],
                "history_db_path": str(db_path),
                "strategy_prefilter_top_k": 10,
                "quotes": [{"symbol": "TEST", "price": prices[-1], "technical": {"recommend_all": 0.4}}],
                "positions_snapshot": [],
            }
            result = run_strategies(["autoresearch_trend"], context, min_confidence=0.0)
            self.assertIn("signals_all", result)
            self.assertGreaterEqual(len(result["signals_all"]), 1)
            self.assertEqual(result["signals_all"][0]["strategy"], "autoresearch_trend")
            self.assertEqual(result["signals_all"][0]["action"], "buy")


class PipelineSyncTests(unittest.TestCase):
    def test_select_fundamentals_sync_symbols_only_returns_stale_or_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stock_daily.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE fundamentals_overview_daily (
                    symbol TEXT NOT NULL,
                    as_of_date TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE fundamentals_quarterly (
                    symbol TEXT NOT NULL,
                    fiscal_date_ending TEXT NOT NULL
                )
                """
            )

            conn.execute(
                "INSERT INTO fundamentals_overview_daily(symbol, as_of_date) VALUES (?, ?)",
                ("FRESH", "2026-04-04"),
            )
            for idx in range(5):
                conn.execute(
                    "INSERT INTO fundamentals_quarterly(symbol, fiscal_date_ending) VALUES (?, ?)",
                    ("FRESH", f"2025-0{idx + 1}-30"),
                )

            conn.execute(
                "INSERT INTO fundamentals_overview_daily(symbol, as_of_date) VALUES (?, ?)",
                ("STALE", "2026-03-20"),
            )
            for idx in range(5):
                conn.execute(
                    "INSERT INTO fundamentals_quarterly(symbol, fiscal_date_ending) VALUES (?, ?)",
                    ("STALE", f"2025-0{idx + 1}-30"),
                )

            conn.execute(
                "INSERT INTO fundamentals_overview_daily(symbol, as_of_date) VALUES (?, ?)",
                ("SHORTQ", "2026-04-04"),
            )
            for idx in range(2):
                conn.execute(
                    "INSERT INTO fundamentals_quarterly(symbol, fiscal_date_ending) VALUES (?, ?)",
                    ("SHORTQ", f"2025-0{idx + 1}-30"),
                )
            conn.commit()
            conn.close()

            selected = _select_fundamentals_sync_symbols(
                db_path=db_path,
                symbols=["FRESH", "STALE", "SHORTQ", "MISSING"],
                stale_after_days=7,
                min_quarterly_rows=5,
                as_of_date=date(2026, 4, 10),
            )
            self.assertEqual(selected, ["STALE", "SHORTQ", "MISSING"])

    def test_persist_account_snapshot_appends_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            account = {
                "account_number": "paper-1",
                "status": "ACTIVE",
                "cash": 15000.0,
                "buying_power": 30000.0,
                "portfolio_value": 15250.0,
                "equity": 15250.0,
                "last_equity": 15000.0,
                "long_market_value": 250.0,
                "short_market_value": 0.0,
                "initial_margin": 0.0,
                "maintenance_margin": 0.0,
                "daytrade_count": 0,
                "pattern_day_trader": False,
            }
            positions = [
                {
                    "symbol": "MSFT",
                    "qty": 2.0,
                    "avg_entry_price": 400.0,
                    "market_value": 820.0,
                    "current_price": 410.0,
                    "unrealized_pl": 20.0,
                    "unrealized_plpc": 2.5,
                    "side": "long",
                }
            ]

            with patch("query_alpaca_account.resolve_skill_data_dir", return_value=base_dir):
                first = persist_account_snapshot(account, positions, source="test", action="snapshot_a")
                second = persist_account_snapshot(account, positions, source="test", action="snapshot_b")

            position_file = Path(first["position_file"])
            balance_file = Path(first["balance_file"])
            self.assertTrue(position_file.exists())
            self.assertTrue(balance_file.exists())
            self.assertEqual(first["position_record_id"], 1)
            self.assertEqual(second["position_record_id"], 2)

            position_rows = [json.loads(line) for line in position_file.read_text(encoding="utf-8").splitlines() if line]
            balance_rows = [json.loads(line) for line in balance_file.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(position_rows), 2)
            self.assertEqual(len(balance_rows), 2)
            self.assertEqual(position_rows[-1]["this_action"]["action"], "snapshot_b")
            self.assertEqual(balance_rows[-1]["positions"][0]["symbol"], "MSFT")


class OrderAndRiskTests(unittest.TestCase):
    def test_build_trade_plan_and_risk_guard(self):
        signals = [
            {
                "strategy": "news_momentum",
                "symbol": "NVDA",
                "action": "buy",
                "confidence": 0.8,
                "price": 100.0,
                "reason": "strong",
            },
            {
                "strategy": "news_momentum",
                "symbol": "AAPL",
                "action": "sell",
                "confidence": 0.9,
                "price": 50.0,
                "reason": "weak",
            },
        ]
        risk_cfg = {
            "max_position_pct": 0.1,
            "max_positions": 5,
            "max_trade_notional": 2000,
        }
        account = {"cash": 10000, "buying_power": 10000}
        positions = [{"symbol": "AAPL", "qty": 10}]

        built = build_trade_plan(
            signals=signals,
            risk_config=risk_cfg,
            account_snapshot=account,
            positions_snapshot=positions,
        )
        self.assertGreaterEqual(len(built["trade_plan"]), 1)

        guarded = apply_risk_guard(
            trade_plan=built["trade_plan"],
            risk_config=risk_cfg,
            account_snapshot=account,
            positions_snapshot=positions,
        )
        self.assertIn("accepted_plan", guarded)
        self.assertIn("rejections", guarded)

    def test_risk_guard_allows_add_on_same_symbol_when_under_limit(self):
        risk_cfg = {
            "max_position_pct": 0.2,   # 单标的上限 20%
            "max_positions": 5,
            "max_trade_notional": 2000,
        }
        account = {"cash": 5000, "buying_power": 5000, "equity": 10000}
        positions = [
            {"symbol": "TXN", "qty": 10, "current_price": 100},  # 现有约 $1000
        ]
        trade_plan = [
            {"action": "buy", "symbol": "TXN", "qty": 5, "notional_estimate": 500},
        ]

        guarded = apply_risk_guard(
            trade_plan=trade_plan,
            risk_config=risk_cfg,
            account_snapshot=account,
            positions_snapshot=positions,
        )
        self.assertEqual(len(guarded["accepted_plan"]), 1)
        self.assertEqual(len(guarded["rejections"]), 0)

    def test_risk_guard_rejects_add_on_same_symbol_when_exceed_limit(self):
        risk_cfg = {
            "max_position_pct": 0.1,   # 单标的上限 10%
            "max_positions": 5,
            "max_trade_notional": 2000,
        }
        account = {"cash": 5000, "buying_power": 5000, "equity": 10000}
        positions = [
            {"symbol": "TXN", "qty": 10, "current_price": 90},  # 现有约 $900
        ]
        trade_plan = [
            {"action": "buy", "symbol": "TXN", "qty": 2, "notional_estimate": 200},  # 累计 $1100 > $1000
        ]

        guarded = apply_risk_guard(
            trade_plan=trade_plan,
            risk_config=risk_cfg,
            account_snapshot=account,
            positions_snapshot=positions,
        )
        self.assertEqual(len(guarded["accepted_plan"]), 0)
        self.assertEqual(len(guarded["rejections"]), 1)
        self.assertIn("exceed_max_position_pct", guarded["rejections"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
