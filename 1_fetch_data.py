import os
import requests
import geopandas as gpd
import pandas as pd

def main():
    os.makedirs("outputs", exist_ok=True)
    
    print("==================================================")
    print("   WASHOE COUNTY & FEMA PROPERTY RISK LOOKUP TOOL ")
    print("==================================================")
    
    # Prompt user for search choice
    search_type = input("Do you want to search by [1] APN (Parcel Number) or [2] Street Name? Enter 1 or 2: ").strip()
    
    if search_type == "1":
        apn_input = input("Enter the APN (e.g., 010-410 or part of it): ").strip()
        where_clause = f"APN LIKE '%{apn_input}%'"
    else:
        street_input = input("Enter a Reno street name (e.g., PINE, LIBERTY, PLUMB): ").strip().upper()
        where_clause = f"STREET LIKE '%{street_input}%' AND CITY = 'RENO'"

    print(f"\nQuerying Washoe County database...")
    washoe_url = "https://wcgisweb.washoecounty.us/arcgis/rest/services/OpenData/OpenData/MapServer/0/query"
    params = {
        'where': where_clause,
        'outFields': 'APN,STREETNUM,STREET,CITY,SITUSZIP',
        'outSR': '4326',
        'f': 'geojson',
        'resultRecordCount': '25'
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    resp = requests.get(washoe_url, params=params, headers=headers)
    resp.raise_for_status()
    
    parcels_gdf = gpd.read_file(resp.text)
    if parcels_gdf.empty:
        print("❌ No parcels found matching that search. Try a different street name or APN.")
        return
        
    parcels_gdf['Address'] = parcels_gdf['STREETNUM'].astype(str) + " " + parcels_gdf['STREET'] + " St, " + parcels_gdf['CITY']
    print(f"-> Successfully loaded {len(parcels_gdf)} matching parcel(s)!")
    
    print("Fetching intersecting FEMA flood hazard zones...")
    minx, miny, maxx, maxy = parcels_gdf.total_bounds
    
    hazards_gdf = gpd.GeoDataFrame()
    try:
        fema_url = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
        fema_params = {
            'geometry': f"{minx},{miny},{maxx},{maxy}",
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'FLD_ZONE,ZONE_SUBTY,SFHA_TF',
            'outSR': '4326',
            'f': 'geojson'
        }
        fema_resp = requests.get(fema_url, params=fema_params, headers=headers)
        if fema_resp.status_code == 200:
            hazards_gdf = gpd.read_file(fema_resp.text)
    except Exception:
        pass
    
    print("Running spatial risk analysis...")
    parcels_gdf = parcels_gdf.to_crs(epsg=4326)
    
    if not hazards_gdf.empty:
        hazards_gdf = hazards_gdf.to_crs(epsg=4326)
        joined = gpd.sjoin(parcels_gdf, hazards_gdf, how="left", predicate="intersects")
    else:
        joined = parcels_gdf.copy()
        joined['FLD_ZONE'] = 'Unmapped / Zone X'
        joined['SFHA_TF'] = 'F'
        
    def check_risk(row):
        zone = str(row.get('FLD_ZONE', ''))
        sfha = str(row.get('SFHA_TF', ''))
        if sfha == 'T' or zone in ['A', 'AE', 'AH', 'AO', 'VE']:
            return "HIGH RISK"
        return "LOW RISK"
        
    joined['Risk_Level'] = joined.apply(check_risk, axis=1)
    joined['FLD_ZONE'] = joined['FLD_ZONE'].fillna("Zone X (Minimal Risk)")
    joined = joined.drop_duplicates(subset=['APN'])
    
    output_path = "outputs/parcels_analyzed.geojson"
    joined.to_file(output_path, driver="GeoJSON")
    print(f"\n-> Done! Saved results to '{output_path}'.")
    print("-> Now run: python3 2_make_map.py to generate and view your custom map.")

if __name__ == "__main__":
    main()