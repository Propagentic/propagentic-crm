"""Unified schema for the multi-region parcel explorer.

Defined per CLAUDE.md "Unified data schema" section. Tables:
  regions          — one row per loaded region
  owners           — pre-aggregated per region, post-dedup
  parcels          — one row per parcel, FK to owner
  owner_aliases    — audit trail for dedup merges
  contact_imports  — audit trail for CSV/XLSX contact uploads
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    region_id       TEXT PRIMARY KEY,
    display_name    TEXT,
    state           TEXT,
    county          TEXT,
    center_lat      REAL,
    center_lon      REAL,
    last_ingested   TIMESTAMP,
    source_versions TEXT
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id         TEXT NOT NULL,
    owner_name        TEXT NOT NULL,
    owner_name_norm   TEXT,
    mailing_addr1     TEXT,
    mailing_addr2     TEXT,
    mailing_city      TEXT,
    mailing_state     TEXT,
    mailing_zip       TEXT,
    mailing_addr_norm TEXT,
    n_parcels         INTEGER,
    n_homes           INTEGER,
    n_vacant          INTEGER,
    n_commercial      INTEGER,
    total_value       INTEGER,
    home_value        INTEGER,
    absentee_pct      REAL,
    icp               INTEGER,
    is_hoa            INTEGER,
    entity_type       TEXT,
    phone1            TEXT, phone1_type TEXT, phone1_confidence TEXT,
    phone1_source     TEXT, phone1_dnc INTEGER, phone1_updated TIMESTAMP,
    phone2            TEXT, phone2_type TEXT, phone2_confidence TEXT,
    phone2_source     TEXT, phone2_dnc INTEGER, phone2_updated TIMESTAMP,
    phone3            TEXT, phone3_type TEXT, phone3_confidence TEXT,
    phone3_source     TEXT, phone3_dnc INTEGER, phone3_updated TIMESTAMP,
    email1            TEXT, email1_confidence TEXT, email1_source TEXT, email1_updated TIMESTAMP,
    email2            TEXT, email2_confidence TEXT, email2_source TEXT, email2_updated TIMESTAMP,
    status            TEXT,
    notes             TEXT,
    last_touched      TIMESTAMP,
    FOREIGN KEY (region_id) REFERENCES regions(region_id)
);

CREATE TABLE IF NOT EXISTS parcels (
    parcel_id        TEXT,
    region_id        TEXT NOT NULL,
    owner_id         INTEGER NOT NULL,
    parcel_addr      TEXT,
    parcel_city      TEXT,
    parcel_zip       TEXT,
    land_use         TEXT,
    land_use_code    TEXT,
    zoning           TEXT,
    acres            REAL,
    land_value       INTEGER,
    imp_value        INTEGER,
    total_value      INTEGER,
    sale_date        TEXT,
    sale_price       INTEGER,
    council_district TEXT,
    lat              REAL,
    lon              REAL,
    apn              TEXT,
    raw_owner_name   TEXT,
    raw_owner_addr   TEXT,
    PRIMARY KEY (region_id, parcel_id),
    FOREIGN KEY (region_id) REFERENCES regions(region_id),
    FOREIGN KEY (owner_id)  REFERENCES owners(owner_id)
);

CREATE TABLE IF NOT EXISTS owner_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    raw_owner_name  TEXT NOT NULL,
    raw_owner_addr  TEXT,
    match_method    TEXT,
    match_score     REAL,
    confirmed_at    TIMESTAMP,
    confirmed_by    TEXT,
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id)
);

CREATE TABLE IF NOT EXISTS contact_imports (
    import_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    filename         TEXT,
    imported_at      TIMESTAMP,
    rows_total       INTEGER,
    rows_matched     INTEGER,
    rows_unmatched   INTEGER,
    rows_updated     INTEGER,
    rows_overwritten INTEGER,
    source_label     TEXT,
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_owners_region    ON owners(region_id);
CREATE INDEX IF NOT EXISTS idx_owners_namenorm  ON owners(region_id, owner_name_norm);
CREATE INDEX IF NOT EXISTS idx_owners_addrnorm  ON owners(region_id, mailing_addr_norm);
CREATE INDEX IF NOT EXISTS idx_owners_icp       ON owners(region_id, icp);
CREATE INDEX IF NOT EXISTS idx_parcels_owner    ON parcels(owner_id);
CREATE INDEX IF NOT EXISTS idx_parcels_region   ON parcels(region_id);
CREATE INDEX IF NOT EXISTS idx_aliases_owner    ON owner_aliases(owner_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
