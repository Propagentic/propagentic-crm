# Propagentic Parcel Explorer

A multi-region property prospecting tool — interactive heatmap + searchable
owner database covering Nashville (Davidson County, TN), Baltimore County (MD),
and the Lexington area (Fayette County, KY).

Built for Propagentic, a real estate SaaS targeting absentee / multi-property
landlords.

## What it does

- **Map view** — density and value heatmaps for parcels in each region, with
  per-parcel markers (owner / address / appraised value / sale history).
- **Owners search** — cross-region table of every owner with sortable
  columns (parcel count, total value, ICP bucket, state, status, flags).
- **Outreach tracker** — per-owner status (New / Researching / Contacted /
  Demo / Trial / Paid / Not interested) and free-text notes.
- **Owner dedup** — collapses name and mailing-address variants. Tier-1
  matches (exact normalized name + address) auto-merge. Tier-2 (same address,
  fuzzy-similar names) queue for human review.
- **CSV / XLSX contact import** — upload skip-trace vendor exports (BatchData,
  Tracerfy, REISkip, Datazapp) to attach phones and emails to existing owners.

## Repo layout

```
.
├── CLAUDE.md                  # Spec / design doc (reads as context for Claude)
├── README.md                  # This file
├── .env.example               # Template for Supabase credentials
├── .gitignore
│
├── ingestion/                 # Per-region data loaders
│   ├── nashville.py           # → enrichment.db, region_id='nashville_tn'
│   ├── baltimore.py           # → region_id='baltimore_md'
│   └── fayette.py             # → region_id='fayette_ky'
│
├── lib/
│   ├── db.py                  # SQLite schema (mirrors Supabase Postgres)
│   ├── normalize.py           # Owner-name + address normalization
│   ├── dedup.py               # Tier-1/Tier-2 matching + merging
│   ├── store.py               # Store interface — LocalStore (SQLite) +
│   │                          #   SupabaseStore (stub) for future backend
│   └── vendor_mappings.py     # Skip-trace CSV column auto-detection
│
├── scripts/
│   ├── dedup_nashville.py     # CLI: run Nashville dedup pass
│   ├── dedup_region.py        # CLI: run dedup against any region
│   ├── supabase_schema.sql    # Postgres schema for Supabase paste-in
│   └── migrate_to_supabase.py # SQLite → Supabase bulk uploader
│
├── app_template.html          # Source template (no data inlined)
├── build.py                   # Pipeline: enrichment.db → app.html
├── serve.py                   # Local HTTP launcher (avoids file:// quirks)
│
└── (gitignored — generated locally:)
    ├── enrichment.db          # SQLite, built from raw source files
    ├── *.geojson, *.csv       # Raw assessor data per region
    ├── app.html               # 100+ MB single-page build with all data
    ├── app_*.html             # Single-region builds
    └── dist/                  # Lazy-load variant (shell + per-region JSON)
```

## Local setup

```bash
# 1. Drop the raw source data files in the repo root (gitignored):
#    nashville:  nashville.db                  (existing SQLite from legacy build)
#    baltimore:  baltimoreparcel.csv + baltimore.geojson
#    fayette:    parcelcsv.csv      + parcelgeojson.geojson

# 2. Install Python deps
pip3 install pandas ijson psycopg2-binary python-dotenv

# 3. Run ingestions to populate enrichment.db
python3 ingestion/nashville.py
python3 ingestion/baltimore.py
python3 ingestion/fayette.py

# 4. Run dedup (idempotent; --apply commits, default is dry-run)
python3 scripts/dedup_region.py nashville_tn --apply
python3 scripts/dedup_region.py baltimore_md --apply

# 5. Build the single-page app
python3 build.py                                  # all regions, all data (~140 MB)
python3 build.py --trim                           # multi-property owners only (~26 MB)
python3 build.py --regions nashville_tn           # single region (~60 MB)
python3 build.py --lazy --out dist/index.html     # lazy-load shell + per-region JSON

# 6. Open it
python3 serve.py                # serves on http://localhost:8000/app.html
```

## Hosting

The static build (single file or `dist/`) deploys to any static host. The
fast no-account path:

1. Go to https://app.netlify.com/drop
2. Drag the file/folder onto the page
3. Get a URL like `https://something-random.netlify.app`

For a permanent URL, claim the site with a free Netlify account.

## Backend (Supabase)

For multi-user / shared state (everyone seeing the same outreach tracker,
contact imports, dedup decisions), the Store interface in `lib/store.py`
swaps `LocalStore` → `SupabaseStore`. To get a Supabase project running:

1. Create a project at https://supabase.com
2. Paste `scripts/supabase_schema.sql` into the SQL Editor and run
3. Copy `.env.example` → `.env`, fill in `DATABASE_URL` from
   Project Settings → Database → Connection string → Transaction pooler
4. Run `python3 scripts/migrate_to_supabase.py`

Free tier limit is 500 MB; with ~590k owners + ~640k parcels you'll use
~487 MB of that. Pro tier ($25/mo) gives 8 GB and is the right tier once
you start importing skip-trace data at scale.

## Stack

- **Python 3** — ingestion, dedup, build, migrations
- **SQLite** — local working store
- **Postgres / Supabase** — hosted backend (when ready)
- **Leaflet + Leaflet.heat + Leaflet.markercluster** — map rendering
- **Vanilla JS, no frameworks** — keeps the single-file build practical
- **Netlify** — recommended static host

## License / data

Property records are public assessor data. Owner contact info (phones, emails)
is sourced separately via skip-trace services and is licensee-restricted per
their terms — don't redistribute imported contact data publicly.
