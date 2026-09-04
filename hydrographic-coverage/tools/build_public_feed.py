#!/usr/bin/env python3
"""Build stable public hydrographic coverage summary, history, and latest-change feeds."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
NOAA = ROOT / "data" / "noaa" / "noaa-enc-coverage-summary.json"
IENC = ROOT / "data" / "usace" / "usace-ienc-coverage-summary.json"
EHYDRO = ROOT / "data" / "usace" / "usace-ehydro-survey-coverage-summary.json"
USGS = ROOT / "data" / "usgs" / "usgs-inland-bathymetry-survey-coverage-summary.json"
HISTORY = PUBLIC / "history.json"
LATEST = PUBLIC / "latest-changes.json"
SUMMARY = PUBLIC / "coverage-summary.json"
BANDS = ["Overview", "General", "Coastal", "Approach", "Harbor", "Berthing"]


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def parse_timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def noaa_observation(summary: dict) -> dict:
    return {
        "observed_at": summary["generated_at"],
        "noaa_published_cells": summary["total_cells"],
        "noaa_usage_bands": {band: summary["cells_by_usage_band"].get(band, 0) for band in BANDS},
    }


def same_noaa_state(a: dict, b: dict) -> bool:
    return (
        a.get("noaa_published_cells") == b.get("noaa_published_cells")
        and a.get("noaa_usage_bands") == b.get("noaa_usage_bands")
    )


def change_set(previous: dict | None, current: dict | None) -> list[dict]:
    if not previous or not current:
        return []

    changes: list[dict] = []
    before = previous["noaa_published_cells"]
    after = current["noaa_published_cells"]
    if before != after:
        changes.append(
            {
                "source": "noaa_enc",
                "metric": "published_cells",
                "before": before,
                "after": after,
                "delta": after - before,
            }
        )

    for band in BANDS:
        before = previous["noaa_usage_bands"].get(band, 0)
        after = current["noaa_usage_bands"].get(band, 0)
        if before != after:
            changes.append(
                {
                    "source": "noaa_enc",
                    "metric": "usage_band",
                    "band": band,
                    "before": before,
                    "after": after,
                    "delta": after - before,
                }
            )
    return changes


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    noaa = load(NOAA)
    ienc = load(IENC)
    ehydro = load(EHYDRO)
    usgs = load(USGS)

    generated_at = max(
        [noaa["generated_at"], ienc["generated_at"], ehydro["generated_at"], usgs["generated_at"]],
        key=parse_timestamp,
    )

    write(
        SUMMARY,
        {
            "schema_version": 1,
            "generated_at": generated_at,
            "sources": {
                "noaa_enc": {
                    "authority": "NOAA Office of Coast Survey",
                    "data_class": "navigation-grade authoritative ENC",
                    "published_cells": noaa["total_cells"],
                    "usage_bands": noaa["cells_by_usage_band"],
                    "source": noaa["coverage_source"],
                },
                "usace_ienc": {
                    "authority": "U.S. Army Corps of Engineers",
                    "data_class": "navigation-grade authoritative Inland ENC",
                    "coverage_features": ienc["coverage_features"],
                    "source": ienc["coverage_source"],
                },
                "usace_ehydro": {
                    "authority": "U.S. Army Corps of Engineers",
                    "data_class": "authoritative supplemental hydrography; not ENC coverage",
                    "survey_footprints": ehydro["coverage_features"],
                    "source": ehydro["coverage_source"],
                },
                "usgs_inland_bathymetry": {
                    "authority": "U.S. Geological Survey / 3DEP",
                    "data_class": "authoritative supplemental bathymetry inventory; not ENC coverage",
                    "survey_footprints": usgs["coverage_features"],
                    "source": usgs["coverage_source"],
                },
            },
            "disclaimer": "Coverage research artifact. Supplemental survey footprints are not ENC coverage and this feed is not a navigation chart.",
        },
    )

    history = load(HISTORY) if HISTORY.exists() else {"schema_version": 1, "observations": []}
    observations = history.setdefault("observations", [])
    current = noaa_observation(noaa)
    if not observations or not same_noaa_state(observations[-1], current):
        observations.append(current)
        write(HISTORY, history)

    previous = observations[-2] if len(observations) >= 2 else None
    latest = observations[-1] if observations else None
    write(
        LATEST,
        {
            "schema_version": 1,
            "observed_at": latest.get("observed_at") if latest else None,
            "previous_observed_at": previous.get("observed_at") if previous else None,
            "changes": change_set(previous, latest),
            "semantics": "latest meaningful NOAA published-inventory change observed by FINN; unchanged refreshes do not create history entries",
        },
    )

    print(
        f"public coverage summary: NOAA {noaa['total_cells']} cells, "
        f"IENC {ienc['coverage_features']}, eHydro {ehydro['coverage_features']}, USGS {usgs['coverage_features']}"
    )
    print(f"NOAA meaningful observations: {len(observations)}; latest change records: {len(load(LATEST)['changes'])}")


if __name__ == "__main__":
    main()
