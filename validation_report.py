#!/usr/bin/env python3
"""
Validation report for Washoe parcel risk scores.

Checks:
  - AHP consistency from config
  - Fault decay calibration checkpoints vs configured targets
  - Score distributions / tier shares
  - Coincidence of HIGH tier with positive FEMA flood sub-score
  - Sensitivity of tier flips when AHP flood weight ±20%

Inputs (first found):
  outputs/reno_risk.parquet
  outputs/analyzed_parcels.geojson

Outputs:
  outputs/validation_report.json
  outputs/validation_report.md
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

from config_loader import (
    citations,
    disclaimer,
    fault_calibration_targets,
    fault_decay_lambda,
    load_scoring_config,
    tier_thresholds,
)
from risk_engine import (
    DEFAULT_AHP_CRITERIA,
    DEFAULT_AHP_MATRIX,
    calculate_fault_score,
    compute_ahp_weights,
    categorize_risk,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"


def load_scored() -> gpd.GeoDataFrame:
    parquet = OUT / "reno_risk.parquet"
    geojson = OUT / "analyzed_parcels.geojson"
    if parquet.exists():
        gdf = gpd.read_parquet(parquet)
        source = str(parquet)
    elif geojson.exists():
        gdf = gpd.read_file(geojson)
        source = str(geojson)
    else:
        raise SystemExit(
            "No scored dataset found. Run 02_run_analysis.py or 04_build_reno_tiles.py first."
        )
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf.attrs["source"] = source
    return gdf


def fault_calibration_check() -> dict:
    lam = fault_decay_lambda()
    rows = []
    for target in fault_calibration_targets():
        d = float(target["distance_m"])
        approx = float(target["score_approx"])
        actual = calculate_fault_score(d, decay_lambda=lam)
        rows.append(
            {
                "distance_m": d,
                "target_approx": approx,
                "model_score": actual,
                "abs_error": round(abs(actual - approx), 2),
            }
        )
    max_err = max((r["abs_error"] for r in rows), default=0.0)
    return {
        "lambda": lam,
        "checkpoints": rows,
        "max_abs_error": max_err,
        "pass_loose_tolerance": max_err <= 5.0,
    }


def coincidence_metrics(gdf: gpd.GeoDataFrame) -> dict:
    n = len(gdf)
    high = gdf[gdf["risk_category"] == "HIGH"] if "risk_category" in gdf.columns else gdf.head(0)
    flood_pos = gdf["flood_subscore"] > 0 if "flood_subscore" in gdf.columns else pd.Series(False, index=gdf.index)
    high_and_flood = high[high["flood_subscore"] > 0] if len(high) and "flood_subscore" in high.columns else high.head(0)
    return {
        "n_parcels": int(n),
        "tier_counts": gdf["risk_category"].value_counts().to_dict() if "risk_category" in gdf.columns else {},
        "pct_flood_positive": round(100.0 * float(flood_pos.mean()), 2) if n else 0.0,
        "n_high": int(len(high)),
        "pct_high_with_flood_gt0": round(
            100.0 * (len(high_and_flood) / len(high)), 2
        )
        if len(high)
        else None,
        "mean_composite": round(float(gdf["composite_risk_score"].mean()), 3)
        if "composite_risk_score" in gdf.columns and n
        else None,
        "mean_fault_subscore": round(float(gdf["fault_subscore"].mean()), 3)
        if "fault_subscore" in gdf.columns and n
        else None,
        "mean_flood_subscore": round(float(gdf["flood_subscore"].mean()), 3)
        if "flood_subscore" in gdf.columns and n
        else None,
    }


def sensitivity_ahp(gdf: gpd.GeoDataFrame, delta: float = 0.20) -> dict:
    """Recompute tiers with flood weight ±delta relative, fault gets remainder."""
    if gdf.empty or "flood_subscore" not in gdf.columns or "fault_subscore" not in gdf.columns:
        return {"skipped": True}

    ahp = compute_ahp_weights(DEFAULT_AHP_MATRIX, DEFAULT_AHP_CRITERIA, verbose=False)
    w_f = float(ahp.as_dict()["Flood"])
    base = gdf["risk_category"].astype(str)

    def tiers_for(flood_w: float) -> pd.Series:
        fault_w = 1.0 - flood_w
        comp = gdf["flood_subscore"] * flood_w + gdf["fault_subscore"] * fault_w
        return comp.apply(categorize_risk).astype(str)

    up = min(0.95, w_f * (1.0 + delta))
    down = max(0.05, w_f * (1.0 - delta))
    tiers_up = tiers_for(up)
    tiers_down = tiers_for(down)
    return {
        "base_flood_weight": round(w_f, 4),
        "flood_weight_plus": round(up, 4),
        "flood_weight_minus": round(down, 4),
        "pct_tier_change_plus": round(100.0 * float((tiers_up != base).mean()), 2),
        "pct_tier_change_minus": round(100.0 * float((tiers_down != base).mean()), 2),
    }


def to_markdown(report: dict) -> str:
    lines = [
        "# Washoe Parcel Risk — Validation Report",
        "",
        f"**Dataset:** `{report['dataset_source']}`",
        f"**Parcels:** {report['coincidence']['n_parcels']}",
        "",
        "## Disclaimer",
        "",
        report.get("disclaimer") or "_None_",
        "",
        "## AHP consistency",
        "",
        f"- Flood weight: **{report['ahp']['weights'].get('Flood', 'n/a'):.4f}**",
        f"- Fault weight: **{report['ahp']['weights'].get('Fault', 'n/a'):.4f}**",
        f"- CI: {report['ahp']['ci']:.6f}",
        f"- CR: {report['ahp']['cr']:.6f}",
        f"- Check: **{'PASS' if report['ahp']['consistent'] else 'FAIL'}**",
        "",
        "## Fault decay calibration",
        "",
        f"- λ = `{report['fault_calibration']['lambda']}`",
        f"- Max |error| vs targets: **{report['fault_calibration']['max_abs_error']}**",
        f"- Within ±5 points: **{'PASS' if report['fault_calibration']['pass_loose_tolerance'] else 'FAIL'}**",
        "",
        "| Distance (m) | Target ≈ | Model | |error| |",
        "|---:|---:|---:|---:|",
    ]
    for row in report["fault_calibration"]["checkpoints"]:
        lines.append(
            f"| {row['distance_m']} | {row['target_approx']} | {row['model_score']} | {row['abs_error']} |"
        )
    lines.extend(
        [
            "",
            "## Score / tier summary",
            "",
            f"- Tier counts: `{report['coincidence']['tier_counts']}`",
            f"- % parcels with flood_subscore > 0: **{report['coincidence']['pct_flood_positive']}**",
            f"- % of HIGH parcels with flood_subscore > 0: **{report['coincidence']['pct_high_with_flood_gt0']}**",
            f"- Mean composite: **{report['coincidence']['mean_composite']}**",
            "",
            "## AHP weight sensitivity (±20% flood weight)",
            "",
            f"- Tier flips if flood weight ↑: **{report['sensitivity'].get('pct_tier_change_plus')}%**",
            f"- Tier flips if flood weight ↓: **{report['sensitivity'].get('pct_tier_change_minus')}%**",
            "",
            "## Citations (from config)",
            "",
        ]
    )
    for c in report.get("citations", []):
        lines.append(
            f"- **{c.get('id')}**: {c.get('author', '')} ({c.get('year', '')}). "
            f"*{c.get('title', '')}*. Used for: {c.get('used_for', '')}"
        )
    lines.append("")
    lines.append(
        "_This report validates internal consistency and calibration targets; "
        "it does not replace local expert review or official hazard maps._"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    cfg = load_scoring_config()
    gdf = load_scored()
    print(f"Loaded {len(gdf)} parcels from {gdf.attrs.get('source')}")

    ahp = compute_ahp_weights(DEFAULT_AHP_MATRIX, DEFAULT_AHP_CRITERIA, verbose=True)
    ahp_block = {
        "weights": ahp.as_dict(),
        "lambda_max": ahp.lambda_max,
        "ci": ahp.ci,
        "cr": ahp.cr,
        "consistent": ahp.is_consistent(),
        "tiers": tier_thresholds(),
    }

    report = {
        "dataset_source": gdf.attrs.get("source"),
        "disclaimer": disclaimer(cfg),
        "ahp": ahp_block,
        "fault_calibration": fault_calibration_check(),
        "coincidence": coincidence_metrics(gdf),
        "sensitivity": sensitivity_ahp(gdf),
        "citations": citations(cfg),
        "config_meta": cfg.get("meta", {}),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / "validation_report.json"
    md_path = OUT / "validation_report.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        f"AHP CR={ahp.cr:.4f} ({'PASS' if ahp.is_consistent() else 'FAIL'}) | "
        f"Fault calib max_err={report['fault_calibration']['max_abs_error']}"
    )


if __name__ == "__main__":
    main()
