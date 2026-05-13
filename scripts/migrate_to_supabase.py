#!/usr/bin/env python3
"""Bulk-upload enrichment.db → Supabase Postgres.

One-time-ish migration. Idempotent: re-running upserts on (region_id) /
(owner_id) / (region_id, parcel_id), so it's safe to interrupt and resume.

Prereqs:
  1. Create your Supabase project.
  2. Paste scripts/supabase_schema.sql into the SQL Editor and run it.
  3. Copy .env.example -> .env and fill in DATABASE_URL.
  4. pip install psycopg2-binary python-dotenv

Usage:
  python3 scripts/migrate_to_supabase.py            # all tables
  python3 scripts/migrate_to_supabase.py --table owners
  python3 scripts/migrate_to_supabase.py --skip parcels   # everything except parcels
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("Missing dependency: pip install psycopg2-binary python-dotenv")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    sys.exit("Missing dependency: pip install python-dotenv")

PROJECT = Path(__file__).resolve().parent.parent
SRC_DB = PROJECT / "enrichment.db"
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set in .env. See .env.example.")
if not SRC_DB.exists():
    sys.exit(f"ERROR: {SRC_DB} not found")


# Tables in dependency order (regions → owners → parcels → aliases).
TABLES = [
    {
        "name": "regions",
        "pk": ["region_id"],
        "cols": [
            "region_id", "display_name", "state", "county",
            "center_lat", "center_lon", "last_ingested", "source_versions",
        ],
        "batch_size": 100,
    },
    {
        "name": "owners",
        "pk": ["owner_id"],
        "cols": [
            "owner_id", "region_id", "owner_name", "owner_name_norm",
            "mailing_addr1", "mailing_addr2", "mailing_city", "mailing_state",
            "mailing_zip", "mailing_addr_norm",
            "n_parcels", "n_homes", "n_vacant", "n_commercial",
            "total_value", "home_value", "absentee_pct", "icp", "is_hoa",
            "entity_type",
            "phone1", "phone1_type", "phone1_confidence",
            "phone1_source", "phone1_dnc", "phone1_updated",
            "phone2", "phone2_type", "phone2_confidence",
            "phone2_source", "phone2_dnc", "phone2_updated",
            "phone3", "phone3_type", "phone3_confidence",
            "phone3_source", "phone3_dnc", "phone3_updated",
            "email1", "email1_confidence", "email1_source", "email1_updated",
            "email2", "email2_confidence", "email2_source", "email2_updated",
            "status", "notes", "last_touched",
        ],
        "batch_size": 2000,
    },
    {
        "name": "parcels",
        "pk": ["region_id", "parcel_id"],
        "cols": [
            "parcel_id", "region_id", "owner_id",
            "parcel_addr", "parcel_city", "parcel_zip",
            "land_use", "land_use_code", "zoning", "acres",
            "land_value", "imp_value", "total_value",
            "sale_date", "sale_price", "council_district",
            "lat", "lon", "apn", "raw_owner_name", "raw_owner_addr",
        ],
        "batch_size": 2000,
    },
    {
        "name": "owner_aliases",
        "pk": [],  # use SQL upsert on (owner_id, raw_owner_name) — simplest is to wipe + reinsert
        "cols": [
            "owner_id", "raw_owner_name", "raw_owner_addr",
            "match_method", "match_score", "confirmed_at", "confirmed_by",
        ],
        "batch_size": 2000,
    },
]


def upsert_table(pg, sqlite_conn: sqlite3.Connection, table_meta: dict) -> None:
    name = table_meta["name"]
    cols = table_meta["cols"]
    pk = table_meta["pk"]
    batch_size = table_meta["batch_size"]

    src = sqlite_conn.execute(f"SELECT COUNT(*) FROM {name}")
    total = src.fetchone()[0]
    print(f"\n[{name}] {total:,} rows to upload")

    cur = pg.cursor()
    cols_csv = ", ".join(cols)
    template = "(" + ",".join(["%s"] * len(cols)) + ")"

    if pk:
        # ON CONFLICT (pk) DO UPDATE — idempotent upsert
        non_pk = [c for c in cols if c not in pk]
        update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        conflict_target = ", ".join(pk)
        sql = (
            f"INSERT INTO {name} ({cols_csv}) VALUES %s "
            f"ON CONFLICT ({conflict_target}) DO UPDATE SET {update_clause}"
        )
    else:
        # No PK to conflict on — wipe and reinsert (used for owner_aliases)
        print(f"  truncating {name} (no PK for upsert) ...")
        cur.execute(f"DELETE FROM {name}")
        sql = f"INSERT INTO {name} ({cols_csv}) VALUES %s"

    src = sqlite_conn.execute(f"SELECT {cols_csv} FROM {name}")
    batch: list[tuple] = []
    written = 0
    t0 = time.time()
    while True:
        rows = src.fetchmany(batch_size)
        if not rows:
            break
        execute_values(cur, sql, rows, template=template, page_size=batch_size)
        written += len(rows)
        elapsed = time.time() - t0
        rate = written / max(elapsed, 0.001)
        print(f"  {written:>9,} / {total:,}  ({rate:.0f}/s, {elapsed:.0f}s)")
        pg.commit()
    print(f"  done — {written:,} rows in {time.time()-t0:.1f}s")


def fix_sequences(pg) -> None:
    """Advance auto-increment sequences past the imported max(id)s, so future
    INSERTs from the app don't collide with existing rows."""
    cur = pg.cursor()
    # owners.owner_id is BIGINT PRIMARY KEY (no sequence — we control IDs).
    # owner_aliases.alias_id and contact_imports.import_id are BIGSERIAL.
    for table, col in [("owner_aliases", "alias_id"), ("contact_imports", "import_id")]:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}','{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM {table}), 1))"
        )
    pg.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", help="Upload only this table")
    ap.add_argument("--skip", action="append", default=[], help="Skip these tables")
    args = ap.parse_args()

    print(f"Connecting to Supabase Postgres ...")
    pg = psycopg2.connect(DATABASE_URL)
    pg.autocommit = False

    sqlite_conn = sqlite3.connect(str(SRC_DB))
    sqlite_conn.row_factory = sqlite3.Row

    tables = TABLES
    if args.table:
        tables = [t for t in TABLES if t["name"] == args.table]
        if not tables:
            sys.exit(f"unknown table: {args.table}")
    if args.skip:
        tables = [t for t in tables if t["name"] not in set(args.skip)]

    for t in tables:
        upsert_table(pg, sqlite_conn, t)

    if not args.table:
        print("\nFixing sequences ...")
        fix_sequences(pg)

    sqlite_conn.close()
    pg.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
