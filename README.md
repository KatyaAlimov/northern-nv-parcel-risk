# Northern Nevada Parcel Risk Pipeline

Flood + fault scoring for **northern Nevada** parcels. Pick a county in the lookup
app or the interactive map — same scoring rules everywhere.

**Map-ready counties today:** Washoe, Storey, Lyon, Carson City, Douglas, Churchill,
Humboldt, Elko. Add more in `config/regions.yaml` when a public parcel service
exists and tiles are built.

> Not an official flood, geologic, or insurance determination.

---

## Walkthrough

Short narrated overview of the lookup app and county map (~1 min).

**[Download / watch the walkthrough](https://github.com/KatyaAlimov/northern-nv-parcel-risk/releases/download/walkthrough-1/northern_nv_parcel_risk_walkthrough.mp4)**

(Also on the [Releases](https://github.com/KatyaAlimov/northern-nv-parcel-risk/releases/tag/walkthrough-1) page. GitHub’s file viewer can’t preview a video this size in-browser — use the download link.)

---

## Quick start

### Docker

Requires Docker Desktop. County tiles should be in `outputs/*_risk.pmtiles`
(see [Build county tiles](#build-county-tiles) if missing).

```bash
cd wash_county_risk_pipeline
docker compose up --build
```

| What | URL |
|---|---|
| Lookup app | http://localhost:8501 |
| County map | http://localhost:8080/city_map.html |

Stop with `Ctrl+C` or `docker compose down`.

### Without Docker

```bash
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py          # http://localhost:8501
python3 05_serve_city_map.py             # http://localhost:8080/city_map.html
```

Or: `./run_app.sh` for Streamlit only.

### Lookup app

1. Choose a **county**, then **City area**, **Street**, or **APN**
2. Click **Analyze risk** (~10–40 seconds)
3. Review the map, summary, and optional CSV / GeoJSON download

### Build county tiles

```bash
brew install tippecanoe

python3 04_build_reno_tiles.py --region washoe   # → outputs/reno_risk.pmtiles
python3 04_build_reno_tiles.py --region storey
python3 04_build_reno_tiles.py --region lyon
python3 04_build_reno_tiles.py --region carson
python3 04_build_reno_tiles.py --region douglas
python3 04_build_reno_tiles.py --region churchill
python3 04_build_reno_tiles.py --region humboldt
python3 04_build_reno_tiles.py --region elko

# Optional: --max-parcels 5000 | --tiles-only
```

If a run stops early, run the same command again — completed grid cells are skipped.
The map dropdown only lists counties with a valid `*_risk.pmtiles` file.

### Validate scores

```bash
python3 validation_report.py
# → outputs/validation_report.md
```

---

## What it does

1. Downloads parcels, flood zones, and fault lines from public web GIS services  
2. Tests flood-zone intersection and distance to the nearest fault  
3. Combines those into **High / Moderate / Low**  
4. Delivers results in:
   - **Lookup app** — live street / APN / district search  
   - **County map** — pre-built PMTiles for pan/zoom  

Scoring: `config/scoring_config.yaml`. Counties: `config/regions.yaml`.

---

## Architecture

```
  County parcels  ──┐
  FEMA / Esri flood ┼──►  risk_engine.py  ──►  Streamlit app   (:8501)
  USGS faults     ──┘           │
                                └──►  PMTiles  ──►  MapLibre map (:8080)
```

---

## Scoring method

1. **Flood** — parcel in FEMA SFHA → 100; moderate / 500-year → 50; else 0  
2. **Fault** — closer to USGS fault → higher score (exponential decay)  
3. **Composite** — weighted average (~60% flood / ~40% fault by default)  
   - ≥ 70 HIGH · 30–69 MODERATE · &lt; 30 LOW  

- Distances in **UTM Zone 11N**; maps in **WGS84**  
- Topology repair and CRS checks in `spatial_ops.py`  
- Hazard queries use a **5 km** buffer past the study window  

---

## Repository layout

| Path | Role |
|---|---|
| `app.py` | Streamlit lookup UI |
| `risk_engine.py` | REST fetch, overlay scoring, Folium maps |
| `parcel_lookup.py` | Street/APN lookup for map API + app |
| `regions_loader.py` | Load `config/regions.yaml` |
| `config_loader.py` | Load `config/scoring_config.yaml` |
| `spatial_ops.py` | CRS, topology, GeoParquet I/O |
| `config/regions.yaml` | County bounds, parcel URLs, field maps |
| `config/scoring_config.yaml` | AHP weights, flood/fault rules, tiers |
| `04_build_reno_tiles.py` | Grid fetch → score → tippecanoe PMTiles |
| `05_serve_city_map.py` | Local map server + `/api/lookup` + `/api/regions` |
| `templates/city_map.html` | MapLibre county viewer |
| `01_prepare_data.py` | Offline demo: fetch sample neighborhood |
| `02_run_analysis.py` | Offline demo: score sample |
| `03_build_app.py` | Offline demo: static Folium HTML |
| `validation_report.py` | Score / AHP sanity report |
| `docker-compose.yml` | web (nginx) + api + Streamlit |
| `deploy/nginx.conf` | PMTiles Range + API proxy |
| `requirements.txt` | Python dependencies |
| `run_app.sh` | Convenience launcher for Streamlit |
| `docs/northern_nv_parcel_risk_walkthrough.mp4` | Short project walkthrough video |

Generated tiles and reports live under `outputs/` (gitignored).

---

## Data sources

- County parcel ArcGIS REST layers (Washoe Open Data and other northern NV GIS endpoints)  
- Esri Living Atlas — flood hazard  
- USGS Quaternary faults (Nevada)  

---

## Offline demo (optional)

```bash
python3 01_prepare_data.py
python3 02_run_analysis.py
python3 03_build_app.py
# open outputs/index.html
```
