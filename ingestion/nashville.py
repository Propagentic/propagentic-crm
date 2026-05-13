#!/usr/bin/env python3
"""Migrate the existing Nashville parcel data into the unified schema.

Reads from ~/Desktop/propagentic-nashville/nashville.db (the existing
sqlite produced by the legacy build_app.py pipeline) and writes into
~/Desktop/propagentic-explorer/enrichment.db under region_id='nashville_tn'.

Per CLAUDE.md step 1: pure schema migration. The _norm columns, entity_type,
and all contact fields are intentionally left NULL — they're filled by
later build-order steps (normalize/dedup in step 2, contact import in step 8).

Re-runnable: deletes existing nashville_tn rows before reinserting.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

# Make `lib` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import connect, create_schema  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SRC_DB = Path(os.path.expanduser("~/Desktop/propagentic-nashville/nashville.db"))
DST_DB = Path(__file__).resolve().parent.parent / "enrichment.db"
REGION_ID = "nashville_tn"

# ---------------------------------------------------------------------------
# Classifiers — mirror the existing build_app.py so the aggregation matches
# what the existing index.html was built from.
# ---------------------------------------------------------------------------
RES_HOME_CODES = {11, 12, 13, 14, 15, 16, 17, 18, 19, 38, 39, 81, 88}
RES_VACANT_CODES = {10, 80}
PT_HOME, PT_VACANT_RES, PT_COMMERCIAL = 0, 1, 2

HOA_PATTERNS = (
    "HOA", "HOMEOWNERS", "HOME OWNERS",
    "OWNERS ASSOC", "OWNERS ASSN",
    "CONDOMINIUM ASSOC", "CONDO ASSOC",
    "COMMUNITY ASSOC", "PROPERTY OWNERS", "MASTER ASSOC",
)


def classify_property_type(code) -> int:
    if code is None or (isinstance(code, float) and math.isnan(code)):
        return PT_COMMERCIAL
    try:
        c = int(code)
    except (ValueError, TypeError):
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if not digits:
            return PT_COMMERCIAL
        c = int(digits)
    if c in RES_HOME_CODES:
        return PT_HOME
    if c in RES_VACANT_CODES:
        return PT_VACANT_RES
    return PT_COMMERCIAL


def is_hoa_name(name: str) -> bool:
    if not name:
        return False
    up = name.upper()
    return any(p in up for p in HOA_PATTERNS)


def icp_bucket(n: int) -> int:
    if n < 5:
        return 0
    if n <= 19:
        return 1
    if n <= 49:
        return 2
    if n <= 99:
        return 3
    if n <= 500:
        return 4
    return 5


# ---------------------------------------------------------------------------
# Load + classify
# ---------------------------------------------------------------------------
def load_parcels() -> pd.DataFrame:
    print(f"Reading parcels from {SRC_DB} ...")
    conn = sqlite3.connect(str(SRC_DB))
    df = pd.read_sql_query(
        """
        SELECT
            "OBJECTID"                    AS objectid,
            "APN"                         AS apn,
            "Owner"                       AS owner,
            "Parcel Address"              AS parcel_addr,
            "Parcel City"                 AS parcel_city,
            "Parcel Zip Code"             AS parcel_zip,
            "Owner Address 1"             AS owner_addr1,
            "Owner Address 2"             AS owner_addr2,
            "Owner City"                  AS owner_city,
            "Owner State"                 AS owner_state,
            "Owner Zip Code"              AS owner_zip,
            "Sale Date"                   AS sale_date,
            "Sale Price"                  AS sale_price,
            "Land Use Code"               AS land_use_code,
            "Land Use Description"        AS land_use,
            "Total Appraised Value"       AS total_val,
            "Land Appraised Value"        AS land_val,
            "Improvement Appraised Value" AS imp_val,
            "Acres"                       AS acres,
            "Zoning"                      AS zoning,
            "Council District"            AS district,
            "Latitude"                    AS lat,
            "Longitude"                   AS lon
        FROM parcels
        WHERE "Latitude"  IS NOT NULL
          AND "Longitude" IS NOT NULL
          AND "Latitude"  BETWEEN 35.5 AND 36.6
          AND "Longitude" BETWEEN -87.5 AND -86.4
        """,
        conn,
    )
    conn.close()
    print(f"  {len(df):,} parcels with valid coordinates")
    return df


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df["owner"] = df["owner"].fillna("(unknown)")
    df["property_type"] = df["land_use_code"].map(classify_property_type)
    df["absentee"] = (
        (df["owner_state"].fillna("").str.strip().str.upper() != "TN")
        | (
            df["owner_city"].fillna("").str.strip().str.upper()
            != df["parcel_city"].fillna("").str.strip().str.upper()
        )
    )
    df["sale_date"] = (
        df["sale_date"].fillna("").astype(str).str.split(" ").str[0]
    )
    return df


def aggregate_owners(df: pd.DataFrame) -> pd.DataFrame:
    """Group raw owner names → one row per unique-string owner.

    Note: this is the *legacy* aggregation (string equality on raw owner name).
    Step 2's dedup will collapse trivial name/address variants further; the
    audit trail goes through owner_aliases.
    """
    grouped = df.groupby("owner")

    def mode_or_blank(s):
        s = s.dropna()
        return s.mode().iat[0] if not s.empty else ""

    owners = pd.DataFrame(
        {
            "n_parcels":     grouped.size(),
            "n_homes":       grouped["property_type"].apply(lambda s: int((s == PT_HOME).sum())),
            "n_vacant":      grouped["property_type"].apply(lambda s: int((s == PT_VACANT_RES).sum())),
            "n_commercial":  grouped["property_type"].apply(lambda s: int((s == PT_COMMERCIAL).sum())),
            "total_value":   grouped["total_val"].sum(min_count=1).fillna(0).astype(float),
            "home_value":    grouped.apply(
                lambda g: float(g.loc[g["property_type"] == PT_HOME, "total_val"].fillna(0).sum()),
                include_groups=False,
            ),
            "mailing_state": grouped["owner_state"].agg(mode_or_blank),
            "mailing_city":  grouped["owner_city"].agg(mode_or_blank),
            "mailing_zip":   grouped["owner_zip"].agg(mode_or_blank),
            "mailing_addr1": grouped["owner_addr1"].agg(mode_or_blank),
            "mailing_addr2": grouped["owner_addr2"].agg(mode_or_blank),
            "absentee_pct":  grouped["absentee"].mean(),
        }
    ).reset_index().rename(columns={"owner": "owner_name"})

    owners["icp"]    = owners["n_parcels"].map(icp_bucket).astype(int)
    owners["is_hoa"] = owners["owner_name"].map(is_hoa_name).astype(int)
    owners = owners.sort_values("n_parcels", ascending=False).reset_index(drop=True)
    print(f"  {len(owners):,} unique owners (raw-name groupby; dedup in step 2)")
    return owners


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def write_region(conn: sqlite3.Connection) -> None:
    src_versions = json.dumps({
        "source": str(SRC_DB),
        "method": "legacy_nashville_db_v1",
    })
    conn.execute(
        """
        INSERT OR REPLACE INTO regions
          (region_id, display_name, state, county,
           center_lat, center_lon, last_ingested, source_versions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            REGION_ID, "Nashville", "TN", "Davidson",
            36.1627, -86.7816,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            src_versions,
        ),
    )


