import geopandas as gpd
import folium
from folium.plugins import Fullscreen, MeasureControl

def main():
    print("1. Loading analyzed parcel data...")
    gdf = gpd.read_file("outputs/parcels_analyzed.geojson")
    gdf = gdf.to_crs(epsg=4326)
    
    bounds = gdf.total_bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2
    
    print(f"2. Building map centered at ({center_lat:.4f}, {center_lon:.4f})...")
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=17,
        tiles=None
    )
    
    # Base tile layers
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
        name="Esri Satellite",
        overlay=False
    ).add_to(m)
    
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr="CartoDB Dark",
        name="Dark Mode",
        overlay=False
    ).add_to(m)
    
    folium.TileLayer(
        tiles="OpenStreetMap",
        name="Street Map",
        overlay=False
    ).add_to(m)
    
    print("3. Adding parcels to the map with risk styling...")
    for _, row in gdf.iterrows():
        is_high = row['Risk_Level'] == "HIGH RISK"
        color = "#ff3333" if is_high else "#2ecc71"
        banner_text = "HIGH RISK (SFHA Zone)" if is_high else "LOW / MINIMAL RISK"
        
        popup_html = f"""
        <div style="font-family: Arial, sans-serif; min-width: 220px; padding: 4px;">
            <div style="background-color: {color}; color: white; padding: 6px 10px; border-radius: 4px; font-weight: bold; margin-bottom: 8px;">
                {banner_text}
            </div>
            <table style="width: 100%; font-size: 12px; border-collapse: collapse;">
                <tr><td style="padding: 2px; font-weight: bold;">APN:</td><td>{row['APN']}</td></tr>
                <tr><td style="padding: 2px; font-weight: bold;">Address:</td><td>{row['Address']}</td></tr>
                <tr><td style="padding: 2px; font-weight: bold;">FEMA Zone:</td><td>{row['FLD_ZONE']}</td></tr>
            </table>
        </div>
        """
        
        folium.GeoJson(
            row['geometry'],
            style_function=lambda x, fill=color: {
                'fillColor': fill,
                'color': 'white',
                'weight': 1.5,
                'fillOpacity': 0.65
            },
            highlight_function=lambda x: {'weight': 3, 'color': '#ffff00', 'fillOpacity': 0.85},
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=f"APN: {row['APN']}"
        ).add_to(m)
        
    folium.LayerControl(position="topright", collapsed=False).add_to(m)
    Fullscreen(position="topleft").add_to(m)
    MeasureControl(position="bottomleft").add_to(m)
    
    legend_html = """
     <div style="position: fixed; bottom: 30px; right: 30px; width: 210px; height: 105px; 
                 background-color: white; z-index:9999; font-size:12px; border:1px solid #ccc;
                 border-radius:6px; padding: 10px; font-family: sans-serif; box-shadow: 2px 2px 6px rgba(0,0,0,0.2);">
     <b>Flood Risk Evaluation</b><br><br>
     <i style="background:#ff3333; width:12px; height:12px; float:left; margin-right:8px; opacity:0.8;"></i> High Risk (100-Yr)<br>
     <i style="background:#2ecc71; width:12px; height:12px; float:left; margin-right:8px; opacity:0.8; margin-top:4px;"></i> Low Risk / Minimal<br>
     </div>
     """
    m.get_root().html.add_child(folium.Element(legend_html))
    
    output_file = "outputs/final_risk_map.html"
    m.save(output_file)
    print(f"-> Success! Open '{output_file}' in your web browser.")

if __name__ == "__main__":
    main()