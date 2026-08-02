# Northern Nevada Parcel Risk Pipeline

Flood + fault scoring for **northern Nevada** parcels. Pick a county in the lookup
app or the interactive map — same scoring rules everywhere.

**Map-ready counties today:** Washoe, Storey, Lyon, Carson City, Douglas, Churchill,
Humboldt, Elko. More counties can be added in `config/regions.yaml` once a public
parcel service is available and tiles are built.

> Demo / portfolio project only — not an official flood, geologic, or insurance determination.

---

## Quick Start Guide

### Fastest path (Docker)

You need Docker Desktop running. County tiles should already be in `outputs/*_risk.pmtiles`
(if not, see [Build county tiles](#build-county-tiles-once) below).

```bash
cd wash_county_risk_pipeline
docker compose up --build
```

Then open:

| What | Link |
|---|---|
| **Lookup app** (street / APN / district) | http://localhost:8501 |
| **County map** (pick county in the UI) | http://localhost:8080/city_map.html |

Stop everything with `Ctrl+C`, or in another terminal: `docker compose down`.

### Without Docker

```bash
python3 -m pip install -r requirements.txt

# App
python3 -m streamlit run app.py
# → http://localhost:8501

# County map (needs PMTiles first)
python3 05_serve_city_map.py
# → http://localhost:8080/city_map.html
```

### How to use the lookup app

1. Pick a **county**, then **City area**, **Street address**, or **APN**
2. Click **Analyze risk** (wait ~10–40 seconds)
3. Read the map, risk table, and download CSV / GeoJSON if you want

### Build county tiles (once per region)

```bash
brew install tippecanoe

python3 04_build_reno_tiles.py --region washoe       # → outputs/reno_risk.pmtiles (legacy filename)
python3 04_build_reno_tiles.py --region storey
python3 04_build_reno_tiles.py --region lyon
python3 04_build_reno_tiles.py --region carson
python3 04_build_reno_tiles.py --region douglas
python3 04_build_reno_tiles.py --region churchill
python3 04_build_reno_tiles.py --region humboldt
python3 04_build_reno_tiles.py --region elko

# Optional: --max-parcels 5000 for a quicker sample build
# Optional: --tiles-only to rebuild PMTiles from an existing parquet
```

The map dropdown only lists counties that already have a valid `*_risk.pmtiles` file.

### Check scoring math

```bash
python3 validation_report.py
# creates outputs/validation_report.md
```

---

## Project Overview

**Problem:** Checking flood zones and nearby faults one parcel at a time in desktop
GIS is slow, and raw layers don’t give a clear High / Moderate / Low answer.

**What this does:**

1. Downloads parcels, flood zones, and fault lines from public web GIS services  
2. Measures whether a parcel is in a flood zone, and how far it is from a fault  
3. Combines those into one **High / Moderate / Low** risk score  
4. Shows results in two places:
   - **Lookup app** — live street / APN / district search for a chosen county  
   - **County map** — pre-built PMTiles so you can pan/zoom northern NV counties  

Same scoring rules for both. Rules live in `config/scoring_config.yaml` so you can
change weights without rewriting code. County parcel endpoints and field maps live
in `config/regions.yaml`.

---

## Architecture Diagram

```
  DATA (web GIS services)              ANALYSIS                    YOU SEE
 ─────────────────────────        ─────────────────          ─────────────────

  County parcels  ──┐
  (per-county REST) │
                    │
  FEMA / Esri flood ┼──►  Scoring engine  ──►  Lookup app     (:8501)
                    │     (Python)              search street / APN / area
  USGS faults     ──┘           │
                                │
                                └──►  County tiles ──► County map (:8080)
                                      (PMTiles)         pan/zoom by county
```

| Piece | Job |
|---|---|
| Scoring engine (`risk_engine.py`) | Fetch data, run GIS math, apply weights |
| Region config (`regions.yaml`) | Multi-county parcel URLs + field maps |
| Streamlit app | Interactive search and Folium map |
| PMTiles + MapLibre | Fast map for each built county |
| Docker (`app` + `web` + `api`) | Runs the app and hosts the county map |

---

## Spatial Methodology


1. **Flood** — Does the parcel sit in a flood zone?  
   - High-risk FEMA zones → score 100  
   - Moderate / 500-year style → 50  
   - Otherwise → 0  

2. **Fault** — How close is the nearest fault line (in meters)?  
   - Closer = higher score (smooth decay curve, not hard rings)  

3. **Combine** — Weighted average (default ~60% flood, ~40% fault)  
   - ≥ 70 → **HIGH**  
   - 30–69 → **MODERATE**  
   - &lt; 30 → **LOW**  

Technical notes:

- Distances use **UTM Zone 11N** (meters); maps use **WGS84**  
- After every raw read: **CRS gate** + **`make_valid`** topology repair ([`spatial_ops.py`](spatial_ops.py))  
- Hazard queries use a **5 km edge buffer** beyond the study window so border floods/faults are not truncated  
- Batch outputs prefer **GeoParquet** / **FlatGeobuf** (GeoJSONL only for tippecanoe tiles)  
- Weights come from an **AHP** pairwise matrix in the config file  
- Fault decay and flood zone lists are also in that config  

---

## Tech Stack

| Need | Tool |
|---|---|
| Language | Python, HTML/JS |
| GIS analysis | GeoPandas, Shapely (`make_valid`), GeoParquet / FlatGeobuf |
| Live data | ArcGIS REST (county parcels, Esri flood, USGS faults) |
| Lookup app | Streamlit + Folium |
| County map | MapLibre + tippecanoe / PMTiles |
| Hosting | Docker Compose + nginx |
| Settings | YAML (`scoring_config.yaml`, `regions.yaml`) |

**Useful files**

| File | What it is |
|---|---|
| `app.py` | Lookup app |
| `risk_engine.py` | Shared GIS + scoring |
| `config/regions.yaml` | Northern NV counties + parcel sources |
| `config/scoring_config.yaml` | Weights, flood rules, tiers |
| `04_build_reno_tiles.py` | Build county-wide tiles (`--region …`) |
| `templates/city_map.html` | County map page |
| `docker-compose.yml` | One-command deploy |
| `validation_report.py` | Sanity-check the scores |

---

## Data sources

- **County parcel layers** via ArcGIS REST (Washoe Open Data; other northern NV counties use their own GIS / ArcGIS Online endpoints where public)  
- **Esri Living Atlas** — flood hazard  
- **USGS** — Quaternary faults (Nevada)  

---

## Optional: small offline demo

```bash
python3 01_prepare_data.py
python3 02_run_analysis.py
python3 03_build_app.py
# open outputs/index.html
```
