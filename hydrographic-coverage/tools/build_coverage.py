#!/usr/bin/env python3
"""Build the public FINN hydrographic coverage atlas from authoritative public sources."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOAA_DATA = ROOT / "data" / "noaa"
USACE_DATA = ROOT / "data" / "usace"
USGS_DATA = ROOT / "data" / "usgs"
ASSETS = ROOT / "public" / "assets"

NOAA_SERVICE = "https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/Status_New_NOAA_ENCs/MapServer"
IENC_SERVICE = "https://ienccloud.us/arcgis/rest/services/IENC/USACE_IENC_Master_Service/MapServer"
IENC_LAYER = 63
EHYDRO_SERVICE = "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/eHydro_Survey_Data/FeatureServer"
EHYDRO_LAYER = 0
USGS_SERVICE = "https://partnerships.nationalmap.gov/arcgis/rest/services/USGS_Inland_Bathymetry/MapServer"
USGS_LAYER = 0
NATURAL_EARTH = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"

BANDS = {5: "Overview", 4: "General", 3: "Coastal", 2: "Approach", 1: "Harbor", 0: "Berthing"}
MAP_WIDTH = 1800
PANEL_WIDTH = 430
WIDTH = MAP_WIDTH + PANEL_WIDTH
HEIGHT = 900
TRANSIENT_HTTP_CODES = {502, 503, 504}
USER_AGENT = "FINN-Field-Notes-Hydrographic-Coverage/1.0"


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def post(url: str, params: dict, retries: int = 4) -> dict:
    body = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= retries:
                raise
            delay = 2**attempt
            print(f"transient HTTP {exc.code} from {url}; retry {attempt + 1}/{retries} in {delay}s", flush=True)
            time.sleep(delay)


def query(service: str, layer_id: int, where: str = "1=1", out_fields: str = "*", extra: dict | None = None) -> dict:
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    if extra:
        params.update(extra)
    return get(f"{service}/{layer_id}/query?{urllib.parse.urlencode(params)}")


def query_ids(service: str, layer_id: int, where: str = "1=1") -> tuple[str, list[int]]:
    params = urllib.parse.urlencode({"where": where, "returnIdsOnly": "true", "f": "json"})
    payload = get(f"{service}/{layer_id}/query?{params}")
    oid = payload.get("objectIdFieldName") or "OBJECTID"
    ids = sorted(payload.get("objectIds") or [])
    return oid, ids


def query_id_batch(
    service: str,
    layer_id: int,
    oid: str,
    ids: list[int],
    geometry_precision: int = 4,
    max_allowable_offset: float = 0.0005,
) -> list[dict]:
    params = {
        "objectIds": ",".join(str(value) for value in ids),
        "outFields": oid,
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": geometry_precision,
        "maxAllowableOffset": max_allowable_offset,
        "f": "geojson",
    }
    return post(f"{service}/{layer_id}/query", params).get("features", [])


def query_all_by_ids(
    service: str,
    layer_id: int,
    label: str,
    where: str = "1=1",
    batch_size: int = 400,
    workers: int = 6,
    geometry_precision: int = 4,
    max_allowable_offset: float = 0.0005,
) -> tuple[list[dict], int, int, int]:
    oid, ids = query_ids(service, layer_id, where)
    batches = [ids[i : i + batch_size] for i in range(0, len(ids), batch_size)]
    features: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                query_id_batch,
                service,
                layer_id,
                oid,
                batch,
                geometry_precision,
                max_allowable_offset,
            )
            for batch in batches
        ]
        for number, future in enumerate(futures, 1):
            batch = future.result()
            features.extend(batch)
            print(f"{label} batch {number}/{len(batches)}: {len(batch)} features", flush=True)

    returned = {feature.get("properties", {}).get(oid) for feature in features}
    missing = [value for value in ids if value not in returned]
    if missing:
        raise RuntimeError(
            f"{label} object-ID completeness failure: expected {len(ids)}, got {len(returned)}, missing {len(missing)}"
        )
    return features, len(ids), batch_size, len(batches)


def rings(geometry: dict):
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        yield from coordinates
    elif geometry.get("type") == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon


def path(ring: list) -> str:
    points = []
    for lon, lat, *_ in ring:
        lon = max(-180, min(180, lon))
        lat = max(-90, min(90, lat))
        points.append(((lon + 180) / 360 * MAP_WIDTH, (90 - lat) / 180 * HEIGHT))
    return "" if not points else "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in points) + " Z"


def paths(features: list[dict]) -> list[str]:
    output = []
    for feature in features:
        for ring in rings(feature.get("geometry") or {}):
            svg_path = path(ring)
            if svg_path:
                output.append(f'<path d="{svg_path}"/>')
    return output


def svg(noaa: list[dict], ienc: list[dict], ehydro: list[dict], usgs: list[dict], land: list[dict]) -> str:
    grid = []
    for lon in range(-180, 181, 30):
        x = (lon + 180) / 360 * MAP_WIDTH
        grid.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{HEIGHT}"/>')
    for lat in range(-60, 61, 30):
        y = (90 - lat) / 180 * HEIGHT
        grid.append(f'<line x1="0" y1="{y}" x2="{MAP_WIDTH}" y2="{y}"/>')

    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    px = MAP_WIDTH + 30
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">
<title id="title">FINN authoritative hydrographic data coverage</title>
<desc id="desc">NOAA published ENC, USACE Inland ENC, USACE eHydro survey footprints, and USGS inland bathymetry survey footprints. Supplemental survey coverage is not ENC coverage.</desc>
<rect width="{MAP_WIDTH}" height="{HEIGHT}" fill="#eef4f6"/>
<g stroke="#d7e0e3" stroke-width="1">{''.join(grid)}</g>
<g fill="#e8e4da" stroke="#9aa4a6" stroke-width="0.8" fill-rule="evenodd">{''.join(paths(land))}</g>
<g fill="#177a89" fill-opacity="0.42" stroke="#075a66" stroke-opacity="0.62" stroke-width="0.65" fill-rule="evenodd">{''.join(paths(noaa))}</g>
<g fill="#4f9f55" fill-opacity="0.72" stroke="#2f6f35" stroke-opacity="0.95" stroke-width="1.15" fill-rule="evenodd">{''.join(paths(ienc))}</g>
<g fill="#e0a13b" fill-opacity="0.15" stroke="#c47a11" stroke-opacity="0.96" stroke-width="1.05" fill-rule="evenodd">{''.join(paths(usgs))}</g>
<g fill="#8b63a8" fill-opacity="0.10" stroke="#743aa3" stroke-opacity="0.92" stroke-width="0.75" fill-rule="evenodd">{''.join(paths(ehydro))}</g>
<rect x="{MAP_WIDTH}" y="0" width="{PANEL_WIDTH}" height="{HEIGHT}" fill="#ffffff"/>
<line x1="{MAP_WIDTH}" y1="0" x2="{MAP_WIDTH}" y2="{HEIGHT}" stroke="#b6c1c5" stroke-width="2"/>
<text x="{px}" y="54" font-family="system-ui,sans-serif" font-size="27" font-weight="700" fill="#18323a">FINN Hydrographic</text>
<text x="{px}" y="88" font-family="system-ui,sans-serif" font-size="27" font-weight="700" fill="#18323a">Coverage Atlas</text>
<text x="{px}" y="120" font-family="system-ui,sans-serif" font-size="13" fill="#65787d">Generated {when}</text>
<line x1="{px}" y1="145" x2="{WIDTH-28}" y2="145" stroke="#d6dde0"/>
<text x="{px}" y="180" font-family="system-ui,sans-serif" font-size="18" font-weight="700" fill="#263e45">LEGEND</text>
<rect x="{px}" y="200" width="22" height="22" rx="2" fill="#177a89" fill-opacity="0.72"/><text x="{px+34}" y="216" font-family="system-ui,sans-serif" font-size="15" fill="#40545a">NOAA published ENC</text>
<text x="{px+34}" y="237" font-family="system-ui,sans-serif" font-size="12" fill="#718187">{len(noaa):,} published cell footprints</text>
<rect x="{px}" y="260" width="22" height="22" rx="2" fill="#4f9f55" fill-opacity="0.88"/><text x="{px+34}" y="276" font-family="system-ui,sans-serif" font-size="15" fill="#40545a">USACE Inland ENC</text>
<text x="{px+34}" y="297" font-family="system-ui,sans-serif" font-size="12" fill="#718187">{len(ienc):,} coverage polygons</text>
<rect x="{px}" y="320" width="22" height="22" rx="2" fill="#e0a13b" fill-opacity="0.55" stroke="#c47a11"/><text x="{px+34}" y="336" font-family="system-ui,sans-serif" font-size="15" fill="#40545a">USGS inland bathymetry</text>
<text x="{px+34}" y="357" font-family="system-ui,sans-serif" font-size="12" fill="#718187">{len(usgs):,} survey footprints</text>
<rect x="{px}" y="380" width="22" height="22" rx="2" fill="#8b63a8" fill-opacity="0.55" stroke="#743aa3"/><text x="{px+34}" y="396" font-family="system-ui,sans-serif" font-size="15" fill="#40545a">USACE eHydro</text>
<text x="{px+34}" y="417" font-family="system-ui,sans-serif" font-size="12" fill="#718187">{len(ehydro):,} survey footprints</text>
<line x1="{px}" y1="450" x2="{WIDTH-28}" y2="450" stroke="#d6dde0"/>
<text x="{px}" y="487" font-family="system-ui,sans-serif" font-size="18" font-weight="700" fill="#263e45">DATA NOTE</text>
<text x="{px}" y="517" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">USGS and eHydro show</text>
<text x="{px}" y="537" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">authoritative survey footprints.</text>
<text x="{px}" y="565" font-family="system-ui,sans-serif" font-size="13" font-weight="700" fill="#743aa3">They are not ENC coverage.</text>
<line x1="{px}" y1="600" x2="{WIDTH-28}" y2="600" stroke="#d6dde0"/>
<text x="{px}" y="637" font-family="system-ui,sans-serif" font-size="18" font-weight="700" fill="#263e45">SOURCES</text>
<text x="{px}" y="668" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">NOAA Office of Coast Survey</text>
<text x="{px}" y="691" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">U.S. Army Corps of Engineers</text>
<text x="{px}" y="714" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">U.S. Geological Survey / 3DEP</text>
<text x="{px}" y="737" font-family="system-ui,sans-serif" font-size="13" fill="#40545a">Natural Earth basemap</text>
<text x="{px}" y="830" font-family="system-ui,sans-serif" font-size="12" fill="#718187">Coverage research artifact.</text>
<text x="{px}" y="850" font-family="system-ui,sans-serif" font-size="12" fill="#718187">Not a navigation chart.</text>
</svg>'''


def write_json(path: pathlib.Path, payload: dict, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        path.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    NOAA_DATA.mkdir(parents=True, exist_ok=True)
    USACE_DATA.mkdir(parents=True, exist_ok=True)
    USGS_DATA.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    noaa: list[dict] = []
    counts: dict[str, int] = {}
    for layer_id, band in BANDS.items():
        features = query(NOAA_SERVICE, layer_id, "status='Published'").get("features", [])
        counts[band] = len(features)
        noaa.extend(features)

    ienc = query(IENC_SERVICE, IENC_LAYER).get("features", [])
    ehydro, ehydro_expected, ehydro_batch, ehydro_batches = query_all_by_ids(
        EHYDRO_SERVICE, EHYDRO_LAYER, "eHydro"
    )
    usgs, usgs_expected, usgs_batch, usgs_batches = query_all_by_ids(
        USGS_SERVICE,
        USGS_LAYER,
        "USGS",
        batch_size=100,
        workers=2,
        geometry_precision=4,
        max_allowable_offset=0.001,
    )
    land = get(NATURAL_EARTH).get("features", [])
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    write_json(NOAA_DATA / "noaa-enc-cells.geojson", {"type": "FeatureCollection", "features": noaa})
    write_json(
        NOAA_DATA / "noaa-enc-coverage-summary.json",
        {
            "generated_at": now,
            "coverage_source": NOAA_SERVICE,
            "coverage_filter": "status = Published",
            "total_cells": len(noaa),
            "cells_by_usage_band": counts,
        },
    )
    write_json(USACE_DATA / "usace-ienc-coverage.geojson", {"type": "FeatureCollection", "features": ienc})
    write_json(
        USACE_DATA / "usace-ienc-coverage-summary.json",
        {
            "generated_at": now,
            "coverage_source": f"{IENC_SERVICE}/{IENC_LAYER}",
            "coverage_features": len(ienc),
        },
    )
    write_json(
        USACE_DATA / "usace-ehydro-survey-coverage.geojson",
        {"type": "FeatureCollection", "name": "FINN generalized USACE eHydro survey coverage", "features": ehydro},
        compact=True,
    )
    write_json(
        USACE_DATA / "usace-ehydro-survey-coverage-summary.json",
        {
            "generated_at": now,
            "coverage_source": f"{EHYDRO_SERVICE}/{EHYDRO_LAYER}",
            "coverage_layer": "SurveyJob",
            "coverage_features": len(ehydro),
            "source_feature_count": ehydro_expected,
            "fetch_strategy": "objectIds + bounded concurrent POST batches",
            "batch_size": ehydro_batch,
            "batch_count": ehydro_batches,
            "geometry_precision_decimal_places": 4,
            "max_allowable_offset_degrees": 0.0005,
            "artifact_role": "generalized coverage visualization",
            "data_class": "authoritative supplemental hydrography; not ENC coverage",
        },
    )
    write_json(
        USGS_DATA / "usgs-inland-bathymetry-survey-coverage.geojson",
        {"type": "FeatureCollection", "name": "FINN generalized USGS inland bathymetry survey coverage", "features": usgs},
        compact=True,
    )
    write_json(
        USGS_DATA / "usgs-inland-bathymetry-survey-coverage-summary.json",
        {
            "generated_at": now,
            "coverage_source": f"{USGS_SERVICE}/{USGS_LAYER}",
            "coverage_layer": "USGS_Inland_Bathymetry",
            "coverage_features": len(usgs),
            "source_feature_count": usgs_expected,
            "fetch_strategy": "objectIds + conservative concurrent POST batches with transient retry",
            "batch_size": usgs_batch,
            "batch_count": usgs_batches,
            "geometry_precision_decimal_places": 4,
            "max_allowable_offset_degrees": 0.001,
            "transient_http_retries": 4,
            "artifact_role": "generalized coverage visualization",
            "data_class": "authoritative supplemental bathymetry inventory; not ENC coverage",
            "rights_note": "USGS-produced information is generally public domain in the United States; credit USGS as source.",
        },
    )

    (ASSETS / "hydrographic-coverage.svg").write_text(svg(noaa, ienc, ehydro, usgs, land))
    print(
        f"NOAA {len(noaa)}; IENC {len(ienc)}; eHydro {len(ehydro)}/{ehydro_expected}; USGS {len(usgs)}/{usgs_expected}"
    )


if __name__ == "__main__":
    main()
