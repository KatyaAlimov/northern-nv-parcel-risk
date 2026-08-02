"""
Batch step 1: fetch and prepare a sample parcel neighborhood.

Downloads parcels (default street sample), flood zones, and fault lines via
risk_engine, checks CRS/topology, and writes GeoParquet under outputs/.

Run before 02_run_analysis.py for the offline demo pipeline.
"""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from risk_engine import (
    EDGE_BUFFER_METERS,
    TARGET_CRS,
    WGS84,
    bounds_to_envelope,
    envelope_from_parcel_buffers,
    fetch_fault_lines,
    fetch_flood_zones,
    hazard_query_bounds,
    map_flood_risk,
    prepare_layer,
    query_parcels_by_search,
    write_geodata,
)

# Batch demo defaults (override by editing these before running)
DEMO_STREET = "RIVERSIDE"
DEMO_CITY = "RENO"
DEMO_MATCH_LIMIT = 25
DEMO_RADIUS_M = 1000

print("=========================================================")
print(" STEP 1: DATA PREPARATION & AUDIT")
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

study_bounds = envelope_from_parcel_buffers(parcels, DEMO_RADIUS_M)
hazard_bounds = hazard_query_bounds(study_bounds, buffer_m=EDGE_BUFFER_METERS)
query_envelope = bounds_to_envelope(hazard_bounds)
print(f"  └─ Hazard query window edge-buffered by {EDGE_BUFFER_METERS:.0f} m")

fema_zones = fetch_flood_zones(query_envelope)
print(f"  └─ FEMA Zones Loaded:  {len(fema_zones)} features")

fault_lines = fetch_fault_lines(query_envelope)
print(f"  └─ USGS Faults Loaded: {len(fault_lines)} features\n")

print(f"[2/5] CRS validation + transform gate → {TARGET_CRS} (meters)...")
parcels = prepare_layer(parcels, TARGET_CRS, layer_name="parcels", assume_crs_if_missing=WGS84)
fema_zones = prepare_layer(
    fema_zones, TARGET_CRS, layer_name="fema_flood", assume_crs_if_missing=WGS84
)
fault_lines = prepare_layer(
    fault_lines, TARGET_CRS, layer_name="usgs_faults", assume_crs_if_missing=WGS84
)
print("  └─ CRS gate PASS for all layers.\n")

print("[3/5] Topology validation (make_valid) already applied in prepare_layer.")
print(f"  └─ Parcels={len(parcels)} | FEMA={len(fema_zones)} | Faults={len(fault_lines)}\n")

print("[4/5] Subsetting nationwide layers & mapping numeric risk scores...")
minx, miny, maxx, maxy = parcels.total_bounds
pad = EDGE_BUFFER_METERS
if not fault_lines.empty:
    fault_lines = fault_lines.cx[minx - pad : maxx + pad, miny - pad : maxy + pad]
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
    crs_match = current_crs == expected_crs or (
        gdf.crs is not None and gdf.crs.to_epsg() == int(expected_crs.split(":")[1])
    )
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

print("\n[!] Writing prepared layers (GeoParquet + FlatGeobuf)...")
for name, gdf in (
    ("parcels_prepared", parcels),
    ("fema_prepared", fema_zones),
    ("faults_prepared", fault_lines),
):
    paths = write_geodata(
        gdf,
        f"outputs/{name}",
        formats=("parquet", "fgb"),
        target_crs=WGS84,
        layer_name=name,
    )
    print(f"  └─ {name}: {', '.join(str(p) for p in paths)}")

print("\n=========================================================")
print(" DATA PREPARATION COMPLETE & VERIFIED!")
print(" Ready for Step 3: Spatial Analysis & Composite Scoring")
print("=========================================================")
