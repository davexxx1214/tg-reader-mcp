import hashlib
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from run_analysis_trade_pipeline import (  # noqa: E402
    _assert_no_conflicting_open_orders,
    _execute_trade_plan,
    _load_factor_execution_state,
    _load_factor_basket,
    _prepare_execution_journal,
    _load_or_create_state_key,
    _confirmed_owned_quantities,
    _run_pipeline,
    _strategy_sleeve,
    _state_hmac,
    _owned_quantities_after_fills,
    _validate_factor_target_transition,
    validate_factor_execution,
)
from taco_strategy import build_rebalance_plan  # noqa: E402
from execute_alpaca_trade import validate_cash_long_only_order  # noqa: E402


def factor_target(decision_date: str = "2026-07-31"):
    return {
        "method": "v4_6_r1_factor_selection",
        "research_id": "v4_6_r1_0001",
        "parameter_mode": "frozen",
        "decision_date": decision_date,
        "allocation_method": "equal_weight",
        "selected": [
            {"ticker": f"T{index:02d}", "security_id": f"SEC{index:02d}", "selection_rank": index}
            for index in range(1, 11)
        ],
    }


class FactorBasketExecutionTests(unittest.TestCase):
    def test_loads_frozen_monthly_basket_as_ten_equal_base_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            path.write_text(json.dumps(factor_target()), encoding="utf-8")
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            basket = _load_factor_basket(
                path,
                execution_date="2026-08-12",
                expected_research_id="v4_6_r1_0001",
                expected_holdings=10,
                maximum_age_days=40,
                approved_sha256=expected_hash,
            )
        self.assertEqual(len(basket["base_weights"]), 10)
        self.assertEqual(set(basket["base_weights"].values()), {0.1})
        self.assertEqual(
            basket["artifact_sha256"],
            expected_hash,
        )

    def test_rejects_stale_factor_basket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            path.write_text(json.dumps(factor_target("2026-05-31")), encoding="utf-8")
            approved_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "stale"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-12",
                    expected_research_id="v4_6_r1_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256=approved_hash,
                )

    def test_rejects_target_from_another_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            payload = factor_target()
            payload["method"] = "unknown"
            path.write_text(json.dumps(payload), encoding="utf-8")
            approved_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "method"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-12",
                    expected_research_id="v4_6_r1_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256=approved_hash,
                )

    def test_rejects_unapproved_target_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            path.write_text(json.dumps(factor_target()), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "approved"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-12",
                    expected_research_id="v4_6_r1_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256="b" * 64,
                )

    def test_v46r1_basket_is_always_fully_invested(self):
        base = {f"T{index:02d}": 0.1 for index in range(1, 11)}
        self.assertEqual(set(base.values()), {0.1})
        self.assertAlmostEqual(sum(base.values()), 1.0)

    def test_factor_rebalance_uses_fractional_shares_for_expensive_names(self):
        plan = build_rebalance_plan(
            account={"equity": 10_000},
            positions=[],
            prices={"HIGH": 4_000},
            target_weights={"HIGH": 0.08},
            fractional=True,
        )
        self.assertEqual(plan[0]["qty"], 0.2)

    def test_strategy_sleeve_excludes_unmanaged_positions_and_value(self):
        positions = [
            {"symbol": "QQQ", "qty": 10, "current_price": 500, "market_value": 5000},
            {"symbol": "MANUAL", "qty": 20, "current_price": 100, "market_value": 2000},
        ]
        sleeve = _strategy_sleeve(
            {"equity": 17_000, "cash": 10_000},
            positions,
            {},
            initial_legacy_symbols={"QQQ"},
            target_symbols={"T01"},
            capital_allocation_usd=15_000,
        )
        self.assertEqual([row["symbol"] for row in sleeve["positions"]], ["QQQ"])
        self.assertEqual(sleeve["notional"], 15_000)
        self.assertAlmostEqual(sleeve["current_exposure"], 1 / 3)

    def test_strategy_sleeve_never_uses_buying_power_or_borrowed_funds(self):
        sleeve = _strategy_sleeve(
            {"equity": 100_000, "cash": 60_000, "buying_power": 400_000},
            [{"symbol": "OLD", "qty": 200, "current_price": 100, "market_value": 20_000}],
            {"OLD": 200},
            target_symbols={"T01"},
            capital_allocation_usd=100_000,
        )
        self.assertEqual(sleeve["notional"], 80_000)

    def test_strategy_sleeve_rejects_short_positions(self):
        with self.assertRaisesRegex(RuntimeError, "short"):
            _strategy_sleeve(
                {"equity": 100_000, "cash": 100_000},
                [{
                    "symbol": "OLD", "qty": -10, "side": "short",
                    "current_price": 100, "market_value": -1_000,
                }],
                {"OLD": 10},
                target_symbols={"T01"},
                capital_allocation_usd=100_000,
            )

    def test_target_collision_with_manual_position_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "collides"):
            _strategy_sleeve(
                {"equity": 10_000, "cash": 9_000},
                [{"symbol": "T01", "qty": 10, "current_price": 100, "market_value": 1000}],
                {},
                target_symbols={"T01"},
                capital_allocation_usd=5_000,
            )

    def test_missing_broker_position_cannot_remain_ghost_owned(self):
        with self.assertRaisesRegex(RuntimeError, "missing at Alpaca"):
            _strategy_sleeve(
                {"equity": 10_000, "cash": 10_000},
                [],
                {"OLD": 2.0},
                target_symbols={"T01"},
                capital_allocation_usd=5_000,
            )

    def test_state_key_is_stable_and_separate_from_broker_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.key"
            first = _load_or_create_state_key(path)
            second = _load_or_create_state_key(path)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            stored = path.read_text(encoding="ascii").strip()
            if sys.platform == "win32":
                self.assertTrue(stored.startswith("dpapi-v1:"))
                self.assertNotIn(first, stored)

    def test_post_trade_state_uses_confirmed_broker_quantities(self):
        confirmed = _confirmed_owned_quantities(
            [
                {"symbol": "T01", "qty": 1.25},
                {"symbol": "OLD", "qty": 0.001},
                {"symbol": "MANUAL", "qty": 99},
            ],
            {"T01", "OLD"},
        )
        self.assertEqual(confirmed, {"T01": 1.25, "OLD": 0.001})

    def test_live_execution_is_restricted_to_alpaca_paper(self):
        with self.assertRaisesRegex(ValueError, "Paper"):
            validate_factor_execution(
                execute_trades=True,
                paper_only=True,
                alpaca_paper=False,
            )

    def test_factor_execution_cannot_disable_paper_only(self):
        with self.assertRaisesRegex(ValueError, "Paper-only"):
            validate_factor_execution(
                execute_trades=False,
                paper_only=False,
                alpaca_paper=True,
            )

    def test_conflicting_open_order_on_target_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "open Alpaca order"):
            _assert_no_conflicting_open_orders(
                [{"symbol": "T01", "client_order_id": "manual-123"}],
                {"T01", "OLD"},
            )

    def test_only_own_fills_are_added_to_ownership_ledger(self):
        owned = _owned_quantities_after_fills(
            {"OLD": 3.0},
            [
                {"action": "sell", "symbol": "OLD", "qty": 3.0},
                {"action": "buy", "symbol": "T01", "qty": 2.0},
            ],
            [
                {"status": "ok", "trade": {"status": "filled", "filled_qty": 3.0}},
                {"status": "ok", "trade": {"status": "filled", "filled_qty": 2.0}},
            ],
            [
                {"symbol": "T01", "qty": 7.0},  # includes 5 manually acquired shares
            ],
        )
        self.assertEqual(owned, {"T01": 2.0})

    def test_previous_factor_names_remain_managed_for_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = {
                        "schema_version": 1,
                        "strategy": "v4_6_r1_top10",
                        "owned_quantities": {"OLD": 3.0, "T01": 2.0},
                        "target_decision_date": "2026-07-31",
                        "target_artifact_sha256": "a" * 64,
                    }
            payload["state_hmac_sha256"] = _state_hmac(payload, "secret")
            path.write_text(json.dumps(payload), encoding="utf-8")
            state = _load_factor_execution_state(path, secret="secret")
        self.assertEqual(state["owned_quantities"], {"OLD": 3.0, "T01": 2.0})

    def test_same_month_factor_target_cannot_change_after_execution(self):
        state = {
            "target_decision_date": "2026-07-31",
            "target_artifact_sha256": "a" * 64,
        }
        basket = {"decision_date": "2026-07-31", "artifact_sha256": "b" * 64}
        with self.assertRaisesRegex(RuntimeError, "changed"):
            _validate_factor_target_transition(basket, state)

    def test_factor_target_date_cannot_roll_back(self):
        state = {
            "target_decision_date": "2026-07-31",
            "target_artifact_sha256": "a" * 64,
        }
        basket = {"decision_date": "2026-06-30", "artifact_sha256": "b" * 64}
        with self.assertRaisesRegex(RuntimeError, "older"):
            _validate_factor_target_transition(basket, state)

    @patch("run_analysis_trade_pipeline.subprocess.run")
    def test_each_order_carries_the_paper_only_guard(self, run_mock):
        run_mock.return_value = Mock(
            returncode=0,
            stdout='{"status":"filled","symbol":"T01"}',
            stderr="",
        )
        _execute_trade_plan(
            [{
                "action": "buy", "symbol": "T01", "qty": 1,
                "price": 100, "client_order_id": "fv46-test",
            }],
            require_paper=True,
        )
        command = run_mock.call_args.args[0]
        self.assertIn("--require-paper", command)
        self.assertIn("--client-order-id", command)

    @patch("run_analysis_trade_pipeline.subprocess.run")
    def test_factor_buy_is_cash_only_limit_order(self, run_mock):
        run_mock.return_value = Mock(
            returncode=0,
            stdout='{"status":"filled","symbol":"T01","filled_qty":1}',
            stderr="",
        )
        _execute_trade_plan(
            [{
                "action": "buy", "symbol": "T01", "qty": 1,
                "price": 100, "client_order_id": "fv46-test-buy",
            }],
            require_paper=True,
        )
        command = run_mock.call_args.args[0]
        self.assertIn("--require-cash-only", command)
        self.assertIn("--require-long-only", command)
        self.assertEqual(command[command.index("--order-type") + 1], "limit")
        self.assertEqual(command[command.index("--limit-price") + 1], "100.0")

    def test_cash_only_guard_rejects_buy_above_cash(self):
        with self.assertRaisesRegex(ValueError, "cash"):
            validate_cash_long_only_order(
                action="buy", qty=2, order_type="limit", limit_price=60,
                cash=100, current_long_qty=0,
            )

    def test_long_only_guard_rejects_oversell(self):
        with self.assertRaisesRegex(ValueError, "long position"):
            validate_cash_long_only_order(
                action="sell", qty=2, order_type="market", limit_price=None,
                cash=100, current_long_qty=1,
            )

    def test_corrupt_existing_factor_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "state"):
                _load_factor_execution_state(path, secret="secret")

    def test_tampered_execution_state_fails_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = {
                "schema_version": 1,
                "strategy": "v4_6_r1_top10",
                "owned_quantities": {"T01": 1.0},
                "target_decision_date": "2026-07-31",
                "target_artifact_sha256": "a" * 64,
            }
            payload["state_hmac_sha256"] = _state_hmac(payload, "secret")
            payload["owned_quantities"]["MANUAL"] = 20.0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                _load_factor_execution_state(path, secret="secret")

    def test_existing_execution_journal_blocks_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            plan = [{"action": "buy", "symbol": "T01", "qty": 2}]
            first = _prepare_execution_journal(
                path, plan, target_sha256="a" * 64, execution_date="2026-08-12"
            )
            self.assertTrue(path.exists())
            self.assertTrue(first[0]["client_order_id"].startswith("fv46-"))
            first_id = first[0]["client_order_id"]
            with self.assertRaisesRegex(RuntimeError, "journal"):
                _prepare_execution_journal(
                    path, plan, target_sha256="a" * 64, execution_date="2026-08-12"
                )
            path.unlink()
            changed = _prepare_execution_journal(
                path,
                [{"action": "buy", "symbol": "T01", "qty": 3}],
                target_sha256="a" * 64,
                execution_date="2026-08-12",
            )
            self.assertNotEqual(first_id, changed[0]["client_order_id"])

    @patch("run_analysis_trade_pipeline._load_prices")
    @patch("run_analysis_trade_pipeline._load_account")
    def test_pipeline_trades_ten_factor_names_and_preserves_unmanaged_position(
        self, account_mock, prices_mock
    ):
        account_mock.return_value = (
            {"equity": 17_000, "portfolio_value": 17_000, "cash": 10_000},
            [
                {"symbol": "QQQ", "qty": 10, "current_price": 500, "market_value": 5000},
                {"symbol": "MANUAL", "qty": 20, "current_price": 100, "market_value": 2000},
            ],
            {"status": "local_snapshot"},
        )
        prices_mock.return_value = {
            **{f"T{index:02d}": 100.0 for index in range(1, 11)},
            "QQQ": 500.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = root / "factor.json"
            target_path.write_text(json.dumps(factor_target()), encoding="utf-8")
            approved_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            args = Namespace(
                execute_trades=False,
                skip_account_refresh=True,
                skip_data_sync=True,
                skip_price_sync=True,
                av_calls_per_minute=75,
                execution_date="2026-08-12",
                output_file=str(root / "result.json"),
            )
            output, _ = _run_pipeline(
                args,
                config={"alpaca": {"paper": True, "secret_key": "secret"}},
                selection_config={
                    "enabled": True,
                    "research_id": "v4_6_r1_0001",
                    "holdings": 10,
                },
                execution_config={
                    "enabled": True,
                    "target_path": str(target_path),
                    "approved_target_sha256": approved_hash,
                    "maximum_target_age_days": 40,
                    "legacy_managed_symbols": [],
                    "paper_only": True,
                    "capital_allocation_usd": 15_000,
                    "journal_path": str(root / "journal.json"),
                    "state_key_path": str(root / "state.key"),
                },
                state_path=root / "state.json",
            )
        self.assertEqual(len(output["target_weights"]), 10)
        self.assertEqual(set(output["target_weights"].values()), {0.1})
        self.assertNotIn("signal", output)
        self.assertNotIn("taco", output["data_sync"])
        self.assertNotIn("QQQ", output["target_weights"])
        planned_symbols = {row["symbol"] for row in output["trade_plan"]}
        self.assertNotIn("QQQ", planned_symbols)
        self.assertNotIn("MANUAL", planned_symbols)


if __name__ == "__main__":
    unittest.main()
