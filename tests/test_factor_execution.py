import hashlib
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from execute_alpaca_trade import validate_cash_long_only_order  # noqa: E402
from factor_portfolio import allocate_score_tilt  # noqa: E402
from recover_factor_execution_state import recover  # noqa: E402
from run_analysis_trade_pipeline import (  # noqa: E402
    _account_fingerprint,
    _assert_no_conflicting_open_orders,
    _build_factor_rebalance_plan,
    _execute_trade_plan,
    _exclusive_run_lock,
    _inspect_execution_journal_orders,
    _load_execution_journal,
    _load_factor_basket,
    _load_factor_execution_state,
    _load_or_create_state_key,
    _prepare_execution_journal,
    _run_pipeline,
    _state_hmac,
    _strategy_sleeve,
    validate_factor_execution,
)


V47_EFFECTIVE_CONFIG = {
    "holdings": 10,
    "max_names_per_industry": 3,
    "minimum_industry_count": 10,
    "minimum_adv20_usd": 10_000_000.0,
    "minimum_signal_rows": 450,
    "winsor_lower": 0.01,
    "winsor_upper": 0.99,
    "factor_lag_months": 2,
    "allocation_method": "score_tilt",
    "score_power": 6.0,
    "minimum_weight": 0.05,
    "maximum_weight": 0.20,
    "maximum_industry_weight": 0.35,
    "rebalance_frequency": "monthly",
    "weights": {
        "size": 0.10,
        "value": 0.30,
        "profitability": 0.10,
        "investment": 0.30,
        "momentum": 0.20,
    },
}


