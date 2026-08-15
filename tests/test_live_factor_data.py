import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

import yaml
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _config import get_factor_data_config  # noqa: E402
from build_live_factor_signals import (  # noqa: E402
    LiveFactorDataError,
    SecIdentityError,
    _latest_sec_identity_filing,
    _strict_get,
    calculate_price_signals,
    derive_sec_fundamentals,
    extract_sec_filing_tickers,
    assemble_signal_row,
    build_live_signal_snapshot,
    ff12_for_sic,
    fetch_ff12_mapping,
    parse_sp500_csv,
    parse_ff12_sic_text,
    validate_sec_ticker_identity,
    validate_live_decision,
    validate_live_coverage,
)
from query_stock_prices import _fetch_alpaca_snapshots  # noqa: E402


def _sp500_csv(count: int = 500) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Symbol",
            "Security",
            "GICS Sector",
            "GICS Sub-Industry",
            "Headquarters Location",
            "Date added",
            "CIK",
            "Founded",
        ]
    )
    for index in range(count):
        writer.writerow(
            [
                f"T{index:03d}",
                f"Company {index}",
                "Industrials",
                "Test",
                "New York",
                "2020-01-01",
                1000000 + index,
                "2000",
            ]
        )
    return output.getvalue().encode("utf-8")


def _submissions(*rows):
    fields = {
        "accessionNumber": [],
        "filingDate": [],
        "acceptanceDateTime": [],
        "reportDate": [],
        "form": [],
        "primaryDocument": [],
    }
    for row in rows:
        for field in fields:
            fields[field].append(row.get(field, ""))
    return {"filings": {"recent": fields}}


def _fact(tag, units, entries, *, namespace="us-gaap"):
    return namespace, tag, {"units": {units: entries}}


class FactorDataConfigTests(unittest.TestCase):
    def test_live_data_contract_is_latest_only_and_uses_sip(self):
        parsed = get_factor_data_config(
            {
                "factor_data": {
                    "sec_user_agent": "factor-model/1.0 research@example.com"
                }
            }
        )
        self.assertEqual(parsed["universe_mode"], "latest_only")
        self.assertEqual(parsed["sp500_repository"], "fja05680/sp500")
        self.assertEqual(parsed["sp500_file"], "sp500.csv")
        self.assertEqual(parsed["alpaca_feed"], "sip")
        self.assertEqual(parsed["alpaca_snapshot_feed"], "iex")
        self.assertEqual(parsed["alpaca_adjustment"], "all")
        self.assertEqual(parsed["minimum_constituents"], 490)
        self.assertEqual(parsed["maximum_constituents"], 510)
        self.assertEqual(parsed["approved_constituent_sha256"], "")

    def test_constituent_approval_must_be_a_full_sha256(self):
        with self.assertRaisesRegex(ValueError, "approved_constituent_sha256"):
            get_factor_data_config(
                {
                    "factor_data": {
                        "sec_user_agent": "factor-model/1.0 research@example.com",
                        "approved_constituent_sha256": "short",
                    }
                }
            )

    def test_sec_user_agent_requires_contact_email(self):
        with self.assertRaisesRegex(ValueError, "email"):
            get_factor_data_config(
                {"factor_data": {"sec_user_agent": "anonymous-bot"}}
            )

    def test_execution_quote_loader_uses_the_frozen_iex_snapshot_feed(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "snapshots": {
                "AAPL": {
                    "latestTrade": {"p": 200.0},
                    "latestQuote": {},
                    "dailyBar": {"v": 1_000_000},
                    "prevDailyBar": {"c": 199.0},
                }
            }
        }
        config = {
            "alpaca": {"api_key": "key", "secret_key": "secret", "paper": True},
            "factor_data": {"sec_user_agent": "factor-model/1.0 research@example.com"},
        }
        with (
            patch("query_stock_prices.load_config", return_value=config),
            patch("query_stock_prices.requests.get", return_value=response) as request,
        ):
            result = _fetch_alpaca_snapshots(["AAPL"])
        self.assertEqual(result["AAPL"]["feed"], "iex")
        self.assertEqual(request.call_args.kwargs["params"]["feed"], "iex")


