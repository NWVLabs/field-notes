#!/usr/bin/env python3
"""Build a compact, browser-interactive SVG from public hydrographic coverage data.

The full source GeoJSON remains an ephemeral workflow product. This artifact is an
overview visualization: geometry is simplified for web delivery, source classes stay
separate, and supplemental survey footprints are never labeled as ENC coverage.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PUBLIC_ASSETS = ROOT / "public" / "assets"
NOAA_SERVICE = "https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/Status_New_NOAA_ENCs/MapServer"
NATURAL_EARTH = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"
BANDS = {5: "Overview", 4: "General", 3: "Coastal", 2: "Approach", 1: "Harbor", 0: "Berthing"}
WIDTH = 1800
HEIGHT = 900
USER_AGENT = "FINN-Field-Notes-Hydrographic-Coverage/1.0"


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def query_noaa(layer_id: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "where": "status='Published'",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": 4,
        "f": "geojson",
    })
    return get_json(f"{NOAA_SERVICE}/{layer_id}/query?{params}").get("features", [])


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def rings(geometry: dict):
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        yield from coordinates
    elif geometry.get("type") == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon


def project(point: list | tuple) -> tuple[float, float]:
    lon, lat = point[:2]
    lon = max(-180.0, min(180.0, float(lon)))
    lat = max(-90.0, min(90.0, float(lat)))
    return ((lon + 180.0) / 360.0 * WIDTH, (90.0 - lat) / 180.0 * HEIGHT)


def perpendicular_distance(point, start, end) -> float:
    x, y = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start_i, end_i = stack.pop()
        start = points[start_i]
        end = points[end_i]
        max_distance = -1.0
        index = None
        for i in range(start_i + 1, end_i):
            distance = perpendicular_distance(points[i], start, end)
            if distance > max_distance:
                max_distance = distance
                index = i
        if index is not None and max_distance > epsilon:
            keep.add(index)
            stack.append((start_i, index))
            stack.append((index, end_i))
    return [points[i] for i in sorted(keep)]


def simplify_ring(ring: list, epsilon: float, max_points: int) -> list[tuple[float, float]]:
    points = [project(point) for point in ring]
    if len(points) < 3:
        return []
    # Remove consecutive duplicate projected coordinates.
    deduped = [points[0]]
    for point in points[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    if len(deduped) < 3:
        return []
    closed = deduped[0] == deduped[-1]
    core = deduped[:-1] if closed else deduped
    if len(core) < 3:
        return []
    simplified = rdp(core + [core[0]], epsilon)[:-1]
    if len(simplified) > max_points:
        step = max(1, math.ceil(len(simplified) / max_points))
        simplified = simplified[::step]
    return simplified if len(simplified) >= 3 else core[:3]


def compound_path(features: list[dict], epsilon: float, max_points: int) -> str:
    parts: list[str] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        for ring in rings(geometry):
            points = simplify_ring(ring, epsilon, max_points)
            if not points:
                continue
            parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in points) + "Z")
    return "".join(parts)


def group(group_id: str, features: list[dict], epsilon: float, max_points: int, style: str) -> str:
    return f'<g id="{group_id}" style="{style}"><path d="{compound_path(features, epsilon, max_points)}"/></g>'


def main() -> None:
    PUBLIC_ASSETS.mkdir(parents=True, exist_ok=True)

    noaa_by_band = {band: query_noaa(layer_id) for layer_id, band in BANDS.items()}
    ienc = load(DATA / "usace" / "usace-ienc-coverage.geojson").get("features", [])
    ehydro = load(DATA / "usace" / "usace-ehydro-survey-coverage.geojson").get("features", [])
    usgs = load(DATA / "usgs" / "usgs-inland-bathymetry-survey-coverage.geojson").get("features", [])
    land = get_json(NATURAL_EARTH).get("features", [])

    grid = []
    for lon in range(-180, 181, 30):
        x = (lon + 180) / 360 * WIDTH
        grid.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{HEIGHT}"/>')
    for lat in range(-60, 61, 30):
        y = (90 - lat) / 180 * HEIGHT
        grid.append(f'<line x1="0" y1="{y:.1f}" x2="{WIDTH}" y2="{y:.1f}"/>')

    elements = [
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#eef4f6"/>',
        f'<g id="grid" stroke="#d7e0e3" stroke-width="1" vector-effect="non-scaling-stroke">{"".join(grid)}</g>',
        group("land", land, 0.35, 180, "fill:#e8e4da;stroke:#9aa4a6;stroke-width:.8;fill-rule:nonzero;vector-effect:non-scaling-stroke"),
    ]

    for band in BANDS.values():
        elements.append(group(
            f"noaa-{band.lower()}",
            noaa_by_band[band],
            0.18,
            96,
            "fill:#177a89;fill-opacity:.36;stroke:#075a66;stroke-opacity:.58;stroke-width:.65;fill-rule:nonzero;vector-effect:non-scaling-stroke",
        ))

    elements.extend([
        group("usace-ienc", ienc, 0.28, 96, "fill:#4f9f55;fill-opacity:.62;stroke:#2f6f35;stroke-opacity:.9;stroke-width:1.05;fill-rule:nonzero;vector-effect:non-scaling-stroke"),
        group("usgs-bathymetry", usgs, 0.4, 72, "fill:#e0a13b;fill-opacity:.14;stroke:#c47a11;stroke-opacity:.86;stroke-width:.9;fill-rule:nonzero;vector-effect:non-scaling-stroke"),
        group("usace-ehydro", ehydro, 0.9, 32, "fill:#8b63a8;fill-opacity:.08;stroke:#743aa3;stroke-opacity:.68;stroke-width:.55;fill-rule:nonzero;vector-effect:non-scaling-stroke"),
    ])

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc" data-finn-map-version="1">
<title id="title">Interactive FINN hydrographic coverage atlas</title>
<desc id="desc">Published NOAA ENC coverage, USACE Inland ENC coverage, USGS inland bathymetry survey footprints, and USACE eHydro survey footprints. Supplemental survey footprints are not ENC coverage. Geometry is simplified for overview visualization.</desc>
{"".join(elements)}
</svg>'''
    out = PUBLIC_ASSETS / "hydrographic-coverage-interactive.svg"
    out.write_text(svg)
    print(f"interactive SVG: {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
