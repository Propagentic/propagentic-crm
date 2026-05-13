# Propagentic parcel explorer — multi-region expansion

This project extends an existing Nashville-only parcel heatmap into a multi-region property prospecting tool covering Nashville (Davidson County, TN), Baltimore County (MD), and the Lexington area (Fayette County, KY) — with optional expansion to neighboring KY counties later. It also adds CSV/XLSX contact enrichment, owner deduplication across name/address variations, and an abstraction layer ready for a Supabase or Firebase backend.

You (Claude Code) are the agent building this. Read this whole file. The user has also given you two files for reference:

- `index.html` — the existing single-page heatmap app for Nashville. Match its visual design, sidebar filters, drawer UX, and outreach-tracker tab.
- `build_heatmap.py` — the existing data-build script for Nashville.

Everything you build expands what's there. Don't redesign the visual language. Don't rewrite features that already work.

## Project goal

The user is building Propagentic, a real estate SaaS targeting absentee/multi-property landlords. The heatmap is their prospecting frontend. Today it covers Nashville. They want to expand to two additional markets without rebuilding from scratch, add a way to attach phone/email data (from skip-tracing) to specific owners over time, and clean up the owner data so the same human isn't counted as three separate prospects because of comma placement in the source records.

## What changes

Four major additions on top of the existing app:

1. **Multi-region data model** — Nashville, Baltimore County, Lexington area as first-class regions. Data ingested separately, queryable individually or jointly.
2. **Region tabs on the Map view** — switch which county you're looking at. Sidebar filters apply within the selected region.
3. **Unified Owners search** — one search tab that queries across all regions with advanced filters (region, ICP bucket, property type, contact-info-present, etc.).
4. **CSV/XLSX contact import** — upload skip-trace output to enrich owner records with phones, emails, source URLs, confidence scores. Incremental: every upload adds/updates without overwriting existing data.

Plus two infrastructure changes:

5. **Owner deduplication** — fuzzy-match owners across name and mailing-address variations within a region. Surface duplicates for review, allow merging, store merge decisions.
6. **Backend abstraction** — data access goes through a thin layer with two implementations: local (current in-browser data blob) and remote (Supabase or Firebase). Build the local one now, leave the remote one as a clean interface for later.

## Data sources

Each region has different source URLs and data shapes. You'll need an ingestion script per region that normalizes everything into the unified schema described below.

### Nashville (Davidson County, TN) — existing

Already working. Sources:
- ArcGIS Hub: https://datanashvillegov-nashville.hub.arcgis.com/datasets/fa26cd9326c446179be059e00449cb1f_0/explore
- Property Assessor: https://portal.padctn.org/OFS/WP/Home

Don't touch the existing Nashville pipeline. Use it as the reference schema for the other two.

### Baltimore County (MD)

Two-source pipeline (Maryland data is split):

1. **Geometry** — Baltimore County GIS Open Data Portal at opendata.baltimorecountymd.gov. ArcGIS Hub layout, downloadable as GeoJSON or Shapefile. Includes parcel boundaries with PROP_ID linking to the state assessor data.

2. **Owner data** — Maryland Planning Department's statewide CAMA (Computer Assisted Mass Appraisal) bulk file at planning.maryland.gov/Pages/OurProducts/DownloadFiles.aspx. Published quarterly, covers all 24 Maryland jurisdictions. Filter rows where the county code is `03` for Baltimore County. The CAMA file includes owner name, mailing address, assessed value, land use code, and acres. **This is the bulk owner data source.**

3. **Fallback for single lookups** — Maryland SDAT Real Property Search at sdat.dat.maryland.gov/RealProperty. Useful for spot-checking. Note: SDAT's web interface does NOT allow searching by owner name (state privacy policy). You can only search by address or account number. The bulk CAMA file, however, contains the owner names — privacy applies to the search UI, not the bulk product.

Join key: the Maryland account number (county code + assessment district + account number). Geometry from the county GIS, owner data from the state CAMA file.

### Lexington / Fayette County (KY)

The user has already obtained the Fayette County data manually. Two files are present at the project root:

- **`parcelcsv.csv`** — tabular parcel + owner data (likely from the LFUCG Data Hub or Fayette PVA export)
- **`parcelgeojson.geojson`** — parcel boundary polygons (geometry)

