"""
Multi-county Nevada parcel flood + fault risk lookup.

Interactive web GIS: search by street/APN or map a district using live REST services.
Counties are configured in config/regions.yaml (Washoe, Storey, Lyon, Carson City,
Douglas, Churchill, …).
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

from regions_loader import (
    default_region_id,
    get_region,
    list_region_ids,
    region_area_presets,
    region_label,
)
from risk_engine import (
    build_risk_map,
    run_area_analysis,
    run_neighborhood_analysis,
    summary_table,
)

st.set_page_config(
    page_title="NV Parcel Risk Lookup",
    layout="wide",
)

st.title("Nevada Parcel Risk Lookup")
st.caption(
    "Multi-county live REST lookup — FEMA flood overlay + USGS fault proximity "
    "(AHP weights from config; placeholders pending local calibration). "
    "Not an official flood or insurance determination."
)

with st.sidebar:
    st.header("Search")
    st.markdown(
        "**County map (PMTiles)**  \n"
        "Open [http://localhost:8080/city_map.html](http://localhost:8080/city_map.html)"
    )
    st.divider()

    region_ids = list_region_ids()
    region_labels = {rid: region_label(rid) for rid in region_ids}
    default_rid = default_region_id()
    region = st.selectbox(
        "County / region",
        options=region_ids,
        index=region_ids.index(default_rid) if default_rid in region_ids else 0,
        format_func=lambda rid: region_labels.get(rid, rid),
        help="Parcel source switches with the region (see config/regions.yaml).",
    )
    region_cfg = get_region(region)
    st.caption(region_cfg.get("description", "").strip())

    presets = region_area_presets(region)
    parcels_cfg = region_cfg.get("parcels") or {}
    lookup_cfg = region_cfg.get("lookup") or {}
    search_cfg = lookup_cfg.get("search") or parcels_cfg.get("search") or {}
    has_street = bool(search_cfg.get("street_field")) or bool(
        lookup_cfg.get("geocode_fallback")
    )
    has_city = bool(search_cfg.get("city_field")) and region == "washoe"

    mode_options = ["City area (large map)", "APN"]
    if has_street:
        mode_options.insert(1, "Street address")
    mode = st.radio(
        "Live search by",
        mode_options,
        index=0,
        help="Storey has limited street fields — prefer APN or district map.",
    )

    city = None
    if has_city:
        default_city = "RENO" if region == "washoe" else ""
        city_val = st.text_input(
            "City filter (optional)",
            value=default_city,
            help="Washoe: RENO / SPARKS. Leave blank to skip.",
        ).strip()
        city = city_val.upper() if city_val else None

    max_parcels = st.slider(
        "Max parcels to score",
        min_value=500,
        max_value=4000,
        value=2000,
        step=500,
        help="District samples stay responsive; full counties use the PMTiles batch build.",
    )

    apn = None
    street = None
    area_name = None
    bounds = None
    radius_m = 1000
    match_limit = 25

    if mode == "City area (large map)":
        area_name = st.selectbox("Study area", list(presets.keys()), index=0)
        bounds = presets[area_name]
        st.caption(
            f"BBox: {bounds[0]:.3f}, {bounds[1]:.3f} → {bounds[2]:.3f}, {bounds[3]:.3f}"
        )
        query_label = area_name
    elif mode == "Street address":
        street = st.text_input(
            "Street name",
            value="HARDIE" if region == "lyon" else "RIVERSIDE",
            help="Example: HARDIE (Lyon), RIVERSIDE (Washoe)",
        )
        radius_m = st.slider(
            "Neighborhood radius (meters)",
            min_value=500,
            max_value=5000,
            value=2000,
            step=250,
        )
        match_limit = st.slider("Max street matches", 5, 50, 25, 5)
        query_label = street
    else:
        apn_default = "502155" if region == "storey" else ""
        apn = st.text_input(
            "APN (parcel number)",
            value=apn_default,
            help="Storey example: 502155 · Lyon example: 021-121-18",
        )
        radius_m = st.slider(
            "Neighborhood radius (meters)",
            min_value=500,
            max_value=5000,
            value=2000,
            step=250,
        )
        query_label = apn

    run = st.button("Analyze risk", type="primary", use_container_width=True)
    if st.button("Clear results", use_container_width=True):
        st.session_state.pop("analysis", None)
        st.rerun()

st.info(
    f"**{region_label(region)}** — use **City area** for a district sample, or street/APN "
    "for a neighborhood. Full-county maps stay fast via precomputed PMTiles "
    f"(`python3 04_build_reno_tiles.py --region {region}`)."
)

if run:
    needs_query = mode == "City area (large map)" or bool((query_label or "").strip())
    if not needs_query:
        st.error("Enter a street name or APN before analyzing.")
    else:
        with st.spinner(
            f"Querying {region_label(region)} / flood / fault REST services..."
        ):
            try:
                if mode == "City area (large map)":
                    matches, scored, fema, faults = run_area_analysis(
                        bounds=bounds,
                        city=city,
                        max_parcels=int(max_parcels),
                        region=region,
                    )
                else:
                    matches, scored, fema, faults = run_neighborhood_analysis(
                        apn=apn,
                        street=street,
                        city=city,
                        radius_m=float(radius_m),
                        match_limit=int(match_limit),
                        neighborhood_limit=int(max_parcels),
                        region=region,
                    )
            except Exception as exc:
                st.error(f"Analysis failed: {exc}")
                st.exception(exc)
            else:
                empty_search = mode != "City area (large map)" and (
                    matches is None or matches.empty
                )
                empty_area = scored is None or scored.empty
                if empty_search or empty_area:
                    st.warning(
                        "No parcels found for that search. Try another area, street, or APN."
                    )
                    st.session_state.pop("analysis", None)
                else:
                    st.session_state["analysis"] = {
                        "matches": matches,
                        "scored": scored,
                        "fema": fema,
                        "faults": faults,
                        "query": query_label,
                        "city": city or "",
                        "mode": mode,
                        "region": region,
                    }

result = st.session_state.get("analysis")
if not result:
    st.write(
        "Ready when you are. Pick a **county**, then **City area** or APN/street, then **Analyze risk**."
    )
    st.stop()

matches = result["matches"]
scored = result["scored"]
fema = result["fema"]
faults = result["faults"]

focus_apns = []
if matches is not None and not matches.empty and "APN" in matches.columns:
    focus_apns = matches["APN"].astype(str).tolist()

table = summary_table(scored)

st.success(
    f"Results for **{result.get('query', '')}** ({result.get('mode', '')}) in "
    f"**{region_label(result.get('region', 'washoe'))}** "
    f"{('· ' + result['city']) if result.get('city') else ''} — "
    f"**{len(scored)}** parcels scored."
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Search matches", 0 if matches is None else len(matches))
c2.metric("Parcels on map", len(scored))
c3.metric("Flood polygons", len(fema))
c4.metric("Fault segments", len(faults))

if "risk_category" in scored.columns:
    dist = (
        scored["risk_category"]
        .value_counts()
        .rename_axis("tier")
        .reset_index(name="parcels")
    )
    st.write("**Risk tier distribution**")
    st.dataframe(dist, use_container_width=True, hide_index=True)

st.subheader("Interactive risk map")
try:
    risk_map = build_risk_map(scored, focus_apns=focus_apns or None)
    # scrolling=False so the mouse wheel zooms the map instead of scrolling the iframe
    components.html(risk_map.get_root().render(), height=620, scrolling=False)
except Exception as exc:
    st.error(f"Map failed to render: {exc}")
    st.exception(exc)

if focus_apns:
    st.caption("Bold black outlines highlight parcels that matched your street/APN search.")
else:
    st.caption("Parcels are colored by composite risk tier across the selected city area.")

st.subheader("Scored parcel summary")
st.dataframe(table, use_container_width=True, hide_index=True)

csv_bytes = table.to_csv(index=False).encode("utf-8")
# GeoJSON must not contain NaN/Inf (breaks some clients)
_scored_dl = scored.copy()
for _col in _scored_dl.select_dtypes(include="number").columns:
    _scored_dl[_col] = _scored_dl[_col].replace([float("inf"), float("-inf")], pd.NA)
geojson_str = _scored_dl.to_json()

d1, d2 = st.columns(2)
d1.download_button(
    "Download CSV summary",
    data=csv_bytes,
    file_name=f"{region}_risk_summary.csv",
    mime="text/csv",
    use_container_width=True,
)
d2.download_button(
    "Download GeoJSON",
    data=geojson_str,
    file_name=f"{region}_risk_parcels.geojson",
    mime="application/geo+json",
    use_container_width=True,
)
