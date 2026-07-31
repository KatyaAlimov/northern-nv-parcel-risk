"""
Washoe County Address / APN Risk Lookup App

Interactive web GIS tool: search a parcel or map a large city district for
flood + fault composite risk using live ArcGIS REST services.
"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from risk_engine import (
    AREA_PRESETS,
    build_risk_map,
    run_area_analysis,
    run_neighborhood_analysis,
    summary_table,
)

st.set_page_config(
    page_title="Washoe Parcel Risk Lookup",
    layout="wide",
)

st.title("Washoe County Parcel Risk Lookup")
st.caption(
    "Live REST lookup of Washoe parcels with FEMA flood overlay and USGS fault "
    "proximity scoring (60% flood / 40% fault)."
)

with st.sidebar:
    st.header("Search")
    st.markdown(
        "**City-wide Reno map (PMTiles)**  \n"
        "Open [http://localhost:8080/city_map.html](http://localhost:8080/city_map.html)  \n"
        "(Docker: `docker compose up --build` serves this app on :8501 and the map on :8080)"
    )
    st.divider()
    mode = st.radio(
        "Live search by",
        ["City area (large map)", "Street address", "APN"],
        index=0,
        help="City area maps a whole Reno/Sparks district. Street/APN zooms to a neighborhood.",
    )

    city = st.text_input("City filter", value="RENO").strip().upper() or "RENO"
    max_parcels = st.slider(
        "Max parcels to score",
        min_value=500,
        max_value=4000,
        value=2000,
        step=500,
        help="Reno has ~93,000 parcels. This app maps a district sample for performance.",
    )

    apn = None
    street = None
    area_name = None
    bounds = None
    radius_m = 1000
    match_limit = 25

    if mode == "City area (large map)":
        area_name = st.selectbox("Study area", list(AREA_PRESETS.keys()), index=0)
        bounds = AREA_PRESETS[area_name]
        st.caption(
            f"BBox: {bounds[0]:.3f}, {bounds[1]:.3f} → {bounds[2]:.3f}, {bounds[3]:.3f}"
        )
        query_label = area_name
    elif mode == "Street address":
        street = st.text_input(
            "Street name",
            value="RIVERSIDE",
            help="Example: RIVERSIDE, VIRGINIA, PLUMB",
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
        apn = st.text_input("APN (parcel number)", value="", help="Example: 1047000")
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
    "Tip: choose **City area (large map)** to score Downtown / Midtown / Sparks — "
    "not just a few streets. Full Reno (~93k parcels) is too large for one browser map, "
    "so this loads a dense district sample (up to your Max parcels setting)."
)

if run:
    needs_query = mode == "City area (large map)" or bool((query_label or "").strip())
    if not needs_query:
        st.error("Enter a street name or APN before analyzing.")
    else:
        with st.spinner(
            "Querying Washoe / flood / fault REST services across the study area..."
        ):
            try:
                if mode == "City area (large map)":
                    matches, scored, fema, faults = run_area_analysis(
                        bounds=bounds,
                        city=city,
                        max_parcels=int(max_parcels),
                    )
                else:
                    matches, scored, fema, faults = run_neighborhood_analysis(
                        apn=apn,
                        street=street,
                        city=city,
                        radius_m=float(radius_m),
                        match_limit=int(match_limit),
                        neighborhood_limit=int(max_parcels),
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
                        "city": city,
                        "mode": mode,
                    }

result = st.session_state.get("analysis")
if not result:
    st.write(
        "Ready when you are. Start with **City area → Downtown Reno**, then click **Analyze risk**."
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
    f"**{result.get('city', '')}** — **{len(scored)}** parcels scored."
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
geojson_str = scored.to_json()

d1, d2 = st.columns(2)
d1.download_button(
    "Download CSV summary",
    data=csv_bytes,
    file_name="washoe_risk_summary.csv",
    mime="text/csv",
    use_container_width=True,
)
d2.download_button(
    "Download GeoJSON",
    data=geojson_str,
    file_name="washoe_risk_parcels.geojson",
    mime="application/geo+json",
    use_container_width=True,
)
