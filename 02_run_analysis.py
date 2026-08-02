"""
ESRI STEP 3 & 4: Choose methods and perform analysis (batch / demo mode).

GIS-MCDA framework:
  - AHP (Saaty 1980) criteria weights for Flood vs Fault
  - FEMA NFIP flood sub-scores
  - Continuous Alquist-Priolo exponential fault distance decay

Exports GeoParquet (+ FlatGeobuf) for 03_build_app.py.
"""

import os

from risk_engine import (
    DEFAULT_AHP_CRITERIA,
    DEFAULT_AHP_MATRIX,
    FAULT_DECAY_LAMBDA,
    WGS84,
    calculate_fault_score,
    compute_ahp_weights,
    run_neighborhood_analysis,
    summary_table,
    write_geodata,
)

# Keep in sync with 01_prepare_data.py demo defaults
DEMO_STREET = "RIVERSIDE"
DEMO_CITY = "RENO"
DEMO_MATCH_LIMIT = 25
DEMO_RADIUS_M = 1000
DEMO_NEIGHBORHOOD_LIMIT = 300

print("\n=========================================================")
print(" ESRI STEP 3 & 4: GIS-MCDA RISK ANALYSIS")
print("=========================================================\n")

print("[1/4] Analytic Hierarchy Process (AHP) criteria weighting...")
print("  Pairwise preference: Flood preferred 1.5× over Fault")
print("  (regional flood recurrence frequency vs fault exposure)")
print(f"  Matrix: {DEFAULT_AHP_MATRIX}")
ahp = compute_ahp_weights(
    pairwise_matrix=DEFAULT_AHP_MATRIX,
    criteria_names=DEFAULT_AHP_CRITERIA,
    verbose=True,
)
weights = ahp.as_dict()
flood_weight = float(weights["Flood"])
fault_weight = float(weights["Fault"])
if not ahp.is_consistent():
    raise SystemExit("AHP Consistency Ratio CR >= 0.10 — revise pairwise matrix.")
print(f"  └─ Adopted weights → Flood={flood_weight:.4f}, Fault={fault_weight:.4f}\n")

print("[2/4] Fault proximity: continuous exponential distance decay")
print(f"  Formula: fault_subscore = 100 * exp(-λ * d_meters)")
print(f"  λ = {FAULT_DECAY_LAMBDA} (calibrated to ~80 at 150 m Alquist-Priolo boundary)")
for d in (0, 150, 500, 2000):
    print(f"  └─ d = {d:>4} m → fault_subscore = {calculate_fault_score(d):6.2f}")
print("")

print("[3/4] Overlay analysis + proximity MCDA...")
print("  Flood sub-scores (FEMA NFIP):")
print("    SFHA 100-yr (A/AE/AH/AO/VE) = 100 | 500-yr (X500/B) = 50 | Zone X = 0")
print(
    f"  Composite = flood×{flood_weight:.4f} + fault×{fault_weight:.4f}"
)
print("  Tiers: HIGH ≥ 70 | MODERATE 30–69 | LOW < 30")

matches, parcels, fema_zones, fault_lines = run_neighborhood_analysis(
    street=DEMO_STREET,
    city=DEMO_CITY,
    radius_m=DEMO_RADIUS_M,
    match_limit=DEMO_MATCH_LIMIT,
    neighborhood_limit=DEMO_NEIGHBORHOOD_LIMIT,
)

if matches.empty or parcels.empty:
    raise SystemExit(
        f"No parcels for STREET LIKE '%{DEMO_STREET}%' in {DEMO_CITY}. "
        "Update DEMO_STREET / DEMO_CITY."
    )

required_cols = [
    "APN",
    "SITUS_ADDRESS",
    "flood_subscore",
    "fault_dist_meters",
    "fault_subscore",
    "composite_risk_score",
    "risk_category",
]
missing = [c for c in required_cols if c not in parcels.columns]
if missing:
    raise SystemExit(f"Missing required output columns: {missing}")

print(f"  └─ Search matches: {len(matches)} | Neighborhood parcels: {len(parcels)}")
print(f"  └─ Flood polygons: {len(fema_zones)} | Fault segments: {len(fault_lines)}")
print(f"  └─ Parcels with flood_subscore > 0: {(parcels['flood_subscore'] > 0).sum()}")
avg_dist = parcels["fault_dist_meters"].replace([float("inf")], float("nan")).mean()
print(f"  └─ Average fault distance: {avg_dist:.1f} m")
print(f"  └─ Mean composite score: {parcels['composite_risk_score'].mean():.2f}")

print("\n[4/4] Exporting analyzed parcels (EPSG:4326 GeoParquet + FlatGeobuf)...")
os.makedirs("outputs", exist_ok=True)
written = write_geodata(
    parcels,
    "outputs/analyzed_parcels",
    formats=("parquet", "fgb"),
    target_crs=WGS84,
    layer_name="analyzed_parcels",
)
for path in written:
    print(f"  └─ Saved '{path}'")

print("\n=========================================================")
print(" ANALYSIS SUMMARY RESULTS")
print("=========================================================")
print(summary_table(parcels).head(10).to_string())
print("\nRisk Tier Distribution:")
print(parcels["risk_category"].value_counts().to_string())
print("=========================================================")
print(" Next: python3 03_build_app.py")
print("=========================================================")
