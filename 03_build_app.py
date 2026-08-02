"""
ESRI STEP 5: Build interactive GIS application (batch / demo mode).

Loads scored parcels (GeoParquet preferred) and writes a Folium/Leaflet HTML map.
"""

import os

from risk_engine import build_risk_map, read_geodata

print("=========================================================")
print(" ESRI STEP 5: BUILD INTERACTIVE GIS APPLICATION")
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

print("[3/3] Interactive GIS application compiled successfully!")
print("=========================================================")
print(f" Web app saved to: '{output_html}'")
print(" Open 'outputs/index.html' in Chrome or Safari to view!")
print(" Tip: enable 'Esri Satellite', then optionally 'Satellite Labels'.")
print(" For interactive address search, run: streamlit run app.py")
print("=========================================================")
