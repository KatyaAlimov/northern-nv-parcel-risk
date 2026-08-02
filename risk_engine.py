"""
Shared multi-county Nevada flood + fault risk analysis engine.

Uses ArcGIS REST geospatial web services and GeoPandas overlays.
CRS workflow: query EPSG:4326 -> analyze EPSG:32611 (meters) -> map EPSG:4326.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Union

import folium
import geopandas as gpd
import pandas as pd
import requests
from folium import Element

from config_loader import (
    ahp_criteria as _cfg_ahp_criteria,
    ahp_cr_threshold as _cfg_ahp_cr_threshold,
    ahp_matrix as _cfg_ahp_matrix,
    fault_decay_lambda as _cfg_fault_lambda,
    flood_rules as _cfg_flood_rules,
    load_scoring_config,
    tier_thresholds as _cfg_tier_thresholds,
)
from regions_loader import (
    default_region_id,
    get_region,
    list_region_ids,
    region_area_presets,
    region_bounds,
    region_label,
    parquet_stem,
)
from spatial_ops import (
    EDGE_BUFFER_METERS,
    TARGET_CRS as _SPATIAL_TARGET_CRS,
    WGS84 as _SPATIAL_WGS84,
    edge_buffered_bounds,
    ensure_crs,
    prepare_layer,
    read_geodata,
    sanitize_geometries,
    write_geodata,
)

TARGET_CRS = _SPATIAL_TARGET_CRS
WGS84 = _SPATIAL_WGS84
HEADERS = {"User-Agent": "Mozilla/5.0 (WashoeCountyRiskPipeline)"}

WASHOE_PARCELS_URL = (
    "https://wcgisweb.washoecounty.us/arcgis/rest/services/"
    "OpenData/OpenData/MapServer/0/query"
)
FEMA_FLOOD_URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)
USGS_FAULTS_URL = (
    "https://earthquake.usgs.gov/arcgis/rest/services/haz/Qfaults/MapServer/11/query"
)

PARCEL_OUT_FIELDS = (
    "OBJECTID,APN,STREETNUM,STREETDIR,STREET,CITY,SITUSZIP,FullAddress"
)

# ---------------------------------------------------------------------------
# GIS-MCDA parameters loaded from config/scoring_config.yaml
# ---------------------------------------------------------------------------
try:
    _SCORING_CFG = load_scoring_config()
except FileNotFoundError:
    _SCORING_CFG = None

FAULT_DECAY_LAMBDA = (
    _cfg_fault_lambda(_SCORING_CFG) if _SCORING_CFG else 0.0015
)
DEFAULT_AHP_MATRIX = (
    _cfg_ahp_matrix(_SCORING_CFG)
    if _SCORING_CFG
    else ((1.0, 1.5), (1.0 / 1.5, 1.0))
)
DEFAULT_AHP_CRITERIA = (
    _cfg_ahp_criteria(_SCORING_CFG) if _SCORING_CFG else ("Flood", "Fault")
)
AHP_CR_THRESHOLD = (
    _cfg_ahp_cr_threshold(_SCORING_CFG) if _SCORING_CFG else 0.10
)
_TIER = _cfg_tier_thresholds(_SCORING_CFG) if _SCORING_CFG else {
    "high_min": 70.0,
    "moderate_min": 30.0,
}
TIER_HIGH_MIN = _TIER["high_min"]
TIER_MODERATE_MIN = _TIER["moderate_min"]

RISK_COLORS = {
    "HIGH": "#d9534f",
    "MODERATE": "#f0ad4e",
    "LOW": "#5cb85c",
}

Bounds = Tuple[float, float, float, float]  # minx, miny, maxx, maxy in WGS84

# Backward-compatible Washoe presets (also available via region_area_presets("washoe"))
AREA_PRESETS: dict = region_area_presets("washoe")


def fetch_arcgis_geojson(
    url: str,
    params: Optional[dict] = None,
    *,
    timeout: int = 180,
    retries: int = 2,
) -> gpd.GeoDataFrame:
    """Fetch GeoJSON from an ArcGIS REST query endpoint with CRS + topology gates."""
    last_exc: Optional[Exception] = None
    payload = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_exc = exc
            # Retry transient gateway / server errors from county layers
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or (
                status is not None and status >= 500
            )
            if (not retryable) or attempt >= retries:
                raise
            continue
    else:
        raise last_exc or RuntimeError(f"Failed to fetch {url}")

    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(f"ArcGIS error from {url}: {payload['error']}")

    if isinstance(payload, dict) and not payload.get("features"):
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    raw = gpd.GeoDataFrame.from_features(payload, crs=WGS84)
    return prepare_layer(
        raw,
        WGS84,
        layer_name=url.rsplit("/", 2)[-2] if "/" in url else "arcgis_layer",
        assume_crs_if_missing=WGS84,
    )


_PARQUET_STEM = None  # use parquet_stem() from regions_loader


def _outputs_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "outputs", Path.cwd() / "outputs", Path("/app/outputs")):
        if candidate.is_dir():
            return candidate
    return here / "outputs"


def _local_parcels_in_bounds(
    region_id: str,
    bounds: Bounds,
    limit: int = 400,
    city: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """
    Fallback when county ArcGIS REST is down: clip pre-scored GeoParquet tiles.
    Geometries are enough to re-run flood/fault scoring in the live app.
    """
    stem = parquet_stem(region_id)
    path = _outputs_dir() / f"{stem}.parquet"
    if not path.exists():
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84)
    elif str(gdf.crs).upper() not in {WGS84, "EPSG:4326"}:
        gdf = gdf.to_crs(WGS84)

    minx, miny, maxx, maxy = bounds
    try:
        hit = gdf.cx[minx:maxx, miny:maxy].copy()
    except Exception:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    if city and str(city).strip() and "CITY" in hit.columns:
        hit = hit[hit["CITY"].astype(str).str.upper() == str(city).strip().upper()]

    if hit.empty:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    # Ensure schema fields the scorer expects
    if "FullAddress" not in hit.columns and "SITUS_ADDRESS" in hit.columns:
        hit["FullAddress"] = hit["SITUS_ADDRESS"]
    for col in ("STREETNUM", "STREETDIR", "STREET", "CITY", "FullAddress"):
        if col not in hit.columns:
            hit[col] = None

    hit["COUNTY"] = get_region(region_id).get("name", region_id)
    hit["REGION_ID"] = region_id
    return hit.head(max(1, int(limit))).copy()


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _combine_where(*parts: Optional[str]) -> str:
    clauses = [p.strip() for p in parts if p and str(p).strip() and str(p).strip() != "1=1"]
    if not clauses:
        return "1=1"
    # Keep explicit 1=1 only if alone
    return " AND ".join(f"({c})" for c in clauses)


def normalize_parcels(gdf: gpd.GeoDataFrame, region_id: str) -> gpd.GeoDataFrame:
    """Remap county-specific fields into the shared engine schema."""
    if gdf is None or gdf.empty:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    region = get_region(region_id)
    fmap = region.get("parcels", {}).get("field_map") or {}
    out = gdf.copy()

    def _src(logical: str) -> Optional[str]:
        v = fmap.get(logical)
        return str(v) if v else None

    for logical in ("APN", "STREETNUM", "STREETDIR", "STREET", "CITY", "FullAddress"):
        src = _src(logical)
        if src and src in out.columns:
            out[logical] = out[src]
        elif logical not in out.columns:
            out[logical] = None

    out["COUNTY"] = region.get("name", region_id)
    out["REGION_ID"] = region["id"]

    # Build FullAddress if missing
    if out["FullAddress"].isna().all() or (
        out["FullAddress"].astype(str).str.strip().isin(["", "None", "nan"]).all()
    ):
        parts = []
        for col in ("STREETNUM", "STREETDIR", "STREET", "CITY"):
            if col in out.columns:
                parts.append(out[col].astype(str).replace({"None": "", "nan": ""}))
        if parts:
            addr = parts[0]
            for p in parts[1:]:
                addr = addr.str.strip() + " " + p.str.strip()
            out["FullAddress"] = addr.str.replace(r"\s+", " ", regex=True).str.strip()

    return out


def query_parcels_by_search(
    apn: Optional[str] = None,
    street: Optional[str] = None,
    city: Optional[str] = None,
    limit: int = 25,
    region: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Query parcels by APN and/or street for a configured region."""
    from parcel_lookup import lookup_parcels

    region_id = (region or default_region_id()).strip().lower()
    term = (apn or "").strip() or (street or "").strip()
    if not term:
        raise ValueError("Provide an APN and/or street search term.")

    fc = lookup_parcels(term, region=region_id, city=city, limit=limit)
    feats = fc.get("features") or []
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    raw = gpd.GeoDataFrame.from_features(fc, crs=WGS84)
    return prepare_layer(
        raw, WGS84, layer_name=f"{region_id}_lookup", assume_crs_if_missing=WGS84
    )


