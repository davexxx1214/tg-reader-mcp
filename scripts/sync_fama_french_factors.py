#!/usr/bin/env python3
"""Download official Fama-French daily FF5 + Momentum into audited SQLite.

Adapted from factor-model/src/data_sources/factors.py and the V4.6-R1 source
snapshot workflow. Source percentages are stored as decimal simple returns.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


FF5_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)
MOMENTUM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Momentum_Factor_daily_CSV.zip"
)
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "fama_french_daily.sqlite"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "fama_french_manifest.json"
DEFAULT_VINTAGE_ROOT = Path(__file__).resolve().parents[1] / "data" / "fama_french_vintages"
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CSV_BYTES = 50 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
MAX_CSV_ROWS = 100_000
MAX_CSV_LINE_LENGTH = 16_384


class FactorSyncError(ValueError):
    """Raised when an official factor download or parse is unsafe."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_csv(archive: bytes, label: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = [entry for entry in bundle.infolist() if entry.filename.lower().endswith(".csv")]
            if len(entries) != 1:
                raise FactorSyncError(f"{label} archive must contain exactly one CSV")
            entry = entries[0]
            ratio = entry.file_size / max(entry.compress_size, 1)
            if entry.file_size > MAX_EXTRACTED_CSV_BYTES or ratio > MAX_COMPRESSION_RATIO:
                raise FactorSyncError(f"{label} CSV exceeds safe extraction limits")
            return bundle.read(entry)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise FactorSyncError(f"{label} archive is invalid") from exc


def parse_factor_csv(
    data: bytes,
    expected_columns: list[str],
    header_marker: str,
) -> list[dict[str, Any]]:
    """Parse a Kenneth French CSV and convert percentage values to decimals."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FactorSyncError("factor CSV encoding is invalid") from exc
    lines = text.splitlines()
    if len(data) > MAX_EXTRACTED_CSV_BYTES or len(lines) > MAX_CSV_ROWS + 100:
        raise FactorSyncError("factor CSV exceeds safe size limits")
    if any(len(line) > MAX_CSV_LINE_LENGTH for line in lines):
        raise FactorSyncError("factor CSV contains an oversized line")
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith(header_marker)),
        None,
    )
    if header_index is None:
        raise FactorSyncError(f"factor header not found: {header_marker}")
    data_lines = [lines[header_index]]
    for line in lines[header_index + 1 :]:
        first = line.split(",", maxsplit=1)[0].strip()
        if not line.strip() or not first or line.lstrip().lower().startswith("copyright"):
            break
        data_lines.append(line)
    rows = list(csv.reader(io.StringIO("\n".join(data_lines))))
    if len(rows) < 2:
        raise FactorSyncError("factor CSV has no observations")
    header = [column.strip() for column in rows[0]]
    header[0] = "date"
    while header and not header[-1]:
        header.pop()
    if header != ["date", *expected_columns]:
        raise FactorSyncError(f"unexpected factor columns: {header!r}")

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows[1:]:
        if len(raw) < len(header):
            raise FactorSyncError("factor row is incomplete")
        raw_date = raw[0].strip()
        try:
            observation_date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        except ValueError as exc:
            raise FactorSyncError(f"invalid factor date: {raw_date}") from exc
        if observation_date in seen:
            raise FactorSyncError(f"duplicate factor date: {observation_date}")
        seen.add(observation_date)
        row: dict[str, Any] = {"date": observation_date}
        for index, column in enumerate(expected_columns, 1):
            try:
                value = float(raw[index].strip())
            except (ValueError, IndexError) as exc:
                raise FactorSyncError(f"invalid {column} on {observation_date}") from exc
            if value in {-99.99, -999.0}:
                raise FactorSyncError(f"sentinel {column} on {observation_date}")
            row[column] = value / 100.0
        parsed.append(row)
    return sorted(parsed, key=lambda row: row["date"])


def merge_factor_rows(
    ff5_rows: Iterable[dict[str, Any]], momentum_rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Inner-join FF5 and Momentum on unique trading dates."""
    ff5 = {row["date"]: dict(row) for row in ff5_rows}
    momentum = {row["date"]: dict(row) for row in momentum_rows}
    common = sorted(set(ff5) & set(momentum))
    if not common:
        raise FactorSyncError("FF5 and Momentum have no common dates")
    result = []
    for observation_date in common:
        row = dict(ff5[observation_date])
        row["Mom"] = momentum[observation_date]["Mom"]
        result.append(row)
    return result