**First step for stage 6**: open both files (head -5 the CSV, jq the GeoJSON properties of the first feature) and write down the actual schema before writing any ingestion code. Don't guess at column names — they might differ from what you'd expect based on the LFUCG ArcGIS Hub documentation. The CSV is probably the canonical source for owner names and mailing addresses; the GeoJSON provides lat/lon centroids and parcel boundary geometry. Join them on whatever parcel-ID column they share (likely `PVA_ID`, `PARCEL_ID`, or `LRSN`).

If the CSV is missing some fields the unified schema needs (e.g. mailing address might be absent in some LFUCG exports), log which fields are missing and ingest what's available. Surface a warning to the user listing the missing fields so they can decide whether to source supplementary data from fayettepva.com or qPublic.

For reference if you need to fill gaps:
- Fayette County PVA: fayettepva.com (web search, no bulk download)
- qPublic Fayette interface: qpublic.net/ky/fayette/search1.html (single-parcel lookups)
- Fayette County Clerk land records: fayettecountyclerk.com/web/landrecords (recent deeds)

But default to using only what's in `parcelcsv.csv` and `parcelgeojson.geojson`. Don't scrape or hit external sources without explicit user approval.

### Adjacent KY counties (deferred)

The "surrounding Lexington area" includes Jessamine, Woodford, Scott, Bourbon, Clark, and Madison counties. Each has its own PVA on the qPublic system. Structurally identical to Fayette but each is a separate ingestion. **Do not implement these in v1.** Build Fayette first. Add adjacent counties one at a time once the Fayette ingestion is solid.

## Unified data schema

The existing app uses a two-table mental model — OWNERS and PARCELS — with the OWNERS table pre-aggregated. Keep that structure. Add a `region` column to both tables and a `region` dimension to every filter.

```sql
-- One row per region, defines what counties are loaded
CREATE TABLE regions (
    region_id       TEXT PRIMARY KEY,   -- 'nashville_tn', 'baltimore_md', 'fayette_ky'
    display_name    TEXT,
    state           TEXT,
    county          TEXT,
    center_lat      REAL,
    center_lon      REAL,
    last_ingested   TIMESTAMP,
    source_versions TEXT
);

-- One row per unique owner per region, AFTER dedup
CREATE TABLE owners (
    owner_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id         TEXT NOT NULL,
    owner_name        TEXT NOT NULL,    -- canonical form
    owner_name_norm   TEXT,             -- normalized for matching
    mailing_addr1     TEXT,
    mailing_addr2     TEXT,
    mailing_city      TEXT,
    mailing_state     TEXT,
    mailing_zip       TEXT,
    mailing_addr_norm TEXT,             -- normalized for matching
    n_parcels         INTEGER,
    n_homes           INTEGER,
    n_vacant          INTEGER,
    n_commercial      INTEGER,
    total_value       INTEGER,
    home_value        INTEGER,
    absentee_pct      REAL,
    icp               INTEGER,
    is_hoa            INTEGER,
    entity_type       TEXT,             -- 'individual','llc','corp','trust','lp','other'
    phone1            TEXT, phone1_type TEXT, phone1_confidence TEXT,
    phone1_source     TEXT, phone1_dnc INTEGER, phone1_updated TIMESTAMP,
    phone2            TEXT, phone2_type TEXT, phone2_confidence TEXT,
    phone2_source     TEXT, phone2_dnc INTEGER, phone2_updated TIMESTAMP,
    phone3            TEXT, phone3_type TEXT, phone3_confidence TEXT,
    phone3_source     TEXT, phone3_dnc INTEGER, phone3_updated TIMESTAMP,
    email1            TEXT, email1_confidence TEXT, email1_source TEXT, email1_updated TIMESTAMP,
    email2            TEXT, email2_confidence TEXT, email2_source TEXT, email2_updated TIMESTAMP,
    status            TEXT,             -- preserved from existing outreach tracker
    notes             TEXT,
    last_touched      TIMESTAMP,
    FOREIGN KEY (region_id) REFERENCES regions(region_id)
);

CREATE TABLE parcels (
    parcel_id       TEXT,
    region_id       TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    parcel_addr     TEXT,
    parcel_city     TEXT,
    parcel_zip      TEXT,
    land_use        TEXT,
    zoning          TEXT,
    acres           REAL,
    land_value      INTEGER,
    imp_value       INTEGER,
    total_value     INTEGER,
    sale_date       TEXT,
    sale_price      INTEGER,
    council_district TEXT,
    lat             REAL,
    lon             REAL,
    raw_owner_name  TEXT,               -- preserved from source
    raw_owner_addr  TEXT,
    PRIMARY KEY (region_id, parcel_id),
    FOREIGN KEY (region_id) REFERENCES regions(region_id),
    FOREIGN KEY (owner_id)  REFERENCES owners(owner_id)
);

CREATE TABLE owner_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    raw_owner_name  TEXT NOT NULL,
    raw_owner_addr  TEXT,
    match_method    TEXT,               -- 'exact','normalized_name','fuzzy_name','address_only','manual_merge'
    match_score     REAL,
    confirmed_at    TIMESTAMP,
    confirmed_by    TEXT,
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id)
);

CREATE TABLE contact_imports (
    import_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT,
    imported_at     TIMESTAMP,
    rows_total      INTEGER,
    rows_matched    INTEGER,
    rows_unmatched  INTEGER,
    rows_updated    INTEGER,
    rows_overwritten INTEGER,
    source_label    TEXT,               -- 'batchdata_2026q2','tracerfy_test','manual'
    notes           TEXT
);

CREATE INDEX idx_owners_region    ON owners(region_id);
CREATE INDEX idx_owners_namenorm  ON owners(region_id, owner_name_norm);
CREATE INDEX idx_owners_addrnorm  ON owners(region_id, mailing_addr_norm);
CREATE INDEX idx_owners_icp       ON owners(region_id, icp);
CREATE INDEX idx_parcels_owner    ON parcels(owner_id);
CREATE INDEX idx_parcels_region   ON parcels(region_id);
CREATE INDEX idx_aliases_owner    ON owner_aliases(owner_id);
```