class SourceResponseSafetyTests(unittest.TestCase):
    class _Response:
        status_code = 200
        headers = {}

        def __init__(self, chunks):
            self._chunks = chunks
            self._content = b""

        @property
        def content(self):
            if not self._content:
                raise AssertionError("response.content must not be read before streaming")
            return self._content

        @content.setter
        def content(self, value):
            self._content = value

        def iter_content(self, chunk_size=65536):
            yield from self._chunks

        def close(self):
            return None

    class _Session:
        def __init__(self, response):
            self.response = response
            self.kwargs = None

        def get(self, *args, **kwargs):
            self.kwargs = kwargs
            return self.response

    def test_http_body_is_streamed_and_aborted_at_the_hard_limit(self):
        response = self._Response([b"1234", b"5678"])
        session = self._Session(response)
        with self.assertRaisesRegex(LiveFactorDataError, "exceeded"):
            _strict_get(session, "https://example.test/data", maximum_bytes=6)
        self.assertTrue(session.kwargs["stream"])

    def test_ff12_zip_rejects_extreme_decompression_ratio(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Siccodes12.txt", b"0" * 1_000_000)
        session = self._Session(self._Response([raw.getvalue()]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(LiveFactorDataError, "compression ratio"):
                fetch_ff12_mapping(Path(tmp), session=session)

class CurrentConstituentTests(unittest.TestCase):
    def test_current_sp500_uses_cik_and_ticker_identity(self):
        rows = parse_sp500_csv(
            _sp500_csv(), minimum_constituents=490, maximum_constituents=510
        )
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["ticker"], "T000")
        self.assertEqual(rows[0]["cik"], "0001000000")
        self.assertEqual(rows[0]["security_id"], "sec:0001000000:T000")

    def test_small_or_duplicate_universe_is_rejected(self):
        with self.assertRaisesRegex(LiveFactorDataError, "constituent count"):
            parse_sp500_csv(
                _sp500_csv(30), minimum_constituents=490, maximum_constituents=510
            )
        duplicated = _sp500_csv().decode("utf-8").replace(
            "T499,Company 499", "T000,Company 499"
        )
        with self.assertRaisesRegex(LiveFactorDataError, "duplicate ticker"):
            parse_sp500_csv(
                duplicated.encode(), minimum_constituents=490, maximum_constituents=510
            )


class SecPointInTimeTests(unittest.TestCase):
    def test_sec_ticker_identity_accepts_class_separator_but_rejects_wrong_cik(self):
        validate_sec_ticker_identity(
            [{"ticker": "BRK.B", "cik": "0001067983"}],
            {"cik": "1067983", "tickers": ["BRK-B"]},
        )
        with self.assertRaisesRegex(LiveFactorDataError, "ticker/CIK mismatch"):
            validate_sec_ticker_identity(
                [{"ticker": "AAPL", "cik": "0000320193"}],
                {"cik": "320193", "tickers": ["MSFT"]},
            )

    def test_sec_ticker_identity_requires_filing_evidence_when_ticker_list_is_empty(self):
        row = {"ticker": "XOM", "cik": "0000034088", "security_name": "ExxonMobil"}
        submissions = {
            "cik": "0000034088",
            "tickers": [],
            "name": "EXXON MOBIL CORP",
        }
        with self.assertRaisesRegex(LiveFactorDataError, "ticker/CIK mismatch"):
            validate_sec_ticker_identity([row], submissions)
        validate_sec_ticker_identity(
            [row], submissions, filing_tickers={"XOM", "XOM28"}
        )
        with self.assertRaisesRegex(LiveFactorDataError, "ticker/CIK mismatch"):
            validate_sec_ticker_identity(
                [{"ticker": "OTHER", "cik": "0000034088", "security_name": "ExxonMobil"}],
                submissions,
                filing_tickers={"XOM"},
            )
        with self.assertRaisesRegex(LiveFactorDataError, "ticker/CIK mismatch"):
            validate_sec_ticker_identity(
                [{"ticker": "XOM", "cik": "0000034088", "security_name": "ExxonMobil"}],
                {
                    "cik": "0000034088",
                    "tickers": ["NOTXOM"],
                    "name": "EXXON MOBIL CORP",
                },
            )

    def test_sec_inline_xbrl_trading_symbols_are_parsed(self):
        content = b"""
        <html><body>
          <ix:nonNumeric name="dei:TradingSymbol">XOM</ix:nonNumeric>
          <ix:nonnumeric NAME="dei:TradingSymbol"><span>XOM28</span></ix:nonnumeric>
        </body></html>
        """
        self.assertEqual(extract_sec_filing_tickers(content), {"XOM", "XOM28"})
        with self.assertRaisesRegex(SecIdentityError, "no dei:TradingSymbol"):
            extract_sec_filing_tickers(b"<html><body>No symbol</body></html>")

    def test_latest_sec_identity_filing_is_causal_and_path_safe(self):
        submissions = _submissions(
            {
                "accessionNumber": "0000034088-26-000090",
                "filingDate": "2026-07-01",
                "acceptanceDateTime": "2026-07-01T15:00:00-04:00",
                "form": "10-Q",
                "primaryDocument": "older.htm",
            },
            {
                "accessionNumber": "0000034088-26-000093",
                "filingDate": "2026-08-03",
                "acceptanceDateTime": "2026-08-03T17:00:00-04:00",
                "form": "10-Q",
                "primaryDocument": "xom-20260630.htm",
            },
            {
                "accessionNumber": "0000034088-26-000094",
                "filingDate": "2026-08-04",
                "acceptanceDateTime": "2026-08-04T15:00:00-04:00",
                "form": "10-Q",
                "primaryDocument": "../escape.htm",
            },
        )
        selected = _latest_sec_identity_filing(submissions, date(2026, 8, 14))
        self.assertEqual(selected["accessionNumber"], "0000034088-26-000093")

    def test_fundamentals_use_only_accessions_available_by_decision_close(self):
        accessions = []
        fact_entries = {
            "Assets": [],
            "NetIncomeLoss": [],
            "OperatingIncomeLoss": [],
        }
        quarter_ends = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        for index, end in enumerate(quarter_ends, start=1):
            accession = f"0001000000-26-{index:06d}"
            accepted = f"2026-0{min(index + 1, 7)}-15T15:00:00.000Z"
            accessions.append(
                {
                    "accessionNumber": accession,
                    "filingDate": accepted[:10],
                    "acceptanceDateTime": accepted,
                    "reportDate": end,
                    "form": "10-Q",
                    "primaryDocument": "report.htm",
                }
            )
            for tag, value in (
                ("NetIncomeLoss", 10.0 * index),
                ("OperatingIncomeLoss", 20.0 * index),
            ):
                fact_entries[tag].append(
                    {
                        "start": str(date.fromisoformat(end).replace(day=1)),
                        "end": end,
                        "val": value,
                        "accn": accession,
                        "form": "10-Q",
                        "filed": accepted[:10],
                    }
                )
            fact_entries["Assets"].append(
                {
                    "end": end,
                    "val": 1000.0 + 100 * index,
                    "accn": accession,
                    "form": "10-Q",
                    "filed": accepted[:10],
                }
            )
        # An amendment accepted after the decision must not replace the public value.
        accessions.append(
            {
                "accessionNumber": "0001000000-26-999999",
                "filingDate": "2026-08-03",
                "acceptanceDateTime": "2026-08-03T15:00:00.000Z",
                "reportDate": "2026-06-30",
                "form": "10-Q/A",
                "primaryDocument": "amendment.htm",
            }
        )
        fact_entries["Assets"].append(
            {
                "end": "2026-06-30",
                "val": 9999.0,
                "accn": "0001000000-26-999999",
                "form": "10-Q/A",
                "filed": "2026-08-03",
            }
        )
        companyfacts = {"facts": {"us-gaap": {}}}
        for tag, entries in fact_entries.items():
            companyfacts["facts"]["us-gaap"][tag] = {"units": {"USD": entries}}
        companyfacts["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": "2026-06-30",
                            "val": 100.0,
                            "accn": "0001000000-26-000004",
                            "form": "10-Q",
                            "filed": "2026-07-15",
                        }
                    ]
                }
            }
        }
        result = derive_sec_fundamentals(
            _submissions(*accessions), companyfacts, decision_date=date(2026, 7, 31)
        )
        self.assertEqual(result["assets_current"], 1400.0)
        self.assertEqual(result["shares_outstanding"], 100.0)
        self.assertLessEqual(result["fundamental_available_date"], "2026-07-31")

    def test_cumulative_sec_facts_are_converted_to_four_discrete_quarters(self):
        rows = []
        income = []
        operating = []
        assets = []
        periods = [
            ("2025-01-01", "2025-03-31", 10.0, 20.0),
            ("2025-01-01", "2025-06-30", 25.0, 50.0),
            ("2025-01-01", "2025-09-30", 45.0, 90.0),
            ("2025-01-01", "2025-12-31", 70.0, 140.0),
        ]
        for index, (start, end, net, op) in enumerate(periods, start=1):
            accession = f"0001000000-26-{index:06d}"
            accepted = f"2026-0{index + 1}-10T15:00:00Z"
            rows.append(
                {
                    "accessionNumber": accession,
                    "filingDate": accepted[:10],
                    "acceptanceDateTime": accepted,
                    "reportDate": end,
                    "form": "10-Q" if index < 4 else "10-K",
                }
            )
            income.append({"start": start, "end": end, "val": net, "accn": accession})
            operating.append({"start": start, "end": end, "val": op, "accn": accession})
            assets.append({"end": end, "val": 100.0 + index, "accn": accession})
        companyfacts = {
            "facts": {
                "us-gaap": {
                    "Assets": {"units": {"USD": assets}},
                    "NetIncomeLoss": {"units": {"USD": income}},
                    "OperatingIncomeLoss": {"units": {"USD": operating}},
                },
                "dei": {},
            }
        }
        result = derive_sec_fundamentals(
            _submissions(*rows), companyfacts, decision_date=date(2026, 6, 30)
        )
        self.assertEqual(result["net_income_ttm"], 70.0)
        self.assertEqual(result["operating_income_ttm"], 140.0)


