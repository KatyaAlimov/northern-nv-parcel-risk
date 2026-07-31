#!/usr/bin/env python3
"""
Serve the Reno city-wide PMTiles map over HTTP.

PMTiles requires real HTTP 206 byte-range responses.
Also proxies /api/lookup to Washoe parcel REST (avoids browser CORS issues).

Usage:
  python3 05_serve_city_map.py
  # open http://localhost:8080/city_map.html
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WASHOE_QUERY = (
    "https://wcgisweb.washoecounty.us/arcgis/rest/services/"
    "OpenData/OpenData/MapServer/0/query"
)


def lookup_parcels(q: str, city: str = "RENO", limit: int = 40) -> dict:
    """Query Washoe OpenData for street name or APN matches."""
    q = (q or "").strip()
    city = (city or "RENO").strip().upper() or "RENO"
    limit = max(1, min(int(limit or 40), 100))

    if not q:
        return {"type": "FeatureCollection", "features": []}

    # APN-like if mostly digits (allows dashes); otherwise treat as street name
    compact = re.sub(r"[\s\-]", "", q)
    if compact.isdigit():
        safe = q.replace("'", "''")
        where = f"APN LIKE '%{safe}%' AND CITY = '{city}'"
    else:
        street = q.upper().replace("'", "''")
        where = f"STREET LIKE '%{street}%' AND CITY = '{city}'"

    params = {
        "where": where,
        "outFields": "APN,STREETNUM,STREETDIR,STREET,CITY,FullAddress",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(limit),
    }
    url = WASHOE_QUERY + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "WashoeRiskMapLookup/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(payload["error"])

    # Normalize a friendly address property for popups
    for feat in payload.get("features", []):
        props = feat.setdefault("properties", {})
        full = props.get("FullAddress")
        if full:
            props["address"] = str(full)
        else:
            parts = [
                str(props.get("STREETNUM") or "").strip(),
                str(props.get("STREETDIR") or "").strip(),
                str(props.get("STREET") or "").strip(),
                str(props.get("CITY") or "").strip(),
            ]
            props["address"] = " ".join(p for p in parts if p and p.lower() != "none")

    return payload


class PMTilesHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Static file server with HTTP 206 ranges + /api/lookup proxy."""

    extensions_map = {
        **getattr(SimpleHTTPRequestHandler, "extensions_map", {}),
        ".pmtiles": "application/octet-stream",
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Accept-Ranges, Content-Length, Content-Range",
        )
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/lookup":
            self.handle_lookup(parsed)
            return
        return super().do_GET()

    def handle_lookup(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        q = (qs.get("q") or [""])[0]
        city = (qs.get("city") or ["RENO"])[0]
        limit = (qs.get("limit") or ["40"])[0]
        try:
            payload = lookup_parcels(q, city=city, limit=limit)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/geo+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        if not os.path.exists(path) or not os.path.isfile(path):
            self.send_error(404, "File not found")
            return None

        ctype = self.guess_type(path)
        try:
            file_size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                f.close()
                self.send_error(400, "Invalid Range header")
                return None
            start_s, end_s = match.groups()
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
            if start >= file_size or end >= file_size or start > end:
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return None

            length = end - start + 1
            f.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.send_header(
                "Last-Modified", self.date_time_string(os.path.getmtime(path))
            )
            self.end_headers()

            class _RangeReader:
                def __init__(self, fh, remaining):
                    self._fh = fh
                    self._remaining = remaining

                def read(self, n=-1):
                    if self._remaining <= 0:
                        return b""
                    if n is None or n < 0:
                        n = self._remaining
                    data = self._fh.read(min(n, self._remaining))
                    self._remaining -= len(data)
                    return data

                def close(self):
                    self._fh.close()

            return _RangeReader(f, length)

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        self.send_header(
            "Last-Modified", self.date_time_string(os.path.getmtime(path))
        )
        self.end_headers()
        return f


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (0.0.0.0 in Docker)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dir", default="outputs")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        raise SystemExit(f"Directory not found: {root}")

    html = root / "city_map.html"
    template = Path(__file__).resolve().parent / "templates" / "city_map.html"
    if template.exists():
        try:
            html.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: could not sync city_map.html into {root}: {exc}")

    tiles = root / "reno_risk.pmtiles"
    if not tiles.exists():
        print(f"WARNING: {tiles} missing. Run: python3 04_build_reno_tiles.py")

    handler = functools.partial(PMTilesHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {root} on {args.host}:{args.port}")
    print(f"Open http://localhost:{args.port}/city_map.html")
    print("Endpoints: /city_map.html  /reno_risk.pmtiles  /api/lookup?q=RIVERSIDE")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