The existing Nashville data should be migrated into this schema with `region_id = 'nashville_tn'`. The `parcels.raw_owner_name` and `parcels.raw_owner_addr` fields capture the un-cleaned source values so the dedup audit trail is preserved.

## Owner deduplication

This is the hardest part. Source records have the same owner appearing multiple times with trivial name variations (comma placement, period in initials, "LLC" vs ", LLC", etc.). The dedup logic consolidates these into a single canonical owner row.

### Variation patterns

Real cases from the Nashville data:

- **Comma placement**: `SMITH JOHN`, `SMITH, JOHN`, `SMITH JOHN,`
- **Period in initials**: `SMITH JOHN J`, `SMITH JOHN J.`
- **Whitespace**: `SMITH  JOHN`, `SMITH JOHN ` (trailing), tabs, non-breaking spaces
- **Middle name vs initial**: `SMITH JOHN JAMES`, `SMITH JOHN J`
- **Joint owner formatting**: `SMITH JOHN & MARY`, `SMITH, JOHN & MARY`, `SMITH JOHN AND MARY`
- **Trust variations**: `SMITH JOHN TRUST`, `JOHN SMITH REVOCABLE TRUST`, `SMITH, JOHN, TRUSTEE`
- **LLC formatting**: `MIDTOWN REALTY LLC`, `MIDTOWN REALTY, LLC`, `MIDTOWN REALTY L.L.C.`, `MIDTOWN REALTY,LLC`
- **LP formatting**: `COOKEVILLE COMMONS LP`, `COOKEVILLE COMMONS, LP`, `COOKEVILLE COMMONS, L.P.`, `COOKEVILLE COMMONS L. P.` — all the same owner. The dotted form at end of string is a common gotcha; see the regex note in `normalize_name`.
- **Suffix placement**: `SMITH JOHN III`, `SMITH JOHN, III`, `SMITH III, JOHN`

Address variations:

- **Directionals**: `123 N MAIN ST`, `123 NORTH MAIN ST`, `123 N. MAIN ST`
- **Street types**: `ST` vs `STREET`, `AVE` vs `AVENUE`, `RD` vs `ROAD`
- **Unit info**: `123 MAIN ST APT 4B`, `123 MAIN ST #4B`, `123 MAIN ST 4B`
- **PO boxes**: `PO BOX 100`, `P.O. BOX 100`, `POBOX 100`

### Normalization