class PriceAndCoverageTests(unittest.TestCase):
    def test_price_signal_uses_exact_252_and_21_session_endpoints(self):
        bars = []
        for index in range(260):
            bars.append(
                {
                    "date": date(2025, 1, 1).toordinal() + index,
                    "close": 100.0 + index,
                    "volume": 1_000_000.0,
                }
            )
        signals = calculate_price_signals(bars)
        self.assertEqual(signals["momentum_start_index"], 7)
        self.assertEqual(signals["momentum_end_index"], 238)
        self.assertAlmostEqual(signals["momentum_12_1"], 338.0 / 107.0 - 1.0)
        self.assertEqual(signals["adv20_observations"], 20)


class LiveSnapshotIntegrationTests(unittest.TestCase):
    def test_disabled_data_builder_fails_before_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "factor_data": {
                            "enabled": False,
                            "sec_user_agent": "factor-model/1.0 research@example.com",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LiveFactorDataError, "disabled"):
                build_live_signal_snapshot(root=root, decision_date=date(2026, 8, 31))

    def test_current_only_builder_writes_a_full_manifest_bound_cross_section(self):
        decision = date(2026, 8, 31)
        constituents = [
            {
                "security_id": f"sec:{1000000 + index:010d}:T{index:03d}",
                "ticker": f"T{index:03d}",
                "cik": f"{1000000 + index:010d}",
                "security_name": f"Company {index}",
                "gics_sector": "Industrials",
                "gics_sub_industry": "Test",
            }
            for index in range(500)
        ]
        fundamental = {
            "assets_current": 120.0,
            "assets_lag_4q": 100.0,
            "net_income_ttm": 12.0,
            "operating_income_ttm": 22.0,
            "shares_outstanding": 10.0,
            "fundamental_available_date": "2026-08-15",
            "sic": "1311",
        }
        fundamentals = {row["cik"]: dict(fundamental) for row in constituents}
        start = decision.toordinal() - 759
        shared_bars = [
            {
                "date": date.fromordinal(start + index).isoformat(),
                "close": 100.0 + index / 10,
                "volume": 1_000_000.0,
            }
            for index in range(760)
        ]
        bars = {row["ticker"]: shared_bars for row in constituents}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "alpaca": {"api_key": "paper-key", "secret_key": "paper-secret", "paper": True},
                "factor_data": {
                    "sec_user_agent": "factor-model/1.0 research@example.com",
                    "approved_constituent_sha256": "1" * 64,
                },
                "factor_portfolio": {},
                "factor_execution": {},
            }
            source_root = root / "data" / "factor_sources"
            artifacts = {}
            for name in ("constituents", "fundamentals", "prices", "industries", "fama_french"):
                path = source_root / f"{name}.snapshot"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
                artifacts[name] = path
            artifacts["constituents"].write_bytes(_sp500_csv())
            artifacts["industries"].write_text(
                " 4 Enrgy  Energy\n          1200-1399\n12 Other  Other\n",
                encoding="utf-8",
            )
            artifacts["fama_french"].write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decision_date": decision.isoformat(),
                        "vintage_id": "vintage-test",
                        "rows": [{"trade_date": "2026-06-30"}],
                    }
                ),
                encoding="utf-8",
            )
            constituent_hash = hashlib.sha256(
                artifacts["constituents"].read_bytes()
            ).hexdigest()
            config["factor_data"]["approved_constituent_sha256"] = constituent_hash
            (root / "config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
            with (
                patch(
                    "build_live_factor_signals.fetch_alpaca_calendar",
                    return_value=[date(2026, 8, 28), decision],
                ) as market_calendar,
                patch(
                    "build_live_factor_signals.fetch_current_sp500",
                    return_value=(
                        constituents,
                        artifacts["constituents"],
                        {
                            "commit": "a" * 40,
                            "sha256": constituent_hash,
                            "source_url": "https://example",
                            "retrieved_at_utc": "2026-08-31T20:10:00+00:00",
                        },
                    ),
                ) as current_sp500,
                patch(
                    "build_live_factor_signals.fetch_ff12_mapping",
                    return_value=(
                        [(1200, 1399, "4"), (-1, -1, "12")],
                        artifacts["industries"],
                        {
                            "sha256": "2" * 64,
                            "source_url": "https://example",
                            "retrieved_at_utc": "2026-08-31T20:11:00+00:00",
                        },
                    ),
                ),
                patch(
                    "build_live_factor_signals.SecLiveUpdater.sync",
                    return_value=(
                        fundamentals,
                        artifacts["fundamentals"],
                        {
                            "sha256": "3" * 64,
                            "errors": {},
                            "retrieved_at_utc": "2026-09-01T13:00:00+00:00",
                        },
                    ),
                ),
                patch(
                    "build_live_factor_signals.fetch_alpaca_daily_bars",
                    return_value=(
                        bars,
                        artifacts["prices"],
                        {
                            "sha256": "4" * 64,
                            "request_count": 1,
                            "symbol_count": 500,
                            "feed": "sip",
                            "adjustment": "all",
                            "retrieved_at_utc": "2026-08-31T20:15:00+00:00",
                        },
                    ),
                ) as daily_bars,
                patch(
                    "build_live_factor_signals.capture_factor_window",
                    return_value=(
                        [date.fromordinal(start + index) for index in range(4, 760)],
                        artifacts["fama_french"],
                        {
                            "sha256": hashlib.sha256(artifacts["fama_french"].read_bytes()).hexdigest(),
                            "vintage_id": "vintage-test",
                            "retrieved_at_utc": "2026-08-31T20:12:00+00:00",
                        },
                    ),
                ),
                patch(
                    "build_live_factor_signals.load_captured_factor_window",
                    return_value=(
                        [date.fromordinal(start + index) for index in range(4, 760)],
                        "vintage-test",
                    ),
                ),
                patch(
                    "build_live_factor_signals.load_captured_daily_bars",
                    return_value=bars,
                ),
            ):
                result = build_live_signal_snapshot(
                    root=root,
                    decision_date=decision,
                    observed_at=datetime(
                        2026, 8, 31, 16, 20, tzinfo=ZoneInfo("America/New_York")
                    ),
                )
                current_sp500.side_effect = AssertionError(
                    "a resumed capture must not download a later current universe"
                )
                market_calendar.side_effect = AssertionError(
                    "a resumed capture must use its frozen market calendar"
                )
                daily_bars.side_effect = AssertionError(
                    "a resumed capture must replay its frozen daily bars"
                )
                resumed = build_live_signal_snapshot(
                    root=root,
                    decision_date=decision,
                    observed_at=datetime(
                        2026, 9, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")
                    ),
                )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                resumed["universe_capture_id"], result["universe_capture_id"]
            )
            self.assertEqual(resumed["bundle_id"], result["bundle_id"])
            self.assertEqual(result["constituents"], 500)
            self.assertEqual(result["signal_rows"], 500)
            self.assertEqual(result["risk_eligible_rows"], 500)
            manifest = json.loads(
                (root / "data" / "factor_signal_input.manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["universe_mode"], "latest_only")
            self.assertEqual(len({row["path"] for row in manifest["source_snapshots"].values()}), 5)
            self.assertIn("factor_sources", manifest["signal_path"])
            self.assertTrue(Path(manifest["signal_path"]).is_file())
            self.assertTrue(Path(manifest["immutable_artifact_dir"]).is_dir())

    def test_live_decision_must_be_frozen_on_the_actual_month_end_session(self):
        sessions = [date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 1)]
        validate_live_decision(
            decision_date=date(2026, 8, 31),
            observed_at=datetime(
                2026, 8, 31, 16, 20, tzinfo=ZoneInfo("America/New_York")
            ),
            market_sessions=sessions,
        )
        with self.assertRaisesRegex(LiveFactorDataError, "same New York date"):
            validate_live_decision(
                decision_date=date(2026, 8, 31),
                observed_at=datetime(
                    2026, 9, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")
                ),
                market_sessions=sessions,
            )
        with self.assertRaisesRegex(LiveFactorDataError, "final market session"):
            validate_live_decision(
                decision_date=date(2026, 8, 28),
                observed_at=datetime(
                    2026, 8, 28, 16, 20, tzinfo=ZoneInfo("America/New_York")
                ),
                market_sessions=sessions,
            )

    def test_sic_mapping_and_signal_row_use_research_formulas(self):
        mapping = parse_ff12_sic_text(
            " 1 NoDur  Consumer Nondurables\n          0100-0999\n"
            " 4 Enrgy  Energy\n          1200-1399\n"
            "12 Other  Other\n          9000-9999\n"
        )
        self.assertEqual(ff12_for_sic("1311", mapping), "4")
        self.assertEqual(ff12_for_sic("1500", mapping), "12")
        row = assemble_signal_row(
            constituent={
                "security_id": "sec:0001000000:TEST",
                "ticker": "TEST",
            },
            fundamental={
                "assets_current": 120.0,
                "assets_lag_4q": 100.0,
                "net_income_ttm": 12.0,
                "operating_income_ttm": 22.0,
                "shares_outstanding": 10.0,
                "fundamental_available_date": "2026-07-15",
                "sic": "1311",
            },
            price={
                "decision_close": 20.0,
                "momentum_12_1": 0.25,
                "adv20_usd": 50_000_000.0,
                "adv20_observations": 20,
                "price_as_of_date": "2026-07-31",
            },
            ff12_mapping=mapping,
            decision_date=date(2026, 7, 31),
            paired_ff6_observations=600,
            minimum_risk_observations=504,
            minimum_adv20_observations=15,
        )
        self.assertEqual(row["ff_industry_12"], "4")
        self.assertAlmostEqual(row["market_cap"], 200.0)
        self.assertAlmostEqual(row["value_raw"], 12.0 / 200.0)
        self.assertAlmostEqual(row["profitability_raw"], 22.0 / 110.0)
        self.assertAlmostEqual(row["investment_raw"], -0.2)
        self.assertAlmostEqual(row["momentum_raw"], 0.25)
        self.assertTrue(row["risk_eligible"])

    def test_coverage_gate_rejects_incomplete_live_cross_section(self):
        with self.assertRaisesRegex(LiveFactorDataError, "fundamental coverage"):
            validate_live_coverage(
                constituent_count=500,
                cik_count=500,
                fundamental_count=300,
                price_count=500,
                industry_count=500,
                minimum_constituents=490,
                maximum_constituents=510,
                minimum_cik_coverage=0.99,
                minimum_fundamental_coverage=0.80,
                minimum_price_coverage=0.98,
                minimum_industry_coverage=0.95,
            )


if __name__ == "__main__":
    unittest.main()
