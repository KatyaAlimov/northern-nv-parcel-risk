"""
Batch step 3: write a static Folium HTML map from scored parcels.

Loads outputs/analyzed_parcels and saves outputs/index.html.
For interactive search use: streamlit run app.py
"""

import os

from risk_engine import build_risk_map, read_geodata

print("=========================================================")
print(" STEP 3: BUILD STATIC RISK MAP")
print("=========================================================\n")

input_stem = "outputs/analyzed_parcels"
print(f"[1/3] Loading analyzed dataset from '{input_stem}' (.parquet / .fgb / .geojson)...")
parcels = read_geodata(input_stem, layer_name="analyzed_parcels")
if parcels.empty:
    raise SystemExit("No parcels found. Run 02_run_analysis.py first.")

print(f"[2/3] Building Folium map for {len(parcels)} parcels...")
risk_map = build_risk_map(parcels)

os.makedirs("outputs", exist_ok=True)
output_html = "outputs/index.html"
risk_map.save(output_html)

print("[3/3] Map written.")
print("=========================================================")
print(f" Saved: '{output_html}'")
print(" Open in a browser to view.")
print(" For interactive address search: streamlit run app.py")
print("=========================================================")