Build these into `lib/normalize.py`. They run on every parcel during ingestion and produce the `_norm` columns that drive dedup matching.

```python
"""Owner name and address normalization for dedup matching."""
import re
import unicodedata
from typing import Optional

ENTITY_SUFFIXES = [
    "LLC", "L.L.C.", "L L C", "LLLP", "LLP", "PLLC", "PC",
    "INC", "INCORPORATED", "INC.", "CORP", "CORPORATION", "CORP.",
    "LP", "L.P.", "LTD", "LTD.", "LIMITED",
    "CO", "COMPANY", "CO.",
]
TRUST_QUALIFIERS = [
    "REVOCABLE", "IRREVOCABLE", "LIVING", "FAMILY", "TESTAMENTARY",
    "GST EXEMPT", "GENERATION SKIPPING", "MARITAL", "BYPASS",
    "CHARITABLE", "GRANTOR", "QTIP", "TRUSTEE", "TRUST",
]
PERSON_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "ESQ", "MD", "PHD"}
DIRECTIONALS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
STREET_TYPES = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "TERRACE": "TER", "CIRCLE": "CIR",
    "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TRAIL": "TRL",
    "WAY": "WAY", "SQUARE": "SQ", "PLAZA": "PLZ",
    "EXPRESSWAY": "EXPY", "FREEWAY": "FWY",
}
UNIT_TYPES = {"APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT", "#": "UNIT"}


def normalize_name(name: str) -> str:
    """
    Produce a normalized owner-name key for dedup matching.
    
    Example outputs (all members of a group must produce the same key):
      'COOKEVILLE COMMONS, LP'      -> 'COMMONS COOKEVILLE'
      'COOKEVILLE COMMONS, L.P.'    -> 'COMMONS COOKEVILLE'
      'COOKEVILLE COMMONS LP'       -> 'COMMONS COOKEVILLE'
      'COOKEVILLE COMMONS L. P.'    -> 'COMMONS COOKEVILLE'
      
      'MIDTOWN REALTY LLC'          -> 'MIDTOWN REALTY'
      'MIDTOWN REALTY, LLC'         -> 'MIDTOWN REALTY'
      'MIDTOWN REALTY L.L.C.'       -> 'MIDTOWN REALTY'
      'MIDTOWN REALTY L. L. C.'     -> 'MIDTOWN REALTY'
      
      'SMITH JOHN J'                -> 'J JOHN SMITH'
      'SMITH JOHN J.'               -> 'J JOHN SMITH'
      'JOHN J. SMITH'               -> 'J JOHN SMITH'
      
      'BARRETT, PAMELA KAYE REVOCABLE TRUST' -> 'BARRETT KAYE PAMELA'
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.upper().strip()
    
    # Compact dotted single-letter acronyms BEFORE punct removal:
    #   'L.L.C.' / 'L. L. C.' -> 'LLC'
    #   'L.P.'   / 'L. P.'    -> 'LP'
    # IMPORTANT: use (?!\w) instead of \b at the trailing edge. \b does NOT
    # match between two non-word chars (e.g. between '.' and end-of-string),
    # so the dotted form at end of string would otherwise slip through.
    s = re.sub(r"\b([A-Z])\.\s*([A-Z])\.\s*([A-Z])\.(?!\w)", r"\1\2\3", s)
    s = re.sub(r"\b([A-Z])\.\s*([A-Z])\.(?!\w)", r"\1\2", s)
    
    s = re.sub(r"[.,'\";:!?]", " ", s)
    s = re.sub(r"\s+", " ", s)
    for term in ENTITY_SUFFIXES + TRUST_QUALIFIERS:
        s = re.sub(rf"\b{re.escape(term)}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = [t for t in s.split() if t not in PERSON_SUFFIXES and t != "&" and t != "AND"]
    return " ".join(sorted(toks))


def normalize_address(addr: str, zip_code: Optional[str] = None) -> str:
    """
    Produce a normalized address key for dedup matching.
    
    Example outputs:
      '123 N MAIN ST APT 4B', zip='37027'   -> '123 N MAIN ST APT 4B 37027'
      '123 NORTH MAIN STREET #4B', '37027'  -> '123 N MAIN ST UNIT 4B 37027'
      'P.O. BOX 100', '37027'               -> 'PO BOX 100 37027'
    """
    if not addr:
        return ""
    s = unicodedata.normalize("NFKD", addr).encode("ascii", "ignore").decode()
    s = s.upper().strip()
    s = re.sub(r"\bP\.?\s?O\.?\s?BOX\b", "PO BOX", s)
    s = re.sub(r"\bPOBOX\b", "PO BOX", s)
    s = re.sub(r"#", " UNIT ", s)
    s = re.sub(r"[.,'\";:!?]", " ", s)
    s = re.sub(r"\s+", " ", s)
    toks = s.split()
    out = []
    for t in toks:
        out.append(DIRECTIONALS.get(t, STREET_TYPES.get(t, UNIT_TYPES.get(t, t))))
    s = " ".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    if zip_code:
        z = re.sub(r"[^0-9]", "", str(zip_code))[:5]
        if z:
            s = f"{s} {z}"
    return s
```

