#!/usr/bin/env python3
"""
Build scored county parcel tiles (Parquet + GeoJSONL + PMTiles).

Fetches parcels in a grid, scores flood/fault risk, and runs tippecanoe.
If interrupted, re-run the same command — finished grid cells are skipped.

Requires tippecanoe: brew install tippecanoe

Examples:
  python3 04_build_reno_tiles.py --region washoe
  python3 04_build_reno_tiles.py --region elko --grid-rows 12 --grid-cols 10
  python3 04_build_reno_tiles.py --region carson --tiles-only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd

from regions_loader import get_region, region_bounds, region_label, parquet_stem
from risk_engine import (
    WGS84,
    _iter_grid_cells,
    bounds_to_envelope,
    fetch_fault_lines,
    fetch_flood_zones,
    hazard_query_bounds,
    query_parcels_in_envelope,
    score_parcels,
)

OUT_DIR = Path("outputs")

EXPORT_COLS = [
    "APN",
    "SITUS_ADDRESS",
    "flood_subscore",
    "fault_dist_meters",
    "fault_subscore",
    "composite_risk_score",
    "risk_category",
    "COUNTY",
    "REGION_ID",
    "geometry",
]


def paths_for_region(region_id: str) -> dict:
    """Output artifact paths (washoe keeps legacy reno_* filenames)."""
    stem = parquet_stem(region_id)
    return {
        "parquet": OUT_DIR / f"{stem}.parquet",
        "geojsonl": OUT_DIR / f"{stem}.geojsonl",
        "pmtiles": OUT_DIR / f"{stem}.pmtiles",
        "state": OUT_DIR / f"{stem}_build_state.json",
        "stem": stem,
    }


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"completed_cells": [], "region": None}


def save_state(state: dict, state_path: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def load_existing(parquet_path: Path) -> gpd.GeoDataFrame:
    if not parquet_path.exists():
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84)
    return gdf


def save_parcels(gdf: gpd.GeoDataFrame, parquet_path: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if gdf.empty:
        return
    keep = [c for c in EXPORT_COLS if c in gdf.columns]
    out = gdf[keep].copy()
    if "APN" in out.columns:
        out = out.drop_duplicates(subset=["APN"], keep="last")
    if "fault_dist_meters" in out.columns:
        out["fault_dist_meters"] = out["fault_dist_meters"].replace(
            [float("inf"), float("-inf")], pd.NA
        )
    out.to_parquet(parquet_path, index=False)


def fetch_all_parcels_in_cell(
    bounds,
    region: str,
    city: str | None,
    page_size: int = 1000,
) -> gpd.GeoDataFrame:
    frames = []
    offset = 0
    while True:
        page = query_parcels_in_envelope(
            bounds,
            limit=page_size,
            city=city,
            offset=offset,
            region=region,
        )
        if page.empty:
            break
        frames.append(page)
        if len(page) < page_size:
            break
        offset += page_size
        if offset > 50_000:
            break
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=WGS84
    )


def process_cell(
    cell_id: str,
    bounds,
    region: str,
    city: str | None,
    seen_apns: set,
) -> gpd.GeoDataFrame:
    parcels = fetch_all_parcels_in_cell(bounds, region=region, city=city)
    if parcels.empty:
        print(f"  [{cell_id}] empty")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    if "APN" in parcels.columns:
        parcels = parcels[~parcels["APN"].astype(str).isin(seen_apns)].copy()
    if parcels.empty:
        print(f"  [{cell_id}] all duplicates, skip score")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    envelope = bounds_to_envelope(hazard_query_bounds(bounds))
    fema = fetch_flood_zones(envelope)
    faults = fetch_fault_lines(envelope)
    scored = score_parcels(parcels, fema, faults)
    print(
        f"  [{cell_id}] scored {len(scored)} "
        f"(flood polys={len(fema)}, faults={len(faults)})"
    )
    return scored


def export_geojsonl(gdf: gpd.GeoDataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = [c for c in EXPORT_COLS if c in gdf.columns]
    slim = gdf[keep].to_crs(WGS84).copy()
    for col in ["flood_subscore", "fault_subscore", "composite_risk_score", "fault_dist_meters"]:
        if col in slim.columns:
            slim[col] = pd.to_numeric(slim[col], errors="coerce")
    if "APN" in slim.columns:
        slim["APN"] = slim["APN"].astype(str)
    if path.exists():
        path.unlink()
    slim.to_file(path, driver="GeoJSONSeq")
    print(f"Wrote {path} ({len(slim)} features)")


def build_pmtiles(geojsonl: Path, pmtiles: Path) -> None:
    if not geojsonl.exists() or geojsonl.stat().st_size == 0:
        raise SystemExit(f"Missing GeoJSONL at {geojsonl}. Run scoring first.")

    tippecanoe = subprocess.run(["which", "tippecanoe"], capture_output=True, text=True)
    if tippecanoe.returncode != 0:
        raise SystemExit("tippecanoe not found. Install with: brew install tippecanoe")

    if pmtiles.exists():
        pmtiles.unlink()

    cmd = [
        "tippecanoe",
        "-o",
        str(pmtiles),
        "-Z6",
        "-z16",
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "-l",
        "parcels",
        "-y",
        "APN",
        "-y",
        "SITUS_ADDRESS",
        "-y",
        "flood_subscore",
        "-y",
        "fault_dist_meters",
        "-y",
        "fault_subscore",
        "-y",
        "composite_risk_score",
        "-y",
        "risk_category",
        "--force",
        str(geojsonl),
    ]
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print(f"Wrote {pmtiles} ({pmtiles.stat().st_size / 1e6:.1f} MB)")


def copy_viewer_template() -> None:
    src = Path("templates/city_map.html")
    dst = OUT_DIR / "city_map.html"
    if src.exists():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Copied viewer to {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build regional risk PMTiles")
    parser.add_argument(
        "--region",
        default="washoe",
        help="Region id from config/regions.yaml",
    )
    parser.add_argument("--grid-rows", type=int, default=10)
    parser.add_argument("--grid-cols", type=int, default=10)
    parser.add_argument(
        "--max-parcels",
        type=int,
        default=0,
        help="Stop after N unique parcels (0 = all in region)",
    )
    parser.add_argument(
        "--tiles-only",
        action="store_true",
        help="Skip fetching; rebuild geojsonl + PMTiles from parquet",
    )
    parser.add_argument(
        "--city",
        default="",
        help="Optional city filter (Washoe only, e.g. RENO). Empty = full county.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear completed-cell resume state (keeps existing parquet parcels).",
    )
    args = parser.parse_args()

    region_id = args.region.strip().lower()
    region = get_region(region_id)
    paths = paths_for_region(region_id)
    study_bounds = region_bounds(region_id)

    city = args.city.strip().upper() if args.city and args.city.strip() else None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Region: {region_label(region_id)} ({region_id})")
    print(f"Bounds: {study_bounds}")
    print(f"City filter: {city or '(none — full county)'}")
    print(f"Outputs: {paths['parquet'].name}, {paths['pmtiles'].name}")

    if not args.tiles_only:
        if args.reset_state and paths["state"].exists():
            paths["state"].unlink()
            print("Cleared build-state resume file.", flush=True)
        state = load_state(paths["state"])
        completed = set(state.get("completed_cells", []))
        existing = load_existing(paths["parquet"])
        seen = (
            set(existing["APN"].astype(str))
            if not existing.empty and "APN" in existing.columns
            else set()
        )
        print(
            f"Resume: {len(completed)} cells done, {len(seen)} parcels on disk. "
            f"Grid {args.grid_rows}x{args.grid_cols}.",
            flush=True,
        )

        cells = list(_iter_grid_cells(study_bounds, args.grid_rows, args.grid_cols))
        t0 = time.time()
        for idx, bounds in enumerate(cells):
            cell_id = f"r{idx // args.grid_cols}_c{idx % args.grid_cols}"
            if cell_id in completed:
                continue
            if args.max_parcels and len(seen) >= args.max_parcels:
                print(f"Reached --max-parcels={args.max_parcels}, stopping fetch.")
                break

            print(f"[{idx + 1}/{len(cells)}] Processing {cell_id} {bounds} ...", flush=True)
            try:
                scored = process_cell(
                    cell_id, bounds, region=region_id, city=city, seen_apns=seen
                )
            except Exception as exc:
                print(f"  ERROR {cell_id}: {exc}", file=sys.stderr)
                continue

            if not scored.empty:
                if "APN" in scored.columns:
                    seen.update(scored["APN"].astype(str).tolist())
                merged = (
                    pd.concat([existing, scored], ignore_index=True)
                    if not existing.empty
                    else scored
                )
                existing = gpd.GeoDataFrame(merged, geometry="geometry", crs=WGS84)
                if "APN" in existing.columns:
                    existing = existing.drop_duplicates(subset=["APN"], keep="last")
                if args.max_parcels:
                    existing = existing.head(args.max_parcels).copy()
                    seen = set(existing["APN"].astype(str))
                save_parcels(existing, paths["parquet"])

            completed.add(cell_id)
            state["completed_cells"] = sorted(completed)
            state["parcel_count"] = len(seen)
            state["region"] = region_id
            save_state(state, paths["state"])

        print(
            f"Fetch complete: {len(seen)} parcels in {time.time() - t0:.0f}s → {paths['parquet']}"
        )

    existing = load_existing(paths["parquet"])
    if existing.empty:
        raise SystemExit("No scored parcels found. Run without --tiles-only first.")

    print(f"Exporting {len(existing)} parcels to GeoJSONL...")
    export_geojsonl(existing, paths["geojsonl"])
    build_pmtiles(paths["geojsonl"], paths["pmtiles"])
    copy_viewer_template()

    print("\nDone.")
    print("Serve with: python3 05_serve_city_map.py")
    print(f"Tiles file: {paths['pmtiles']}")
    print("(City map viewer still defaults to reno_risk.pmtiles — swap filename or symlink to view other counties.)")


if __name__ == "__main__":
    main()
