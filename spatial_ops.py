"""
Spatial helpers: CRS checks, topology repair, buffered hazard windows, I/O.

- TARGET_CRS (EPSG:32611): meter-based analysis
- WGS84 (EPSG:4326): map display and REST queries
- EDGE_BUFFER_METERS: expand flood/fault fetches past the study window
- GeoParquet / FlatGeobuf read-write with GeoJSON fallback
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Union

import geopandas as gpd
import pandas as pd

# Keep in sync with risk_engine
TARGET_CRS = "EPSG:32611"
WGS84 = "EPSG:4326"

# Expand hazard fetches beyond the study window so faults/flood polygons
# that cross the edge are not truncated.
EDGE_BUFFER_METERS = 5000.0

Bounds = Tuple[float, float, float, float]
PathLike = Union[str, Path]


class CRSValidationError(ValueError):
    """Raised when a layer has no usable CRS and none was provided."""


def _crs_key(crs) -> Optional[str]:
    if crs is None:
        return None
    try:
        if hasattr(crs, "to_epsg") and crs.to_epsg():
            return f"EPSG:{crs.to_epsg()}"
    except Exception:
        pass
    return str(crs)


def ensure_crs(
    gdf: gpd.GeoDataFrame,
    target_crs: str = WGS84,
    *,
    layer_name: str = "layer",
    assume_crs_if_missing: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """
    CRS validation + transform gate — call immediately after raw reads.

    - If CRS is missing and assume_crs_if_missing is set, assign it (no warp).
    - If CRS is missing and nothing to assume, raise CRSValidationError.
    - If CRS differs from target, reproject with to_crs.
    """
    if gdf is None:
        return gpd.GeoDataFrame(geometry=[], crs=target_crs)
    if gdf.empty:
        out = gdf.copy()
        if out.crs is None:
            out = out.set_crs(target_crs, allow_override=True)
        elif _crs_key(out.crs) != _crs_key(target_crs):
            out = out.to_crs(target_crs)
        return out

    out = gdf.copy()
    if out.crs is None:
        if assume_crs_if_missing:
            out = out.set_crs(assume_crs_if_missing, allow_override=True)
        else:
            raise CRSValidationError(
                f"{layer_name}: geometry has no CRS. Re-fetch with outSR or "
                f"pass assume_crs_if_missing (e.g. '{WGS84}')."
            )

    src = _crs_key(out.crs)
    dst = _crs_key(target_crs)
    if src != dst:
        out = out.to_crs(target_crs)
    return out


def sanitize_geometries(
    gdf: gpd.GeoDataFrame,
    *,
    layer_name: str = "layer",
    drop_still_invalid: bool = True,
    verbose: bool = False,
) -> gpd.GeoDataFrame:
    """
    Filter null/empty geometries and repair with Shapely make_valid.
    Optionally drop features that remain invalid after repair.
    """
    if gdf is None or gdf.empty:
        crs = gdf.crs if gdf is not None else WGS84
        return gpd.GeoDataFrame(geometry=[], crs=crs)

    before = len(gdf)
    out = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
    if out.empty:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs)

    out["geometry"] = out.geometry.make_valid()
    out = out[out.geometry.notnull() & ~out.geometry.is_empty].copy()

    still_invalid = ~out.geometry.is_valid
    n_bad = int(still_invalid.sum())
    if drop_still_invalid and n_bad:
        out = out.loc[~still_invalid].copy()

    if verbose:
        print(
            f"  [{layer_name}] sanitize: {before} → {len(out)} "
            f"(repaired; dropped_invalid={n_bad if drop_still_invalid else 0})"
        )
    return out


def prepare_layer(
    gdf: gpd.GeoDataFrame,
    target_crs: str,
    *,
    layer_name: str = "layer",
    assume_crs_if_missing: Optional[str] = WGS84,
    verbose: bool = False,
) -> gpd.GeoDataFrame:
    """Full post-read gate: CRS enforce → topology repair."""
    gated = ensure_crs(
        gdf,
        target_crs,
        layer_name=layer_name,
        assume_crs_if_missing=assume_crs_if_missing,
    )
    return sanitize_geometries(gated, layer_name=layer_name, verbose=verbose)


def expand_bounds_meters(bounds: Bounds, radius_m: float) -> Bounds:
    """Expand a WGS84 bounding box by radius_m in each direction."""
    import math

    minx, miny, maxx, maxy = bounds
    mid_lat = (miny + maxy) / 2.0
    lat_pad = radius_m / 111_320.0
    lon_pad = radius_m / (111_320.0 * max(0.2, abs(math.cos(math.radians(mid_lat)))))
    return (minx - lon_pad, miny - lat_pad, maxx + lon_pad, maxy + lat_pad)


def edge_buffered_bounds(
    bounds: Bounds,
    buffer_m: float = EDGE_BUFFER_METERS,
) -> Bounds:
    """Study window expanded for hazard fetches (avoids border truncation)."""
    return expand_bounds_meters(bounds, buffer_m)


def _stem_paths(path: PathLike) -> Tuple[Path, Path, Path]:
    p = Path(path)
    # Accept either a stem path or a full file with suffix
    if p.suffix.lower() in {".parquet", ".fgb", ".geojson", ".shp", ".gpkg"}:
        base = p.with_suffix("")
    else:
        base = p
    return base.with_suffix(".parquet"), base.with_suffix(".fgb"), base.with_suffix(".geojson")


def read_geodata(
    path: PathLike,
    *,
    target_crs: str = WGS84,
    layer_name: Optional[str] = None,
    assume_crs_if_missing: Optional[str] = WGS84,
) -> gpd.GeoDataFrame:
    """
    Read vector data preferring GeoParquet → FlatGeobuf → GeoJSON/shapefile.
    Always runs CRS + topology gates after load.
    """
    p = Path(path)
    parquet_p, fgb_p, geojson_p = _stem_paths(p)

    candidates: list[Path] = []
    if p.exists() and p.is_file():
        candidates.append(p)
    for c in (parquet_p, fgb_p, geojson_p, p.with_suffix(".shp")):
        if c.exists() and c not in candidates:
            candidates.append(c)

    if not candidates:
        raise FileNotFoundError(
            f"No vector dataset found for '{path}' "
            f"(tried .parquet / .fgb / .geojson / .shp)"
        )

    chosen = candidates[0]
    name = layer_name or chosen.name
    suffix = chosen.suffix.lower()
    if suffix == ".parquet":
        gdf = gpd.read_parquet(chosen)
    else:
        gdf = gpd.read_file(chosen)

    return prepare_layer(
        gdf,
        target_crs,
        layer_name=name,
        assume_crs_if_missing=assume_crs_if_missing,
    )


def write_geodata(
    gdf: gpd.GeoDataFrame,
    path: PathLike,
    *,
    formats: Sequence[str] = ("parquet", "fgb"),
    target_crs: Optional[str] = None,
    layer_name: str = "layer",
) -> list[Path]:
    """
    Write GeoParquet and/or FlatGeobuf (default). GeoJSON only if requested.
    Applies CRS gate before write when target_crs is set.
    """
    out = gdf
    if target_crs is not None:
        out = ensure_crs(out, target_crs, layer_name=layer_name, assume_crs_if_missing=WGS84)
    out = sanitize_geometries(out, layer_name=layer_name)

    parquet_p, fgb_p, geojson_p = _stem_paths(path)
    parquet_p.parent.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    wanted = {f.lower().lstrip(".") for f in formats}

    if "parquet" in wanted:
        out.to_parquet(parquet_p, index=False)
        written.append(parquet_p)

    if "fgb" in wanted or "flatgeobuf" in wanted:
        # FlatGeobuf preserves types better than GeoJSON; requires pyogrio/fiona
        try:
            out.to_file(fgb_p, driver="FlatGeobuf")
            written.append(fgb_p)
        except Exception as exc:
            print(f"  Warning: FlatGeobuf write skipped ({exc})")

    if "geojson" in wanted:
        out.to_file(geojson_p, driver="GeoJSON")
        written.append(geojson_p)

    if not written:
        raise ValueError(f"No formats written from {formats}")
    return written