def query_parcels_in_envelope(
    bounds: Bounds,
    limit: int = 400,
    city: Optional[str] = None,
    offset: int = 0,
    region: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Query parcels intersecting a WGS84 envelope for a configured region."""
    region_id = (region or default_region_id()).strip().lower()
    region_cfg = get_region(region_id)
    parcels_cfg = region_cfg["parcels"]
    search = parcels_cfg.get("search") or {}
    city_field = search.get("city_field")

    minx, miny, maxx, maxy = bounds
    envelope = f"{minx},{miny},{maxx},{maxy}"

    extra = None
    if city and str(city).strip() and city_field:
        extra = f"UPPER({city_field}) = '{_escape_sql(str(city).strip().upper())}'"

    where = _combine_where(parcels_cfg.get("base_where"), extra)
    page = min(int(limit), int(parcels_cfg.get("max_record_count") or 1000))

    params = {
        "where": where,
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": parcels_cfg.get("out_fields", "*"),
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(page),
    }
    if offset:
        params["resultOffset"] = str(offset)

    try:
        raw = fetch_arcgis_geojson(parcels_cfg["url"], params=params)
        if raw is not None and not raw.empty:
            return normalize_parcels(raw, region_id)
    except Exception:
        # NDWR / county layers occasionally return HTTP 500 — use local tiles.
        pass

    # Offset pages are not meaningful for local parquet; only use on first page.
    if offset:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    local = _local_parcels_in_bounds(region_id, bounds, limit=page, city=city)
    if not local.empty:
        return normalize_parcels(local, region_id)
    return gpd.GeoDataFrame(geometry=[], crs=WGS84)


def _iter_grid_cells(bounds: Bounds, rows: int, cols: int):
    minx, miny, maxx, maxy = bounds
    dx = (maxx - minx) / cols
    dy = (maxy - miny) / rows
    for r in range(rows):
        for c in range(cols):
            yield (
                minx + c * dx,
                miny + r * dy,
                minx + (c + 1) * dx,
                miny + (r + 1) * dy,
            )


def query_parcels_covering_area(
    bounds: Bounds,
    city: Optional[str] = None,
    max_parcels: int = 2000,
    grid_rows: int = 3,
    grid_cols: int = 3,
    page_size: int = 1000,
    region: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """
    Pull parcels across a large area with spatial coverage.

    A single envelope query returns an arbitrary first N features (often one
    corner of the city). Grid cells + pagination spread coverage across the map.
    """
    region_id = (region or default_region_id()).strip().lower()
    region_cfg = get_region(region_id)
    max_page = int(region_cfg["parcels"].get("max_record_count") or 1000)
    parcels_url = str((region_cfg.get("parcels") or {}).get("url") or "")

    max_parcels = max(100, int(max_parcels))
    page_size = min(max_page, max(100, int(page_size)))

    # Statewide NDWR layer is often 500/gateway — use local county parquet when present.
    if "arcgis.water.nv.gov" in parcels_url:
        local = _local_parcels_in_bounds(
            region_id, bounds, limit=max_parcels, city=city
        )
        if not local.empty:
            return normalize_parcels(local, region_id)

    per_cell = max(page_size // max(1, grid_rows * grid_cols), 150)
    frames = []
    seen = set()

    for cell in _iter_grid_cells(bounds, grid_rows, grid_cols):
        if len(seen) >= max_parcels:
            break
        remaining = max_parcels - len(seen)
        chunk = query_parcels_in_envelope(
            cell,
            limit=min(per_cell, remaining, page_size),
            city=city,
            region=region_id,
        )
        if chunk.empty:
            continue
        if "APN" in chunk.columns:
            chunk = chunk[~chunk["APN"].astype(str).isin(seen)]
            seen.update(chunk["APN"].astype(str).tolist())
        frames.append(chunk)

    offset = 0
    while len(seen) < max_parcels and offset < max_parcels:
        remaining = max_parcels - len(seen)
        page = query_parcels_in_envelope(
            bounds,
            limit=min(page_size, remaining),
            city=city,
            offset=offset,
            region=region_id,
        )
        if page.empty:
            break
        if "APN" in page.columns:
            page = page[~page["APN"].astype(str).isin(seen)]
            seen.update(page["APN"].astype(str).tolist())
        if page.empty:
            offset += page_size
            continue
        frames.append(page)
        offset += page_size
        if len(page) < page_size:
            break

    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    out = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=WGS84
    )
    if "APN" in out.columns:
        out = out.drop_duplicates(subset=["APN"])
    return out.head(max_parcels).copy()


def run_area_analysis(
    bounds: Bounds,
    city: Optional[str] = None,
    max_parcels: int = 2000,
    region: Optional[str] = None,
) -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Score a large geographic area (city district), not a single street.

    Returns (empty_matches, scored_parcels, fema, faults).
    """
    region_id = (region or default_region_id()).strip().lower()

    parcels = query_parcels_covering_area(
        bounds,
        city=city,
        max_parcels=max_parcels,
        grid_rows=4,
        grid_cols=4,
        region=region_id,
    )
    empty_matches = gpd.GeoDataFrame(geometry=[], crs=WGS84)
    if parcels.empty:
        return empty_matches, empty_matches, empty_matches, empty_matches

    envelope = bounds_to_envelope(hazard_query_bounds(bounds))
    fema = fetch_flood_zones(envelope)
    faults = fetch_fault_lines(envelope)
    scored = score_parcels(parcels, fema, faults)
    return empty_matches, scored, fema, faults


def meters_to_deg_pad(radius_m: float, lat: float) -> Tuple[float, float]:
    """Approximate meter radius to lon/lat degree padding at a given latitude."""
    lat_pad = radius_m / 111_320.0
    lon_pad = radius_m / (111_320.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    return lon_pad, lat_pad


def expand_bounds(bounds: Bounds, radius_m: float) -> Bounds:
    """Expand a WGS84 bounding box by radius_m in each direction."""
    return edge_buffered_bounds(bounds, buffer_m=radius_m)


def hazard_query_bounds(
    bounds: Bounds,
    buffer_m: float = EDGE_BUFFER_METERS,
) -> Bounds:
    """
    Expand the study window before fetching flood/fault layers.

    Prevents border truncation when hazards cross the administrative edge.
    """
    return edge_buffered_bounds(bounds, buffer_m=buffer_m)


def bounds_to_envelope(bounds: Bounds) -> str:
    minx, miny, maxx, maxy = bounds
    return f"{minx},{miny},{maxx},{maxy}"


def fetch_flood_zones(envelope: str, limit: int = 2000) -> gpd.GeoDataFrame:
    """Fetch FEMA/Esri flood hazard polygons for a WGS84 envelope string."""
    return fetch_arcgis_geojson(
        FEMA_FLOOD_URL,
        params={
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,SFHA_TF,ZONE_SUBTY",
            "outSR": "4326",
            "f": "geojson",
            "resultRecordCount": str(limit),
        },
    )


def fetch_fault_lines(envelope: str, limit: int = 2000) -> gpd.GeoDataFrame:
    """Fetch USGS Nevada quaternary fault lines for a WGS84 envelope string."""
    return fetch_arcgis_geojson(
        USGS_FAULTS_URL,
        params={
            "where": "1=1",
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "fault_name,age,slip_rate",
            "outSR": "4326",
            "f": "geojson",
            "resultRecordCount": str(limit),
        },
    )


# =========================================================================
# GIS-MCDA: AHP weighting + FEMA NFIP flood + Alquist-Priolo fault decay
# =========================================================================

class AnalyticHierarchyProcess:
    """
    Saaty (1980) Analytic Hierarchy Process for criteria weighting.

    Uses column-normalization to estimate priority weights, then derives
    λ_max, Consistency Index (CI), and Consistency Ratio (CR).
    """

    # Saaty Random Index (RI) by matrix order n
    RANDOM_INDEX = {
        1: 0.00,
        2: 0.00,
        3: 0.58,
        4: 0.90,
        5: 1.12,
        6: 1.24,
        7: 1.32,
        8: 1.41,
        9: 1.45,
        10: 1.49,
    }

    def __init__(
        self,
        pairwise_matrix: Sequence[Sequence[float]],
        criteria_names: Optional[Sequence[str]] = None,
    ):
        self.matrix = [list(map(float, row)) for row in pairwise_matrix]
        self.n = len(self.matrix)
        if self.n == 0 or any(len(row) != self.n for row in self.matrix):
            raise ValueError("Pairwise matrix must be square and non-empty.")
        if criteria_names is None:
            self.criteria_names = [f"C{i + 1}" for i in range(self.n)]
        else:
            self.criteria_names = list(criteria_names)
            if len(self.criteria_names) != self.n:
                raise ValueError("criteria_names length must match matrix order.")

        self.weights: list = []
        self.lambda_max: float = float("nan")
        self.ci: float = float("nan")
        self.cr: float = float("nan")
        self._compute()

    def _compute(self) -> None:
        n = self.n
        # Column sums
        col_sums = [sum(self.matrix[r][c] for r in range(n)) for c in range(n)]
        # Column-normalized matrix
        norm = [
            [
                self.matrix[r][c] / col_sums[c] if col_sums[c] else 0.0
                for c in range(n)
            ]
            for r in range(n)
        ]
        # Priority vector = row means
        self.weights = [sum(norm[r]) / n for r in range(n)]

        # λ_max from Aw / w
        aw = []
        for r in range(n):
            aw.append(sum(self.matrix[r][c] * self.weights[c] for c in range(n)))
        ratios = [
            aw[i] / self.weights[i]
            for i in range(n)
            if self.weights[i] not in (0.0, 0)
        ]
        self.lambda_max = sum(ratios) / len(ratios) if ratios else float("nan")

        if n <= 1:
            self.ci = 0.0
            self.cr = 0.0
        else:
            self.ci = (self.lambda_max - n) / (n - 1)
            ri = self.RANDOM_INDEX.get(n, 1.49)
            # For n=2, RI=0 → reciprocal matrices are always consistent → CR=0
            self.cr = 0.0 if ri == 0 else (self.ci / ri)

    def as_dict(self) -> dict:
        return {
            name: weight
            for name, weight in zip(self.criteria_names, self.weights)
        }

    def is_consistent(self, threshold: Optional[float] = None) -> bool:
        lim = AHP_CR_THRESHOLD if threshold is None else float(threshold)
        return self.cr < lim

    def summary_lines(self) -> list:
        lines = [
            "AHP pairwise matrix:",
            "  " + "  ".join(f"{c:>8}" for c in self.criteria_names),
        ]
        for name, row in zip(self.criteria_names, self.matrix):
            lines.append(
                f"  {name:>8} " + " ".join(f"{v:8.4f}" for v in row)
            )
        lines.append("Normalized priority weights:")
        for name, w in zip(self.criteria_names, self.weights):
            lines.append(f"  {name}: {w:.6f} ({w * 100:.2f}%)")
        lines.append(f"λ_max = {self.lambda_max:.6f}")
        lines.append(f"Consistency Index (CI) = {self.ci:.6f}")
        lines.append(f"Consistency Ratio (CR) = {self.cr:.6f}")
        status = (
            f"PASS (CR < {AHP_CR_THRESHOLD:.2f})"
            if self.is_consistent()
            else f"FAIL (CR >= {AHP_CR_THRESHOLD:.2f})"
        )
        lines.append(f"Consistency check: {status}")
        return lines


def compute_ahp_weights(
    pairwise_matrix: Sequence[Sequence[float]] = DEFAULT_AHP_MATRIX,
    criteria_names: Sequence[str] = DEFAULT_AHP_CRITERIA,
    verbose: bool = False,
) -> AnalyticHierarchyProcess:
    """Run AHP and optionally print CI/CR diagnostics."""
    ahp = AnalyticHierarchyProcess(pairwise_matrix, criteria_names)
    if verbose:
        for line in ahp.summary_lines():
            print(f"  {line}")
    return ahp


# Module-level weights derived from default AHP (Flood vs Fault = 1.5)
_DEFAULT_AHP = compute_ahp_weights(verbose=False)
WEIGHT_FLOOD = float(_DEFAULT_AHP.as_dict().get("Flood", 0.60))
WEIGHT_FAULT = float(_DEFAULT_AHP.as_dict().get("Fault", 0.40))


def map_flood_risk(row: pd.Series) -> float:
    """
    FEMA NFIP standardized flood hazard sub-score (0–100).

    Zone lists and scores come from config/scoring_config.yaml.
    """
    rules = _cfg_flood_rules() if _SCORING_CFG is not None else {
        "sfha_100yr_zones": ["A", "AE", "AH", "AO", "VE", "V"],
        "sfha_100yr_score": 100.0,
        "moderate_500yr_zones": ["X500", "B"],
        "moderate_500yr_score": 50.0,
        "moderate_subtype_tokens": ["0.2", "500"],
        "minimal_score": 0.0,
    }
    zone = str(row.get("FLD_ZONE", "")).strip().upper()
    subtype = str(row.get("ZONE_SUBTY", "") or "")
    sfha = str(row.get("SFHA_TF", "") or "").strip().upper()

    sfha_zones = {z.upper() for z in rules.get("sfha_100yr_zones", [])}
    mod_zones = {z.upper() for z in rules.get("moderate_500yr_zones", [])}
    tokens = [str(t) for t in rules.get("moderate_subtype_tokens", [])]

    if zone in sfha_zones:
        return float(rules.get("sfha_100yr_score", 100.0))
    if sfha in {"T", "TRUE", "YES"} and zone not in {"X", "D"}:
        return float(rules.get("sfha_100yr_score", 100.0))
    if zone in mod_zones or any(tok in subtype for tok in tokens):
        return float(rules.get("moderate_500yr_score", 50.0))
    return float(rules.get("minimal_score", 0.0))


def calculate_fault_score(
    dist_m: float,
    decay_lambda: Optional[float] = None,
) -> float:
    """
    Continuous exponential distance-decay fault sub-score.

    Subscore = 100 * exp(-λ * distance_meters), clamped to [0, 100], 2 d.p.
    λ defaults from config/scoring_config.yaml.
    """
    lam = FAULT_DECAY_LAMBDA if decay_lambda is None else float(decay_lambda)
    if dist_m is None or not math.isfinite(float(dist_m)):
        return 0.0
    d = max(0.0, float(dist_m))
    score = 100.0 * math.exp(-lam * d)
    score = max(0.0, min(100.0, score))
    return round(score, 2)


def categorize_risk(score: float) -> str:
    if score >= TIER_HIGH_MIN:
        return "HIGH"
    if score >= TIER_MODERATE_MIN:
        return "MODERATE"
    return "LOW"


def build_situs_address(row: pd.Series) -> str:
    full = row.get("FullAddress")
    if full is not None and str(full).strip() not in ("", "nan", "None"):
        return str(full).strip()
    parts = [
        str(row.get("STREETNUM", "")).strip(),
        str(row.get("STREETDIR", "") or "").strip(),
        str(row.get("STREET", "")).strip(),
        str(row.get("CITY", "")).strip(),
    ]
    return " ".join(p for p in parts if p and p.lower() != "nan").strip()


def prepare_hazard_layers(
    fema_zones: gpd.GeoDataFrame,
    fault_lines: gpd.GeoDataFrame,
) -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """CRS gate + topology repair, then attach flood subscores in analysis CRS."""
    fema = prepare_layer(
        fema_zones, TARGET_CRS, layer_name="fema_flood", assume_crs_if_missing=WGS84
    )
    faults = prepare_layer(
        fault_lines, TARGET_CRS, layer_name="usgs_faults", assume_crs_if_missing=WGS84
    )

    if not fema.empty:
        fema = fema.copy()
        fema["flood_subscore"] = fema.apply(map_flood_risk, axis=1)
    else:
        fema = gpd.GeoDataFrame(
            {"flood_subscore": pd.Series(dtype=float), "FLD_ZONE": pd.Series(dtype=str)},
            geometry=[],
            crs=TARGET_CRS,
        )

    if faults.empty:
        faults = gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)

    return fema, faults


def score_parcels(
    parcels: gpd.GeoDataFrame,
    fema_zones: gpd.GeoDataFrame,
    fault_lines: gpd.GeoDataFrame,
    flood_weight: Optional[float] = None,
    fault_weight: Optional[float] = None,
) -> gpd.GeoDataFrame:
    """
    Overlay FEMA flood + fault proximity (AHP-weighted MCDA).

    Returns scored parcels in WGS84 (EPSG:4326) with columns:
    APN, SITUS_ADDRESS, flood_subscore, fault_dist_meters, fault_subscore,
    composite_risk_score, risk_category (+ geometry).
    """
    if parcels.empty:
        return parcels.copy()

    w_flood = WEIGHT_FLOOD if flood_weight is None else float(flood_weight)
    w_fault = WEIGHT_FAULT if fault_weight is None else float(fault_weight)

    parcels = prepare_layer(
        parcels, TARGET_CRS, layer_name="parcels", assume_crs_if_missing=WGS84
    ).copy()
    # Drop prior score columns (e.g. local parquet fallback) so overlays recompute cleanly
    for col in (
        "flood_subscore",
        "fault_dist_meters",
        "fault_subscore",
        "composite_risk_score",
        "risk_category",
        "FLD_ZONE",
        "index_right",
    ):
        if col in parcels.columns:
            parcels = parcels.drop(columns=[col])

    fema, faults = prepare_hazard_layers(fema_zones, fault_lines)

    parcels["SITUS_ADDRESS"] = parcels.apply(build_situs_address, axis=1)

    if not fema.empty:
        joined = gpd.sjoin(
            parcels,
            fema[["flood_subscore", "FLD_ZONE", "geometry"]],
            how="left",
            predicate="intersects",
        )
        parcels["flood_subscore"] = (
            joined.groupby(joined.index)["flood_subscore"]
            .max()
            .reindex(parcels.index)
            .fillna(0.0)
        )
    else:
        parcels["flood_subscore"] = 0.0

    parcels["flood_subscore"] = parcels["flood_subscore"].astype(float).round(2)

    if faults.empty:
        parcels["fault_dist_meters"] = float("inf")
    else:
        unary_faults = faults.geometry.union_all()
        parcels["fault_dist_meters"] = parcels.geometry.apply(
            lambda geom: geom.distance(unary_faults)
        )

    parcels["fault_subscore"] = parcels["fault_dist_meters"].apply(calculate_fault_score)
    parcels["composite_risk_score"] = (
        parcels["flood_subscore"] * w_flood + parcels["fault_subscore"] * w_fault
    ).round(2)
    parcels["risk_category"] = parcels["composite_risk_score"].apply(categorize_risk)

    return ensure_crs(parcels, WGS84, layer_name="scored_parcels", assume_crs_if_missing=TARGET_CRS)


def envelope_from_parcel_buffers(
    parcels: gpd.GeoDataFrame,
    radius_m: float,
) -> Bounds:
    """
    Build a WGS84 envelope covering parcels buffered by radius_m.

    Prefer this over expand_bounds(total_bounds) when matches may be scattered —
    a city-wide bbox + record cap returns unrelated parcels.
    """
    if parcels.empty:
        raise ValueError("Cannot build envelope from empty parcel layer.")
    utm = parcels.to_crs(TARGET_CRS)
    buffered = utm.geometry.buffer(radius_m).union_all()
    return tuple(gpd.GeoSeries([buffered], crs=TARGET_CRS).to_crs(WGS84).total_bounds)


def run_neighborhood_analysis(
    apn: Optional[str] = None,
    street: Optional[str] = None,
    city: Optional[str] = None,
    radius_m: float = 1000.0,
    match_limit: int = 25,
    neighborhood_limit: int = 400,
    region: Optional[str] = None,
) -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    End-to-end lookup: find matches, expand neighborhood, fetch hazards, score.

    Returns (matches, scored_neighborhood, fema_wgs84, faults_wgs84).
    """
    region_id = (region or default_region_id()).strip().lower()

    matches = query_parcels_by_search(
        apn=apn, street=street, city=city, limit=match_limit, region=region_id
    )
    if matches.empty:
        empty = gpd.GeoDataFrame(geometry=[], crs=WGS84)
        return matches, empty, empty, empty

    # If many street matches are scattered, keep a spatially tight seed set:
    # use the densest cluster around the first match's centroid.
    matches_utm = matches.to_crs(TARGET_CRS)
    seed_point = matches_utm.geometry.iloc[0].centroid
    matches_utm = matches_utm.copy()
    matches_utm["_seed_dist"] = matches_utm.geometry.centroid.distance(seed_point)
    seed_matches = (
        matches_utm.nsmallest(min(15, len(matches_utm)), "_seed_dist")
        .drop(columns=["_seed_dist"])
        .to_crs(WGS84)
    )

    neighborhood_bounds = envelope_from_parcel_buffers(seed_matches, radius_m)
    # Parcels: study window. Hazards: edge-buffered so border features aren't cut off.
    hazard_bounds = hazard_query_bounds(neighborhood_bounds)
    hazard_envelope = bounds_to_envelope(hazard_bounds)

    neighborhood = query_parcels_in_envelope(
        neighborhood_bounds,
        limit=neighborhood_limit,
        city=city,
        region=region_id,
    )
    if neighborhood.empty:
        neighborhood = seed_matches.copy()

    # Ensure search hits near the seed are present even if envelope query capped them out
    if "APN" in seed_matches.columns and "APN" in neighborhood.columns:
        missing = seed_matches[~seed_matches["APN"].isin(neighborhood["APN"])]
        if not missing.empty:
            neighborhood = pd.concat([neighborhood, missing], ignore_index=True)
            neighborhood = gpd.GeoDataFrame(neighborhood, geometry="geometry", crs=WGS84)

    fema = fetch_flood_zones(hazard_envelope)
    faults = fetch_fault_lines(hazard_envelope)
    scored = score_parcels(neighborhood, fema, faults)
    return seed_matches, scored, fema, faults


def build_risk_map(
    scored: gpd.GeoDataFrame,
    focus_apns: Optional[Union[Sequence[str], Iterable[str]]] = None,
) -> folium.Map:
    """Build a Folium/Leaflet map of scored parcels with optional focus highlight."""
    if scored.empty:
        raise ValueError("Cannot build map from empty scored parcel layer.")

    parcels = scored.to_crs(WGS84).copy()
    keep_cols = [
        "APN",
        "SITUS_ADDRESS",
        "flood_subscore",
        "fault_subscore",
        "fault_dist_meters",
        "composite_risk_score",
        "risk_category",
        "geometry",
    ]
    parcels = parcels[[c for c in keep_cols if c in parcels.columns]].copy()

    for col in ["flood_subscore", "fault_subscore", "fault_dist_meters", "composite_risk_score"]:
        if col in parcels.columns:
            parcels[col] = parcels[col].replace([float("inf")], None).astype(float).round(1)

    focus_set = {str(a) for a in (focus_apns or []) if a is not None}
    if focus_set and "APN" in parcels.columns:
        parcels["is_focus"] = parcels["APN"].astype(str).isin(focus_set)
    else:
        parcels["is_focus"] = False

    bounds = parcels.total_bounds
    map_center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    m = folium.Map(
        location=map_center,
        zoom_start=14,
        tiles=None,
        max_zoom=19,
        min_zoom=10,
        control_scale=True,
        zoom_control=True,
        scrollWheelZoom=True,
        dragging=True,
        width="100%",
        height="100%",
    )

    folium.TileLayer(
        tiles="CartoDB positron", name="Light Streets", max_zoom=19, show=True
    ).add_to(m)
    folium.TileLayer(
        tiles="OpenStreetMap", name="OpenStreetMap", max_zoom=19, show=False
    ).add_to(m)
    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr=(
            "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, "
            "and the GIS User Community"
        ),
        name="Esri Satellite",
        overlay=False,
        control=True,
        show=False,
        max_zoom=19,
        max_native_zoom=18,
        detect_retina=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri",
        name="Satellite Labels",
        overlay=True,
        control=True,
        show=False,
        max_zoom=19,
        max_native_zoom=18,
        detect_retina=False,
    ).add_to(m)

    def style_function(feature):
        props = feature["properties"]
        risk_cat = props.get("risk_category", "LOW")
        is_focus = props.get("is_focus", False)
        return {
            "fillColor": RISK_COLORS.get(risk_cat, "#5cb85c"),
            "color": "#000000" if is_focus else "#222222",
            "weight": 3.0 if is_focus else 1.25,
            "fillOpacity": 0.75 if is_focus else 0.55,
        }

    def highlight_function(_feature):
        return {"weight": 3, "color": "#000000", "fillOpacity": 0.75}

    tooltip_fields = [c for c in ["APN", "SITUS_ADDRESS", "risk_category"] if c in parcels.columns]
    popup_fields = [
        c
        for c in [
            "SITUS_ADDRESS",
            "APN",
            "flood_subscore",
            "fault_dist_meters",
            "composite_risk_score",
            "risk_category",
        ]
        if c in parcels.columns
    ]

    folium.GeoJson(
        parcels,
        name="Parcel Composite Risk",
        style_function=style_function,
        highlight_function=highlight_function,
        smooth_factor=0.5,
        zoom_on_click=False,
        tooltip=folium.GeoJsonTooltip(
            fields=tooltip_fields,
            aliases=["APN:", "Address:", "Risk:"][: len(tooltip_fields)],
            sticky=True,
            labels=True,
        ),
        popup=folium.GeoJsonPopup(
            fields=popup_fields,
            aliases=[
                "Address:",
                "APN:",
                "Flood score:",
                "Fault distance (m):",
                "Composite score:",
                "Risk tier:",
            ][: len(popup_fields)],
            localize=True,
            labels=True,
            max_width=280,
        ),
    ).add_to(m)

    # Keep legend inside the map frame (fixed + Streamlit iframe clips/slides).
    # Collapse layer control so it does not cover the map / steal clicks.
    frame_css = """
    <style>
      html, body {
        width: 100%;
        height: 100%;
        margin: 0;
        padding: 0;
        overflow: hidden;
        position: relative;
      }
      .folium-map, .leaflet-container {
        width: 100% !important;
        height: 100% !important;
      }
      .risk-legend {
        position: absolute;
        bottom: 28px;
        left: 12px;
        z-index: 1000;
        width: 190px;
        background: rgba(255,255,255,0.95);
        border: 1px solid #666;
        border-radius: 6px;
        padding: 10px 12px;
        font-size: 12px;
        font-family: Arial, sans-serif;
        box-shadow: 0 1px 4px rgba(0,0,0,0.25);
        pointer-events: none;
        line-height: 1.45;
      }
      .risk-legend i {
        width: 12px;
        height: 12px;
        display: inline-block;
        margin-right: 6px;
        vertical-align: middle;
      }
      .leaflet-control-layers {
        max-height: 70vh;
        overflow-y: auto;
      }
    </style>
    """
    legend_html = """
    <div class="risk-legend">
      <b>Composite Risk Index</b><br>
      <i style="background:#d9534f;"></i> High (≥ 70)<br>
      <i style="background:#f0ad4e;"></i> Moderate (30–69)<br>
      <i style="background:#5cb85c;"></i> Low (&lt; 30)<br>
      <span style="display:inline-block; margin-top:4px;">Bold outline = search match</span>
    </div>
    """
    # After iframe layout, Leaflet must recompute size or +/- zoom feels broken.
    resize_js = """
    <script>
    (function () {
      function invalidateFoliumMaps() {
        Object.keys(window).forEach(function (k) {
          var m = window[k];
          if (k.indexOf("map_") === 0 && m && typeof m.invalidateSize === "function") {
            try { m.invalidateSize(false); } catch (e) {}
          }
        });
      }
      setTimeout(invalidateFoliumMaps, 50);
      setTimeout(invalidateFoliumMaps, 250);
      setTimeout(invalidateFoliumMaps, 750);
      window.addEventListener("resize", invalidateFoliumMaps);
    })();
    </script>
    """
    m.get_root().header.add_child(Element(frame_css))
    m.get_root().html.add_child(Element(legend_html))
    m.get_root().html.add_child(Element(resize_js))
    folium.LayerControl(collapsed=True, position="topright").add_to(m)
    m.fit_bounds(
        [[bounds[1], bounds[0]], [bounds[3], bounds[2]]],
        padding=(24, 24),
        max_zoom=17,
    )
    return m


def summary_table(scored: gpd.GeoDataFrame) -> pd.DataFrame:
    """Return a non-geometry summary suitable for display/download."""
    cols = [
        "APN",
        "SITUS_ADDRESS",
        "flood_subscore",
        "fault_dist_meters",
        "fault_subscore",
        "composite_risk_score",
        "risk_category",
    ]
    present = [c for c in cols if c in scored.columns]
    table = scored[present].copy()
    if "fault_dist_meters" in table.columns:
        table["fault_dist_meters"] = table["fault_dist_meters"].replace(
            [float("inf")], pd.NA
        )
    return table.reset_index(drop=True)