### Matching algorithm

Run after ingestion finishes for a region, before owners are surfaced in the UI.

**Two non-negotiable rules.** Read these before reading the tier definitions:

1. **Both signals required.** Auto-merging two raw owner records requires BOTH the normalized owner-name AND the normalized mailing address to match. Name alone is never enough. Mailing address alone is never enough. The dedup logic does not have a tier that violates this.

2. **Mailing address only.** The address used for matching is the **owner's mailing address** (where tax bills get sent), pulled from the assessor record. The **property/parcel address** (where the building physically sits) is never used for dedup matching. An owner with 50 parcels has 50 property addresses but typically one mailing address — that one mailing address is the identity anchor.

Tiered matching:

```
Tier 1 (HIGH confidence, auto-merge):
  normalize_name(raw_owner_name_A)     == normalize_name(raw_owner_name_B)
  AND
  normalize_address(mailing_addr_A, mailing_zip_A) == normalize_address(mailing_addr_B, mailing_zip_B)
  
  Both must be EXACT matches on the normalized strings. After normalization,
  trivial variants ('SMITH, JOHN J.' vs 'SMITH JOHN J', 'COOKEVILLE COMMONS LP'
  vs 'COOKEVILLE COMMONS, L.P.', '123 N MAIN ST' vs '123 NORTH MAIN STREET') 
  collapse to identical keys, so this tier catches the deterministic dupes 
  without risk.
  → Same owner. Auto-merge.

Tier 2 (MEDIUM confidence, queue for human review — do NOT auto-merge):
  normalize_address(mailing_addr_A, mailing_zip_A) == normalize_address(mailing_addr_B, mailing_zip_B)
  AND
  token_jaccard_similarity(normalize_name_A, normalize_name_B) > 0.85
  AND not already merged by Tier 1
  
  Same mailing address with very similar (but not identical) names. Examples
  this catches: 'SMITH JOHN' vs 'SMITH JOHN J.' (added middle initial),
  'COOKEVILLE COMMONS LP' vs 'COOKEVILLE COMMONS LLC' (entity-type change).
  Could be the same owner with a record update, or could be two different
  people at the same address (spouses, business partners). Human reviews.
  → Surface in Dedup Review tab. User clicks Confirm or Keep Separate.

NEVER merge (these stay separate, even with the review queue):
  - Same name, different mailing addresses
    → Almost always common-name false positives (many 'JOHN SMITH's).
      Cross-mailing-address consolidation is unsafe.
  - Same mailing address, names with token-Jaccard < 0.85
    → Different people at one address. Roommates, spouses with different
      surnames, business partners at the same office. Two separate owners.
  - Anything where the mailing address differs in any way after normalization
    → The dedup logic does not fuzzy-match mailing addresses. Either the
      normalized strings are identical, or they're not. If your normalizer
      produces near-but-not-identical outputs for the same real address,
      improve the normalizer — don't loosen the dedup threshold.
```

