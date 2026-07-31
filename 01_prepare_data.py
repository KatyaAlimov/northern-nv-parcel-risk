"""
STEP 2: Data exploration, preparation & audit (batch / demo mode).

Fetches a configurable Washoe street sample plus flood/fault layers via risk_engine,
harmonizes CRS, audits quality, and writes a sanity plot.
"""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from risk_engine import (
    TARGET_CRS,
    bounds_to_envelope,
    envelope_from_parcel_buffers,
    fetch_fault_lines,
    fetch_flood_zones,
    map_flood_risk,
    query_parcels_by_search,
    sanitize_geometries,
)

# Batch demo defaults (override by editing these before running)
DEMO_STREET = "RIVERSIDE"
DEMO_CITY = "RENO"
DEMO_MATCH_LIMIT = 25
DEMO_RADIUS_M = 1000

print("=========================================================")
print(" STEP 2: DATA EXPLORATION, PREPARATION & AUDIT")
print("=========================================================\n")

print("[1/5] Fetching live spatial data layers...")
parcels = query_parcels_by_search(
    street=DEMO_STREET, city=DEMO_CITY, limit=DEMO_MATCH_LIMIT
)
if parcels.empty:
    raise ValueError(
        f"No parcels for STREET LIKE '%{DEMO_STREET}%' in {DEMO_CITY}. "
        "Change DEMO_STREET / DEMO_CITY in this script."
    )
print(f"  └─ Parcels Loaded:     {len(parcels)} features")

query_bounds = envelope_from_parcel_buffers(parcels, DEMO_RADIUS_M)
query_envelope = bounds_to_envelope(query_bounds)

fema_zones = fetch_flood_zones(query_envelope)
print(f"  └─ FEMA Zones Loaded:  {len(fema_zones)} features")

fault_lines = fetch_fault_lines(query_envelope)
print(f"  └─ USGS Faults Loaded: {len(fault_lines)} features\n")

print(f"[2/5] Standardizing CRS projections to {TARGET_CRS} (Meters)...")
parcels = parcels.to_crs(TARGET_CRS)
fema_zones = fema_zones.to_crs(TARGET_CRS) if not fema_zones.empty else fema_zones
fault_lines = fault_lines.to_crs(TARGET_CRS) if not fault_lines.empty else fault_lines
print("  └─ Reprojection complete for all layers.\n")

print("[3/5] Cleaning geometries and repairing invalid polygons...")

def _clean(gdf, name):
    cleaned = sanitize_geometries(gdf)
    print(f"  └─ {name}: Cleaned & Validated ({len(cleaned)} features)")
    return cleaned

parcels = _clean(parcels, "Parcels")
fema_zones = _clean(fema_zones, "FEMA Flood")
fault_lines = _clean(fault_lines, "USGS Faults")
print("")

print("[4/5] Subsetting nationwide layers & mapping numeric risk scores...")
minx, miny, maxx, maxy = parcels.total_bounds
if not fault_lines.empty:
    fault_lines = fault_lines.cx[minx - 5000 : maxx + 5000, miny - 5000 : maxy + 5000]
print(f"  └─ Clipped Fault Lines: {len(fault_lines)} local segments remaining")

if not fema_zones.empty:
    fema_zones = fema_zones.copy()
    fema_zones["flood_subscore"] = fema_zones.apply(map_flood_risk, axis=1)
print("  └─ Derived 'flood_subscore' (0-100 scale) created successfully.\n")

print("[5/5] Executing automated data quality audit...")

def run_gis_data_audit(gdf, layer_name, expected_crs=TARGET_CRS):
    print(f"\n --- AUDIT REPORT: {layer_name} ---")
    if gdf.empty:
        print("  Warning: GeoDataFrame is empty.")
        return
    current_crs = str(gdf.crs) if gdf.crs is not None else "None"
    crs_match = current_crs == expected_crs
    unit_name = gdf.crs.axis_info[0].unit_name if gdf.crs else "Unknown"
    print(f"  [1] CRS Check:        {current_crs} | Units: {unit_name}")
    print(f"      Status:           {'PASS' if crs_match else 'FAIL'}")
    invalid_count = (~gdf.geometry.is_valid).sum()
    empty_count = gdf.geometry.is_empty.sum()
    null_count = gdf.geometry.isnull().sum()
    geom_clean = invalid_count == 0 and empty_count == 0 and null_count == 0
    print(f"  [2] Geometry Check:   Invalid: {invalid_count} | Empty: {empty_count} | Null: {null_count}")
    print(f"      Status:           {'PASS' if geom_clean else 'FAIL'}")
    bminx, bminy, bmaxx, bmaxy = gdf.total_bounds
    is_meter_scale = abs(bminx) > 180 or abs(bminy) > 90
    print(f"  [3] Extents Check:    [{bminx:.1f}, {bminy:.1f}, {bmaxx:.1f}, {bmaxy:.1f}]")
    print(f"      Status:           {'PASS (Meter scale confirmed)' if is_meter_scale else 'FAIL (Degrees detected)'}")

run_gis_data_audit(parcels, "Washoe County Parcels")
run_gis_data_audit(fema_zones, "FEMA Flood Zones")
run_gis_data_audit(fault_lines, "USGS Fault Lines")

print("\n --- AUDIT REPORT: Derived Attributes ---")
if not fema_zones.empty and "flood_subscore" in fema_zones.columns:
    print("  [4] FEMA Sub-score Distribution:")
    print(fema_zones["flood_subscore"].value_counts(dropna=False).to_string())

print("\n[!] Generating visual sanity plot...")
os.makedirs("outputs", exist_ok=True)
fig, ax = plt.subplots(figsize=(10, 8))
parcels.plot(ax=ax, color="lightgray", edgecolor="black", alpha=0.6, label="Parcels")
if not fema_zones.empty:
    fema_zones.plot(ax=ax, color="blue", alpha=0.3, label="FEMA Flood Zones")
if not fault_lines.empty:
    fault_lines.plot(ax=ax, color="red", linewidth=2, label="USGS Fault Lines")
plt.title("Step 2 Verification: Unified Spatial Layers (UTM Zone 11N)")
plt.xlabel("Easting (Meters)")
plt.ylabel("Northing (Meters)")
plt.legend()
plt.tight_layout()
plot_path = "outputs/prepare_data_sanity_plot.png"
plt.savefig(plot_path, dpi=150)
plt.close()
print(f"  └─ Saved sanity plot to '{plot_path}'")

print("\n=========================================================")
print(" DATA PREPARATION COMPLETE & VERIFIED!")
print(" Ready for Step 3: Spatial Analysis & Composite Scoring")
print("=========================================================")
