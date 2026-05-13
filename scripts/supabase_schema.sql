-- =====================================================================
-- Propagentic Parcel Explorer — Supabase schema
--
-- Paste this entire file into the Supabase SQL Editor and click "Run".
-- Mirrors lib/db.py (SQLite) with Postgres-native types.
-- Idempotent (CREATE TABLE IF NOT EXISTS) — safe to re-run.
-- =====================================================================

CREATE TABLE IF NOT EXISTS regions (
    region_id        TEXT PRIMARY KEY,
    display_name     TEXT,
    state            TEXT,
    county           TEXT,
    center_lat       DOUBLE PRECISION,
    center_lon       DOUBLE PRECISION,
    last_ingested    TIMESTAMPTZ,
    source_versions  TEXT
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id           BIGINT PRIMARY KEY,
    region_id          TEXT NOT NULL REFERENCES regions(region_id),
    owner_name         TEXT NOT NULL,
    owner_name_norm    TEXT,
    mailing_addr1      TEXT,
    mailing_addr2      TEXT,
    mailing_city       TEXT,
    mailing_state      TEXT,
    mailing_zip        TEXT,
    mailing_addr_norm  TEXT,
    n_parcels          INTEGER,
    n_homes            INTEGER,
    n_vacant           INTEGER,
    n_commercial       INTEGER,
    total_value        BIGINT,
    home_value         BIGINT,
    absentee_pct       DOUBLE PRECISION,
    icp                INTEGER,
    is_hoa             INTEGER,
    entity_type        TEXT,
    phone1             TEXT, phone1_type TEXT, phone1_confidence TEXT,
    phone1_source      TEXT, phone1_dnc INTEGER, phone1_updated TIMESTAMPTZ,
    phone2             TEXT, phone2_type TEXT, phone2_confidence TEXT,
    phone2_source      TEXT, phone2_dnc INTEGER, phone2_updated TIMESTAMPTZ,
    phone3             TEXT, phone3_type TEXT, phone3_confidence TEXT,
    phone3_source      TEXT, phone3_dnc INTEGER, phone3_updated TIMESTAMPTZ,
    email1             TEXT, email1_confidence TEXT, email1_source TEXT, email1_updated TIMESTAMPTZ,
    email2             TEXT, email2_confidence TEXT, email2_source TEXT, email2_updated TIMESTAMPTZ,
    status             TEXT,
    notes              TEXT,
    last_touched       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS parcels (
    parcel_id         TEXT,
    region_id         TEXT NOT NULL REFERENCES regions(region_id),
    owner_id          BIGINT NOT NULL REFERENCES owners(owner_id),
    parcel_addr       TEXT,
    parcel_city       TEXT,
    parcel_zip        TEXT,
    land_use          TEXT,
    land_use_code     TEXT,
    zoning            TEXT,
    acres             DOUBLE PRECISION,
    land_value        BIGINT,
    imp_value         BIGINT,
    total_value       BIGINT,
    sale_date         TEXT,
    sale_price        BIGINT,
    council_district  TEXT,
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    apn               TEXT,
    raw_owner_name    TEXT,
    raw_owner_addr    TEXT,
    PRIMARY KEY (region_id, parcel_id)
);

CREATE TABLE IF NOT EXISTS owner_aliases (
    alias_id         BIGSERIAL PRIMARY KEY,
    owner_id         BIGINT NOT NULL REFERENCES owners(owner_id),
    raw_owner_name   TEXT NOT NULL,
    raw_owner_addr   TEXT,
    match_method     TEXT,
    match_score      DOUBLE PRECISION,
    confirmed_at     TIMESTAMPTZ,
    confirmed_by     TEXT
);

CREATE TABLE IF NOT EXISTS contact_imports (
    import_id         BIGSERIAL PRIMARY KEY,
    filename          TEXT,
    imported_at       TIMESTAMPTZ,
    rows_total        INTEGER,
    rows_matched      INTEGER,
    rows_unmatched    INTEGER,
    rows_updated      INTEGER,
    rows_overwritten  INTEGER,
    source_label      TEXT,
    notes             TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_owners_region    ON owners(region_id);
CREATE INDEX IF NOT EXISTS idx_owners_namenorm  ON owners(region_id, owner_name_norm);
CREATE INDEX IF NOT EXISTS idx_owners_addrnorm  ON owners(region_id, mailing_addr_norm);
CREATE INDEX IF NOT EXISTS idx_owners_icp       ON owners(region_id, icp);
CREATE INDEX IF NOT EXISTS idx_parcels_owner    ON parcels(owner_id);
CREATE INDEX IF NOT EXISTS idx_parcels_region   ON parcels(region_id);
CREATE INDEX IF NOT EXISTS idx_parcels_latlon   ON parcels(lat, lon);
CREATE INDEX IF NOT EXISTS idx_aliases_owner    ON owner_aliases(owner_id);

-- =====================================================================
-- RLS NOTE
-- Supabase enables Row Level Security by default on tables in the public
-- schema. That's intentional and safe — until you add policies, no client
-- can read these via the anon key. The migration script bypasses RLS by
-- using the service_role connection. Policies for end users come later
-- when we wire up the frontend with auth.
-- =====================================================================
