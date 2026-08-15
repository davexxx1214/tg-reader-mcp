"""Versioned hashes shared by live-data publication and portfolio verification."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping


SOURCE_NAMES = ("constituents", "fundamentals", "prices", "industries", "fama_french")


def factor_bundle_id(decision_date: str, source_hashes: Mapping[str, str]) -> str:
    normalized = {name: str(source_hashes.get(name) or "").lower() for name in SOURCE_NAMES}
    if set(source_hashes) != set(SOURCE_NAMES) or any(
        not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in normalized.values()
    ):
        raise ValueError("factor bundle requires five valid source SHA-256 values")
    raw = json.dumps(
        {"version": 1, "decision_date": decision_date, "source_sha256": normalized},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
