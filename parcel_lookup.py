"""
Region-aware parcel street/APN lookup for the map API and Streamlit.

Uses config/regions.yaml `lookup` block when present, else `parcels`.
Falls back to scored GeoParquet under outputs/ when remote ArcGIS is down.
Storey (no street attributes): Nominatim geocode → spatial parcel query (local or REST).
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from regions_loader import get_region, parquet_stem

USER_AGENT = "NVParcelRiskLookup/2.0 (portfolio; multi-county NV)"

# Scored tile builds (APN lookup works even when county REST is down)
# Stem comes from regions_loader.parquet_stem()


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _combine_where(*parts: Optional[str]) -> str:
    clauses = [p.strip() for p in parts if p and str(p).strip() and str(p).strip() != "1=1"]
    if not clauses:
        return "1=1"
    return " AND ".join(f"({c})" for c in clauses)


def _lookup_cfg(region_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    region = get_region(region_id)
    parcels = dict(region.get("parcels") or {})
    lookup = dict(region.get("lookup") or {})
    # lookup overrides parcels keys when present
    merged = {**parcels, **{k: v for k, v in lookup.items() if v is not None}}
    if "search" in lookup:
        merged["search"] = lookup["search"]
    elif "search" in parcels:
        merged["search"] = parcels["search"]
    if "field_map" in lookup:
        merged["field_map"] = lookup["field_map"]
    return region, merged


def _is_apn_query(q: str) -> bool:
    compact = re.sub(r"[\s\-]", "", q)
    return bool(compact) and (compact.isdigit() or bool(re.fullmatch(r"[0-9A-Za-z\-]+", q) and any(c.isdigit() for c in compact) and len(compact) >= 4 and not re.search(r"[A-Za-z]{3,}", compact)))


def _outputs_dir() -> Path:
    # Prefer repo outputs/ next to this file; Docker mounts ./outputs → /app/outputs
    here = Path(__file__).resolve().parent
    for candidate in (here / "outputs", Path.cwd() / "outputs"):
        if candidate.is_dir():
            return candidate
    return here / "outputs"


@lru_cache(maxsize=4)
def _load_scored_parquet(region_id: str):
    """Cached GeoDataFrame of pre-scored parcels for offline/local lookup."""
    import geopandas as gpd

    stem = parquet_stem(region_id)
    if not stem:
        return None
    path = _outputs_dir() / f"{stem}.parquet"
    if not path.exists():
        return None
    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf


def _gdf_to_fc(gdf, region: Dict[str, Any], limit: int) -> dict:
    if gdf is None or gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    from shapely.geometry import mapping

    def _clean(v):
        if v is None:
            return None
        try:
            if isinstance(v, float) and math.isnan(v):
                return None
        except Exception:
            pass
        # pandas NA
        try:
            import pandas as pd

            if pd.isna(v):
                return None
        except Exception:
            pass
        if hasattr(v, "item"):
            try:
                return v.item()
            except Exception:
                pass
        return v

    slim = gdf.head(limit).copy()
    features = []
    for _, row in slim.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        props = {
            "APN": str(row.get("APN") or ""),
            "SITUS_ADDRESS": _clean(row.get("SITUS_ADDRESS")),
            "address": _clean(row.get("SITUS_ADDRESS")) or str(row.get("APN") or "Parcel"),
            "flood_subscore": _clean(row.get("flood_subscore")),
            "fault_dist_meters": _clean(row.get("fault_dist_meters")),
            "fault_subscore": _clean(row.get("fault_subscore")),
            "composite_risk_score": _clean(row.get("composite_risk_score")),
            "risk_category": _clean(row.get("risk_category")),
            "COUNTY": region.get("name"),
            "REGION_ID": region.get("id"),
            "source": "local_parquet",
        }
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _lookup_local(
    q: str,
    region_id: str,
    region: Dict[str, Any],
    limit: int,
    lonlat: Optional[Tuple[float, float]] = None,
) -> dict:
    """APN / address / point lookup against scored parquet."""
    try:
        gdf = _load_scored_parquet(region_id)
    except Exception:
        return {"type": "FeatureCollection", "features": []}
    if gdf is None or gdf.empty:
        return {"type": "FeatureCollection", "features": []}

    if lonlat is not None:
        from shapely.geometry import Point

        lon, lat = lonlat
        lon_pad, lat_pad = _meters_to_deg(400.0, lat)
        minx, miny, maxx, maxy = lon - lon_pad, lat - lat_pad, lon + lon_pad, lat + lat_pad
        try:
            hit = gdf.cx[minx:maxx, miny:maxy]
        except Exception:
            pt = Point(lon, lat)
            hit = gdf[gdf.geometry.intersects(pt.buffer(max(lon_pad, lat_pad)))]
        return _gdf_to_fc(hit, region, limit)

    q_norm = q.strip()
    if _is_apn_query(q_norm) and "APN" in gdf.columns:
        needle = re.sub(r"[\s\-]", "", q_norm).upper()
        apns = gdf["APN"].astype(str).str.replace(r"[\s\-]", "", regex=True).str.upper()
        hit = gdf[apns.str.contains(needle, na=False)]
        return _gdf_to_fc(hit, region, limit)

    if "SITUS_ADDRESS" in gdf.columns:
        needle = q_norm.upper()
        addrs = gdf["SITUS_ADDRESS"].astype(str).str.upper()
        hit = gdf[addrs.str.contains(re.escape(needle), na=False)]
        return _gdf_to_fc(hit, region, limit)

    return {"type": "FeatureCollection", "features": []}


def _http_json(url: str, timeout: int = 90) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _arcgis_geojson(url: str, params: Dict[str, Any], timeout: int = 120) -> dict:
    base = url.strip()
    if not base.rstrip("/").endswith("query"):
        base = base.rstrip("/") + "/query"
    full = base + "?" + urllib.parse.urlencode(params)
    payload = _http_json(full, timeout=timeout)
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(payload["error"])
    if not isinstance(payload, dict):
        return {"type": "FeatureCollection", "features": []}
    return payload


def _normalize_features(payload: dict, cfg: Dict[str, Any], region: Dict[str, Any]) -> dict:
    fmap = cfg.get("field_map") or {}
    features = payload.get("features") or []
    out_features = []
    for feat in features:
        props = dict(feat.get("properties") or feat.get("attributes") or {})
        # Map to common names
        for logical, src in fmap.items():
            if src and src in props and logical not in props:
                props[logical] = props[src]
            elif not src and logical not in props:
                props[logical] = None
        props["COUNTY"] = region.get("name")
        props["REGION_ID"] = region.get("id")
        full = props.get("FullAddress") or props.get("PHY_ADDR")
        if full:
            props["address"] = str(full)
        else:
            parts = [
                str(props.get("STREETNUM") or props.get("PLOCNUM") or "").strip(),
                str(props.get("STREETDIR") or props.get("PLOCDR") or "").strip(),
                str(props.get("STREET") or props.get("PLOCNM") or "").strip(),
                str(props.get("CITY") or props.get("SiteCity") or "").strip(),
            ]
            props["address"] = " ".join(p for p in parts if p and p.lower() not in {"none", "nan"})
            if not props["address"]:
                props["address"] = str(props.get("APN") or "Parcel")
        geom = feat.get("geometry")
        if geom is None:
            continue
        # If Esri geometry rings, skip — we request geojson
        out_features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": out_features}


def _geocode_nominatim(q: str, region: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Return (lon, lat) or None. Biased to region bbox."""
    bounds = region.get("bounds") or [-120, 38, -118, 40]
    minx, miny, maxx, maxy = [float(x) for x in bounds]
    viewbox = f"{minx},{maxy},{maxx},{miny}"  # left,top,right,bottom
    suffix = region.get("geocode_suffix") or f"{region.get('name')}, Nevada"
    query = f"{q}, {suffix}"
    params = {
        "q": query,
        "format": "json",
        "limit": "1",
        "viewbox": viewbox,
        "bounded": "1",
        "countrycodes": "us",
    }
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    try:
        data = _http_json(url, timeout=30)
    except Exception:
        return None
    if not data:
        return None
    hit = data[0]
    return float(hit["lon"]), float(hit["lat"])


