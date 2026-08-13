import sys
import unittest
import hashlib
import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _config import get_factor_execution_config, get_factor_portfolio_config  # noqa: E402
from factor_portfolio import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FactorPortfolioError,
    allocate_score_tilt,
    build_v46_predecessor_payload,
    conservative_factor_cutoff,
    derive_raw_factors,
    score_cross_section,
    select_factor_portfolio,
    validate_signal_manifest,
)
from sync_fama_french_factors import _write_sqlite, merge_factor_rows, parse_factor_csv  # noqa: E402


class FactorPortfolioConfigTests(unittest.TestCase):
    def test_default_v47_contract_is_frozen(self):
        parsed = get_factor_portfolio_config({})
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["weights"], DEFAULT_WEIGHTS)
        self.assertEqual(parsed["holdings"], 10)
        self.assertEqual(parsed["max_names_per_industry"], 3)
        self.assertEqual(parsed["factor_lag_months"], 2)
        self.assertEqual(parsed["allocation_method"], "score_tilt")
        self.assertEqual(parsed["mode"], "v4_7_top10_score_tilt")
        self.assertEqual(parsed["research_id"], "v4_7_0001")
        self.assertEqual(parsed["score_power"], 6.0)

    def test_execution_contract_is_separate_and_paper_only(self):
        parsed = get_factor_execution_config({})
        self.assertTrue(parsed["enabled"])
        self.assertTrue(parsed["paper_only"])
        self.assertTrue(parsed["dedicated_account"])
        self.assertEqual(parsed["state_path"], "data/factor_execution_state.json")
        self.assertEqual(parsed["journal_path"], "data/factor_execution_journal.json")
        self.assertEqual(parsed["maximum_target_age_days"], 40)
        self.assertEqual(parsed["capital_allocation_usd"], 100_000.0)

    def test_execution_rejects_capital_above_paper_account_value(self):
        with self.assertRaisesRegex(ValueError, "100000"):
            get_factor_execution_config(
                {"factor_execution": {"capital_allocation_usd": 100_000.01}}
            )

    def test_frozen_mode_rejects_nonbaseline_weights(self):
        with self.assertRaises(ValueError):
            get_factor_portfolio_config(
                {
                    "factor_portfolio": {
                        "parameter_mode": "frozen",
                        "weights": {
                            "size": 0.2,
                            "value": 0.2,
                            "profitability": 0.1,
                            "investment": 0.3,
                            "momentum": 0.2,
                        },
                    }
                }
            )

    def test_research_mode_requires_a_new_research_id(self):
        with self.assertRaises(ValueError):
            get_factor_portfolio_config(
                {"factor_portfolio": {"parameter_mode": "research", "research_id": "v4_7_0001"}}
            )

    def test_frozen_v47_contract_uses_score_power_six(self):
        parsed = get_factor_portfolio_config(
            {
                "factor_portfolio": {
                    "mode": "v4_7_top10_score_tilt",
                    "parameter_mode": "frozen",
                    "research_id": "v4_7_0001",
                    "allocation_method": "score_tilt",
                    "score_power": 6,
                    "minimum_weight": 0.05,
                    "maximum_weight": 0.20,
                    "maximum_industry_weight": 0.35,
                }
            }
        )
        self.assertEqual(parsed["mode"], "v4_7_top10_score_tilt")
        self.assertEqual(parsed["research_id"], "v4_7_0001")
        self.assertEqual(parsed["allocation_method"], "score_tilt")
        self.assertEqual(parsed["score_power"], 6.0)