def _download(url: str, timeout: float) -> tuple[bytes, dict[str, Any]]:
    with requests.get(url, timeout=timeout, stream=True) as response:
        if response.status_code != 200:
            raise FactorSyncError(f"official factor download failed: HTTP {response.status_code}")
        try:
            declared_size = int(response.headers.get("Content-Length", "0") or 0)
        except ValueError:
            declared_size = 0
        if declared_size > MAX_ARCHIVE_BYTES:
            raise FactorSyncError("official factor archive exceeds safe download limits")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > MAX_ARCHIVE_BYTES:
                raise FactorSyncError("official factor archive exceeds safe download limits")
        if not content:
            raise FactorSyncError("official factor archive is empty")
        payload = bytes(content)
        return payload, {
            "url": url,
            "http_status": response.status_code,
            "last_modified": response.headers.get("Last-Modified"),
            "etag": response.headers.get("ETag"),
            "sha256": _sha256(payload),
        }


def _write_sqlite(
    path: Path,
    rows: list[dict[str, Any]],
    release: str,
    fetched_at: str,
    vintage_id: str,
    manifest_path: str,
    ff5_zip_sha256: str = "",
    momentum_zip_sha256: str = "",
) -> None:
    """Append one immutable factor vintage; never update prior observations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(fama_french_daily)")
        }
        if existing_columns and "vintage_id" not in existing_columns:
            migrated = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fama_french_daily_pre_vintage'"
            ).fetchone()
            if migrated:
                raise FactorSyncError("legacy factor table migration target already exists")
            connection.execute(
                "ALTER TABLE fama_french_daily RENAME TO fama_french_daily_pre_vintage"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fama_french_vintages (
                vintage_id TEXT PRIMARY KEY,
                source_release TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                ff5_zip_sha256 TEXT NOT NULL,
                momentum_zip_sha256 TEXT NOT NULL,
                manifest_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fama_french_daily (
                vintage_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                mkt_rf REAL NOT NULL,
                smb REAL NOT NULL,
                hml REAL NOT NULL,
                rmw REAL NOT NULL,
                cma REAL NOT NULL,
                mom REAL NOT NULL,
                rf REAL NOT NULL,
                PRIMARY KEY(vintage_id, trade_date),
                FOREIGN KEY(vintage_id) REFERENCES fama_french_vintages(vintage_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fama_french_sync_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_release TEXT NOT NULL,
                first_date TEXT NOT NULL,
                last_date TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                fetched_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO fama_french_vintages(
                vintage_id,source_release,fetched_at_utc,ff5_zip_sha256,
                momentum_zip_sha256,manifest_path
            ) VALUES (?,?,?,?,?,?)
            """,
            (vintage_id, release, fetched_at, ff5_zip_sha256, momentum_zip_sha256, manifest_path),
        )
        connection.executemany(
            """
            INSERT INTO fama_french_daily(
                vintage_id,trade_date,mkt_rf,smb,hml,rmw,cma,mom,rf
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(vintage_id,trade_date) DO NOTHING
            """,
            [
                (
                    vintage_id, row["date"], row["Mkt-RF"], row["SMB"], row["HML"],
                    row["RMW"], row["CMA"], row["Mom"], row["RF"],
                )
                for row in rows
            ],
        )
        connection.execute(
            """
            INSERT INTO fama_french_sync_audit(
                source_release,first_date,last_date,row_count,fetched_at_utc
            ) VALUES (?,?,?,?,?)
            """,
            (release, rows[0]["date"], rows[-1]["date"], len(rows), fetched_at),
        )
        connection.execute("DROP VIEW IF EXISTS fama_french_latest")
        connection.execute(
            """
            CREATE VIEW fama_french_latest AS
            SELECT d.*, v.source_release, v.fetched_at_utc
            FROM fama_french_daily d
            JOIN fama_french_vintages v USING(vintage_id)
            WHERE v.fetched_at_utc=(SELECT MAX(fetched_at_utc) FROM fama_french_vintages)
            """
        )
        connection.commit()
    finally:
        connection.close()


