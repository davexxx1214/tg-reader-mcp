#!/usr/bin/env python3
"""Build the latest live V4.7 signal cross-section from public source snapshots.

This module deliberately has no historical-universe mode.  Research history stays
in factor-model; this executable only freezes the S&P 500 membership observed for
the next live monthly decision.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import time as time_module
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from _config import (
    get_alpaca_credentials,
    get_factor_data_config,
    get_factor_portfolio_config,
    load_config,
)
from _file_lock import exclusive_file_lock
from _factor_artifacts import factor_bundle_id


RELEVANT_FORMS = {
    "10-K", "10-K/A", "10-KT", "10-KT/A", "10-Q", "10-Q/A",
    "10-QT", "10-QT/A", "20-F", "20-F/A", "40-F", "40-F/A",
    "6-K", "6-K/A",
}
NEW_YORK = ZoneInfo("America/New_York")
FF12_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes12.zip"
SIGNAL_COLUMNS = (
    "security_id", "ticker", "ff_industry_12", "membership_date", "decision_date",
    "constituent_as_of_date", "fundamental_available_date", "price_as_of_date",
    "industry_as_of_date", "market_cap", "net_income_ttm", "operating_income_ttm",
    "assets_current", "assets_lag_4q", "momentum_12_1", "size_raw", "value_raw",
    "profitability_raw", "investment_raw", "momentum_raw", "adv20_usd",
    "adv20_observations", "paired_ff6_observations", "risk_eligible",
)


class LiveFactorDataError(ValueError):
    """Raised when a live source cannot safely produce a V4.7 signal input."""


class SecIdentityError(LiveFactorDataError):
    """Raised when an external ticker is not owned by the stated SEC CIK."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _strict_get(
    session: requests.Session,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    timeout: float = 60.0,
    maximum_bytes: int = 64 * 1024 * 1024,
) -> requests.Response:
    response = session.get(
        url,
        headers=dict(headers or {}),
        params=dict(params or {}),
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    if not 200 <= response.status_code < 300:
        response.close()
        raise LiveFactorDataError(f"source request failed with HTTP {response.status_code}: {url}")
    content_length = str((response.headers or {}).get("Content-Length") or "").strip()
    if content_length.isdigit() and int(content_length) > maximum_bytes:
        response.close()
        raise LiveFactorDataError(f"source response exceeded {maximum_bytes} bytes: {url}")
    chunks: list[bytes] = []
    received = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            received += len(chunk)
            if received > maximum_bytes:
                raise LiveFactorDataError(
                    f"source response exceeded {maximum_bytes} bytes: {url}"
                )
            chunks.append(chunk)
        response._content = b"".join(chunks)  # requests.Response buffered replay contract
        response._content_consumed = True
    finally:
        response.close()
    return response


def _iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise LiveFactorDataError(f"{label} is not a valid ISO date") from exc


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_sp500_csv(
    content: bytes,
    *,
    minimum_constituents: int,
    maximum_constituents: int,
) -> list[dict[str, str]]:
    """Parse the repository's current list and reject partial/toy universes."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LiveFactorDataError("S&P 500 CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "CIK"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise LiveFactorDataError("S&P 500 CSV schema is invalid")
    rows: list[dict[str, str]] = []
    tickers: set[str] = set()
    identities: set[str] = set()
    for raw in reader:
        ticker = str(raw.get("Symbol", "")).upper().strip()
        cik_raw = str(raw.get("CIK", "")).strip()
        if not ticker or not cik_raw.isdigit():
            raise LiveFactorDataError("S&P 500 row has a missing ticker or CIK")
        if ticker in tickers:
            raise LiveFactorDataError(f"duplicate ticker in current S&P 500: {ticker}")
        tickers.add(ticker)
        cik = cik_raw.zfill(10)
        security_id = f"sec:{cik}:{ticker}"
        if security_id in identities:
            raise LiveFactorDataError(f"duplicate security identity: {security_id}")
        identities.add(security_id)
        rows.append(
            {
                "security_id": security_id,
                "ticker": ticker,
                "cik": cik,
                "security_name": str(raw.get("Security", "")).strip(),
                "gics_sector": str(raw.get("GICS Sector", "")).strip(),
                "gics_sub_industry": str(raw.get("GICS Sub-Industry", "")).strip(),
            }
        )
    if not minimum_constituents <= len(rows) <= maximum_constituents:
        raise LiveFactorDataError(
            f"current S&P 500 constituent count {len(rows)} is outside "
            f"[{minimum_constituents}, {maximum_constituents}]"
        )
    return sorted(rows, key=lambda row: row["ticker"])


def fetch_current_sp500(
    config: Mapping[str, Any],
    cache_root: Path,
    *,
    session: requests.Session | None = None,
) -> tuple[list[dict[str, str]], Path, dict[str, str]]:
    """Resolve mutable master once, then download the list by immutable commit."""

    client = session or requests.Session()
    repository = str(config["sp500_repository"])
    branch = str(config["sp500_branch"])
    api_url = f"{str(config['github_api_base_url']).rstrip('/')}/repos/{repository}/commits/{branch}"
    api = _strict_get(
        client, api_url, headers={"User-Agent": "tg-reader-mcp-v4.7"}, maximum_bytes=2_000_000
    )
    try:
        commit = str(api.json()["sha"]).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveFactorDataError("GitHub commit response is invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise LiveFactorDataError("GitHub returned an invalid commit SHA")
    filename = str(config["sp500_file"])
    raw_url = (
        f"{str(config['github_raw_base_url']).rstrip('/')}/{repository}/"
        f"{commit}/{quote(filename)}"
    )
    raw = _strict_get(
        client, raw_url, headers={"User-Agent": "tg-reader-mcp-v4.7"}, maximum_bytes=5_000_000
    ).content
    rows = parse_sp500_csv(
        raw,
        minimum_constituents=int(config["minimum_constituents"]),
        maximum_constituents=int(config["maximum_constituents"]),
    )
    digest = _sha256_bytes(raw)
    path = cache_root / "constituents" / f"sp500_{commit}_{digest}.csv"
    if not path.exists():
        _atomic_bytes(path, raw)
    return rows, path, {
        "commit": commit,
        "sha256": digest,
        "source_url": raw_url,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def validate_constituent_approval(config: Mapping[str, Any], actual_sha256: str) -> None:
    """Require a human-approved exact universe before expensive data acquisition."""

    approved = str(config.get("approved_constituent_sha256") or "").lower().strip()
    actual = str(actual_sha256).lower().strip()
    if not approved or approved != actual:
        raise LiveFactorDataError(
            "current S&P 500 snapshot is not approved; inspect the constituent CSV and "
            f"set factor_data.approved_constituent_sha256 to {actual}"
        )


def _canonical_ticker(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def validate_sec_ticker_identity(
    constituents: Iterable[Mapping[str, Any]],
    submissions: Mapping[str, Any],
    *,
    filing_tickers: Iterable[str] | None = None,
) -> None:
    """Prove each GitHub ticker belongs to the CIK whose facts will be scored."""

    expected_rows = list(constituents)
    if not expected_rows:
        raise LiveFactorDataError("SEC ticker/CIK validation received no constituents")
    sec_cik = str(submissions.get("cik") or "").strip().zfill(10)
    expected_ciks = {str(row.get("cik") or "").strip().zfill(10) for row in expected_rows}
    sec_tickers = {
        _canonical_ticker(ticker)
        for ticker in submissions.get("tickers", []) or []
        if _canonical_ticker(ticker)
    }
    authoritative_tickers = sec_tickers or {
        _canonical_ticker(ticker)
        for ticker in filing_tickers or []
        if _canonical_ticker(ticker)
    }
    missing = sorted(
        str(row.get("ticker") or "")
        for row in expected_rows
        if _canonical_ticker(row.get("ticker")) not in authoritative_tickers
    )
    if expected_ciks != {sec_cik} or missing:
        raise SecIdentityError(
            "SEC ticker/CIK mismatch: "
            f"expected CIKs={sorted(expected_ciks)}, SEC CIK={sec_cik}, missing tickers={missing}"
        )


class _SecTradingSymbolParser(HTMLParser):
    """Extract inline-XBRL ``dei:TradingSymbol`` facts without executing HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_depth = 0
        self.buffer: list[str] = []
        self.symbols: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.capture_depth:
            self.capture_depth += 1
            return
        if tag.lower() != "ix:nonnumeric":
            return
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        if attributes.get("name", "").lower() == "dei:tradingsymbol":
            self.capture_depth = 1
            self.buffer = []

    def handle_endtag(self, _tag: str) -> None:
        if not self.capture_depth:
            return
        self.capture_depth -= 1
        if self.capture_depth == 0:
            symbol = _canonical_ticker("".join(self.buffer))
            if symbol:
                self.symbols.add(symbol)
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture_depth:
            self.buffer.append(data)


def extract_sec_filing_tickers(content: bytes) -> set[str]:
    """Return authoritative trading symbols declared in an SEC inline-XBRL filing."""

    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LiveFactorDataError("SEC identity filing is not valid UTF-8") from exc
    parser = _SecTradingSymbolParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise LiveFactorDataError("SEC identity filing HTML is invalid") from exc
    if not parser.symbols:
        raise SecIdentityError("SEC identity filing has no dei:TradingSymbol facts")
    return parser.symbols


def _latest_sec_identity_filing(
    submissions: Mapping[str, Any], decision_date: date
) -> dict[str, Any]:
    eligible: list[tuple[date, str, str, dict[str, Any]]] = []
    for raw in _submission_records(submissions):
        row = dict(raw)
        accession = str(row.get("accessionNumber") or "").strip()
        primary_document = str(row.get("primaryDocument") or "").strip()
        form = str(row.get("form") or "").strip()
        if (
            form not in RELEVANT_FORMS
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", primary_document, re.IGNORECASE)
        ):
            continue
        available = _available_date(row)
        if available <= decision_date:
            eligible.append((available, accession, primary_document, row))
    if not eligible:
        raise SecIdentityError("SEC submissions have no identity filing available by decision date")
    return max(eligible, key=lambda item: item[:3])[3]


def fetch_ff12_mapping(
    cache_root: Path,
    *,
    session: requests.Session | None = None,
) -> tuple[list[tuple[int, int, str]], Path, dict[str, str]]:
    client = session or requests.Session()
    content = _strict_get(
        client,
        FF12_URL,
        headers={"User-Agent": "tg-reader-mcp-v4.7"},
        maximum_bytes=5_000_000,
    ).content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != 1:
                raise LiveFactorDataError("FF12 ZIP has unexpected contents")
            info = archive.getinfo(names[0])
            maximum_uncompressed = 2_000_000
            if info.file_size > maximum_uncompressed:
                raise LiveFactorDataError("FF12 ZIP member exceeds the uncompressed size limit")
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > 100:
                raise LiveFactorDataError("FF12 ZIP member has an unsafe compression ratio")
            extracted = bytearray()
            with archive.open(info) as member:
                while True:
                    chunk = member.read(64 * 1024)
                    if not chunk:
                        break
                    extracted.extend(chunk)
                    if len(extracted) > maximum_uncompressed:
                        raise LiveFactorDataError(
                            "FF12 ZIP member exceeds the uncompressed size limit"
                        )
            text = bytes(extracted).decode("latin-1")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise LiveFactorDataError("FF12 source is not a valid ZIP") from exc
    mapping = parse_ff12_sic_text(text)
    if {industry for _, _, industry in mapping} != {str(index) for index in range(1, 13)}:
        raise LiveFactorDataError("FF12 source does not contain all twelve industries")
    raw = text.encode("utf-8")
    digest = _sha256_bytes(raw)
    path = cache_root / "industries" / f"ff12_sic_{digest}.txt"
    if not path.exists():
        _atomic_bytes(path, raw)
    return mapping, path, {
        "sha256": digest,
        "source_url": FF12_URL,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    }


class SecLiveUpdater:
    """Incrementally refresh only current S&P 500 issuers from SEC EDGAR."""

    def __init__(
        self,
        config: Mapping[str, Any],
        cache_root: Path,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.root = cache_root / "sec"
        self.session = session or requests.Session()
        self.headers = {
            "User-Agent": str(config["sec_user_agent"]),
            "Accept-Encoding": "gzip, deflate",
        }
        self.interval = 1.0 / float(config["sec_requests_per_second"])
        self.last_request = 0.0
        self.state_path = self.root / "state.json"

    def _request(self, url: str) -> bytes:
        elapsed = time_module.monotonic() - self.last_request
        if elapsed < self.interval:
            time_module.sleep(self.interval - elapsed)
        response = _strict_get(
            self.session, url, headers=self.headers, timeout=60, maximum_bytes=64 * 1024 * 1024
        )
        self.last_request = time_module.monotonic()
        return response.content

    def _save_raw(
        self, kind: str, cik: str, content: bytes, *, suffix: str = ".json"
    ) -> tuple[Path, str]:
        digest = _sha256_bytes(content)
        path = self.root / kind / f"CIK{cik}_{digest}{suffix}"
        if not path.exists():
            _atomic_bytes(path, content)
        return path, digest

    def _validate_identity(
        self,
        constituents: Iterable[Mapping[str, Any]],
        submissions: Mapping[str, Any],
        *,
        decision_date: date,
    ) -> dict[str, str]:
        rows = list(constituents)
        if submissions.get("tickers"):
            validate_sec_ticker_identity(rows, submissions)
            return {"identity_method": "sec_submissions_tickers"}

        record = _latest_sec_identity_filing(submissions, decision_date)
        cik = str(submissions.get("cik") or "").strip().zfill(10)
        accession = str(record["accessionNumber"])
        primary_document = str(record["primaryDocument"])
        identity_url = (
            f"{str(self.config['sec_archives_base_url']).rstrip('/')}/edgar/data/"
            f"{int(cik)}/{accession.replace('-', '')}/{primary_document}"
        )
        identity_raw = self._request(identity_url)
        identity_path, identity_hash = self._save_raw(
            "identity", cik, identity_raw, suffix=".html"
        )
        filing_tickers = extract_sec_filing_tickers(identity_raw)
        validate_sec_ticker_identity(rows, submissions, filing_tickers=filing_tickers)
        return {
            "identity_method": "sec_inline_xbrl_trading_symbol",
            "identity_path": str(identity_path),
            "identity_sha256": identity_hash,
            "identity_accession": accession,
            "identity_primary_document": primary_document,
        }

    @staticmethod
    def _validate_frozen_identity(
        constituents: Iterable[Mapping[str, Any]],
        submissions: Mapping[str, Any],
        frozen: Mapping[str, Any],
        *,
        decision_date: date,
    ) -> dict[str, str] | None:
        rows = list(constituents)
        if submissions.get("tickers"):
            validate_sec_ticker_identity(rows, submissions)
            return {"identity_method": "sec_submissions_tickers"}
        identity_path = Path(str(frozen.get("identity_path") or ""))
        identity_hash = str(frozen.get("identity_sha256") or "")
        record = _latest_sec_identity_filing(submissions, decision_date)
        if (
            str(frozen.get("identity_accession") or "") != str(record["accessionNumber"])
            or str(frozen.get("identity_primary_document") or "")
            != str(record["primaryDocument"])
            or not identity_path.is_file()
            or not identity_hash
        ):
            # Checkpoints created before filing-level identity evidence existed
            # are deliberately refreshed rather than trusted.
            return None
        if _file_sha256(identity_path) != identity_hash:
            raise LiveFactorDataError("SEC frozen identity filing is missing or changed")
        filing_tickers = extract_sec_filing_tickers(identity_path.read_bytes())
        validate_sec_ticker_identity(rows, submissions, filing_tickers=filing_tickers)
        return {
            "identity_method": "sec_inline_xbrl_trading_symbol",
            "identity_path": str(identity_path),
            "identity_sha256": identity_hash,
            "identity_accession": str(frozen.get("identity_accession") or ""),
            "identity_primary_document": str(frozen.get("identity_primary_document") or ""),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"version": 1, "issuers": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveFactorDataError("SEC live state is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise LiveFactorDataError("SEC live state version is invalid")
        if not isinstance(payload.get("issuers"), dict):
            raise LiveFactorDataError("SEC live issuer state is invalid")
        return payload

    @staticmethod
    def _fingerprint(submissions: Mapping[str, Any], decision_date: date) -> str:
        eligible = []
        for row in _submission_records(submissions):
            accession = str(row.get("accessionNumber") or "")
            form = str(row.get("form") or "")
            if accession and form in RELEVANT_FORMS and _available_date(row) <= decision_date:
                eligible.append((_available_date(row), accession, form))
        if not eligible:
            return ""
        available, accession, form = max(eligible)
        return f"{available.isoformat()}|{accession}|{form}"

    def sync(
        self,
        constituents: Iterable[Mapping[str, Any]],
        *,
        decision_date: date,
        checkpoint_path: Path | None = None,
        constituent_sha256: str,
    ) -> tuple[dict[str, dict[str, Any]], Path, dict[str, Any]]:
        state = self._load_state()
        issuers = dict(state["issuers"])
        receipts: dict[str, dict[str, str]] = {}
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        constituent_rows = [dict(row) for row in constituents]
        by_cik: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in constituent_rows:
            by_cik[str(row["cik"]).zfill(10)].append(row)
        ciks = sorted(by_cik)
        checkpoint: dict[str, Any] = {
            "version": 1,
            "decision_date": decision_date.isoformat(),
            "constituent_sha256": constituent_sha256,
            "issuers": {},
        }
        if checkpoint_path is not None and checkpoint_path.is_file():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LiveFactorDataError("SEC capture checkpoint is invalid") from exc
            if (
                not isinstance(checkpoint, dict)
                or checkpoint.get("version") != 1
                or checkpoint.get("decision_date") != decision_date.isoformat()
                or checkpoint.get("constituent_sha256") != constituent_sha256
                or not isinstance(checkpoint.get("issuers"), dict)
            ):
                raise LiveFactorDataError("SEC capture checkpoint contract is invalid")
        completed = dict(checkpoint["issuers"])
        for position, cik in enumerate(ciks, start=1):
            try:
                expected_tickers = sorted(str(row["ticker"]) for row in by_cik[cik])
                frozen = completed.get(cik) if isinstance(completed.get(cik), dict) else None
                if frozen is not None and frozen.get("expected_tickers") == expected_tickers:
                    for path_key, hash_key in (
                        ("submissions_path", "submissions_sha256"),
                        ("companyfacts_path", "companyfacts_sha256"),
                    ):
                        frozen_path = Path(str(frozen.get(path_key) or ""))
                        if not frozen_path.is_file() or _file_sha256(frozen_path) != frozen.get(hash_key):
                            raise LiveFactorDataError(
                                f"SEC capture checkpoint artifact is missing or changed for {cik}"
                            )
                    frozen_submissions = json.loads(
                        Path(str(frozen["submissions_path"])).read_text(encoding="utf-8")
                    )
                    frozen_identity = self._validate_frozen_identity(
                        by_cik[cik], frozen_submissions, frozen, decision_date=decision_date
                    )
                    if frozen_identity is None:
                        frozen = None
                if frozen is not None and frozen.get("expected_tickers") == expected_tickers:
                    fundamental = frozen.get("fundamental")
                    if not isinstance(fundamental, dict):
                        raise LiveFactorDataError(f"SEC capture checkpoint fundamental is invalid for {cik}")
                    results[cik] = dict(fundamental)
                    receipts[cik] = {
                        "submissions_sha256": str(frozen["submissions_sha256"]),
                        "companyfacts_sha256": str(frozen["companyfacts_sha256"]),
                        "identity_method": str(frozen_identity["identity_method"]),
                        "identity_sha256": str(frozen_identity.get("identity_sha256") or ""),
                    }
                    continue
                submissions_url = (
                    f"{str(self.config['sec_data_base_url']).rstrip('/')}/submissions/CIK{cik}.json"
                )
                submissions_raw = self._request(submissions_url)
                submissions_path, submissions_hash = self._save_raw(
                    "submissions", cik, submissions_raw
                )
                submissions = json.loads(submissions_raw)
                identity = self._validate_identity(
                    by_cik[cik], submissions, decision_date=decision_date
                )
                fingerprint = self._fingerprint(submissions, decision_date)
                previous = issuers.get(cik) if isinstance(issuers.get(cik), dict) else {}
                fundamental = previous.get("fundamental") if previous.get("fingerprint") == fingerprint else None
                companyfacts_hash = str(previous.get("companyfacts_sha256") or "")
                companyfacts_path = str(previous.get("companyfacts_path") or "")
                if not isinstance(fundamental, dict):
                    facts_url = (
                        f"{str(self.config['sec_data_base_url']).rstrip('/')}/api/xbrl/"
                        f"companyfacts/CIK{cik}.json"
                    )
                    facts_raw = self._request(facts_url)
                    facts_path, companyfacts_hash = self._save_raw(
                        "companyfacts", cik, facts_raw
                    )
                    companyfacts_path = str(facts_path)
                    fundamental = derive_sec_fundamentals(
                        submissions, json.loads(facts_raw), decision_date=decision_date
                    )
                else:
                    # SIC is a current issuer attribute and can change independently
                    # of the latest relevant accession fingerprint.
                    fundamental = dict(fundamental)
                    fundamental["sic"] = str(submissions.get("sic") or "").strip() or fundamental.get("sic")
                issuers[cik] = {
                    "fingerprint": fingerprint,
                    "fundamental": fundamental,
                    "submissions_path": str(submissions_path),
                    "submissions_sha256": submissions_hash,
                    "companyfacts_path": companyfacts_path,
                    "companyfacts_sha256": companyfacts_hash,
                    **identity,
                }
                results[cik] = dict(fundamental)
                receipts[cik] = {
                    "submissions_sha256": submissions_hash,
                    "companyfacts_sha256": companyfacts_hash,
                    "identity_method": str(identity["identity_method"]),
                    "identity_sha256": str(identity.get("identity_sha256") or ""),
                }
                completed[cik] = {
                    **issuers[cik],
                    "expected_tickers": expected_tickers,
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                if checkpoint_path is not None:
                    _atomic_json(
                        checkpoint_path,
                        {
                            "version": 1,
                            "decision_date": decision_date.isoformat(),
                            "constituent_sha256": constituent_sha256,
                            "issuers": completed,
                        },
                    )
                _atomic_json(
                    self.state_path,
                    {
                        "version": 1,
                        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "issuers": issuers,
                    },
                )
            except SecIdentityError:
                raise
            except (LiveFactorDataError, json.JSONDecodeError, OSError) as exc:
                errors[cik] = f"{type(exc).__name__}: {exc}"
            if position % 50 == 0 or position == len(ciks):
                print(f"SEC live progress: {position}/{len(ciks)}", flush=True)
        state_payload = {
            "version": 1,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "issuers": issuers,
        }
        _atomic_json(self.state_path, state_payload)
        snapshot = {
            "version": 1,
            "decision_date": decision_date.isoformat(),
            "issuer_count": len(ciks),
            "resolved_count": len(results),
            "errors": errors,
            "receipts": receipts,
            "fundamentals": results,
            "retrieved_at_utc": max(
                (str(row.get("retrieved_at_utc") or "") for row in completed.values()),
                default=datetime.now(timezone.utc).isoformat(),
            ),
        }
        raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        digest = _sha256_bytes(raw)
        path = cache_root_path(self.root) / "fundamentals" / f"fundamentals_{decision_date:%Y%m%d}_{digest}.json"
        _atomic_bytes(path, raw)
        return results, path, {
            "sha256": digest,
            "errors": errors,
            "retrieved_at_utc": snapshot["retrieved_at_utc"],
        }


def cache_root_path(sec_root: Path) -> Path:
    """Return the factor-source root from its SEC child directory."""

    if sec_root.name != "sec":
        raise LiveFactorDataError("SEC cache root is invalid")
    return sec_root.parent


def _submission_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    recent = ((payload.get("filings") or {}).get("recent") or {})
    if not isinstance(recent, Mapping):
        return []
    accessions = list(recent.get("accessionNumber") or [])
    records: list[dict[str, Any]] = []
    for index, accession in enumerate(accessions):
        row = {key: (values[index] if index < len(values) else "") for key, values in recent.items()}
        row["accessionNumber"] = accession
        records.append(row)
    return records


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _available_date(record: Mapping[str, Any]) -> date:
    raw = str(record.get("acceptanceDateTime") or "").strip()
    if raw:
        try:
            accepted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if accepted.tzinfo is None:
                accepted = accepted.replace(tzinfo=timezone.utc)
            local = accepted.astimezone(NEW_YORK)
            if local.weekday() >= 5 or local.time() >= time(16, 0):
                return _next_weekday(local.date())
            return local.date()
        except ValueError:
            pass
    # SEC recent submissions normally provide acceptanceDateTime.  A one-day
    # delay is the conservative fallback for an older record that does not.
    return _next_weekday(_iso_date(record.get("filingDate"), "filingDate"))


def _accession_metadata(
    submissions: Mapping[str, Any], decision_date: date
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _submission_records(submissions):
        accession = str(row.get("accessionNumber") or "").strip()
        form = str(row.get("form") or "").strip()
        if not accession or form not in RELEVANT_FORMS:
            continue
        available = _available_date(row)
        if available <= decision_date:
            result[accession] = {"available_date": available, "form": form}
    return result


def _facts_for_tag(
    companyfacts: Mapping[str, Any],
    namespace: str,
    tag: str,
    units: Iterable[str],
    accessions: Mapping[str, Mapping[str, Any]],
    decision_date: date,
) -> list[dict[str, Any]]:
    fact = (((companyfacts.get("facts") or {}).get(namespace) or {}).get(tag) or {})
    unit_payload = fact.get("units") or {}
    rows: list[dict[str, Any]] = []
    for unit in units:
        for raw in unit_payload.get(unit, []) or []:
            accession = str(raw.get("accn") or "").strip()
            metadata = accessions.get(accession)
            if metadata is None:
                continue
            end = _iso_date(raw.get("end"), f"{tag}.end")
            if end > decision_date:
                continue
            value = _number(raw.get("val"))
            if value is None:
                continue
            start_raw = raw.get("start")
            rows.append(
                {
                    "tag": tag,
                    "unit": unit,
                    "start": _iso_date(start_raw, f"{tag}.start") if start_raw else None,
                    "end": end,
                    "value": value,
                    "accession": accession,
                    "available_date": metadata["available_date"],
                }
            )
    return rows


def _latest_period_versions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        key = (row["tag"], row["unit"], row.get("start"), row["end"])
        current = grouped.get(key)
        order = (row["available_date"], row["accession"])
        if current is None or order > (current["available_date"], current["accession"]):
            grouped[key] = row
    return list(grouped.values())


def _choose_assets(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not rows:
        return None, None
    current = max(rows, key=lambda row: (row["end"], row["available_date"], row["accession"]))
    candidates = [
        row for row in rows
        if row["end"] < current["end"] and 300 <= (current["end"] - row["end"]).days <= 430
    ]
    lag = min(
        candidates,
        key=lambda row: (
            abs((current["end"] - row["end"]).days - 365),
            -row["end"].toordinal(),
            -row["available_date"].toordinal(),
        ),
    ) if candidates else None
    return current, lag


def _ttm(rows: list[dict[str, Any]], decision_date: date) -> dict[str, Any] | None:
    direct: list[dict[str, Any]] = []
    cumulative: list[dict[str, Any]] = []
    for row in rows:
        if row.get("start") is None:
            continue
        duration = (row["end"] - row["start"]).days
        if 60 <= duration <= 120:
            direct.append({**row, "quarter_value": row["value"], "priority": 1})
        elif 120 < duration <= 400:
            cumulative.append(row)
    derived: list[dict[str, Any]] = []
    for current in cumulative:
        priors = [
            row for row in rows
            if row.get("start") == current["start"]
            and row["end"] < current["end"]
            and 60 <= (current["end"] - row["end"]).days <= 120
        ]
        if not priors:
            continue
        prior = max(priors, key=lambda row: row["end"])
        derived.append(
            {
                **current,
                "quarter_value": current["value"] - prior["value"],
                "available_date": max(current["available_date"], prior["available_date"]),
                "accession": "|".join(sorted({current["accession"], prior["accession"]})),
                "priority": 0,
            }
        )
    by_end: dict[date, dict[str, Any]] = {}
    for row in direct + derived:
        current = by_end.get(row["end"])
        order = (row["available_date"], row["priority"], row["accession"])
        if current is None or order > (
            current["available_date"], current["priority"], current["accession"]
        ):
            by_end[row["end"]] = row
    selected = sorted(by_end.values(), key=lambda row: row["end"])[-4:]
    if len(selected) != 4 or (decision_date - selected[-1]["end"]).days > 200:
        return None
    gaps = [(right["end"] - left["end"]).days for left, right in zip(selected, selected[1:])]
    if min(gaps) < 60 or max(gaps) > 120:
        return None
    return {
        "value": sum(float(row["quarter_value"]) for row in selected),
        "available_date": max(row["available_date"] for row in selected),
        "accessions": "|".join(row["accession"] for row in selected),
    }


def derive_sec_fundamentals(
    submissions: Mapping[str, Any],
    companyfacts: Mapping[str, Any],
    *,
    decision_date: date,
) -> dict[str, Any]:
    """Resolve the latest causally available SEC inputs for one current issuer."""

    accessions = _accession_metadata(submissions, decision_date)
    if not accessions:
        raise LiveFactorDataError("issuer has no SEC filings available by decision date")
    assets = _latest_period_versions(
        _facts_for_tag(companyfacts, "us-gaap", "Assets", ("USD",), accessions, decision_date)
    )
    current_assets, lag_assets = _choose_assets(assets)
    income = _latest_period_versions(
        _facts_for_tag(
            companyfacts, "us-gaap", "NetIncomeLoss", ("USD",), accessions, decision_date
        )
    )
    operating = _latest_period_versions(
        _facts_for_tag(
            companyfacts, "us-gaap", "OperatingIncomeLoss", ("USD",), accessions, decision_date
        )
    )
    net_income = _ttm(income, decision_date)
    operating_income = _ttm(operating, decision_date)
    shares: list[dict[str, Any]] = []
    for priority, (namespace, tag) in enumerate(
        (("us-gaap", "CommonStockSharesOutstanding"), ("dei", "EntityCommonStockSharesOutstanding"))
    ):
        for row in _latest_period_versions(
            _facts_for_tag(companyfacts, namespace, tag, ("shares", "share"), accessions, decision_date)
        ):
            if (decision_date - row["end"]).days <= 550:
                shares.append({**row, "tag_priority": priority})
    selected_shares = max(
        shares,
        key=lambda row: (
            row["end"], row["available_date"], row["tag_priority"], row["accession"]
        ),
    ) if shares else None
    availability = [
        item["available_date"]
        for item in (current_assets, lag_assets, net_income, operating_income, selected_shares)
        if item is not None
    ]
    return {
        "assets_current": current_assets["value"] if current_assets else None,
        "assets_lag_4q": lag_assets["value"] if lag_assets else None,
        "net_income_ttm": net_income["value"] if net_income else None,
        "operating_income_ttm": operating_income["value"] if operating_income else None,
        "shares_outstanding": selected_shares["value"] if selected_shares else None,
        "fundamental_available_date": max(availability).isoformat() if availability else None,
        "sic": str(submissions.get("sic") or "").strip() or None,
    }


def calculate_price_signals(bars: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate exact-session 12-1 momentum and research-consistent ADV20."""

    ordered = sorted((dict(row) for row in bars), key=lambda row: row["date"])
    if len(ordered) < 253:
        raise LiveFactorDataError("price history has fewer than 253 sessions")
    decision_index = len(ordered) - 1
    start_index = decision_index - 252
    end_index = decision_index - 21
    start_price = _number(ordered[start_index].get("close"))
    end_price = _number(ordered[end_index].get("close"))
    decision_price = _number(ordered[decision_index].get("close"))
    if not start_price or not end_price or not decision_price or min(start_price, end_price, decision_price) <= 0:
        raise LiveFactorDataError("price history contains an invalid momentum endpoint")
    dollar_volumes = []
    for row in ordered[-20:]:
        close = _number(row.get("close"))
        volume = _number(row.get("volume"))
        if close is not None and volume is not None and close > 0 and volume >= 0:
            dollar_volumes.append(close * volume)
    return {
        "decision_close": decision_price,
        "momentum_12_1": end_price / start_price - 1.0,
        "momentum_start_index": start_index,
        "momentum_end_index": end_index,
        "adv20_usd": sum(dollar_volumes) / len(dollar_volumes) if dollar_volumes else None,
        "adv20_observations": len(dollar_volumes),
        "price_as_of_date": str(ordered[-1]["date"]),
    }


def validate_live_decision(
    *,
    decision_date: date,
    observed_at: datetime,
    market_sessions: Iterable[date],
) -> None:
    """Require the live universe to be observed after the actual month-end close."""

    local = observed_at.astimezone(NEW_YORK) if observed_at.tzinfo else observed_at.replace(tzinfo=NEW_YORK)
    if local.date() != decision_date:
        raise LiveFactorDataError(
            "live constituents must be frozen on the same New York date as the decision"
        )
    if local.time() < time(16, 5):
        raise LiveFactorDataError("live constituents cannot be frozen before the market close")
    sessions = sorted(set(market_sessions))
    in_month = [
        session for session in sessions
        if session.year == decision_date.year and session.month == decision_date.month
    ]
    if not in_month or decision_date != in_month[-1]:
        raise LiveFactorDataError("decision_date is not the final market session of its month")


def parse_ff12_sic_text(text: str) -> list[tuple[int, int, str]]:
    """Parse Kenneth French's SIC12 definition into deterministic ranges."""

    industry: str | None = None
    declared: set[str] = set()
    ranges: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        header = re.match(r"^\s*(\d{1,2})\s+[A-Za-z0-9]+\s+", line)
        if header:
            industry = str(int(header.group(1)))
            declared.add(industry)
            continue
        match = re.match(r"^\s*(\d{4})-(\d{4})\s*$", line)
        if match and industry is not None:
            lower, upper = int(match.group(1)), int(match.group(2))
            if lower > upper:
                raise LiveFactorDataError("FF12 SIC range is reversed")
            ranges.append((lower, upper, industry))
    if not ranges:
        raise LiveFactorDataError("FF12 SIC definition has no ranges")
    if "12" in declared:
        # Kenneth French defines Other as the complement of industries 1-11
        # and therefore intentionally lists no explicit ranges below it.
        ranges.append((-1, -1, "12"))
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if left[0] >= 0 and right[0] >= 0 and right[0] <= left[1] and right[2] != left[2]:
            raise LiveFactorDataError("FF12 SIC definition has overlapping industries")
    return ordered


def ff12_for_sic(sic: Any, mapping: Iterable[tuple[int, int, str]]) -> str | None:
    try:
        value = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    ranges = list(mapping)
    matches = {
        industry for lower, upper, industry in ranges
        if lower >= 0 and lower <= value <= upper
    }
    if len(matches) > 1:
        raise LiveFactorDataError(f"SIC {value:04d} maps to multiple FF12 industries")
    if matches:
        return next(iter(matches))
    return "12" if any(lower < 0 and industry == "12" for lower, _, industry in ranges) else None


def assemble_signal_row(
    *,
    constituent: Mapping[str, Any],
    fundamental: Mapping[str, Any],
    price: Mapping[str, Any],
    ff12_mapping: Iterable[tuple[int, int, str]],
    decision_date: date,
    paired_ff6_observations: int,
    minimum_risk_observations: int,
    minimum_adv20_observations: int,
) -> dict[str, Any]:
    """Combine one live issuer into the exact CSV schema consumed by V4.7."""

    assets_current = _number(fundamental.get("assets_current"))
    assets_lag = _number(fundamental.get("assets_lag_4q"))
    net_income = _number(fundamental.get("net_income_ttm"))
    operating_income = _number(fundamental.get("operating_income_ttm"))
    shares = _number(fundamental.get("shares_outstanding"))
    close = _number(price.get("decision_close"))
    momentum = _number(price.get("momentum_12_1"))
    adv20 = _number(price.get("adv20_usd"))
    market_cap = close * shares if close and shares and close > 0 and shares > 0 else None
    valid_assets = bool(
        assets_current is not None and assets_current > 0
        and assets_lag is not None and assets_lag > 0
    )
    average_assets = (assets_current + assets_lag) / 2.0 if valid_assets else None
    industry = ff12_for_sic(fundamental.get("sic"), ff12_mapping)
    fundamental_date = str(fundamental.get("fundamental_available_date") or "")
    price_date = str(price.get("price_as_of_date") or "")
    core_available = bool(
        market_cap is not None
        and valid_assets
        and net_income is not None
        and operating_income is not None
        and momentum is not None
        and industry is not None
        and fundamental_date
        and price_date
    )
    risk_eligible = bool(
        core_available
        and int(paired_ff6_observations) >= int(minimum_risk_observations)
        and int(price.get("adv20_observations") or 0) >= int(minimum_adv20_observations)
    )
    return {
        "security_id": str(constituent["security_id"]),
        "ticker": str(constituent["ticker"]).upper(),
        "ff_industry_12": industry or "",
        "membership_date": decision_date.isoformat(),
        "decision_date": decision_date.isoformat(),
        "constituent_as_of_date": decision_date.isoformat(),
        "fundamental_available_date": fundamental_date,
        "price_as_of_date": price_date,
        "industry_as_of_date": fundamental_date,
        "market_cap": market_cap,
        "net_income_ttm": net_income,
        "operating_income_ttm": operating_income,
        "assets_current": assets_current,
        "assets_lag_4q": assets_lag,
        "momentum_12_1": momentum,
        "size_raw": -math.log(market_cap) if market_cap and market_cap > 0 else None,
        "value_raw": net_income / market_cap if market_cap and net_income is not None else None,
        "profitability_raw": (
            operating_income / average_assets
            if average_assets and operating_income is not None else None
        ),
        "investment_raw": (
            -(assets_current / assets_lag - 1.0) if valid_assets else None
        ),
        "momentum_raw": momentum,
        "adv20_usd": adv20,
        "adv20_observations": int(price.get("adv20_observations") or 0),
        "paired_ff6_observations": int(paired_ff6_observations),
        "risk_eligible": risk_eligible,
    }


def validate_live_coverage(
    *,
    constituent_count: int,
    cik_count: int,
    fundamental_count: int,
    price_count: int,
    industry_count: int,
    minimum_constituents: int,
    maximum_constituents: int,
    minimum_cik_coverage: float,
    minimum_fundamental_coverage: float,
    minimum_price_coverage: float,
    minimum_industry_coverage: float,
    complete_signal_count: int | None = None,
    minimum_complete_signal_coverage: float | None = None,
) -> dict[str, float]:
    """Fail closed before a partial source can be ranked as a full S&P 500."""

    if not minimum_constituents <= constituent_count <= maximum_constituents:
        raise LiveFactorDataError("constituent count is outside the live contract")
    if constituent_count <= 0:
        raise LiveFactorDataError("constituent count must be positive")
    coverage = {
        "cik": cik_count / constituent_count,
        "fundamental": fundamental_count / constituent_count,
        "price": price_count / constituent_count,
        "industry": industry_count / constituent_count,
    }
    thresholds = {
        "cik": minimum_cik_coverage,
        "fundamental": minimum_fundamental_coverage,
        "price": minimum_price_coverage,
        "industry": minimum_industry_coverage,
    }
    if complete_signal_count is not None and minimum_complete_signal_coverage is not None:
        coverage["complete_signal"] = complete_signal_count / constituent_count
        thresholds["complete_signal"] = minimum_complete_signal_coverage
    for name, minimum in thresholds.items():
        if coverage[name] < minimum:
            raise LiveFactorDataError(
                f"{name} coverage {coverage[name]:.2%} is below {minimum:.2%}"
            )
    return coverage


def fetch_alpaca_calendar(
    api_key: str,
    secret_key: str,
    *,
    start: date,
    end: date,
    session: requests.Session | None = None,
) -> list[date]:
    client = session or requests.Session()
    response = _strict_get(
        client,
        "https://paper-api.alpaca.markets/v2/calendar",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
        params={"start": start.isoformat(), "end": end.isoformat()},
        maximum_bytes=2_000_000,
    )
    try:
        sessions = [_iso_date(row["date"], "Alpaca calendar date") for row in response.json()]
    except (TypeError, KeyError, ValueError) as exc:
        raise LiveFactorDataError("Alpaca calendar response is invalid") from exc
    if not sessions:
        raise LiveFactorDataError("Alpaca returned an empty market calendar")
    return sorted(set(sessions))


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def fetch_alpaca_daily_bars(
    symbols: Iterable[str],
    *,
    decision_date: date,
    api_key: str,
    secret_key: str,
    config: Mapping[str, Any],
    cache_root: Path,
    session: requests.Session | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], Path, dict[str, Any]]:
    """Fetch adjusted SIP daily bars for current members with pagination."""

    client = session or requests.Session()
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    normalized = sorted({str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()})
    start = decision_date - timedelta(days=4 * 366)
    end = decision_date + timedelta(days=1)
    bars: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in normalized}
    request_count = 0
    for chunk in _chunks(normalized, 100):
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": f"{start.isoformat()}T00:00:00Z",
                "end": f"{end.isoformat()}T00:00:00Z",
                "adjustment": str(config["alpaca_adjustment"]),
                "feed": str(config["alpaca_feed"]),
                "sort": "asc",
                "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token
            response = _strict_get(
                client,
                f"{str(config['alpaca_data_base_url']).rstrip('/')}/v2/stocks/bars",
                headers=headers,
                params=params,
                maximum_bytes=64 * 1024 * 1024,
            )
            request_count += 1
            try:
                payload = response.json()
                page = payload.get("bars") or {}
            except (TypeError, ValueError) as exc:
                raise LiveFactorDataError("Alpaca bars response is invalid") from exc
            if not isinstance(page, dict):
                raise LiveFactorDataError("Alpaca bars payload is not grouped by symbol")
            for symbol, rows in page.items():
                if symbol not in bars or not isinstance(rows, list):
                    continue
                for raw in rows:
                    bar_date = _iso_date(raw.get("t"), "Alpaca bar timestamp")
                    close = _number(raw.get("c"))
                    volume = _number(raw.get("v"))
                    if bar_date <= decision_date and close and close > 0 and volume is not None and volume >= 0:
                        bars[symbol].append(
                            {"date": bar_date.isoformat(), "close": close, "volume": volume}
                        )
            page_token = str(payload.get("next_page_token") or "")
            if not page_token:
                break
    for symbol in bars:
        deduplicated = {row["date"]: row for row in bars[symbol]}
        bars[symbol] = [deduplicated[key] for key in sorted(deduplicated)]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=("ticker", "date", "close", "volume"))
    writer.writeheader()
    for symbol in normalized:
        for row in bars[symbol]:
            writer.writerow({"ticker": symbol, **row})
    raw = output.getvalue().encode("utf-8")
    digest = _sha256_bytes(raw)
    path = cache_root / "prices" / f"alpaca_sip_{decision_date:%Y%m%d}_{digest}.csv"
    _atomic_bytes(path, raw)
    return bars, path, {
        "sha256": digest,
        "request_count": request_count,
        "symbol_count": len(normalized),
        "feed": config["alpaca_feed"],
        "adjustment": config["alpaca_adjustment"],
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def load_captured_daily_bars(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Replay a hash-verified captured Alpaca daily-bar artifact."""

    bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                symbol = str(raw.get("ticker") or "").upper().strip()
                bar_date = _iso_date(raw.get("date"), "captured Alpaca bar date")
                close = _number(raw.get("close"))
                volume = _number(raw.get("volume"))
                if not symbol or close is None or close <= 0 or volume is None or volume < 0:
                    raise LiveFactorDataError("captured Alpaca bar contains an invalid row")
                bars[symbol].append(
                    {"date": bar_date.isoformat(), "close": close, "volume": volume}
                )
    except OSError as exc:
        raise LiveFactorDataError("captured Alpaca bars cannot be read") from exc
    return dict(bars)


def capture_factor_window(
    database: Path,
    decision_date: date,
    *,
    cache_root: Path,
    lookback: int = 756,
) -> tuple[list[date], Path, dict[str, Any]]:
    """Freeze the exact FF vintage and rows used for risk eligibility."""

    if not database.is_file():
        raise LiveFactorDataError(f"Fama-French database does not exist: {database}")
    month_index = decision_date.year * 12 + decision_date.month - 1 - 2
    cutoff_year, cutoff_month_zero = divmod(month_index, 12)
    cutoff_month = cutoff_month_zero + 1
    cutoff = date(
        cutoff_year, cutoff_month, calendar.monthrange(cutoff_year, cutoff_month)[1]
    )
    connection = sqlite3.connect(str(database))
    try:
        vintage = connection.execute(
            "SELECT vintage_id,source_release,fetched_at_utc,ff5_zip_sha256,"
            "momentum_zip_sha256 FROM fama_french_vintages WHERE date(fetched_at_utc)<=? "
            "ORDER BY fetched_at_utc DESC LIMIT 1",
            (decision_date.isoformat(),),
        ).fetchone()
        if not vintage:
            raise LiveFactorDataError("no Fama-French vintage existed by the live decision")
        rows = connection.execute(
            "SELECT trade_date,mkt_rf,smb,hml,rmw,cma,mom,rf FROM fama_french_daily "
            "WHERE vintage_id=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (vintage[0], cutoff.isoformat(), int(lookback)),
        ).fetchall()
    except sqlite3.Error as exc:
        raise LiveFactorDataError("Fama-French database schema is invalid") from exc
    finally:
        connection.close()
    rows = sorted(rows, key=lambda row: row[0])
    dates = [_iso_date(row[0], "factor trade_date") for row in rows]
    if len(dates) < 504:
        raise LiveFactorDataError("Fama-French risk window has fewer than 504 observations")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "version": 1,
        "decision_date": decision_date.isoformat(),
        "contractual_cutoff": cutoff.isoformat(),
        "vintage_id": vintage[0],
        "source_release": vintage[1],
        "vintage_fetched_at_utc": vintage[2],
        "ff5_zip_sha256": vintage[3],
        "momentum_zip_sha256": vintage[4],
        "retrieved_at_utc": retrieved_at,
        "rows": [
            {
                "trade_date": row[0], "mkt_rf": row[1], "smb": row[2],
                "hml": row[3], "rmw": row[4], "cma": row[5],
                "mom": row[6], "rf": row[7],
            }
            for row in rows
        ],
    }
    raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = _sha256_bytes(raw)
    path = cache_root / "fama_french" / f"ff6_{decision_date:%Y%m%d}_{digest}.json"
    _atomic_bytes(path, raw)
    return dates, path, {
        "sha256": digest,
        "vintage_id": vintage[0],
        "retrieved_at_utc": retrieved_at,
    }


def load_captured_factor_window(path: Path) -> tuple[list[date], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveFactorDataError("captured Fama-French window is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("rows"), list):
        raise LiveFactorDataError("captured Fama-French window contract is invalid")
    dates = [_iso_date(row.get("trade_date"), "captured factor trade_date") for row in payload["rows"]]
    if len(dates) < 504 or dates != sorted(dates):
        raise LiveFactorDataError("captured Fama-French window is incomplete or unordered")
    return dates, str(payload.get("vintage_id") or "")


def _price_with_missing_momentum(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: row["date"])
    latest = ordered[-1]
    close = _number(latest.get("close"))
    if close is None or close <= 0:
        return None
    dollar = []
    for row in ordered[-20:]:
        row_close, volume = _number(row.get("close")), _number(row.get("volume"))
        if row_close is not None and volume is not None and row_close > 0 and volume >= 0:
            dollar.append(row_close * volume)
    result = {
        "decision_close": close,
        "momentum_12_1": None,
        "adv20_usd": sum(dollar) / len(dollar) if dollar else None,
        "adv20_observations": len(dollar),
        "price_as_of_date": str(latest["date"]),
    }
    if len(ordered) >= 253:
        result.update(calculate_price_signals(ordered))
    return result


def _paired_factor_observations(
    rows: Iterable[Mapping[str, Any]], factor_dates: Iterable[date]
) -> int:
    dates = sorted({_iso_date(row["date"], "price date") for row in rows})
    returns_available = set(dates[1:])
    return len(returns_available.intersection(set(factor_dates)))


def _complete_fundamental(fundamental: Mapping[str, Any]) -> bool:
    values = [
        _number(fundamental.get(key))
        for key in (
            "assets_current", "assets_lag_4q", "net_income_ttm",
            "operating_income_ttm", "shares_outstanding",
        )
    ]
    return (
        all(value is not None for value in values)
        and bool(fundamental.get("fundamental_available_date"))
        and values[0] > 0 and values[1] > 0 and values[4] > 0
    )


def _capture_file(cache_root: Path, decision_date: date) -> Path:
    return cache_root / "captures" / decision_date.isoformat() / "capture.json"


def _verified_capture_artifact(raw: Mapping[str, Any], label: str) -> Path:
    path = Path(str(raw.get("path") or ""))
    digest = str(raw.get("sha256") or "").lower()
    if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise LiveFactorDataError(f"live capture {label} artifact is invalid")
    if _file_sha256(path) != digest:
        raise LiveFactorDataError(f"live capture {label} artifact hash changed")
    return path


def initialize_or_load_live_capture(
    *,
    cache_root: Path,
    data_config: Mapping[str, Any],
    decision_date: date,
    observed_at: datetime,
    api_key: str,
    secret_key: str,
    session: requests.Session | None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[tuple[int, int, str]]]:
    """Freeze the date-sensitive universe once; later retries replay this capture."""

    capture_path = _capture_file(cache_root, decision_date)
    if capture_path.is_file():
        try:
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveFactorDataError("live capture manifest is invalid") from exc
        if (
            not isinstance(capture, dict)
            or capture.get("version") != 1
            or capture.get("decision_date") != decision_date.isoformat()
            or not isinstance(capture.get("market_sessions"), list)
        ):
            raise LiveFactorDataError("live capture manifest contract is invalid")
        try:
            frozen_observed = datetime.fromisoformat(str(capture["observed_at_utc"]))
        except (KeyError, ValueError) as exc:
            raise LiveFactorDataError("live capture observation time is invalid") from exc
        captured_constituents = capture.get("constituents")
        if not isinstance(captured_constituents, dict):
            raise LiveFactorDataError("live capture constituent receipt is invalid")
        expected_capture_id = f"{decision_date:%Y%m%d}_{str(captured_constituents.get('sha256') or '')[:16]}"
        if frozen_observed.tzinfo is None or capture.get("universe_capture_id") != expected_capture_id:
            raise LiveFactorDataError("live capture identity or timezone is invalid")
        validate_live_decision(
            decision_date=decision_date,
            observed_at=frozen_observed,
            market_sessions=[_iso_date(value, "capture market session") for value in capture["market_sessions"]],
        )
        constituent_path = _verified_capture_artifact(capture["constituents"], "constituents")
        industry_path = _verified_capture_artifact(capture["industries"], "industries")
        validate_constituent_approval(data_config, capture["constituents"]["sha256"])
        constituents = parse_sp500_csv(
            constituent_path.read_bytes(),
            minimum_constituents=int(data_config["minimum_constituents"]),
            maximum_constituents=int(data_config["maximum_constituents"]),
        )
        mapping = parse_ff12_sic_text(industry_path.read_text(encoding="utf-8"))
        return capture, constituents, mapping

    month_start = decision_date.replace(day=1)
    month_end = decision_date.replace(
        day=calendar.monthrange(decision_date.year, decision_date.month)[1]
    )
    market_sessions = fetch_alpaca_calendar(
        api_key, secret_key, start=month_start, end=month_end, session=session
    )
    validate_live_decision(
        decision_date=decision_date, observed_at=observed_at, market_sessions=market_sessions
    )
    constituents, constituent_path, constituent_receipt = fetch_current_sp500(
        data_config, cache_root, session=session
    )
    validate_constituent_approval(data_config, constituent_receipt["sha256"])
    mapping, industry_path, industry_receipt = fetch_ff12_mapping(cache_root, session=session)
    capture_id = f"{decision_date:%Y%m%d}_{constituent_receipt['sha256'][:16]}"
    capture = {
        "version": 1,
        "universe_capture_id": capture_id,
        "decision_date": decision_date.isoformat(),
        "observed_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
        "market_sessions": [value.isoformat() for value in market_sessions],
        "constituents": {
            **constituent_receipt,
            "path": str(constituent_path),
            "sha256": _file_sha256(constituent_path),
        },
        "industries": {
            **industry_receipt,
            "path": str(industry_path),
            "sha256": _file_sha256(industry_path),
        },
    }
    _atomic_json(capture_path, capture)
    return capture, constituents, mapping


def build_live_signal_snapshot(
    *,
    root: Path,
    decision_date: date,
    observed_at: datetime | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Acquire/replay one capture under the same lock used to publish latest files."""

    root = root.resolve()
    payload = load_config(root / "config.yaml")
    data_config = get_factor_data_config(payload)
    if not data_config["enabled"]:
        raise LiveFactorDataError("factor data acquisition is disabled")
    cache_root = (root / str(data_config["cache_dir"])).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(cache_root / "build.lock", label="Live factor-data build"):
        return _build_live_signal_snapshot_locked(
            root=root,
            decision_date=decision_date,
            observed_at=observed_at,
            session=session,
            payload=payload,
            data_config=data_config,
            cache_root=cache_root,
        )


def _build_live_signal_snapshot_locked(
    *,
    root: Path,
    decision_date: date,
    observed_at: datetime | None,
    session: requests.Session | None,
    payload: Mapping[str, Any],
    data_config: Mapping[str, Any],
    cache_root: Path,
) -> dict[str, Any]:
    portfolio_config = get_factor_portfolio_config(dict(payload))
    api_key, secret_key, _ = get_alpaca_credentials(dict(payload))
    observed = observed_at or datetime.now(timezone.utc)
    capture, constituents, mapping = initialize_or_load_live_capture(
        cache_root=cache_root,
        data_config=data_config,
        decision_date=decision_date,
        observed_at=observed,
        api_key=api_key,
        secret_key=secret_key,
        session=session,
    )
    capture_dir = _capture_file(cache_root, decision_date).parent
    print(f"Live universe capture: {capture['universe_capture_id']}", flush=True)
    fundamentals, fundamentals_path, fundamentals_receipt = SecLiveUpdater(
        data_config, cache_root, session=session
    ).sync(
        constituents,
        decision_date=decision_date,
        checkpoint_path=capture_dir / "sec_checkpoint.json",
        constituent_sha256=str(capture["constituents"]["sha256"]),
    )
    price_checkpoint_path = capture_dir / "prices.json"
    if price_checkpoint_path.is_file():
        try:
            price_checkpoint = json.loads(price_checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveFactorDataError("price capture checkpoint is invalid") from exc
        if (
            not isinstance(price_checkpoint, dict)
            or price_checkpoint.get("version") != 1
            or price_checkpoint.get("decision_date") != decision_date.isoformat()
            or price_checkpoint.get("constituent_sha256") != capture["constituents"]["sha256"]
        ):
            raise LiveFactorDataError("price capture checkpoint contract is invalid")
        prices_path = _verified_capture_artifact(price_checkpoint, "prices")
        bars = load_captured_daily_bars(prices_path)
        prices_receipt = dict(price_checkpoint.get("receipt") or {})
    else:
        bars, prices_path, prices_receipt = fetch_alpaca_daily_bars(
            (row["ticker"] for row in constituents),
            decision_date=decision_date,
            api_key=api_key,
            secret_key=secret_key,
            config=data_config,
            cache_root=cache_root,
            session=session,
        )
        _atomic_json(
            price_checkpoint_path,
            {
                "version": 1,
                "decision_date": decision_date.isoformat(),
                "constituent_sha256": capture["constituents"]["sha256"],
                "path": str(prices_path),
                "sha256": _file_sha256(prices_path),
                "receipt": prices_receipt,
            },
        )
    factor_checkpoint_path = capture_dir / "fama_french.json"
    if factor_checkpoint_path.is_file():
        try:
            factor_checkpoint = json.loads(factor_checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveFactorDataError("Fama-French capture checkpoint is invalid") from exc
        if (
            not isinstance(factor_checkpoint, dict)
            or factor_checkpoint.get("version") != 1
            or factor_checkpoint.get("decision_date") != decision_date.isoformat()
            or factor_checkpoint.get("constituent_sha256") != capture["constituents"]["sha256"]
        ):
            raise LiveFactorDataError("Fama-French capture checkpoint contract is invalid")
        factor_path = _verified_capture_artifact(factor_checkpoint, "Fama-French")
        factor_dates, factor_vintage_id = load_captured_factor_window(factor_path)
        factor_receipt = dict(factor_checkpoint.get("receipt") or {})
        if factor_receipt.get("vintage_id") != factor_vintage_id:
            raise LiveFactorDataError("Fama-French checkpoint vintage does not match its artifact")
    else:
        factor_dates, factor_path, factor_receipt = capture_factor_window(
            (root / str(portfolio_config["factor_db"])).resolve(),
            decision_date,
            cache_root=cache_root,
            lookback=int(data_config["price_lookback_sessions"]),
        )
        _atomic_json(
            factor_checkpoint_path,
            {
                "version": 1,
                "decision_date": decision_date.isoformat(),
                "constituent_sha256": capture["constituents"]["sha256"],
                "path": str(factor_path),
                "sha256": _file_sha256(factor_path),
                "receipt": factor_receipt,
            },
        )

    signal_rows: list[dict[str, Any]] = []
    fundamental_source_count = 0
    price_count = 0
    industry_count = 0
    complete_signal_count = 0
    for constituent in constituents:
        fundamental = fundamentals.get(constituent["cik"]) or {}
        price = _price_with_missing_momentum(bars.get(constituent["ticker"], [])) or {}
        if fundamental.get("fundamental_available_date"):
            fundamental_source_count += 1
        if price.get("price_as_of_date") == decision_date.isoformat():
            price_count += 1
        industry = ff12_for_sic(fundamental.get("sic"), mapping)
        if industry:
            industry_count += 1
        complete = _complete_fundamental(fundamental) and price.get("momentum_12_1") is not None
        if complete:
            complete_signal_count += 1
        if not (
            fundamental.get("fundamental_available_date")
            and price.get("price_as_of_date") == decision_date.isoformat()
            and industry
        ):
            continue
        signal_rows.append(
            assemble_signal_row(
                constituent=constituent,
                fundamental=fundamental,
                price=price,
                ff12_mapping=mapping,
                decision_date=decision_date,
                paired_ff6_observations=_paired_factor_observations(
                    bars.get(constituent["ticker"], []), factor_dates
                ),
                minimum_risk_observations=int(data_config["minimum_risk_observations"]),
                minimum_adv20_observations=int(data_config["minimum_adv20_observations"]),
            )
        )
    coverage = validate_live_coverage(
        constituent_count=len(constituents),
        cik_count=sum(bool(row.get("cik")) for row in constituents),
        fundamental_count=fundamental_source_count,
        price_count=price_count,
        industry_count=industry_count,
        complete_signal_count=complete_signal_count,
        minimum_constituents=int(data_config["minimum_constituents"]),
        maximum_constituents=int(data_config["maximum_constituents"]),
        minimum_cik_coverage=float(data_config["minimum_cik_coverage"]),
        minimum_fundamental_coverage=float(data_config["minimum_fundamental_coverage"]),
        minimum_price_coverage=float(data_config["minimum_price_coverage"]),
        minimum_industry_coverage=float(data_config["minimum_industry_coverage"]),
        minimum_complete_signal_coverage=float(data_config["minimum_complete_signal_coverage"]),
    )
    if len(signal_rows) < int(data_config["minimum_constituents"] * data_config["minimum_industry_coverage"]):
        raise LiveFactorDataError("too few identity-complete rows remain for cross-sectional ranking")

    signal_output = (root / str(data_config["signal_output"])).resolve()
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=SIGNAL_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in sorted(signal_rows, key=lambda value: value["ticker"]):
        writer.writerow(row)
    signal_raw = csv_buffer.getvalue().encode("utf-8")
    signal_hash = _sha256_bytes(signal_raw)
    immutable_artifact_dir = cache_root / "signals" / decision_date.isoformat()
    immutable_signal_path = immutable_artifact_dir / f"signal_{signal_hash}.csv"
    if immutable_signal_path.exists() and _file_sha256(immutable_signal_path) != signal_hash:
        raise LiveFactorDataError("immutable signal artifact path contains different bytes")
    if not immutable_signal_path.exists():
        _atomic_bytes(immutable_signal_path, signal_raw)
    _atomic_bytes(signal_output, signal_raw)
    source_snapshots = {
        "constituents": {
            **capture["constituents"],
            "available_through": decision_date.isoformat(),
        },
        "fundamentals": {
            "path": str(fundamentals_path),
            "sha256": _file_sha256(fundamentals_path),
            "available_through": decision_date.isoformat(),
            "errors": fundamentals_receipt["errors"],
            "retrieved_at_utc": fundamentals_receipt["retrieved_at_utc"],
        },
        "prices": {
            **prices_receipt,
            "path": str(prices_path),
            "sha256": _file_sha256(prices_path),
            "available_through": decision_date.isoformat(),
        },
        "industries": {
            **capture["industries"],
            "available_through": decision_date.isoformat(),
        },
        "fama_french": {
            **factor_receipt,
            "path": str(factor_path),
            "sha256": _file_sha256(factor_path),
            "available_through": decision_date.isoformat(),
        },
    }
    source_hashes = {
        name: str(source_snapshots[name]["sha256"]) for name in source_snapshots
    }
    bundle_id = factor_bundle_id(decision_date.isoformat(), source_hashes)
    retrieved_times = [
        str(source_snapshots[name].get("retrieved_at_utc") or "")
        for name in source_snapshots
    ]
    if any(not value for value in retrieved_times):
        raise LiveFactorDataError("source retrieval audit is incomplete")
    manifest = {
        "version": 1,
        "research_id": portfolio_config["research_id"],
        "universe_mode": "latest_only",
        "membership_date": decision_date.isoformat(),
        "decision_date": decision_date.isoformat(),
        "universe_observed_at_utc": capture["observed_at_utc"],
        "built_at_utc": max(retrieved_times),
        "universe_capture_id": capture["universe_capture_id"],
        "bundle_id": bundle_id,
        "signal_path": str(immutable_signal_path),
        "signal_sha256": signal_hash,
        "immutable_artifact_dir": str(immutable_artifact_dir),
        "coverage": coverage,
        "source_snapshots": source_snapshots,
    }
    manifest_output = (root / str(data_config["manifest_output"])).resolve()
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_hash = _sha256_bytes(manifest_raw)
    immutable_manifest_path = immutable_artifact_dir / f"manifest_{manifest_hash}.json"
    if immutable_manifest_path.exists() and _file_sha256(immutable_manifest_path) != manifest_hash:
        raise LiveFactorDataError("immutable manifest artifact path contains different bytes")
    if not immutable_manifest_path.exists():
        _atomic_bytes(immutable_manifest_path, manifest_raw)
    _atomic_bytes(manifest_output, manifest_raw)
    return {
        "status": "passed",
        "universe_capture_id": capture["universe_capture_id"],
        "bundle_id": bundle_id,
        "decision_date": decision_date.isoformat(),
        "constituents": len(constituents),
        "signal_rows": len(signal_rows),
        "risk_eligible_rows": sum(bool(row["risk_eligible"]) for row in signal_rows),
        "coverage": coverage,
        "signal_path": str(immutable_signal_path),
        "latest_signal_path": str(signal_output),
        "signal_sha256": signal_hash,
        "manifest_path": str(immutable_manifest_path),
        "latest_manifest_path": str(manifest_output),
        "manifest_sha256": manifest_hash,
        "github_commit": capture["constituents"]["commit"],
    }


def probe_live_sources(root: Path, *, session: requests.Session | None = None) -> dict[str, Any]:
    """Read-only source probe; it never generates a target or submits an order."""

    root = root.resolve()
    payload = load_config(root / "config.yaml")
    config = get_factor_data_config(payload)
    if not config["enabled"]:
        raise LiveFactorDataError("factor data acquisition is disabled")
    cache_root = (root / str(config["cache_dir"])).resolve()
    with exclusive_file_lock(cache_root / "build.lock", label="Live factor-data build"):
        return _probe_live_sources_locked(root, payload, config, cache_root, session=session)


def _probe_live_sources_locked(
    root: Path,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    cache_root: Path,
    *,
    session: requests.Session | None,
) -> dict[str, Any]:
    api_key, secret_key, _ = get_alpaca_credentials(payload)
    constituents, _, github = fetch_current_sp500(config, cache_root, session=session)
    mapping, _, ff12 = fetch_ff12_mapping(cache_root, session=session)
    sample = next((row for row in constituents if row["ticker"] == "AAPL"), constituents[0])
    updater = SecLiveUpdater(config, cache_root, session=session)
    submissions_url = (
        f"{str(config['sec_data_base_url']).rstrip('/')}/submissions/CIK{sample['cik']}.json"
    )
    submissions = json.loads(updater._request(submissions_url))
    facts_url = (
        f"{str(config['sec_data_base_url']).rstrip('/')}/api/xbrl/"
        f"companyfacts/CIK{sample['cik']}.json"
    )
    facts = json.loads(updater._request(facts_url))
    latest_session = fetch_alpaca_calendar(
        api_key,
        secret_key,
        start=date.today() - timedelta(days=10),
        end=date.today(),
        session=session,
    )[-1]
    sample_bars, _, prices = fetch_alpaca_daily_bars(
        [sample["ticker"]],
        decision_date=latest_session,
        api_key=api_key,
        secret_key=secret_key,
        config=config,
        cache_root=cache_root,
        session=session,
    )
    fundamental = derive_sec_fundamentals(
        submissions, facts, decision_date=latest_session
    )
    return {
        "status": "passed",
        "orders_submitted": 0,
        "sp500_constituents": len(constituents),
        "github_commit": github["commit"],
        "constituent_sha256": github["sha256"],
        "constituent_approved": github["sha256"] == config["approved_constituent_sha256"],
        "ff12_ranges": len(mapping),
        "ff12_sha256": ff12["sha256"],
        "sec_sample": sample["ticker"],
        "sec_fundamental_available_date": fundamental.get("fundamental_available_date"),
        "alpaca_feed": prices["feed"],
        "alpaca_sample_bars": len(sample_bars.get(sample["ticker"], [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-date", default="", help="Live month-end YYYY-MM-DD")
    parser.add_argument("--probe-sources", action="store_true", help="Test sources without building a target")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.probe_sources:
        print(json.dumps(probe_live_sources(root), ensure_ascii=False, indent=2))
        return
    if not args.decision_date:
        raise LiveFactorDataError("--decision-date is required for a production snapshot")
    decision = _iso_date(args.decision_date, "decision_date")
    print(json.dumps(build_live_signal_snapshot(root=root, decision_date=decision), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