def clear_region(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM parcels WHERE region_id = ?", (REGION_ID,))
    n_p = cur.rowcount
    cur.execute("DELETE FROM owners WHERE region_id = ?", (REGION_ID,))
    n_o = cur.rowcount
    if n_p or n_o:
        print(f"  cleared {n_o:,} existing owners and {n_p:,} parcels for re-load")


def insert_owners(conn: sqlite3.Connection, owners: pd.DataFrame) -> dict[str, int]:
    """Insert owners, return mapping {raw_owner_name: owner_id}."""
    name_to_id: dict[str, int] = {}
    cur = conn.cursor()
    rows = []
    for r in owners.itertuples(index=False):
        rows.append((
            REGION_ID,
            r.owner_name,
            (r.mailing_addr1 or "") or None,
            (r.mailing_addr2 or "") or None,
            (r.mailing_city or "") or None,
            (r.mailing_state or "") or None,
            (str(r.mailing_zip).strip() or None) if r.mailing_zip not in (None, "") else None,
            int(r.n_parcels),
            int(r.n_homes),
            int(r.n_vacant),
            int(r.n_commercial),
            int(round(r.total_value)),
            int(round(r.home_value)),
            round(float(r.absentee_pct), 4),
            int(r.icp),
            int(r.is_hoa),
        ))
    # Insert one-by-one so we can capture the autoincrement id per row.
    for params in rows:
        cur.execute(
            """
            INSERT INTO owners
              (region_id, owner_name,
               mailing_addr1, mailing_addr2, mailing_city, mailing_state, mailing_zip,
               n_parcels, n_homes, n_vacant, n_commercial,
               total_value, home_value, absentee_pct, icp, is_hoa)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        name_to_id[params[1]] = cur.lastrowid
    return name_to_id


def insert_parcels(conn: sqlite3.Connection, df: pd.DataFrame, name_to_id: dict[str, int]) -> int:
    cur = conn.cursor()
    written = 0
    skipped_dupes = 0
    batch: list[tuple] = []
    BATCH = 5000

    def flush():
        nonlocal written, skipped_dupes
        if not batch:
            return
        try:
            cur.executemany(
                """
                INSERT INTO parcels
                  (parcel_id, region_id, owner_id,
                   parcel_addr, parcel_city, parcel_zip,
                   land_use, land_use_code, zoning, acres,
                   land_value, imp_value, total_value,
                   sale_date, sale_price, council_district,
                   lat, lon, apn,
                   raw_owner_name, raw_owner_addr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            written += len(batch)
        except sqlite3.IntegrityError:
            # PK collision somewhere in the batch — fall back to per-row.
            for params in batch:
                try:
                    cur.execute(
                        """
                        INSERT INTO parcels
                          (parcel_id, region_id, owner_id,
                           parcel_addr, parcel_city, parcel_zip,
                           land_use, land_use_code, zoning, acres,
                           land_value, imp_value, total_value,
                           sale_date, sale_price, council_district,
                           lat, lon, apn,
                           raw_owner_name, raw_owner_addr)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        params,
                    )
                    written += 1
                except sqlite3.IntegrityError:
                    skipped_dupes += 1
        batch.clear()

    for r in df.itertuples(index=False):
        owner_id = name_to_id.get(r.owner)
        if owner_id is None:
            continue
        raw_addr_parts = [
            str(r.owner_addr1) if not pd.isna(r.owner_addr1) else "",
            str(r.owner_addr2) if not pd.isna(r.owner_addr2) else "",
            str(r.owner_city) if not pd.isna(r.owner_city) else "",
            str(r.owner_state) if not pd.isna(r.owner_state) else "",
            str(r.owner_zip) if not pd.isna(r.owner_zip) else "",
        ]
        raw_owner_addr = ", ".join(p for p in raw_addr_parts if p)
        # land_use_code can be float (e.g. 11.0), string ('80M'), or NaN.
        # Stored as TEXT — preserve source representation without forcing int.
        if pd.isna(r.land_use_code):
            land_use_code_str = None
        elif isinstance(r.land_use_code, float) and r.land_use_code.is_integer():
            land_use_code_str = str(int(r.land_use_code))
        else:
            land_use_code_str = str(r.land_use_code)
        batch.append((
            str(int(r.objectid)),       # parcel_id — globally unique row id from source
            REGION_ID,
            owner_id,
            str(r.parcel_addr) if not pd.isna(r.parcel_addr) else None,
            str(r.parcel_city) if not pd.isna(r.parcel_city) else None,
            str(r.parcel_zip) if not pd.isna(r.parcel_zip) else None,
            str(r.land_use) if not pd.isna(r.land_use) else None,
            land_use_code_str,
            str(r.zoning) if not pd.isna(r.zoning) else None,
            round(float(r.acres), 4) if not pd.isna(r.acres) else None,
            int(round(r.land_val)) if not pd.isna(r.land_val) else None,
            int(round(r.imp_val)) if not pd.isna(r.imp_val) else None,
            int(round(r.total_val)) if not pd.isna(r.total_val) else None,
            str(r.sale_date) if r.sale_date else None,
            int(round(r.sale_price)) if not pd.isna(r.sale_price) else None,
            str(int(r.district)) if not pd.isna(r.district) else None,
            round(float(r.lat), 6),
            round(float(r.lon), 6),
            str(r.apn) if not pd.isna(r.apn) else None,
            r.owner,
            raw_owner_addr or None,
        ))
        if len(batch) >= BATCH:
            flush()
    flush()
    if skipped_dupes:
        print(f"  WARNING: skipped {skipped_dupes:,} parcel rows with duplicate parcel_id")
    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not SRC_DB.exists():
        sys.exit(f"ERROR: source DB not found at {SRC_DB}")

    print(f"Destination: {DST_DB}")
    conn = connect(DST_DB)
    create_schema(conn)

    df = load_parcels()
    df = enrich(df)
    owners = aggregate_owners(df)

    write_region(conn)
    clear_region(conn)
    name_to_id = insert_owners(conn, owners)
    n_parcels = insert_parcels(conn, df, name_to_id)
    conn.commit()

    # --- verification ---
    n_owners = conn.execute(
        "SELECT COUNT(*) FROM owners WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]
    n_parcel_rows = conn.execute(
        "SELECT COUNT(*) FROM parcels WHERE region_id = ?", (REGION_ID,)
    ).fetchone()[0]

    print()
    print(f"=== Nashville migration complete ===")
    print(f"  Region rows:  1")
    print(f"  Owner rows:   {n_owners:,}")
    print(f"  Parcel rows:  {n_parcel_rows:,}  (inserted: {n_parcels:,})")
    print(f"  Source rows:  {len(df):,}")
    conn.close()


if __name__ == "__main__":
    main()
