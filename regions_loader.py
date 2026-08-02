"""
Region configuration helpers for northern Nevada counties.

Loads config/regions.yaml: parcel REST URLs, field maps, bounds, and the compact
catalog used by the MapLibre county map (/api/regions).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_REGIONS_PATH = ROOT / "config" / "regions.yaml"

Bounds = Tuple[float, float, float, float]


@lru_cache(maxsize=4)
def load_regions_config(path: str | None = None) -> Dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_REGIONS_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Regions config not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "regions" not in data:
        raise ValueError(f"Invalid regions config: {cfg_path}")
    return data


def clear_regions_cache() -> None:
    load_regions_config.cache_clear()


def list_region_ids(cfg: Dict[str, Any] | None = None) -> List[str]:
    cfg = cfg or load_regions_config()
    return list(cfg.get("regions", {}).keys())


def default_region_id(cfg: Dict[str, Any] | None = None) -> str:
    cfg = cfg or load_regions_config()
    return str(cfg.get("default_region", "washoe"))


def get_region(region_id: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = cfg or load_regions_config()
    regions = cfg.get("regions", {})
    key = (region_id or default_region_id(cfg)).strip().lower()
    if key not in regions:
        known = ", ".join(sorted(regions))
        raise KeyError(f"Unknown region '{region_id}'. Known: {known}")
    region = dict(regions[key])
    region["id"] = key
    return region


def region_bounds(region_id: str) -> Bounds:
    r = get_region(region_id)
    b = r["bounds"]
    return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))


def region_area_presets(region_id: str) -> Dict[str, Bounds]:
    r = get_region(region_id)
    presets = r.get("area_presets") or {}
    out: Dict[str, Bounds] = {}
    for name, b in presets.items():
        out[str(name)] = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    return out


def region_label(region_id: str) -> str:
    r = get_region(region_id)
    return str(r.get("name", region_id))


def parquet_stem(region_id: str) -> str:
    """Output artifact stem for scored tiles (Washoe keeps legacy reno_risk)."""
    rid = (region_id or "").strip().lower()
    if rid == "washoe":
        return "reno_risk"
    return f"{rid}_risk"


def map_region_catalog(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Compact catalog for the city map UI (dropdown + PMTiles + camera).
    """
    cfg = cfg or load_regions_config()
    default = default_region_id(cfg)
    out_dir = Path(__file__).resolve().parent / "outputs"
    regions_out: Dict[str, Any] = {}
    for rid in list_region_ids(cfg):
        r = get_region(rid, cfg)
        b = r.get("bounds") or [-120, 38, -114, 42]
        center = r.get("center")
        if not center:
            center = [
                (float(b[0]) + float(b[2])) / 2.0,
                (float(b[1]) + float(b[3])) / 2.0,
            ]
        zoom = int(r.get("zoom") or 10)
        full_zoom = max(7, zoom - 2)
        stem = parquet_stem(rid)
        tiles_path = out_dir / f"{stem}.pmtiles"
        tiles_ok = tiles_path.is_file() and tiles_path.stat().st_size > 1000
        regions_out[rid] = {
            "id": rid,
            "name": r.get("name", rid),
            "title": f"{r.get('name', rid)} — Parcel Risk",
            "file": f"{stem}.pmtiles",
            "tilesAvailable": tiles_ok,
            "center": [float(center[0]), float(center[1])],
            "zoom": zoom,
            "fullCenter": [
                (float(b[0]) + float(b[2])) / 2.0,
                (float(b[1]) + float(b[3])) / 2.0,
            ],
            "fullZoom": full_zoom,
            "placeholder": r.get("placeholder")
            or f"Street or APN ({r.get('name', rid)})",
        }
    if default not in regions_out or not regions_out[default].get("tilesAvailable"):
        for rid, meta in regions_out.items():
            if meta.get("tilesAvailable"):
                default = rid
                break
    return {"default": default, "regions": regions_out}
