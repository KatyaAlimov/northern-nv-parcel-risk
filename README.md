# Washoe County Parcel Risk Pipeline

Score Reno / Washoe parcels for **flood** and **fault** risk, then view results in a lookup app or a city-wide map.

> Demo / portfolio project only — not an official flood, geologic, or insurance determination.

---

## Quick Start Guide

### Fastest path (Docker)

You need Docker Desktop running. City tiles should already be in `outputs/reno_risk.pmtiles` (if not, see [Build city tiles](#build-city-tiles-once) below).

```bash
cd wash_county_risk_pipeline
docker compose up --build
```

Then open:

| What | Link |
|---|---|
| **Lookup app** (search street / APN / district) | http://localhost:8501 |
| **City map** (all of Reno) | http://localhost:8080/city_map.html |

Stop everything with `Ctrl+C`, or in another terminal: `docker compose down`.

### Without Docker

```bash
python3 -m pip install -r requirements.txt

# App
python3 -m streamlit run app.py
# → http://localhost:8501

# City map (needs PMTiles first)
python3 05_serve_city_map.py
# → http://localhost:8080/city_map.html
```

### How to use the lookup app

1. Pick **City area**, **Street address**, or **APN**
2. Click **Analyze risk** (wait ~10–40 seconds)
3. Read the map, risk table, and download CSV / GeoJSON if you want

### Build city tiles (once)

Only needed if `outputs/reno_risk.pmtiles` is missing. Takes a while (full Reno ≈ 93k parcels).

```bash
brew install tippecanoe          # macOS
python3 04_build_reno_tiles.py
```

Smaller test build:

```bash
python3 04_build_reno_tiles.py --max-parcels 3000
```

### Check scoring math

```bash
python3 validation_report.py
# creates outputs/validation_report.md
```

---

## Project Overview

**Problem:** Checking flood zones and nearby faults parcel-by-parcel in a desktop GIS is slow.

**What this does:**

1. Downloads parcels, flood zones, and fault lines from public web GIS services  
2. Measures whether a parcel is in a flood zone, and how far it is from a fault  
3. Combines those into one **High / Moderate / Low** risk score  
4. Shows results in two places:
   - **App** — live lookup for a street, APN, or district  
   - **City map** — pre-built map of all Reno parcels  

Same scoring rules for both. Rules live in `config/scoring_config.yaml` so you can change weights without rewriting code.

---

## Architecture Diagram

```
  DATA (web GIS services)              ANALYSIS                    YOU SEE
 ─────────────────────────        ─────────────────          ─────────────────

  Washoe parcels  ──┐
                    │
  FEMA / Esri flood ┼──►  Scoring engine  ──►  Lookup app     (:8501)
                    │     (Python)              search street / APN / area
  USGS faults     ──┘           │
                                │
                                └──►  City tiles  ──►  City map  (:8080)
                                      (PMTiles)         pan/zoom all Reno
```

| Piece | Job |
|---|---|
| Scoring engine (`risk_engine.py`) | Fetch data, run GIS math, apply weights |
| Streamlit app | Interactive search and Folium map |
| PMTiles + MapLibre | Fast map for the whole city |
| Docker (`app` + `web` + `api`) | Runs the app and hosts the city map |

---

## Spatial Methodology

Plain English:

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
- Weights come from an **AHP** pairwise matrix in the config file  
- Fault decay and flood zone lists are also in that config  

---

## Tech Stack

| Need | Tool |
|---|---|
| Language | Python, HTML/JS |
| GIS analysis | GeoPandas, Shapely |
| Live data | ArcGIS REST (Washoe, Esri flood, USGS) |
| Lookup app | Streamlit + Folium |
| City map | MapLibre + tippecanoe / PMTiles |
| Hosting | Docker Compose + nginx |
| Settings | YAML config |

**Useful files**

| File | What it is |
|---|---|
| `app.py` | Lookup app |
| `risk_engine.py` | Shared GIS + scoring |
| `config/scoring_config.yaml` | Weights, flood rules, tiers |
| `04_build_reno_tiles.py` | Build city-wide tiles |
| `templates/city_map.html` | City map page |
| `docker-compose.yml` | One-command deploy |
| `validation_report.py` | Sanity-check the scores |

---

## Data sources

- Washoe County Open Data — parcels  
- Esri Living Atlas — flood hazard  
- USGS — Quaternary faults (Nevada)  

---

## Optional: small offline demo

```bash
python3 01_prepare_data.py
python3 02_run_analysis.py
python3 03_build_app.py
# open outputs/index.html
```