class FactorPortfolioScoringTests(unittest.TestCase):
    def _rows(self):
        rows = []
        for industry in range(1, 5):
            for rank in range(12):
                raw = float(rank + 1)
                rows.append(
                    {
                        "security_id": f"{industry:02d}-{rank:02d}",
                        "ticker": f"T{industry}{rank:02d}",
                        "ff_industry_12": str(industry),
                        "membership_date": "2026-07-31",
                        "decision_date": "2026-07-31",
                        "constituent_as_of_date": "2026-07-31",
                        "fundamental_available_date": "2026-07-30",
                        "price_as_of_date": "2026-07-31",
                        "industry_as_of_date": "2026-07-31",
                        "size_raw": raw,
                        "value_raw": raw,
                        "profitability_raw": raw,
                        "investment_raw": raw,
                        "momentum_raw": raw,
                        "risk_eligible": True,
                        "adv20_usd": 50_000_000.0,
                    }
                )
        return rows

    def test_scores_and_selects_diversified_equal_weight_top10(self):
        scored = score_cross_section(self._rows(), DEFAULT_WEIGHTS)
        best = next(row for row in scored if row["security_id"] == "01-11")
        self.assertAlmostEqual(best["score"], 1.0)
        self.assertAlmostEqual(best["size_percentile"], 1.0)
        self.assertAlmostEqual(best["momentum_percentile"], 1.0)

        selected = select_factor_portfolio(
            scored,
            holdings=10,
            max_names_per_industry=3,
            minimum_adv20_usd=1_000_000.0,
        )
        self.assertEqual(len(selected), 10)
        self.assertAlmostEqual(sum(row["target_weight"] for row in selected), 1.0)
        industry_counts = {}
        for row in selected:
            industry_counts[row["ff_industry_12"]] = industry_counts.get(row["ff_industry_12"], 0) + 1
        self.assertLessEqual(max(industry_counts.values()), 3)

    def test_v47_score_tilt_is_fully_invested_bounded_and_monotone(self):
        selected = [
            {
                "security_id": f"S{index}",
                "ticker": f"T{index}",
                "ff_industry_12": "A" if index < 3 else f"I{index}",
                "score": 1.0 - index * 0.05,
            }
            for index in range(10)
        ]
        tilted = allocate_score_tilt(
            selected,
            power=6,
            minimum_weight=0.05,
            maximum_weight=0.20,
            maximum_industry_weight=0.35,
        )
        weights = [row["target_weight"] for row in tilted]
        self.assertAlmostEqual(sum(weights), 1.0, places=8)
        self.assertGreaterEqual(min(weights), 0.05 - 1e-8)
        self.assertLessEqual(max(weights), 0.20 + 1e-8)
        self.assertLessEqual(sum(weights[:3]), 0.35 + 1e-8)
        self.assertTrue(all(left >= right - 1e-8 for left, right in zip(weights, weights[1:])))

    def test_v47_allocator_conforms_to_frozen_research_golden_vector(self):
        industries = ["A", "A", "A", "I3", "I4", "I5", "I6", "I7", "I8", "I9"]
        selected = [
            {"score": 1.0 - index * 0.05, "ff_industry_12": industries[index]}
            for index in range(10)
        ]
        actual = [
            row["target_weight"]
            for row in allocate_score_tilt(
                selected, power=6, minimum_weight=0.05, maximum_weight=0.20,
                maximum_industry_weight=0.35,
            )
        ]
        # Frozen from factor-model v47.research.score_tilt_weights with
        # cvxpy 1.9.2 + CLARABEL before the production migration.
        research_golden = [
            0.14508208261909947, 0.10245895907395129, 0.10245895819243098,
            0.10245895760672569, 0.10245895679001055, 0.10245894524965128,
            0.10081343937089235, 0.08821255001256922, 0.07962973506437031,
            0.07396741602029894,
        ]
        self.assertLess(max(abs(a - b) for a, b in zip(actual, research_golden)), 1e-6)

    def test_default_v47_generation_builds_an_executable_v46_membership_anchor(self):
        config = get_factor_portfolio_config(
            {"factor_portfolio": {"mode": "v4_7_top10_score_tilt"}}
        )
        selected = [
            {
                "security_id": f"S{index}", "ticker": f"T{index}",
                "selection_rank": index + 1, "score": 1.0 - index * 0.05,
                "ff_industry_12": f"I{index}", "target_weight": 0.05,
            }
            for index in range(10)
        ]
        predecessor = build_v46_predecessor_payload(
            config=config, weights=DEFAULT_WEIGHTS, membership_date="2026-07-31",
            decision_date="2026-08-31", manifest={}, risk_audit={}, selected=selected,
        )
        self.assertEqual(predecessor["method"], "v4_6_r1_factor_selection")
        self.assertEqual(predecessor["research_id"], "v4_6_r1_0001")
        self.assertEqual({row["target_weight"] for row in predecessor["selected"]}, {0.1})
        self.assertEqual(
            [(row["selection_rank"], row["security_id"], row["ticker"]) for row in predecessor["selected"]],
            [(row["selection_rank"], row["security_id"], row["ticker"]) for row in selected],
        )

    def test_factor_cutoff_is_two_month_ends_behind(self):
        self.assertEqual(conservative_factor_cutoff(date(2026, 7, 31), 2), date(2026, 5, 31))
        self.assertEqual(conservative_factor_cutoff(date(2026, 6, 2), 2), date(2026, 4, 30))

    def test_missing_risk_eligibility_fails_closed(self):
        rows = self._rows()
        for row in rows:
            row.pop("risk_eligible")
        scored = score_cross_section(rows, DEFAULT_WEIGHTS)
        with self.assertRaises(FactorPortfolioError):
            select_factor_portfolio(scored, holdings=10, max_names_per_industry=3)

    def test_supplied_raw_factor_is_kept_when_other_factors_use_components(self):
        raw = derive_raw_factors(
            {
                "size_raw": 1.25,
                "market_cap": None,
                "net_income_ttm": 10,
                "operating_income_ttm": 20,
                "assets_current": 100,
                "assets_lag_4q": 90,
                "momentum_12_1": 0.3,
            }
        )
        self.assertEqual(raw["size"], 1.25)
        self.assertAlmostEqual(raw["investment"], -(100 / 90 - 1))

    def test_mixed_decision_dates_are_rejected(self):
        rows = self._rows()
        rows[-1]["decision_date"] = "2026-08-01"
        with self.assertRaises(FactorPortfolioError):
            score_cross_section(rows, DEFAULT_WEIGHTS)

    def test_future_available_fundamental_is_rejected(self):
        rows = self._rows()
        rows[0]["fundamental_available_date"] = "2026-08-01"
        with self.assertRaises(FactorPortfolioError):
            score_cross_section(rows, DEFAULT_WEIGHTS)

    def test_signal_manifest_artifacts_are_hashed_and_date_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_path = root / "signals.csv"
            signal_path.write_text("security_id\nA\n", encoding="utf-8")
            signal_hash = hashlib.sha256(signal_path.read_bytes()).hexdigest()
            sources = {}
            for name in ("constituents", "fundamentals", "prices", "industries"):
                source_path = root / f"{name}.snapshot"
                source_path.write_text(name, encoding="utf-8")
                sources[name] = {
                    "path": source_path.name,
                    "available_through": "2026-07-31",
                    "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                }
            manifest_path = root / "signals.manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "research_id": "v4_6_r1_0001",
                        "membership_date": "2026-07-31",
                        "decision_date": "2026-07-31",
                        "signal_sha256": signal_hash,
                        "source_snapshots": sources,
                    }
                ),
                encoding="utf-8",
            )
            verified = validate_signal_manifest(
                manifest_path,
                signal_path,
                research_id="v4_6_r1_0001",
                membership_date="2026-07-31",
                decision_date="2026-07-31",
            )
            self.assertEqual(verified["signalSha256"], signal_hash)