Persistence: every merge writes an `owner_aliases` row with method and score. Tier 1 writes `match_method='exact_name_and_address'`. Tier 2, after user confirms, writes `match_method='fuzzy_name_same_address'` with `confirmed_by='user'`. Keep-Separate decisions also persist (so the same pair isn't shown again).

### UI for reviewing dedup decisions

Add a "Dedup Review" tab visible only when the queue is non-empty:

```
┌─────────────────────────────────────────────────────────────┐
│  Dedup Review · 47 pending                                  │
├─────────────────────────────────────────────────────────────┤
│  Match 1 of 47                              [ Keep separate ]│
│  ─────────────────────────────────────────  [ Confirm merge ]│
│  Owner A                    │  Owner B                       │
│  SMITH, JOHN J.             │  SMITH JOHN J                  │
│  123 Main St                │  123 MAIN STREET               │
│  Nashville, TN 37027        │  NASHVILLE TN 37027            │
│  ICP-2 · 23 parcels         │  ICP-1 · 8 parcels             │
│  $4.2M total                │  $1.1M total                   │
│                                                              │
│  Match score: 0.94 · method: normalized_name+address_fuzzy  │
└─────────────────────────────────────────────────────────────┘
```

Both Confirm and Keep separate decisions persist (so the same pair isn't shown again).

## CSV / XLSX contact import

The user will skip-trace owners externally (BatchData, Tracerfy, REISkip, Datazapp) and bring back CSV/XLSX files with phones, emails, source URLs, confidence scores. They need to attach these to existing owner records, additively, without overwriting.

### Upload flow

1. **Upload page** under "Import Contacts" sidebar option. Drag-and-drop area, accepts CSV or XLSX.

2. **Column mapper** — once uploaded, show the user the file's column names and let them map them to schema fields: `owner_name`, `mailing_address`, `phone1`, `phone1_type`, `phone1_confidence`, `email1`, `email1_confidence`, `source_url`, `source_label`. Save mapping per source so they don't redo it.

3. **Match preview** before commit:
   - X rows matched cleanly to existing owners (high confidence)
   - Y rows matched fuzzily (medium confidence — show side-by-side)
   - Z rows unmatched (no owner with that name/address — option to add as new owner or discard)

4. **Region selector** — which region this file is for, OR `auto` to detect from mailing state. Gates matching to right region so "John Smith" in Lexington doesn't merge with "John Smith" in Baltimore.

5. **Merge rules** per phone/email field:
   - If owner has no existing value → write it directly
   - If owner has existing value AND new value identical → no-op
   - If owner has existing value AND new value different → write to next-available slot (phone1 → phone2 → phone3). If all 3 taken, surface conflict for review.
   - Always update `_updated` and `_source` fields.

6. **Audit row** — write to `contact_imports` summarizing what happened.

### Vendor column mappings

Pre-built mappings to streamline imports:

```python
SKIPTRACE_VENDORS = {
    "batchdata": {
        "owner_name":   ["full_name", "owner_name"],
        "mailing_addr": ["mailing_address", "mail_address_line_1"],
        "mailing_city": ["mailing_city", "mail_city"],
        "mailing_state":["mailing_state"],
        "mailing_zip":  ["mailing_zip", "mail_zip"],
        "phone1":       ["phone_1", "phone1"],
        "phone1_type":  ["phone_1_type"],
        "phone1_dnc":   ["phone_1_dnc"],
        "email1":       ["email_1", "email1"],
    },
    "tracerfy": {
        "owner_name":   ["owner_name"],
        "phone1":       ["phone_1"], "phone2": ["phone_2"],
        "email1":       ["email_1"],
    },
    # add reiskip, datazapp as needed
}
```

On upload, scan column headers and auto-pick the closest match. User can override.

## UI changes

Match the existing app's design language — dark theme, sidebar filters, three-tab layout, owner drawer, outreach tracker. Refer to `index.html`.

### Map tab

Add a **region selector** at the top of the map area (not the sidebar). Three-button toggle:

```
┌──────────────────────────────────────────────────────────┐
│  [ Nashville ] [ Baltimore ] [ Lexington ]   ⌖ Center map │
├──────────────────────────────────────────────────────────┤
│                                                          │
│            (map renders for the selected region)         │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Sidebar filters apply within the selected region. Switching regions: map recenters, parcels reload, heatmap rebuilds, filter state (ICP, ownership, etc.) carries over.

### Owners search tab — unified across regions

Queries across ALL loaded regions. Filters expand to include:

- **Region**: All / Nashville / Baltimore / Lexington (multi-select)
- ICP bucket, property type, ownership: as before
- **Has phone**: any / yes / no
- **Has email**: any / yes / no
- **Has been contacted**: any / yes / no
- **Contact-info source**: any / batchdata / tracerfy / reiskip / manual
- **Search**: owner name + mailing address

Add column to the owners table: **Region** badge so the user sees at a glance.

### Drawer (owner detail)

Add a new section: **Contact Info** with phone and email rows showing source, confidence, DNC status, last updated. List all contacts if multiple. Add "Mark as bad" button on each that writes `confidence='low'` override.

Add to drawer header: small region badge (NSH / BAL / LEX).

### Outreach Tracker tab

Existing tab. Add **Region** column and **Has Contact Info** column.

### New: Dedup Review tab

Hidden by default. Visible only when queue has entries. Layout as shown earlier.

### New: Import Contacts tab

Drag-drop area + column mapper + preview + commit. Show running history of previous imports at bottom.

## Backend abstraction

The user plans to move to Supabase or Firebase later. Build a thin data-access layer now so that's a one-day swap rather than a rewrite.

### Interface

```python
# lib/store.py

class Store:
    """Abstract interface — both LocalStore and RemoteStore implement this."""
    
    def get_regions(self) -> list[dict]: ...
    def get_owners(self, region_id: str | None, filters: dict, limit: int) -> list[dict]: ...
    def get_owner(self, owner_id: int) -> dict: ...
    def get_owner_parcels(self, owner_id: int) -> list[dict]: ...
    def get_owner_aliases(self, owner_id: int) -> list[dict]: ...
    def update_owner_status(self, owner_id: int, status: str, notes: str) -> None: ...
    def upsert_contact(self, owner_id: int, contact: dict) -> None: ...
    def get_dedup_queue(self, region_id: str) -> list[dict]: ...
    def resolve_dedup(self, alias_id: int, action: str) -> None: ...
    def log_import(self, summary: dict) -> int: ...
```

### LocalStore implementation

Backed by SQLite (server build) or by the in-browser JSON blob (static-file mode, matching the existing app). The current app is browser-only with embedded JSON — keep that as the default. The Store interface wraps it with the same method signatures so the UI layer doesn't change when the backend swaps.

### RemoteStore (later)

```python
# Stub for later. Same methods, different implementation.
class SupabaseStore(Store):
    def __init__(self, url: str, key: str): ...
    def get_owners(self, region_id, filters, limit):
        # POSTGREST query against owners table
        ...
```

Same shape for Firebase — Firestore document queries.

**Every UI handler goes through the Store interface.** Migration day: swap LocalStore for SupabaseStore, pre-populate cloud tables. UI doesn't change.

## Project layout

```
propagentic-explorer/
├── CLAUDE.md                       ← this file
├── index.html                      ← existing Nashville app (provided as reference)
├── build_heatmap.py                ← existing Nashville build script (provided)
├── parcelcsv.csv                   ← Fayette County tabular data (user-provided)
├── parcelgeojson.geojson           ← Fayette County geometry (user-provided)
│
├── data/                           ← raw downloads from each region (gitignored)
│   ├── nashville_tn/
│   │   └── parcels_oct2025.csv
│   └── baltimore_md/
│       ├── geometry_q1_2026.geojson
│       └── cama_q1_2026.csv
│
├── ingestion/                      ← one per region
│   ├── nashville.py
│   ├── baltimore.py
│   └── fayette.py
│
├── lib/
│   ├── normalize.py                ← name + address normalization
│   ├── dedup.py                    ← matching algorithm + merging
│   ├── classify.py                 ← entity-type detection
│   ├── icp.py                      ← ICP bucket scoring
│   ├── store.py                    ← Store interface + LocalStore impl
│   ├── import_contacts.py          ← CSV/XLSX parsing + matching for uploads
│   └── vendor_mappings.py          ← SKIPTRACE_VENDORS dict
│
├── build.py                        ← orchestrator: ingest all regions, dedup, emit data blob
├── app.html                        ← new unified single-page app (replaces index.html)
├── app/
│   ├── styles.css
│   ├── main.js
│   ├── components/
│   │   ├── map_tab.js
│   │   ├── owners_tab.js
│   │   ├── outreach_tab.js
│   │   ├── dedup_tab.js
│   │   ├── import_tab.js
│   │   └── owner_drawer.js
│   └── store_client.js             ← JS-side mirror of Store interface
│
└── enrichment.db                   ← SQLite, working store during build
```

## Build order

Don't try to do all of this at once. Build in this exact order:

1. **Migrate existing Nashville data** into the new unified schema. Confirm the existing app still works against the new data shape. Safest first step — if it breaks, you know the schema or migration was wrong, not the new features.

2. **Build the normalize.py + dedup.py modules.** Run dedup against existing Nashville data. Manually review the first 50 merges. Tune thresholds.

3. **Add the Dedup Review tab** to the UI. Wire it to the queue. Confirm user can confirm/reject merges and decisions persist.

4. **Build the Baltimore County ingestion** (`ingestion/baltimore.py`). Download geometry + CAMA file. Normalize, dedup, write to database. Spot-check 20 random owners against SDAT.

5. **Add region tabs to the Map view.** Wire the region selector. Verify switching regions works without page reload.

6. **Build the Fayette County ingestion.** The data files are already at the project root: `parcelcsv.csv` and `parcelgeojson.geojson`. First inspect their schemas (head the CSV, examine the GeoJSON properties block) and document which columns map to which unified-schema fields. Then write `ingestion/fayette.py` to load them, join on parcel ID, normalize, dedup, and write to the database. If any required fields are missing from the source files, list them and ask the user how to proceed — don't fall back to scraping qPublic or any other external source without explicit user approval.

7. **Unified Owners search.** Update existing Owners tab to query across regions. Add the new filters.

8. **CSV/XLSX import** for contact info. Build upload + column-mapper + match-preview + commit flow. Test with a real BatchData or Tracerfy export.

9. **Refactor data layer to use Store interface.** Keep `LocalStore` as the only implementation for v1. Every UI handler goes through it.

10. **Stub the SupabaseStore.** No need to make it work — just have the interface defined and a TODO with the user's planned project URL.

## Rules

Hard rules that must be followed throughout the build:

1. **Don't redesign the visual language.** Match the existing app — dark theme, sidebar filters, three-tab layout. Don't introduce new color palettes, fonts, or component patterns.

2. **Preserve outreach data.** Existing `status` and `notes` on Nashville owners (some marked `researching`) must survive schema migration. Test explicitly.

3. **Idempotency everywhere.** Re-running ingestion for a region produces the same database state, not duplicates. Use upserts keyed on `(region_id, parcel_id)`.

4. **Dedup is brittle and false merges are unrecoverable.** Two non-negotiable conditions for auto-merging two owner records: (a) their normalized mailing addresses are identical, AND (b) their normalized names are identical. Both must be true. The "mailing address" is the address the assessor sends tax bills to — never the property/parcel address. A single signal — same name, OR same address — is never enough to auto-merge. When name matches but address doesn't, keep them separate (common-name false positives). When address matches but name is only fuzzy-similar, route to the Dedup Review queue for human confirmation, never auto-merge. Wrong merges destroy data that's expensive to reconstruct; missed merges show up later in the review queue and cost nothing.

5. **Be polite to public data sources.** 1 req/sec to qPublic. Real user agent. Cache aggressively (parcel data doesn't change daily). Respect robots.txt.

6. **No credentials in code.** API keys go in `.env` (gitignored).

7. **Audit every contact import.** Every row matched, unmatched, overwritten — log it.

8. **Don't lose source data.** Keep `raw_owner_name` and `raw_owner_addr` on every parcel even after dedup.

## Things not to do

- Don't implement surrounding KY counties (Jessamine, Woodford, Scott, Bourbon, Clark, Madison) in v1. Fayette only.
- Don't migrate to Supabase/Firebase yet. Stub the interface only.
- Don't scrape qPublic, fayettepva.com, or any other external Fayette source without explicit user approval. The user has already provided the data files; treat those as the source of truth for v1.
- Don't auto-merge owners with the same normalized name but no address match.
- Don't drop existing outreach data during migration.

## How the user will use this file

The user creates a new project directory in VS Code, drops `index.html`, `build_heatmap.py`, and this `CLAUDE.md` in there, plus the raw data files for each region. Then runs `claude` in that directory.

Their first prompt to you should be:

> Read CLAUDE.md and the two reference files (index.html, build_heatmap.py). Then do step 1 only: migrate the existing Nashville data into the new unified schema. Don't touch the UI yet. Show me the resulting database before continuing.

Each subsequent step gets its own focused prompt with manual verification in between. The dedup logic in particular has high blast radius if it goes wrong — checkpoints are intentional.