def _meters_to_deg(radius_m: float, lat: float) -> Tuple[float, float]:
    lat_pad = radius_m / 111_320.0
    lon_pad = radius_m / (111_320.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    return lon_pad, lat_pad


def _spatial_lookup(
    cfg: Dict[str, Any],
    region: Dict[str, Any],
    lon: float,
    lat: float,
    limit: int,
    radius_m: float = 400.0,
) -> dict:
    lon_pad, lat_pad = _meters_to_deg(radius_m, lat)
    envelope = f"{lon - lon_pad},{lat - lat_pad},{lon + lon_pad},{lat + lat_pad}"
    where = _combine_where(cfg.get("base_where"))
    params = {
        "where": where,
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": cfg.get("out_fields", "*"),
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(limit),
    }
    payload = _arcgis_geojson(cfg["url"], params)
    return _normalize_features(payload, cfg, region)


def lookup_parcels(
    q: str,
    *,
    region: str = "washoe",
    city: Optional[str] = None,
    limit: int = 40,
) -> dict:
    """
    Street or APN lookup for a region → GeoJSON FeatureCollection.
    """
    q = (q or "").strip()
    limit = max(1, min(int(limit or 40), 100))
    if not q:
        return {"type": "FeatureCollection", "features": []}

    region_cfg, cfg = _lookup_cfg(region)
    search = cfg.get("search") or {}
    apn_field = search.get("apn_field") or "APN"
    street_field = search.get("street_field")
    city_field = search.get("city_field")
    extra_street_fields = search.get("extra_street_fields") or []
    parcels_cfg = region_cfg.get("parcels") or {}

    def _geocode_spatial() -> dict:
        xy = _geocode_nominatim(q, region_cfg)
        if not xy:
            return {"type": "FeatureCollection", "features": []}
        # Prefer bulk parcel layer (NDWR) for spatial hits when lookup is county GIS
        spatial_cfg = {
            **cfg,
            **{
                k: parcels_cfg[k]
                for k in ("url", "out_fields", "base_where", "field_map")
                if k in parcels_cfg
            },
        }
        try:
            out = _spatial_lookup(spatial_cfg, region_cfg, xy[0], xy[1], limit)
            if out.get("features"):
                return out
        except Exception:
            pass
        # Remote spatial failed (e.g. NDWR 500) → scored parquet near the point
        return _lookup_local(q, region, region_cfg, limit, lonlat=xy)

    def _local_or_empty() -> dict:
        return _lookup_local(q, region, region_cfg, limit)

    clauses: List[str] = []
    if _is_apn_query(q):
        safe = _escape_sql(q)
        # APN search: try lookup URL, fall back to parcels URL, then local parquet
        for try_cfg in (cfg, parcels_cfg):
            if not try_cfg.get("url"):
                continue
            apn_f = (try_cfg.get("search") or search).get("apn_field") or apn_field
            where = _combine_where(
                try_cfg.get("base_where") or cfg.get("base_where"),
                f"{apn_f} LIKE '%{safe}%'",
            )
            params = {
                "where": where,
                "outFields": try_cfg.get("out_fields") or cfg.get("out_fields", "*"),
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": str(limit),
            }
            try:
                payload = _arcgis_geojson(try_cfg["url"], params, timeout=45)
                out = _normalize_features(
                    payload, try_cfg if try_cfg.get("field_map") else cfg, region_cfg
                )
                if out.get("features"):
                    return out
            except Exception:
                continue
        return _local_or_empty()

    if street_field:
        street = _escape_sql(q.upper())
        street_clause = f"UPPER({street_field}) LIKE '%{street}%'"
        extras = [f"UPPER({f}) LIKE '%{street}%'" for f in extra_street_fields if f]
        if extras:
            clauses.append("(" + " OR ".join([street_clause] + extras) + ")")
        else:
            clauses.append(street_clause)

        if city and str(city).strip() and city_field:
            clauses.append(
                f"UPPER({city_field}) = '{_escape_sql(str(city).strip().upper())}'"
            )

        where = _combine_where(cfg.get("base_where"), " AND ".join(clauses))
        params = {
            "where": where,
            "outFields": cfg.get("out_fields", "*"),
            "outSR": "4326",
            "f": "geojson",
            "resultRecordCount": str(limit),
        }
        try:
            payload = _arcgis_geojson(cfg["url"], params, timeout=45)
            out = _normalize_features(payload, cfg, region_cfg)
            if out.get("features"):
                return out
        except Exception:
            pass
        # Local address match first (fast), then geocode spatial
        local = _local_or_empty()
        if local.get("features"):
            return local
        if cfg.get("geocode_fallback") or (region_cfg.get("lookup") or {}).get(
            "geocode_fallback"
        ):
            return _geocode_spatial()
        return {"type": "FeatureCollection", "features": []}

    # No street attribute — geocode then spatial query, then local
    if cfg.get("geocode_fallback") or (region_cfg.get("lookup") or {}).get(
        "geocode_fallback"
    ):
        try:
            out = _geocode_spatial()
            if out.get("features"):
                return out
        except Exception:
            pass
    return _local_or_empty()