class FamaFrenchParserTests(unittest.TestCase):
    def test_parses_percent_units_and_merges_ff5_with_momentum(self):
        ff5 = b"header\n,Mkt-RF,SMB,HML,RMW,CMA,RF\n20260102,1.00,0.20,-0.30,0.40,0.50,0.01\n20260105,2.00,0.10,0.30,0.20,0.10,0.01\n\nCopyright"
        momentum = b"header\n,Mom\n20260102,0.70\n20260105,-0.20\n\nCopyright"
        ff5_rows = parse_factor_csv(ff5, ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"], ",Mkt-RF")
        momentum_rows = parse_factor_csv(momentum, ["Mom"], ",Mom")
        merged = merge_factor_rows(ff5_rows, momentum_rows)
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0]["Mkt-RF"], 0.01)
        self.assertAlmostEqual(merged[0]["Mom"], 0.007)
        self.assertEqual(merged[-1]["date"], "2026-01-05")

    def test_factor_vintages_are_append_only(self):
        rows = [
            {
                "date": "2026-01-02", "Mkt-RF": 0.01, "SMB": 0.002,
                "HML": -0.003, "RMW": 0.004, "CMA": 0.005,
                "Mom": 0.007, "RF": 0.0001,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "ff.sqlite"
            _write_sqlite(database, rows, "202601", "2026-02-01T00:00:00+00:00", "v1", "v1.json")
            changed = [dict(rows[0], **{"Mkt-RF": 0.02})]
            _write_sqlite(database, changed, "202601", "2026-02-02T00:00:00+00:00", "v2", "v2.json")
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM fama_french_vintages").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM fama_french_daily").fetchone()[0], 2)
                values = connection.execute(
                    "SELECT mkt_rf FROM fama_french_daily ORDER BY vintage_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(values, [(0.01,), (0.02,)])


if __name__ == "__main__":
    unittest.main()