def sync_factors(
    *,
    database: Path = DEFAULT_DB,
    manifest_path: Path = DEFAULT_MANIFEST,
    vintage_root: Path = DEFAULT_VINTAGE_ROOT,
    ff5_url: str = FF5_URL,
    momentum_url: str = MOMENTUM_URL,
    timeout: float = 30.0,
) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    ff5_zip, ff5_meta = _download(ff5_url, timeout)
    momentum_zip, momentum_meta = _download(momentum_url, timeout)
    ff5_csv = _extract_csv(ff5_zip, "FF5")
    momentum_csv = _extract_csv(momentum_zip, "Momentum")
    ff5_rows = parse_factor_csv(
        ff5_csv, ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"], ",Mkt-RF"
    )
    momentum_rows = parse_factor_csv(momentum_csv, ["Mom"], ",Mom")
    rows = merge_factor_rows(ff5_rows, momentum_rows)
    release = rows[-1]["date"][:7].replace("-", "")
    vintage_id = f"{release}-{_sha256(ff5_zip + momentum_zip)[:12]}"
    vintage_directory = vintage_root / vintage_id
    vintage_directory.mkdir(parents=True, exist_ok=True)

    def preserve(name: str, content: bytes) -> Path:
        destination = vintage_directory / name
        if destination.exists():
            if _sha256(destination.read_bytes()) != _sha256(content):
                raise FactorSyncError(f"immutable vintage artifact changed: {destination}")
            return destination
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        return destination

    artifacts = {
        "ff5_zip": preserve("ff5.zip", ff5_zip),
        "momentum_zip": preserve("momentum.zip", momentum_zip),
        "ff5_csv": preserve("ff5.csv", ff5_csv),
        "momentum_csv": preserve("momentum.csv", momentum_csv),
    }
    manifest = {
        "version": 2,
        "vintage_id": vintage_id,
        "provider": "Kenneth French Data Library",
        "source_release": release,
        "fetched_at_utc": fetched_at,
        "units": "decimal_simple_returns",
        "coverage": {
            "rows": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "ff5_rows": len(ff5_rows),
            "momentum_rows": len(momentum_rows),
        },
        "sources": {"ff5": ff5_meta, "momentum": momentum_meta},
        "parsed_sha256": {"ff5_csv": _sha256(ff5_csv), "momentum_csv": _sha256(momentum_csv)},
        "artifacts": {key: str(value) for key, value in artifacts.items()},
        "database": str(database),
    }
    vintage_manifest = vintage_directory / "manifest.json"
    if vintage_manifest.exists():
        prior = json.loads(vintage_manifest.read_text(encoding="utf-8"))
        comparable_keys = ("vintage_id", "source_release", "coverage", "sources", "parsed_sha256")
        if any(prior.get(key) != manifest.get(key) for key in comparable_keys):
            raise FactorSyncError("immutable vintage manifest changed")
        manifest = prior
    else:
        temporary_vintage = vintage_manifest.with_name(f".{vintage_manifest.name}.{os.getpid()}.tmp")
        temporary_vintage.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_vintage, vintage_manifest)
    _write_sqlite(
        database, rows, release, manifest["fetched_at_utc"], vintage_id, str(vintage_manifest),
        manifest["sources"]["ff5"]["sha256"], manifest["sources"]["momentum"]["sha256"],
    )
    manifest["vintage_manifest"] = str(vintage_manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync official Fama-French daily FF6 factors")
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--vintage-root", default=str(DEFAULT_VINTAGE_ROOT))
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = sync_factors(
        database=Path(args.database),
        manifest_path=Path(args.manifest),
        vintage_root=Path(args.vintage_root),
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
