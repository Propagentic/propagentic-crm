#!/usr/bin/env python3
"""Migrate Baltimore County parcel + owner data into the unified schema.

Source files (at project root):
  baltimoreparcel.csv  — 374k rows. Full owner+parcel attributes (CSV).
  baltimore.geojson    — 374k features. Same attributes + polygon geometry.

We use the CSV for the bulk of attribute data (faster than parsing 1GB of GeoJSON)
and streaming-parse the GeoJSON for parcel centroids (lat/lon). Join on OBJECTID.

Many of the first ~24k rows have no owner data ("NOT LOCATED" TAXPINs) — these
are filtered out.

Output: rows in enrichment.db with region_id='baltimore_md'. Idempotent.
"""
from __future__ import annotations

import csv
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterator

import ijson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import connect, create_schema  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
CSV_PATH = PROJECT / "baltimoreparcel.csv"
GEOJSON_PATH = PROJECT / "baltimore.geojson"
DST_DB = PROJECT / "enrichment.db"
REGION_ID = "baltimore_md"

# Baltimore County is centered around 39.40 N, -76.61 W (Towson area).
# Tight bbox to drop nonsense lat/lon values.
LAT_MIN, LAT_MAX = 39.15, 39.75
LON_MIN, LON_MAX = -77.05, -76.30

# Baltimore County LU_CODE values mostly come as strings ("RESIDENTIAL",
# "AGRICULTURAL", "COMMERCIAL", "INDUSTRIAL", "9999", numeric digits).
# Map onto our three buckets: 0=home, 1=vacant residential, 2=commercial/other.
PT_HOME, PT_VACANT_RES, PT_COMMERCIAL = 0, 1, 2


def classify_property_type(lu_code, brf_property_type) -> int:
    """Best-effort: Baltimore's coding is text-heavy and inconsistent.
    Falls back to commercial when we can't tell — same conservative default
    as Nashville."""
    code = (lu_code or "").strip().upper()
    brf = (brf_property_type or "").strip().upper()

    if "RESID" in code or "RESID" in brf or "DWELL" in brf or "TOWNHOUSE" in code:
        return PT_HOME
    if "VACANT" in code:
        if "RESID" in code:
            return PT_VACANT_RES
        return PT_COMMERCIAL  # vacant commercial / industrial
    if "AGRI" in code or "FARM" in code:
        return PT_COMMERCIAL
    if "COMM" in code or "INDUS" in code or "RETAIL" in code or "OFFICE" in code:
        return PT_COMMERCIAL
    return PT_COMMERCIAL


# HOA detection — same patterns as Nashville
HOA_PATTERNS = (
    "HOA", "HOMEOWNERS", "HOME OWNERS",
    "OWNERS ASSOC", "OWNERS ASSN",
    "CONDOMINIUM ASSOC", "CONDO ASSOC",
    "COMMUNITY ASSOC", "PROPERTY OWNERS", "MASTER ASSOC",
)


def is_hoa_name(name: str) -> bool:
    if not name:
        return False
    up = name.upper()
    return any(p in up for p in HOA_PATTERNS)


def icp_bucket(n: int) -> int:
    if n < 5: return 0
    if n <= 19: return 1
    if n <= 49: return 2
    if n <= 99: return 3
    if n <= 500: return 4
    return 5


def polygon_centroid(geom: dict) -> tuple[float, float] | None:
    """Quick centroid of a Polygon or MultiPolygon's first ring (good enough
    for heatmap markers — we're not doing area-weighted centroids).
    Returns (lon, lat) or None if unparsable.
    """
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


# ---------------------------------------------------------------------------
# 1) Stream the geojson once to build {OBJECTID: (lat, lon)}
# ---------------------------------------------------------------------------
def load_centroids() -> dict[int, tuple[float, float]]:
    print(f"Streaming centroids from {GEOJSON_PATH} (1GB, takes a minute) ...")
    out: dict[int, tuple[float, float]] = {}
    n_seen = 0
    n_kept = 0
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
            n_kept += 1
            if n_seen % 50000 == 0:
                elapsed = time.time() - t0
                print(f"  {n_seen:>7,} features scanned, {n_kept:>7,} centroids kept ({elapsed:.0f}s)")
    print(f"  {n_seen:,} features, {n_kept:,} centroids in-bbox  ({time.time()-t0:.0f}s)")
    return out


