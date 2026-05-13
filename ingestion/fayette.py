#!/usr/bin/env python3
"""Migrate Fayette County (Lexington KY) parcel data into the unified schema.

Source files (at project root):
  parcelcsv.csv          — 114k rows. LFUCG/PVA parcel attributes (NO OWNER NAMES).
  parcelgeojson.geojson  — 114k features. Same attributes + MultiPolygon geometry.

IMPORTANT — DATA LIMITATION:
  The LFUCG GIS parcel export does NOT include owner names, mailing addresses, or
  assessed values. Only parcel geometry, street address, tax district (TAX), and
  property class (R/C/M/A/etc) are present. PVANUM groups parcels owned by the same
  PVA record but the actual owner identity is opaque without separate qPublic /
  PVA-office data (CLAUDE.md says don't scrape without explicit approval).

  We ingest what we have: one synthetic owner per PVANUM with name 'PVA #<num>'.
  Map view works. Owner-search / ICP / value filters are degraded for Fayette
  until skip-trace data is layered in via the future Import Contacts flow.

Output: rows in enrichment.db with region_id='fayette_ky'. Idempotent.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import time
from pathlib import Path

import ijson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import connect, create_schema  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
CSV_PATH = PROJECT / "parcelcsv.csv"
GEOJSON_PATH = PROJECT / "parcelgeojson.geojson"
DST_DB = PROJECT / "enrichment.db"
REGION_ID = "fayette_ky"

# Fayette County, KY — centered around 38.04 N, -84.50 W (Lexington).
LAT_MIN, LAT_MAX = 37.85, 38.25
LON_MIN, LON_MAX = -84.85, -84.20

# Property-class → property_type mapping. KY PVA CLASS codes:
#   R = Residential, M = Multi-family, C = Commercial, I = Industrial,
#   A = Agricultural, E = Exempt/Government, V = Vacant, X = Mixed
PT_HOME, PT_VACANT_RES, PT_COMMERCIAL = 0, 1, 2
CLASS_TO_PT = {
    "R": PT_HOME, "M": PT_HOME,
    "V": PT_VACANT_RES,
    "C": PT_COMMERCIAL, "I": PT_COMMERCIAL,
    "A": PT_COMMERCIAL, "E": PT_COMMERCIAL, "X": PT_COMMERCIAL,
}


def classify_property_type(class_code: str) -> int:
    return CLASS_TO_PT.get((class_code or "").strip().upper(), PT_COMMERCIAL)


def icp_bucket(n: int) -> int:
    if n < 5: return 0
    if n <= 19: return 1
    if n <= 49: return 2
    if n <= 99: return 3
    if n <= 500: return 4
    return 5


def polygon_centroid(geom: dict) -> tuple[float, float] | None:
    """Centroid of first ring of Polygon / MultiPolygon."""
    if not geom:
        return None
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Polygon" and coords:
        ring = coords[0]
    elif t == "MultiPolygon" and coords:
        ring = coords[0][0]
    else:
        return None
    if not ring:
        return None
    sx = sy = 0.0
    n = 0
    for pt in ring:
        if isinstance(pt, list) and len(pt) >= 2:
            sx += float(pt[0])
            sy += float(pt[1])
            n += 1
    if n == 0:
        return None
    return (sx / n, sy / n)


def load_centroids() -> dict[int, tuple[float, float]]:
    print(f"Streaming centroids from {GEOJSON_PATH} (100 MB) ...")
    out: dict[int, tuple[float, float]] = {}
    n_seen = 0
    t0 = time.time()
    with open(GEOJSON_PATH, "rb") as f:
        for feat in ijson.items(f, "features.item"):
            n_seen += 1
            props = feat.get("properties") or {}
            oid = props.get("OBJECTID")
            if oid is None:
                continue
            c = polygon_centroid(feat.get("geometry"))
            if c is None:
                continue
            lon, lat = c
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue
            out[int(oid)] = (round(lat, 6), round(lon, 6))
    print(f"  {n_seen:,} features, {len(out):,} centroids in-bbox ({time.time()-t0:.0f}s)")
    return out


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def ingest() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"ERROR: {CSV_PATH} not found")
    if not GEOJSON_PATH.exists():
        sys.exit(f"ERROR: {GEOJSON_PATH} not found")

    centroids = load_centroids()

    print(f"\nReading {CSV_PATH} ({CSV_PATH.stat().st_size/1e6:.0f} MB) ...")
    # Group parcels by PVANUM. Owner name = "PVA #<num>" placeholder.
    owner_agg: dict[str, dict] = {}
    parcel_rows: list[tuple] = []

    rows_total = 0
    rows_no_pvanum = 0
    rows_no_centroid = 0

    with open(CSV_PATH, encoding="utf-8-sig", errors="replace") as f:
        r = csv.DictReader(f)
        for row in r:
            rows_total += 1
            oid = to_int(row.get("OBJECTID"))
            if oid is None:
                continue
            pvanum = (row.get("PVANUM") or "").strip()
            if not pvanum:
                rows_no_pvanum += 1
                continue
            latlon = centroids.get(oid)
            if latlon is None:
                rows_no_centroid += 1
                continue
            lat, lon = latlon

            class_code = (row.get("CLASS") or "").strip().upper()
            tax_dist = (row.get("TAX") or "").strip()
            ptype = classify_property_type(class_code)
            parcel_addr = (row.get("ADDRESS") or "").strip()
            unit = (row.get("UNIT") or "").strip()
            if unit:
                parcel_addr = f"{parcel_addr} #{unit}"
            acres = to_float(row.get("PVA_ACRE"))

            # synthetic owner name
            owner_name = f"PVA #{pvanum}"

            agg = owner_agg.setdefault(owner_name, {
                "n_parcels": 0, "n_homes": 0, "n_vacant": 0, "n_commercial": 0,
                "total_value": 0, "home_value": 0,
            })
            agg["n_parcels"] += 1
            if ptype == PT_HOME:
                agg["n_homes"] += 1
            elif ptype == PT_VACANT_RES:
                agg["n_vacant"] += 1
            else:
                agg["n_commercial"] += 1

            parcel_rows.append((
                owner_name,
                str(oid),                # parcel_id
                parcel_addr or None,
                "LEXINGTON",             # parcel_city
                None,                    # parcel_zip
                f"{class_code} (KY PVA class)" if class_code else None,  # land_use
                class_code or None,                                       # land_use_code
                None,                    # zoning
                round(acres, 4) if acres else None,
                None,                    # land_value
                None,                    # imp_value
                None,                    # total_value (not in source)
                None,                    # sale_date
                None,                    # sale_price
                tax_dist or None,        # council_district — actually tax district for Fayette
                lat, lon,
                pvanum,                  # apn — use PVANUM
                owner_name,              # raw_owner_name
                None,                    # raw_owner_addr
            ))

    print(f"\nCSV scan complete:")
    print(f"  total rows:                 {rows_total:,}")
    print(f"  dropped (no PVANUM):        {rows_no_pvanum:,}")
    print(f"  dropped (no centroid):      {rows_no_centroid:,}")
    print(f"  parcel rows ready:          {len(parcel_rows):,}")
    print(f"  unique PVANUM groups:       {len(owner_agg):,}")
    print()
    print("  *** NOTE: Fayette CSV/GeoJSON have NO owner names or mailing data. ***")
    print("  ***       Owners are synthesized as 'PVA #<num>' placeholders.     ***")

    conn = connect(DST_DB)
    create_schema(conn)
    cur = conn.cursor()

    # idempotency
    cur.execute("DELETE FROM parcels WHERE region_id = ?", (REGION_ID,))
    cur.execute(
        """DELETE FROM owner_aliases
            WHERE owner_id IN (SELECT owner_id FROM owners WHERE region_id = ?)""",
        (REGION_ID,),
    )
    cur.execute("DELETE FROM owners WHERE region_id = ?", (REGION_ID,))
    cur.execute("DELETE FROM regions WHERE region_id = ?", (REGION_ID,))

    cur.execute(
        """
        INSERT INTO regions
          (region_id, display_name, state, county,
           center_lat, center_lon, last_ingested, source_versions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            REGION_ID, "Lexington / Fayette", "KY", "Fayette",
            38.0406, -84.5037,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            "parcelcsv.csv + parcelgeojson.geojson (no owner data in source)",
        ),
    )

    # Insert owners (one per PVANUM)
    name_to_id: dict[str, int] = {}
    for name, agg in owner_agg.items():
        n = agg["n_parcels"]
        cur.execute(
            """
            INSERT INTO owners
              (region_id, owner_name,
               mailing_addr1, mailing_addr2, mailing_city, mailing_state, mailing_zip,
               n_parcels, n_homes, n_vacant, n_commercial,
               total_value, home_value, absentee_pct, icp, is_hoa)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                REGION_ID, name,
                None, None, None, None, None,                  # no mailing data
                n, agg["n_homes"], agg["n_vacant"], agg["n_commercial"],
                0, 0,                                          # no values in source
                0.0, icp_bucket(n), 0,
            ),
        )
        name_to_id[name] = cur.lastrowid

    # Insert parcels
    BATCH = 5000
    batch: list[tuple] = []
    written = 0
    for r in parcel_rows:
        owner_name = r[0]
        oid = name_to_id.get(owner_name)
        if oid is None:
            continue
        batch.append((r[1], REGION_ID, oid, *r[2:]))
        if len(batch) >= BATCH:
            cur.executemany(
                """INSERT OR IGNORE INTO parcels
                   (parcel_id, region_id, owner_id,
                    parcel_addr, parcel_city, parcel_zip,
                    land_use, land_use_code, zoning, acres,
                    land_value, imp_value, total_value,
                    sale_date, sale_price, council_district,
                    lat, lon, apn, raw_owner_name, raw_owner_addr)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            written += len(batch)
            batch.clear()
    if batch:
        cur.executemany(
            """INSERT OR IGNORE INTO parcels
               (parcel_id, region_id, owner_id,
                parcel_addr, parcel_city, parcel_zip,
                land_use, land_use_code, zoning, acres,
                land_value, imp_value, total_value,
                sale_date, sale_price, council_district,
                lat, lon, apn, raw_owner_name, raw_owner_addr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
        written += len(batch)
    conn.commit()

    n_owners = cur.execute(
        "SELECT COUNT(*) FROM owners WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]
    n_parcels = cur.execute(
        "SELECT COUNT(*) FROM parcels WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]
    print()
    print(f"=== Fayette migration complete ===")
    print(f"  Synthetic owners (PVA #<num>):  {n_owners:,}")
    print(f"  Parcel rows:                    {n_parcels:,}  (inserted: {written:,})")
    conn.close()


if __name__ == "__main__":
    ingest()
