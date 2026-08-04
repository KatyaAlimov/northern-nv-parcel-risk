# Northern Nevada Parcel Risk Pipeline

Independent portfolio project: score **northern Nevada** parcels for flood and
fault exposure, then explore results in a lookup app or a county-wide map.

Uses public ArcGIS REST services (county parcels, FEMA/Esri flood, USGS faults).
Same scoring logic everywhere — weights live in YAML.

**Map-ready counties:** Washoe, Storey, Lyon, Carson City, Douglas, Churchill,
Humboldt, Elko.

---

## Walkthrough

[**Project walkthrough (~1 min)**](https://katyaalimov.github.io/northern-nv-parcel-risk/walkthrough.html)

---

## What it does

1. Pulls parcels, flood zones, and fault lines from public web GIS services  
2. Checks flood-zone overlap and distance to the nearest fault  
3. Combines them into **High / Moderate / Low**  
4. Ships two interfaces:
   - **Lookup app** — search by street, APN, or area (~10–40s on demand)  
   - **County map** — pan/zoom pre-scored parcels (PMTiles + MapLibre)

---

## Quick start

```bash
git clone https://github.com/KatyaAlimov/northern-nv-parcel-risk.git
cd northern-nv-parcel-risk
python3 -m pip install -r requirements.txt
```

### Lookup app (works without building tiles)

```bash
python3 -m streamlit run app.py
# → http://localhost:8501
```

Or: `./run_app.sh`

1. Choose a **county**
2. Search by **City area**, **Street**, or **APN**
3. Click **Analyze risk**

### County map (needs tiles)

Tiles are large and gitignored. Build at least one county first (tippecanoe required):

```bash
brew install tippecanoe   # macOS
python3 04_build_reno_tiles.py --region washoe
# → outputs/reno_risk.pmtiles
```

Then either:

```bash
python3 05_serve_city_map.py
# → http://localhost:8080/city_map.html
```

or, with Docker Desktop:

```bash
docker compose up --build
# Lookup:  http://localhost:8501
# County map: http://localhost:8080/city_map.html
```

Other counties: `storey`, `lyon`, `carson`, `douglas`, `churchill`, `humboldt`, `elko`.  
Optional flags: `--max-parcels 5000`, `--tiles-only` (rebuild tiles from an existing score file).

---

## Scoring (short version)

| Input | Rule |
|---|---|
| Flood | In FEMA SFHA → 100; moderate / 500-year → 50; else 0 |
| Fault | Closer to USGS fault → higher score (exponential decay) |
| Combined | Weighted average (~60% flood / ~40% fault by default) |
| Label | ≥ 70 HIGH · 30–69 MODERATE · &lt; 30 LOW |

- Map display: **WGS84**. Fault distance: **UTM Zone 11N** (real meters).  
- Edit weights / thresholds in `config/scoring_config.yaml`.  
- County parcel sources in `config/regions.yaml`.  

```bash
python3 validation_report.py   # → outputs/validation_report.md
```

---

## Architecture

```
  County parcels  ──┐
  FEMA / Esri flood ┼──►  risk_engine.py  ──►  Streamlit app   (:8501)
  USGS faults     ──┘           │
                                └──►  PMTiles  ──►  MapLibre map (:8080)
```

---

## Repository layout

| Path | Role |
|---|---|
| `app.py` | Streamlit lookup UI |
| `risk_engine.py` | REST fetch, overlay scoring, Folium maps |
| `config/regions.yaml` | Counties, parcel URLs, field maps |
| `config/scoring_config.yaml` | Weights, flood/fault rules, tiers |
| `04_build_reno_tiles.py` | Batch score → tippecanoe PMTiles |
| `05_serve_city_map.py` | Local map server + lookup API |
| `templates/city_map.html` | MapLibre county viewer |
| `docker-compose.yml` | nginx + API + Streamlit |
| `docs/walkthrough.html` | Playable walkthrough (GitHub Pages) |

Other modules: `spatial_ops.py`, `parcel_lookup.py`, `*_loader.py`, `validation_report.py`,  
and optional offline `01` / `02` / `03` demo scripts.

Generated tiles and reports live under `outputs/` (gitignored).

---

## Data sources

- County parcel ArcGIS REST layers (Washoe Open Data and other northern NV GIS endpoints)  
- Esri Living Atlas — flood hazard  
- USGS Quaternary faults (Nevada)  

No API keys required — services used here are public query endpoints.
