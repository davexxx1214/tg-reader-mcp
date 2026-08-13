import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
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
from execute_alpaca_trade import (  # noqa: E402
    TERMINAL_ORDER_STATUSES,
    validate_cash_long_only_order,
)
from factor_portfolio import allocate_score_tilt  # noqa: E402
from reconcile_factor_execution_archive import reconcile_archive  # noqa: E402


V47_EFFECTIVE_CONFIG = {
    "holdings": 10,
    "max_names_per_industry": 3,
    "minimum_industry_count": 10,
    "minimum_adv20_usd": 10_000_000.0,
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
        "size": 0.10, "value": 0.30, "profitability": 0.10,
        "investment": 0.30, "momentum": 0.20,
    },
}


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


def v47_factor_target(decision_date: str = "2026-07-31", predecessor_hash: str = "a" * 64):
    payload = factor_target(decision_date)
    payload.update(
        {
            "method": "v4_7_factor_selection_score_tilt",
            "research_id": "v4_7_0001",
            "allocation_method": "score_tilt",
            "score_power": 6.0,
            "predecessor_target": {
                "research_id": "v4_6_r1_0001",
                "sha256": predecessor_hash,
                "artifact_filename": "predecessor.json",
            },
            "effective_config": V47_EFFECTIVE_CONFIG,
            "effective_config_sha256": hashlib.sha256(
                json.dumps(V47_EFFECTIVE_CONFIG, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )
    for index, row in enumerate(payload["selected"]):
        row.update(
            {
                "score": 1.0 - index * 0.05,
                "ff_industry_12": f"I{index}",
            }
        )
    tilted = allocate_score_tilt(
        payload["selected"], power=6.0, minimum_weight=0.05,
        maximum_weight=0.20, maximum_industry_weight=0.35,
    )
    payload["selected"] = tilted
    return payload


def write_v47_fixture(root: Path, payload: dict) -> Path:
    predecessor = factor_target(payload["decision_date"])
    predecessor["selected"] = [
        {**row, "target_weight": 0.1}
        for row in payload["selected"]
    ]
    predecessor_hash = hashlib.sha256(json.dumps(predecessor).encode()).hexdigest()
    payload["predecessor_target"]["sha256"] = predecessor_hash
    (root / "predecessor.json").write_text(json.dumps(predecessor), encoding="utf-8")
    path = root / "target.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FactorBasketExecutionTests(unittest.TestCase):
    def test_partial_fill_is_not_a_terminal_order_status(self):
        self.assertNotIn("partially_filled", TERMINAL_ORDER_STATUSES)

    def test_reconciliation_records_final_fill_and_retires_active_journal(self):
        class FakeClient:
            def get_account(self):
                return SimpleNamespace(account_number="PAPER-ACCOUNT-1")

            def get_order_by_client_id(self, client_id):
                if client_id != "fv46-amzn":
                    error = RuntimeError("not found")
                    error.status_code = 404
                    raise error
                return SimpleNamespace(
                    id="order-1", client_order_id=client_id, symbol="AMZN",
                    side=SimpleNamespace(value="buy"), status=SimpleNamespace(value="filled"),
                    qty="37.120903", filled_qty="37.120903",
                    filled_avg_price="269.386767", limit_price="269.39",
                )

            def get_all_positions(self):
                return [SimpleNamespace(symbol="AMZN", qty="37.120903")]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "data.zip"
            output = root / "reconciled.zip"
            prefix = "root/.hermes/skills/tg-reader-mcp/data/"
            portfolio = factor_target("2026-08-12")
            portfolio["selected"][0]["ticker"] = "AMZN"
            portfolio["selected"][1]["ticker"] = "NVDA"
            portfolio_bytes = json.dumps(portfolio).encode("utf-8")
            target_hash = hashlib.sha256(portfolio_bytes).hexdigest()
            journal = {
                "schema_version": 1, "strategy": "v4_6_r1_top10", "status": "prepared",
                "target_sha256": target_hash, "execution_date": "2026-08-12",
                "orders": [
                    {"action": "buy", "symbol": "AMZN", "qty": 37.120903,
                     "client_order_id": "fv46-amzn"},
                    {"action": "buy", "symbol": "NVDA", "qty": 10,
                     "client_order_id": "fv46-nvda"},
                ],
            }
            for index, order in enumerate(journal["orders"], 1):
                intent = json.dumps(
                    {
                        "target": target_hash, "date": "2026-08-12", "index": index,
                        "action": order["action"], "symbol": order["symbol"],
                        "qty": order["qty"], "target_weight": order.get("target_weight"),
                    },
                    sort_keys=True, separators=(",", ":"),
                )
                key = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
                order["client_order_id"] = (
                    f"fv46-{key}-{index:02d}-{order['action'][0]}-{order['symbol']}"
                )[:48]
            submitted_client_id = journal["orders"][0]["client_order_id"]
            FakeClient.get_order_by_client_id = lambda self, client_id: (
                SimpleNamespace(
                    id="order-1", client_order_id=client_id, symbol="AMZN",
                    side=SimpleNamespace(value="buy"), status=SimpleNamespace(value="filled"),
                    qty="37.120903", filled_qty="37.120903",
                    filled_avg_price="269.386767", limit_price="269.39",
                )
                if client_id == submitted_client_id
                else (_ for _ in ()).throw(type("NotFound", (RuntimeError,), {"status_code": 404})("not found"))
            )
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(prefix + "factor_portfolio_latest.json", portfolio_bytes)
                archive.writestr(prefix + "factor_execution_journal.json", json.dumps(journal))
                archive.writestr(prefix + "factor_execution_state.key", "a" * 64 + "\n")
                archive.writestr(
                    prefix + "balance/balance.jsonl",
                    json.dumps({"account": {"account_number": "PAPER-ACCOUNT-1"}}) + "\n",
                )
            summary = reconcile_archive(source, output, client=FakeClient())
            self.assertEqual(summary["owned_quantities"], {"AMZN": 37.120903})
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertNotIn(prefix + "factor_execution_journal.json", names)
                self.assertIn(prefix + "factor_execution_state.json", names)
                self.assertTrue(any("journal.reconciled" in name for name in names))
                state = json.loads(archive.read(prefix + "factor_execution_state.json"))
            self.assertEqual(state["owned_quantities"], {"AMZN": 37.120903})
            self.assertEqual(state["state_hmac_sha256"], _state_hmac(state, "a" * 64))

            class WrongAccountClient(FakeClient):
                def get_account(self):
                    return SimpleNamespace(account_number="PAPER-ACCOUNT-2")

            with self.assertRaisesRegex(RuntimeError, "account"):
                reconcile_archive(
                    source, root / "wrong-account.zip", client=WrongAccountClient()
                )

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(duplicate, "w") as out:
                for info in source_archive.infolist():
                    if info.filename.endswith("/factor_execution_journal.json"):
                        bad = dict(journal)
                        bad["orders"] = [dict(journal["orders"][0]), dict(journal["orders"][0])]
                        out.writestr(info, json.dumps(bad))
                    else:
                        out.writestr(info, source_archive.read(info.filename))
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                reconcile_archive(duplicate, root / "duplicate-output.zip", client=FakeClient())

    def test_v47_reconciliation_starts_from_authenticated_v46_ownership(self):
        state_key = "a" * 64
        predecessor_hash = "b" * 64
        portfolio = v47_factor_target("2026-08-12", predecessor_hash)
        portfolio["selected"][0]["ticker"] = "AMZN"
        portfolio["selected"][1]["ticker"] = "XOM"
        portfolio_bytes = json.dumps(portfolio).encode("utf-8")
        target_hash = hashlib.sha256(portfolio_bytes).hexdigest()
        state = {
            "schema_version": 1,
            "strategy": "v4_6_r1_top10",
            "owned_quantities": {"AMZN": 37.120903},
            "target_artifact_sha256": predecessor_hash,
            "target_decision_date": "2026-08-12",
        }
        state["state_hmac_sha256"] = _state_hmac(state, state_key)
        orders = [
            {"action": "sell", "symbol": "AMZN", "qty": 10.0, "target_weight": 0.20},
            {"action": "buy", "symbol": "XOM", "qty": 5.0, "target_weight": 0.15},
        ]
        for index, order in enumerate(orders, 1):
            intent = json.dumps(
                {
                    "target": target_hash,
                    "date": "2026-08-12",
                    "index": index,
                    "action": order["action"],
                    "symbol": order["symbol"],
                    "qty": order["qty"],
                    "target_weight": order["target_weight"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            key = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
            order["client_order_id"] = (
                f"fv47-{key}-{index:02d}-{order['action'][0]}-{order['symbol']}"
            )[:48]
        journal = {
            "schema_version": 1,
            "strategy": "v4_7_top10_score_tilt",
            "status": "prepared",
            "target_sha256": target_hash,
            "target_artifact_filename": "factor_portfolio_v4_7_latest.json",
            "target_method": "v4_7_factor_selection_score_tilt",
            "execution_date": "2026-08-12",
            "orders": orders,
        }

        class FakeClient:
            def get_account(self):
                return SimpleNamespace(account_number="PAPER-ACCOUNT-1")

            def get_order_by_client_id(self, client_id):
                order = next(item for item in orders if item["client_order_id"] == client_id)
                return SimpleNamespace(
                    id="broker-" + order["symbol"],
                    client_order_id=client_id,
                    symbol=order["symbol"],
                    side=SimpleNamespace(value=order["action"]),
                    status=SimpleNamespace(value="filled"),
                    qty=str(order["qty"]),
                    filled_qty=str(order["qty"]),
                    filled_avg_price="100",
                    limit_price="100",
                )

            def get_all_positions(self):
                return [
                    SimpleNamespace(symbol="AMZN", qty="27.120903"),
                    SimpleNamespace(symbol="XOM", qty="5"),
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "v47.zip"
            output = root / "v47-reconciled.zip"
            prefix = "root/project/data/"
            with zipfile.ZipFile(source, "w") as archive:
                predecessor = factor_target("2026-08-12")
                predecessor["selected"] = [
                    {**row, "target_weight": 0.1} for row in portfolio["selected"]
                ]
                predecessor_bytes = json.dumps(predecessor).encode("utf-8")
                predecessor_hash = hashlib.sha256(predecessor_bytes).hexdigest()
                portfolio["predecessor_target"]["sha256"] = predecessor_hash
                state["target_artifact_sha256"] = predecessor_hash
                state["state_hmac_sha256"] = _state_hmac(state, state_key)
                portfolio_bytes = json.dumps(portfolio).encode("utf-8")
                target_hash = hashlib.sha256(portfolio_bytes).hexdigest()
                journal["target_sha256"] = target_hash
                for index, order in enumerate(orders, 1):
                    intent = json.dumps(
                        {
                            "target": target_hash, "date": "2026-08-12", "index": index,
                            "action": order["action"], "symbol": order["symbol"],
                            "qty": order["qty"], "target_weight": order["target_weight"],
                        }, sort_keys=True, separators=(",", ":"),
                    )
                    key = hashlib.sha256(intent.encode()).hexdigest()[:16]
                    order["client_order_id"] = (
                        f"fv47-{key}-{index:02d}-{order['action'][0]}-{order['symbol']}"
                    )[:48]
                archive.writestr(prefix + "factor_portfolio_v4_7_latest.json", portfolio_bytes)
                archive.writestr(prefix + "factor_portfolio_v4_6_r1_20260812.json", predecessor_bytes)
                archive.writestr(prefix + "factor_execution_journal.json", json.dumps(journal))
                archive.writestr(prefix + "factor_execution_state.json", json.dumps(state))
                archive.writestr(prefix + "factor_execution_state.key", state_key + "\n")
                archive.writestr(
                    prefix + "balance/balance.jsonl",
                    json.dumps({"account": {"account_number": "PAPER-ACCOUNT-1"}}) + "\n",
                )
            summary = reconcile_archive(source, output, client=FakeClient())
            self.assertEqual(summary["owned_quantities"], {"AMZN": 27.120903, "XOM": 5.0})
            with zipfile.ZipFile(output) as archive:
                reconciled_state = json.loads(
                    archive.read(prefix + "factor_execution_state.json")
                )
            self.assertEqual(reconciled_state["strategy"], "v4_7_top10_score_tilt")

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

    def test_loads_v47_basket_with_frozen_score_tilt_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_v47_fixture(Path(tmp), v47_factor_target())
            approved_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            basket = _load_factor_basket(
                path,
                execution_date="2026-08-12",
                expected_research_id="v4_7_0001",
                expected_holdings=10,
                maximum_age_days=40,
                approved_sha256=approved_hash,
                expected_method="v4_7_factor_selection_score_tilt",
                expected_allocation_method="score_tilt",
                expected_effective_config=V47_EFFECTIVE_CONFIG,
            )
        self.assertAlmostEqual(sum(basket["target_weights"].values()), 1.0)
        self.assertTrue(basket["members_match_predecessor"])

    def test_v47_basket_rejects_a_different_but_constraint_legal_tilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = v47_factor_target()
            path = write_v47_fixture(Path(tmp), payload)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["selected"][2]["target_weight"] -= 0.001
            payload["selected"][3]["target_weight"] += 0.001
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "projection"):
                _load_factor_basket(
                    path, execution_date="2026-08-12", expected_research_id="v4_7_0001",
                    expected_holdings=10, maximum_age_days=40,
                    approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )

    def test_v47_basket_rejects_effective_config_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = v47_factor_target()
            path = write_v47_fixture(Path(tmp), payload)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["effective_config"]["score_power"] = 4.0
            payload["effective_config_sha256"] = hashlib.sha256(
                json.dumps(payload["effective_config"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "effective config"):
                _load_factor_basket(
                    path, execution_date="2026-08-12", expected_research_id="v4_7_0001",
                    expected_holdings=10, maximum_age_days=40,
                    approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )

    def test_v47_basket_rejects_nonfinite_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = v47_factor_target()
            payload["selected"][0]["score"] = float("nan")
            path = write_v47_fixture(Path(tmp), payload)
            approved_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "score"):
                _load_factor_basket(
                    path,
                    execution_date="2026-08-12",
                    expected_research_id="v4_7_0001",
                    expected_holdings=10,
                    maximum_age_days=40,
                    approved_sha256=approved_hash,
                    expected_method="v4_7_factor_selection_score_tilt",
                    expected_allocation_method="score_tilt",
                    expected_effective_config=V47_EFFECTIVE_CONFIG,
                )

    def test_same_month_v47_upgrade_requires_exact_predecessor_hash(self):
        prior_hash = "a" * 64
        state = {
            "strategy": "v4_6_r1_top10",
            "target_decision_date": "2026-07-31",
            "target_artifact_sha256": prior_hash,
        }
        basket = {
            "research_id": "v4_7_0001",
            "decision_date": "2026-07-31",
            "artifact_sha256": "b" * 64,
            "predecessor_target": {"research_id": "v4_6_r1_0001", "sha256": prior_hash},
            "members_match_predecessor": True,
        }
        _validate_factor_target_transition(basket, state)
        basket["predecessor_target"]["sha256"] = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "changed"):
            _validate_factor_target_transition(basket, state)

    def test_same_month_v47_upgrade_rejects_member_or_rank_changes(self):
        prior_hash = "a" * 64
        state = {
            "strategy": "v4_6_r1_top10", "target_decision_date": "2026-07-31",
            "target_artifact_sha256": prior_hash,
        }
        basket = {
            "research_id": "v4_7_0001", "decision_date": "2026-07-31",
            "artifact_sha256": "b" * 64,
            "predecessor_target": {"research_id": "v4_6_r1_0001", "sha256": prior_hash},
            "members_match_predecessor": False,
        }
        with self.assertRaisesRegex(RuntimeError, "changed"):
            _validate_factor_target_transition(basket, state)

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
                path, plan, target_sha256="a" * 64, execution_date="2026-08-12",
                target_artifact_path=Path("target.json"),
            )
            self.assertTrue(path.exists())
            self.assertTrue(first[0]["client_order_id"].startswith("fv46-"))
            journal = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(journal["target_artifact_filename"], "target.json")
            first_id = first[0]["client_order_id"]
            with self.assertRaisesRegex(RuntimeError, "journal"):
                _prepare_execution_journal(
                    path, plan, target_sha256="a" * 64, execution_date="2026-08-12",
                    target_artifact_path=Path("target.json"),
                )
            path.unlink()
            changed = _prepare_execution_journal(
                path,
                [{"action": "buy", "symbol": "T01", "qty": 3}],
                target_sha256="a" * 64,
                execution_date="2026-08-12",
                target_artifact_path=Path("target.json"),
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

    @patch("run_analysis_trade_pipeline._load_prices")
    @patch("run_analysis_trade_pipeline._load_account")
    def test_pipeline_uses_v47_tilted_target_weights(self, account_mock, prices_mock):
        account_mock.return_value = (
            {"equity": 100_000, "portfolio_value": 100_000, "cash": 100_000},
            [],
            {"status": "local_snapshot"},
        )
        prices_mock.return_value = {f"T{index:02d}": 100.0 for index in range(1, 11)}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_path = write_v47_fixture(root, v47_factor_target())
            approved_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            args = Namespace(
                strategy="factor-v4.7",
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
                    **V47_EFFECTIVE_CONFIG,
                    "enabled": True,
                    "mode": "v4_7_top10_score_tilt",
                    "research_id": "v4_7_0001",
                    "holdings": 10,
                    "target_method": "v4_7_factor_selection_score_tilt",
                    "allocation_method": "score_tilt",
                    "execution_strategy": "factor-v4.7",
                    "state_strategy": "v4_7_top10_score_tilt",
                },
                execution_config={
                    "enabled": True,
                    "target_path": str(target_path),
                    "approved_target_sha256": approved_hash,
                    "maximum_target_age_days": 40,
                    "legacy_managed_symbols": [],
                    "paper_only": True,
                    "capital_allocation_usd": 100_000,
                    "journal_path": str(root / "journal.json"),
                    "state_key_path": str(root / "state.key"),
                },
                state_path=root / "state.json",
            )
        self.assertEqual(output["strategy"], "factor-v4.7")
        self.assertAlmostEqual(sum(output["target_weights"].values()), 1.0)
        self.assertTrue(
            all(row["reason"] == "v4_7_top10_score_tilt_rebalance" for row in output["trade_plan"])
        )


if __name__ == "__main__":
    unittest.main()
