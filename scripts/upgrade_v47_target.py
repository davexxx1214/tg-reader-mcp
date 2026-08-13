#!/usr/bin/env python3
"""Create a frozen V4.7 score-tilted target from an approved V4.6-R1 target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from factor_portfolio import (
    FactorPortfolioError,
    allocate_score_tilt,
    effective_config_sha256,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_v47_target_from_v46(
    baseline: Mapping[str, Any], *, predecessor_sha256: str,
    predecessor_filename: str = "factor_portfolio_v4_6_r1_20260812.json",
) -> dict[str, Any]:
    """Preserve the frozen Top 10 and change only its allocation contract."""
    if (
        not isinstance(baseline, Mapping)
        or baseline.get("method") != "v4_6_r1_factor_selection"
        or baseline.get("research_id") != "v4_6_r1_0001"
        or baseline.get("parameter_mode") != "frozen"
        or baseline.get("allocation_method") != "equal_weight"
    ):
        raise FactorPortfolioError("V4.7 predecessor is not a frozen V4.6-R1 target")
    predecessor_hash = str(predecessor_sha256).lower()
    if len(predecessor_hash) != 64 or any(
        character not in "0123456789abcdef" for character in predecessor_hash
    ):
        raise FactorPortfolioError("V4.7 predecessor SHA-256 is invalid")
    predecessor_name = Path(str(predecessor_filename)).name
    if not predecessor_name or predecessor_name != str(predecessor_filename):
        raise FactorPortfolioError("V4.7 predecessor filename is invalid")
    selected = baseline.get("selected")
    if not isinstance(selected, list) or len(selected) != 10:
        raise FactorPortfolioError("V4.7 predecessor must contain exactly ten stocks")
    ordered = sorted(selected, key=lambda row: int(row.get("selection_rank", 0)))
    if [int(row.get("selection_rank", 0)) for row in ordered] != list(range(1, 11)):
        raise FactorPortfolioError("V4.7 predecessor selection ranks are invalid")
    tilted = allocate_score_tilt(
        ordered,
        power=6.0,
        minimum_weight=0.05,
        maximum_weight=0.20,
        maximum_industry_weight=0.35,
    )
    effective_config = dict(baseline.get("effective_config") or {})
    effective_config.update(
        {
            "allocation_method": "score_tilt",
            "score_power": 6.0,
            "minimum_weight": 0.05,
            "maximum_weight": 0.20,
            "maximum_industry_weight": 0.35,
        }
    )
    config_hash = effective_config_sha256(effective_config)
    return {
        **dict(baseline),
        "method": "v4_7_factor_selection_score_tilt",
        "research_id": "v4_7_0001",
        "allocation_method": "score_tilt",
        "score_power": 6.0,
        "effective_config": effective_config,
        "effective_config_sha256": config_hash,
        "baseline_deviations": {},
        "predecessor_target": {
            "research_id": "v4_6_r1_0001",
            "sha256": predecessor_hash,
            "artifact_filename": predecessor_name,
        },
        "selected": tilted,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upgrade an approved V4.6-R1 target to V4.7")
    parser.add_argument("--input", required=True, help="Frozen V4.6-R1 target JSON")
    parser.add_argument("--approved-sha256", required=True, help="Approved predecessor hash")
    parser.add_argument("--output", required=True, help="V4.7 target JSON")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    raw = input_path.read_bytes()
    actual_hash = _sha256_bytes(raw)
    if actual_hash != str(args.approved_sha256).lower():
        raise FactorPortfolioError("V4.6-R1 target does not match the approved SHA-256")
    try:
        baseline = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactorPortfolioError("V4.6-R1 target is invalid JSON") from exc
    payload = build_v47_target_from_v46(
        baseline,
        predecessor_sha256=actual_hash,
        predecessor_filename=input_path.name,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_path)
    print(json.dumps({"output": str(output_path), "sha256": _sha256_bytes(output_path.read_bytes())}))


if __name__ == "__main__":
    main()