# ---------------------------------------------------------------------------
# 2) Stream the CSV, build per-parcel + per-owner records
# ---------------------------------------------------------------------------
def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def build_owner_name(row: dict) -> str:
    """Prefer FULL_OWNER_NAME; fall back to OWNER_NA1 + OWNER_NA2."""
    full = (row.get("FULL_OWNER_NAME") or "").strip()
    if full:
        return full
    n1 = (row.get("OWNER_NA1") or "").strip()
    n2 = (row.get("OWNER_NA2") or "").strip()
    return " ".join(p for p in (n1, n2) if p)


def build_parcel_addr(row: dict) -> str:
    """Reconstruct parcel premise address from components."""
    if (premise := (row.get("PREMISE_ADDRESS") or "").strip()):
        return premise
    parts = [
        row.get("ST_NUM") or "",
        row.get("ST_DIR") or "",
        row.get("STREETNAME") or "",
        row.get("STREETTYPE") or "",
    ]
    return " ".join(p for p in parts if p).strip()


def ingest() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"ERROR: {CSV_PATH} not found")
    if not GEOJSON_PATH.exists():
        sys.exit(f"ERROR: {GEOJSON_PATH} not found")

    centroids = load_centroids()

    print(f"\nReading {CSV_PATH} ({CSV_PATH.stat().st_size/1e6:.0f} MB) ...")
    # First pass: aggregate owners (group by raw owner_name).
    owner_agg: dict[str, dict] = {}
    parcel_rows: list[tuple] = []

    rows_total = 0
    rows_no_owner = 0
    rows_no_centroid = 0

    with open(CSV_PATH, encoding="utf-8-sig", errors="replace") as f:
        r = csv.DictReader(f)
        for row in r:
            rows_total += 1
            oid = to_int(row.get("OBJECTID"))
            if oid is None:
                continue
            owner_name = build_owner_name(row)
            if not owner_name:
                rows_no_owner += 1
                continue
            latlon = centroids.get(oid)
            if latlon is None:
                rows_no_centroid += 1
                continue
            lat, lon = latlon

            mailing_state = (row.get("OWNERSTATE") or "").strip()
            mailing_city = (row.get("OWNER_CITY") or "").strip()
            mailing_zip = (row.get("OWNER_ZIP") or "").strip()
            mailing_addr1 = (row.get("ADDRESS_1") or "").strip()
            mailing_addr2 = (row.get("ADDRESS_2") or "").strip()
            parcel_city = (row.get("CITY") or "").strip()
            parcel_zip = (row.get("ZIP_CODE") or "").strip()

            total_value = to_int(row.get("TOTAL_VALUE"))
            land_use = (row.get("LU_CODE") or "").strip()
            brf_pt = (row.get("BRF_PROPERTY_TYPE") or "").strip()
            ptype = classify_property_type(land_use, brf_pt)
            land_area = to_float(row.get("LAND_AREA"))
            # LAND_AREA is sqft for Baltimore — convert to acres
            acres = (land_area / 43560.0) if land_area else None
            council = (row.get("COUNCILMANIC_DISTRICT") or "").strip() or None
            parcel_addr = build_parcel_addr(row)

            # absentee: owner-state != MD OR owner_city != parcel_city
            absentee = bool(
                (mailing_state and mailing_state.upper() != "MD")
                or (mailing_city and parcel_city
                    and mailing_city.upper() != parcel_city.upper())
            )

            raw_owner_addr = ", ".join(p for p in (
                mailing_addr1, mailing_addr2, mailing_city, mailing_state, mailing_zip
            ) if p)

            # accumulate owner aggregate keyed on raw name (post-dedup will collapse)
            agg = owner_agg.setdefault(owner_name, {
                "n_parcels": 0,
                "n_homes": 0,
                "n_vacant": 0,
                "n_commercial": 0,
                "total_value": 0,
                "home_value": 0,
                "absentee_count": 0,
                "mailing_state_counts": {},
                "mailing_city_counts": {},
                "mailing_zip_counts": {},
                "mailing_addr1_counts": {},
                "mailing_addr2_counts": {},
            })
            agg["n_parcels"] += 1
            if ptype == PT_HOME:
                agg["n_homes"] += 1
                agg["home_value"] += total_value or 0
            elif ptype == PT_VACANT_RES:
                agg["n_vacant"] += 1
            else:
                agg["n_commercial"] += 1
            agg["total_value"] += total_value or 0
            if absentee:
                agg["absentee_count"] += 1
            # mode tracking
            for col, key in (
                ("mailing_state_counts", mailing_state),
                ("mailing_city_counts", mailing_city),
                ("mailing_zip_counts", mailing_zip),
                ("mailing_addr1_counts", mailing_addr1),
                ("mailing_addr2_counts", mailing_addr2),
            ):
                if key:
                    agg[col][key] = agg[col].get(key, 0) + 1

            # parcel row — owner_id filled in after we insert owners
            parcel_rows.append((
                owner_name,  # placeholder; replaced with FK after owner insert
                str(oid),
                parcel_addr or None,
                parcel_city or None,
                parcel_zip or None,
                land_use or None,
                land_use or None,  # land_use_code — same as text for Baltimore
                None,              # zoning — not in source
                round(acres, 4) if acres is not None else None,
                None,              # land_value — not split out in source
                None,              # imp_value — same
                total_value,
                None,              # sale_date — not in source
                None,              # sale_price
                council,
                lat, lon,
                str(oid),          # apn — Baltimore uses TAXPIN/OBJECTID; storing OBJECTID
                owner_name,
                raw_owner_addr or None,
                ptype, absentee,   # extra context for parcel row construction
            ))

    print(f"\nCSV scan complete:")
    print(f"  total rows:                 {rows_total:,}")
    print(f"  dropped (no owner):         {rows_no_owner:,}")
    print(f"  dropped (no centroid):      {rows_no_centroid:,}")
    print(f"  parcel rows ready:          {len(parcel_rows):,}")
    print(f"  unique owner-name groups:   {len(owner_agg):,}")

    # ----- write to DB -----
    conn = connect(DST_DB)
    create_schema(conn)
    cur = conn.cursor()

    # idempotency: wipe existing baltimore_md rows
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
            REGION_ID, "Baltimore County", "MD", "Baltimore",
            39.4015, -76.6019,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            f"baltimoreparcel.csv + baltimore.geojson",
        ),
    )

    name_to_id: dict[str, int] = {}
    for name, agg in owner_agg.items():
        def mode(d):
            if not d:
                return None
            return max(d.items(), key=lambda kv: kv[1])[0]

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
                mode(agg["mailing_addr1_counts"]),
                mode(agg["mailing_addr2_counts"]),
                mode(agg["mailing_city_counts"]),
                mode(agg["mailing_state_counts"]),
                mode(agg["mailing_zip_counts"]),
                n, agg["n_homes"], agg["n_vacant"], agg["n_commercial"],
                agg["total_value"], agg["home_value"],
                round(agg["absentee_count"] / n, 4) if n else 0.0,
                icp_bucket(n),
                int(is_hoa_name(name)),
            ),
        )
        name_to_id[name] = cur.lastrowid

    # insert parcels in batches
    BATCH = 5000
    batch: list[tuple] = []
    written = 0
    for row in parcel_rows:
        (
            owner_name, parcel_id, parcel_addr, parcel_city, parcel_zip,
            land_use, land_use_code, zoning, acres,
            land_value, imp_value, total_value, sale_date, sale_price, council,
            lat, lon, apn, raw_owner_name, raw_owner_addr, _ptype, _absentee,
        ) = row
        oid = name_to_id.get(owner_name)
        if oid is None:
            continue
        batch.append((
            parcel_id, REGION_ID, oid,
            parcel_addr, parcel_city, parcel_zip,
            land_use, land_use_code, zoning, acres,
            land_value, imp_value, total_value,
            sale_date, sale_price, council,
            lat, lon, apn, raw_owner_name, raw_owner_addr,
        ))
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

    # report
    n_owners = cur.execute(
        "SELECT COUNT(*) FROM owners WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]
    n_parcels = cur.execute(
        "SELECT COUNT(*) FROM parcels WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]
    print()
    print(f"=== Baltimore migration complete ===")
    print(f"  Owner rows:   {n_owners:,}")
    print(f"  Parcel rows:  {n_parcels:,}  (inserted: {written:,})")
    conn.close()


if __name__ == "__main__":
    ingest()