def v47_target(decision_date: str = "2026-08-12") -> dict:
    selected = [
        {
            "ticker": f"T{index:02d}",
            "security_id": f"SEC{index:02d}",
            "selection_rank": index,
            "score": 1.0 - (index - 1) * 0.05,
            "ff_industry_12": f"I{index}",
        }
        for index in range(1, 11)
    ]
    selected = allocate_score_tilt(
        selected,
        power=6.0,
        minimum_weight=0.05,
        maximum_weight=0.20,
        maximum_industry_weight=0.35,
    )
    config_hash = hashlib.sha256(
        json.dumps(V47_EFFECTIVE_CONFIG, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "method": "v4_7_factor_selection_score_tilt",
        "research_id": "v4_7_0001",
        "parameter_mode": "frozen",
        "decision_date": decision_date,
        "allocation_method": "score_tilt",
        "score_power": 6.0,
        "effective_config": V47_EFFECTIVE_CONFIG,
        "effective_config_sha256": config_hash,
        "predecessor_target": {
            "research_id": "v4_6_r1_0001",
            "sha256": "",
            "artifact_filename": "factor_portfolio_v4_6_r1_20260812.json",
        },
        "selected": selected,
    }


def write_target(root: Path, payload: dict | None = None) -> Path:
    target = payload or v47_target()
    predecessor = {
        "method": "v4_6_r1_factor_selection",
        "research_id": "v4_6_r1_0001",
        "parameter_mode": "frozen",
        "decision_date": target["decision_date"],
        "allocation_method": "equal_weight",
        "selected": [{**row, "target_weight": 0.1} for row in target["selected"]],
    }
    predecessor_path = root / target["predecessor_target"]["artifact_filename"]
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    target["predecessor_target"]["sha256"] = hashlib.sha256(
        predecessor_path.read_bytes()
    ).hexdigest()
    target_path = root / "factor_portfolio_v4_7_latest.json"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    return target_path


class FactorExecutionTests(unittest.TestCase):
    def test_loads_only_reproducible_frozen_v47_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_target(Path(tmp))
            basket = _load_factor_basket(
                path,
                execution_date="2026-08-13",
                expected_research_id="v4_7_0001",
                expected_holdings=10,
                maximum_age_days=40,
                approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                expected_method="v4_7_factor_selection_score_tilt",
                expected_allocation_method="score_tilt",
                expected_effective_config=V47_EFFECTIVE_CONFIG,
            )
        self.assertAlmostEqual(sum(basket["target_weights"].values()), 1.0)
        self.assertTrue(basket["members_match_predecessor"])

    def test_rejects_constraint_legal_but_noncanonical_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = v47_target()
            payload["selected"][2]["target_weight"] -= 0.001
            payload["selected"][3]["target_weight"] += 0.001
            path = write_target(root, payload)
            with self.assertRaisesRegex(RuntimeError, "projection"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-13",
                    expected_research_id="v4_7_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )

    def test_rejects_unapproved_or_stale_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_target(root, v47_target("2026-05-01"))
            with self.assertRaisesRegex(RuntimeError, "approved"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-13",
                    expected_research_id="v4_7_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256="a" * 64,
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )
            with self.assertRaisesRegex(RuntimeError, "stale"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-13",
                    expected_research_id="v4_7_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )

    def test_rebalance_uses_all_ten_positive_weights_and_sell_first(self):
        weights = {f"T{index:02d}": 0.1 for index in range(1, 11)}
        plan = _build_factor_rebalance_plan(
            account={"equity": 100_000},
            positions=[{"symbol": "OLD", "qty": 2, "current_price": 100}],
            prices={**{symbol: 100 for symbol in weights}, "OLD": 100},
            target_weights=weights,
        )
        self.assertEqual(plan[0]["action"], "sell")
        self.assertEqual({row["symbol"] for row in plan[1:]}, set(weights))

    def test_rebalance_ignores_de_minimis_drift(self):
        weights = {f"T{index:02d}": 0.1 for index in range(1, 11)}
        plan = _build_factor_rebalance_plan(
            account={"equity": 1_000},
            positions=[{"symbol": "T01", "qty": 0.999, "current_price": 100}],
            prices={symbol: 100 for symbol in weights},
            target_weights=weights,
        )
        self.assertNotIn("T01", {row["symbol"] for row in plan})

    def test_dedicated_account_adopts_target_position_and_rejects_foreign_symbol(self):
        sleeve = _strategy_sleeve(
            {"equity": 10_000, "cash": 9_000},
            [{"symbol": "T01", "qty": 10, "current_price": 100, "market_value": 1000}],
            {},
            target_symbols={"T01"},
            capital_allocation_usd=10_000,
        )
        self.assertEqual(sleeve["owned_quantities"], {"T01": 10.0})
        with self.assertRaisesRegex(RuntimeError, "unexpected position"):
            _strategy_sleeve(
                {"equity": 10_000, "cash": 9_000},
                [{"symbol": "MANUAL", "qty": 1, "current_price": 100}],
                {},
                target_symbols={"T01"},
                capital_allocation_usd=10_000,
            )

    def test_sleeve_uses_cash_not_buying_power_and_rejects_short(self):
        sleeve = _strategy_sleeve(
            {"equity": 100_000, "cash": 60_000, "buying_power": 400_000},
            [{"symbol": "T01", "qty": 200, "current_price": 100, "market_value": 20_000}],
            {"T01": 200},
            target_symbols={"T01"},
            capital_allocation_usd=100_000,
        )
        self.assertEqual(sleeve["notional"], 79_975)
        with self.assertRaisesRegex(RuntimeError, "short"):
            _strategy_sleeve(
                {"equity": 100_000, "cash": 100_000},
                [{"symbol": "T01", "qty": -1, "side": "short"}],
                {"T01": 1},
                target_symbols={"T01"},
                capital_allocation_usd=100_000,
            )

    def test_state_is_authenticated_and_bound_to_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            secret = "secret"
            payload = {
                "schema_version": 2,
                "strategy": "v4_7_top10_score_tilt",
                "account_fingerprint": _account_fingerprint("PAPER-A", secret),
                "owned_quantities": {"AMZN": 1.0},
                "target_decision_date": "2026-08-12",
                "target_artifact_sha256": "a" * 64,
            }
            payload["state_hmac_sha256"] = _state_hmac(payload, secret)
            path.write_text(json.dumps(payload), encoding="utf-8")
            state = _load_factor_execution_state(
                path,
                secret=secret,
                account_fingerprint=_account_fingerprint("PAPER-A", secret),
            )
            self.assertEqual(state["owned_quantities"], {"AMZN": 1.0})
            with self.assertRaisesRegex(RuntimeError, "different Alpaca account"):
                _load_factor_execution_state(
                    path,
                    secret=secret,
                    account_fingerprint=_account_fingerprint("PAPER-B", secret),
                )

    def test_state_key_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.key"
            self.assertEqual(_load_or_create_state_key(path), _load_or_create_state_key(path))

    @patch("run_analysis_trade_pipeline.subprocess.run")
    def test_partial_fill_does_not_block_later_orders(self, run_mock):
        run_mock.side_effect = [
            Mock(returncode=0, stdout='{"status":"partially_filled","filled_qty":22}', stderr=""),
            Mock(returncode=0, stdout='{"status":"new","filled_qty":0}', stderr=""),
        ]
        results = _execute_trade_plan(
            [
                {"action": "buy", "symbol": "AMZN", "qty": 23, "price": 268,
                 "client_order_id": "fv47-amzn"},
                {"action": "buy", "symbol": "GOOGL", "qty": 14, "price": 342,
                 "client_order_id": "fv47-googl"},
            ],
            require_paper=True,
            available_cash=100_000,
        )
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(len(results), 2)
        for call in run_mock.call_args_list:
            command = call.args[0]
            self.assertIn("--require-paper", command)
            self.assertIn("--require-cash-only", command)
            self.assertIn("--require-long-only", command)
            self.assertEqual(command[command.index("--wait-seconds") + 1], "0")

    @patch("run_analysis_trade_pipeline.subprocess.run")
    def test_execution_reserves_cash_across_all_inflight_buys(self, run_mock):
        run_mock.return_value = Mock(
            returncode=0, stdout='{"status":"new","filled_qty":0}', stderr=""
        )
        results = _execute_trade_plan(
            [
                {"action": "buy", "symbol": "T01", "qty": 1, "price": 60},
                {"action": "buy", "symbol": "T02", "qty": 1, "price": 60},
            ],
            require_paper=True,
            available_cash=100,
        )
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(results[1]["status"], "deferred_cash_reservation")

    def test_journal_is_authenticated_bound_and_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "journal.json"
            target_path = write_target(root)
            target_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            orders = _prepare_execution_journal(
                path,
                [
                    {"action": "buy", "symbol": "AMZN", "qty": 23, "price": 268},
                    {"action": "buy", "symbol": "GOOGL", "qty": 14, "price": 342},
                ],
                target_sha256=target_hash,
                execution_date="2026-08-13",
                strategy="v4_7_top10_score_tilt",
                target_artifact_path=target_path,
                target_method="v4_7_factor_selection_score_tilt",
                account_fingerprint="b" * 64,
                secret="secret",
            )
            loaded = _load_execution_journal(
                path,
                secret="secret",
                account_fingerprint="b" * 64,
                target_sha256=target_hash,
            )
            self.assertEqual(len(loaded["orders"]), 2)

            class FakeClient:
                def get_order_by_client_id(self, client_order_id):
                    if client_order_id == orders[0]["client_order_id"]:
                        return SimpleNamespace(
                            symbol="AMZN", side=SimpleNamespace(value="buy"),
                            status=SimpleNamespace(value="partially_filled"), qty="23",
                            filled_qty="22", filled_avg_price="267.98",
                            client_order_id=client_order_id,
                        )
                    error = RuntimeError("not found")
                    error.status_code = 404
                    raise error

            status = _inspect_execution_journal_orders(FakeClient(), orders)
            self.assertEqual([row["symbol"] for row in status["inflight"]], ["AMZN"])
            self.assertEqual([row["symbol"] for row in status["missing"]], ["GOOGL"])
            self.assertEqual(status["reserved_buy_notional"], 268)
            self.assertFalse(status["terminal"])

    def test_manual_open_order_conflict_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "open Alpaca order"):
            _assert_no_conflicting_open_orders(
                [{"symbol": "T01", "client_order_id": "manual"}], {"T01"}
            )
        with self.assertRaisesRegex(RuntimeError, "open Alpaca order"):
            _assert_no_conflicting_open_orders(
                [{"symbol": "FOREIGN", "client_order_id": "manual"}], {"T01"}
            )

    def test_stale_lock_file_does_not_block_a_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            lock_path = state_path.with_suffix(".json.lock")
            lock_path.write_text("999999999", encoding="ascii")
            with _exclusive_run_lock(state_path):
                self.assertTrue(lock_path.exists())
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with _exclusive_run_lock(state_path):
                        self.fail("overlapping execution lock was acquired")

    def test_cash_and_long_only_guards(self):
        with self.assertRaisesRegex(ValueError, "cash"):
            validate_cash_long_only_order(
                action="buy", qty=2, order_type="limit", limit_price=60,
                cash=100, current_long_qty=0,
            )
        with self.assertRaisesRegex(ValueError, "long position"):
            validate_cash_long_only_order(
                action="sell", qty=2, order_type="market", limit_price=None,
                cash=100, current_long_qty=1,
            )

    def test_live_execution_is_paper_only(self):
        with self.assertRaisesRegex(ValueError, "Paper"):
            validate_factor_execution(execute_trades=True, paper_only=True, alpaca_paper=False)

    @patch("recover_factor_execution_state._load_account")
    @patch("recover_factor_execution_state.get_open_orders", return_value=[])
    @patch("recover_factor_execution_state.get_alpaca_client", return_value=Mock())
    def test_explicit_recovery_rebinds_only_approved_positions(
        self, _client_mock, _orders_mock, account_mock
    ):
        account_mock.return_value = (
            {"account_number": "PAPER-RECOVERY", "equity": 100_000, "cash": 90_000},
            [{"symbol": "T01", "qty": 10, "side": "long"}],
            {"status": "fresh"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = write_target(root)
            target_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            config = {
                "alpaca": {"api_key": "key", "secret_key": "secret", "paper": True},
                "factor_portfolio": V47_EFFECTIVE_CONFIG,
                "factor_execution": {
                    "target_path": str(target_path),
                    "approved_target_sha256": target_hash,
                    "state_path": str(root / "state.json"),
                    "state_key_path": str(root / "state.key"),
                    "journal_path": str(root / "journal.json"),
                },
            }
            with self.assertRaisesRegex(RuntimeError, "confirmation"):
                recover(config, "0" * 64)
            result = recover(config, target_hash)
            self.assertEqual(result["owned_quantities"], {"T01": 10.0})
            self.assertTrue((root / "state.json").exists())

    @patch("run_analysis_trade_pipeline._load_prices")
    @patch("run_analysis_trade_pipeline._load_account")
    def test_pipeline_dry_run_uses_v47_and_alpaca_only(self, account_mock, prices_mock):
        account_mock.return_value = (
            {
                "account_number": "PAPER-TEST",
                "equity": 100_000,
                "portfolio_value": 100_000,
                "cash": 100_000,
            },
            [],
            {"status": "local_snapshot"},
        )
        prices_mock.return_value = {f"T{index:02d}": 100.0 for index in range(1, 11)}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = write_target(root)
            args = Namespace(
                strategy="factor-v4.7",
                execute_trades=False,
                skip_account_refresh=True,
                execution_date="2026-08-13",
                output_file=str(root / "result.json"),
            )
            output, _ = _run_pipeline(
                args,
                config={"alpaca": {"paper": True}},
                selection_config={
                    **V47_EFFECTIVE_CONFIG,
                    "enabled": True,
                    "mode": "v4_7_top10_score_tilt",
                    "research_id": "v4_7_0001",
                    "target_method": "v4_7_factor_selection_score_tilt",
                    "execution_strategy": "factor-v4.7",
                },
                execution_config={
                    "enabled": True,
                    "target_path": str(target_path),
                    "approved_target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                    "maximum_target_age_days": 40,
                    "paper_only": True,
                    "capital_allocation_usd": 100_000,
                    "journal_path": str(root / "journal.json"),
                    "state_key_path": str(root / "state.key"),
                },
                state_path=root / "state.json",
            )
        self.assertEqual(output["strategy"], "factor-v4.7")
        self.assertEqual(output["market_data_provider"], "alpaca")
        self.assertNotIn("data_sync", output)
        self.assertEqual(len(output["trade_plan"]), 10)

    @patch("run_analysis_trade_pipeline.get_open_orders", return_value=[])
    @patch("run_analysis_trade_pipeline.get_alpaca_client")
    @patch("run_analysis_trade_pipeline._load_prices")
    @patch("run_analysis_trade_pipeline._load_account")
    def test_terminal_journal_refreshes_positions_before_settlement(
        self, account_mock, prices_mock, client_mock, _open_orders_mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = write_target(root)
            target = json.loads(target_path.read_text(encoding="utf-8"))
            first = target["selected"][0]
            target_qty = round(99_975 * float(first["target_weight"]) / 100, 6)
            state_key_path = root / "state.key"
            secret = _load_or_create_state_key(state_key_path)
            fingerprint = _account_fingerprint("PAPER-TEST", secret)
            journal_path = root / "journal.json"
            journal_orders = _prepare_execution_journal(
                journal_path,
                [{"action": "buy", "symbol": "T01", "qty": target_qty, "price": 100}],
                target_sha256=hashlib.sha256(target_path.read_bytes()).hexdigest(),
                execution_date="2026-08-13",
                strategy="v4_7_top10_score_tilt",
                target_artifact_path=target_path,
                target_method="v4_7_factor_selection_score_tilt",
                account_fingerprint=fingerprint,
                secret=secret,
            )
            current_payload = v47_target("2026-08-13")
            target_path = write_target(root, current_payload)
            current_target_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            self.assertNotEqual(
                current_target_hash,
                json.loads(journal_path.read_text(encoding="utf-8"))["target_sha256"],
            )

            account = {
                "account_number": "PAPER-TEST",
                "equity": 100_000,
                "portfolio_value": 100_000,
                "cash": 100_000,
            }
            filled_position = {
                "symbol": "T01",
                "qty": target_qty,
                "current_price": 100,
                "market_value": target_qty * 100,
            }
            account_mock.side_effect = [
                (account, [], {"status": "stale_snapshot"}),
                (
                    {**account, "cash": 100_000 - target_qty * 100},
                    [filled_position],
                    {"status": "refreshed_after_settlement"},
                ),
            ]
            prices_mock.return_value = {f"T{index:02d}": 100.0 for index in range(1, 11)}

            broker_order = SimpleNamespace(
                symbol="T01",
                side=SimpleNamespace(value="buy"),
                status=SimpleNamespace(value="filled"),
                qty=str(target_qty),
                filled_qty=str(target_qty),
                filled_avg_price="100",
                client_order_id=journal_orders[0]["client_order_id"],
            )
            broker = Mock()
            broker.get_order_by_client_id.return_value = broker_order
            client_mock.return_value = broker

            args = Namespace(
                strategy="factor-v4.7",
                execute_trades=False,
                skip_account_refresh=True,
                execution_date="2026-08-13",
                output_file=str(root / "result.json"),
            )
            output, _ = _run_pipeline(
                args,
                config={"alpaca": {"paper": True}},
                selection_config={
                    **V47_EFFECTIVE_CONFIG,
                    "enabled": True,
                    "mode": "v4_7_top10_score_tilt",
                    "research_id": "v4_7_0001",
                    "target_method": "v4_7_factor_selection_score_tilt",
                    "execution_strategy": "factor-v4.7",
                },
                execution_config={
                    "enabled": True,
                    "target_path": str(target_path),
                    "approved_target_sha256": current_target_hash,
                    "maximum_target_age_days": 40,
                    "paper_only": True,
                    "capital_allocation_usd": 100_000,
                    "journal_path": str(journal_path),
                    "state_key_path": str(state_key_path),
                },
                state_path=root / "state.json",
            )

        self.assertEqual(account_mock.call_count, 2)
        self.assertNotIn("T01", {row["symbol"] for row in output["trade_plan"]})
        self.assertEqual(output["journal_status"]["status"], "settled")
        self.assertEqual(output["factor_basket"]["decision_date"], "2026-08-13")


if __name__ == "__main__":
    unittest.main()
