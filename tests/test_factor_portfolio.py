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

from _config import get_factor_portfolio_config  # noqa: E402
from factor_portfolio import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FactorPortfolioError,
    candidate_weight_grid,
    conservative_factor_cutoff,
    derive_raw_factors,
    load_ntaco_decision,
    score_cross_section,
    select_factor_portfolio,
    validate_signal_manifest,
)
from sync_fama_french_factors import _write_sqlite, merge_factor_rows, parse_factor_csv  # noqa: E402


class FactorPortfolioConfigTests(unittest.TestCase):
    def test_default_v46r1_contract_is_frozen(self):
        parsed = get_factor_portfolio_config({})
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["weights"], DEFAULT_WEIGHTS)
        self.assertEqual(parsed["holdings"], 10)
        self.assertEqual(parsed["max_names_per_industry"], 3)
        self.assertEqual(parsed["factor_lag_months"], 2)
        self.assertEqual(parsed["allocation_method"], "equal_weight")

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
                {"factor_portfolio": {"parameter_mode": "research", "research_id": "v4_6_r1_0001"}}
            )


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

    def test_candidate_grid_has_19_preregistered_weight_sets(self):
        grid = candidate_weight_grid([0.1, 0.2, 0.3], fundamental_sum=0.8, momentum=0.2)
        self.assertEqual(len(grid), 19)
        self.assertIn(DEFAULT_WEIGHTS, grid)
        self.assertTrue(all(abs(sum(row.values()) - 1.0) < 1e-12 for row in grid))

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
            exposure=0.8,
        )
        self.assertEqual(len(selected), 10)
        self.assertAlmostEqual(sum(row["target_weight"] for row in selected), 0.8)
        industry_counts = {}
        for row in selected:
            industry_counts[row["ff_industry_12"]] = industry_counts.get(row["ff_industry_12"], 0) + 1
        self.assertLessEqual(max(industry_counts.values()), 3)

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

    def test_manifest_and_ntaco_artifacts_are_hashed_and_date_aligned(self):
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

            ntaco_path = root / "ntaco.json"
            ntaco_path.write_text(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "signal": {
                            "signal_date": "2026-07-30",
                            "execution_date": "2026-07-31",
                            "exposure": 0.8,
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision = load_ntaco_decision(ntaco_path, factor_decision_date="2026-07-31")
            self.assertEqual(decision["exposure"], 0.8)
            self.assertEqual(len(decision["artifactSha256"]), 64)


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
