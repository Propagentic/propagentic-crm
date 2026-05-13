# Parcel data export — JSONL

One-time-ish export of the full SQLite working store (`enrichment.db`) into
git-friendly JSONL chunks. Designed to be portable enough to load into Firestore,
Postgres/Supabase, or any other backend.

## Source

| Field | Value |
|---|---|
| Source database | `enrichment.db` (gitignored — local-only) |
| Live preview | https://transcendent-bienenstitch-06dd3d.netlify.app/ |
| Extracted | 2026-05-13 UTC (see `schema.json._meta.extracted_at_utc` for precise timestamp) |
| Extracted by | `scripts/export_to_jsonl.py` |
| Format | JSON Lines — one record per newline-terminated line |
| Chunk target | 50 MB per file |
| Encoding | UTF-8 |

## Files

| File / pattern | Rows | Notes |
|---|---:|---|
| `regions.jsonl` | 3 | One row per loaded county (Nashville TN, Baltimore MD, Fayette KY) |
| `owners-001..005.jsonl` | 590,240 | Aggregated unique owners, post-dedup |
| `parcels-001..007.jsonl` | 750,983 | One row per parcel; FK `owner_id` → owners |
| `owner_aliases.jsonl` | 3,291 | Audit trail of dedup auto-merges |
| `contact_imports.jsonl` | 0 | Empty by design; populated by skip-trace imports |
| `schema.json` | — | Full field inventory + inferred types per table |
| `.extraction_summary.json` | — | Per-table size + chunk metadata from the last run |

**Total: 1,344,517 rows · ~598 MB across 15 chunked files.**

## Schema

See `schema.json` for the authoritative field-by-field type map. Quick overview:

### regions
`region_id` (string PK), `display_name`, `state`, `county`, `center_lat`,
`center_lon`, `last_ingested`, `source_versions`.

### owners
Per-region aggregated owner records (post-dedup canonical row).
`owner_id` (integer PK), `region_id` (FK), `owner_name`, `owner_name_norm`,
mailing address fields, `n_parcels`, `n_homes`, `n_vacant`, `n_commercial`,
`total_value`, `home_value`, `absentee_pct`, `icp`, `is_hoa`,
`entity_type`, three phone slots + metadata, two email slots + metadata,
`status`, `notes`, `last_touched`.

### parcels
`parcel_id` + `region_id` (composite PK), `owner_id` (FK), parcel address +
city + zip, `land_use`, `land_use_code`, `zoning`, `acres`, `land_value`,
`imp_value`, `total_value`, `sale_date`, `sale_price`, `council_district`,
`lat`, `lon`, `apn`, plus `raw_owner_name` / `raw_owner_addr` audit fields.

### owner_aliases
Audit trail of collapsed name/address variants. `alias_id`, `owner_id`,
`raw_owner_name`, `raw_owner_addr`, `match_method`, `match_score`,
`confirmed_at`, `confirmed_by`.

### contact_imports
Log of skip-trace CSV uploads. `import_id`, `filename`, `imported_at`,
`rows_total`, `rows_matched`, `rows_unmatched`, `rows_updated`,
`rows_overwritten`, `source_label`, `notes`.

## Null handling

To keep the total under git's recommended 1 GB ceiling, null values are
**dropped per-record**. A missing key in a JSONL row means the field is null
in the source. The complete field inventory is preserved in `schema.json` so
no information is lost — a round-trip restore would map missing keys back
to NULL.

## Re-running the extraction

```bash
# from the repo root
python3 scripts/export_to_jsonl.py
```

This is idempotent: it wipes any prior `.jsonl` files in `data/` and
re-emits from scratch.  Adjust `CHUNK_TARGET_BYTES` in the script to change
the per-chunk size (default 50 MB).

## Loading into Firestore (sketch)

```js
import { initializeApp, cert } from "firebase-admin/app";
import { getFirestore } from "firebase-admin/firestore";
import { readFileSync } from "fs";
import readline from "readline";
import { createReadStream } from "fs";

initializeApp({ credential: cert("./service-account.json") });
const db = getFirestore();

async function loadJsonl(file, collection) {
  const rl = readline.createInterface({ input: createReadStream(file) });
  let batch = db.batch();
  let n = 0;
  for await (const line of rl) {
    if (!line.trim()) continue;
    const doc = JSON.parse(line);
    const id = String(doc.owner_id ?? doc.parcel_id ?? doc.region_id);
    batch.set(db.collection(collection).doc(id), doc);
    if (++n % 500 === 0) {
      await batch.commit();
      batch = db.batch();
    }
  }
  await batch.commit();
  console.log(`${collection}: ${n} docs`);
}

await loadJsonl("data/regions.jsonl",        "regions");
for (let i = 1; i <= 5; i++)  await loadJsonl(`data/owners-00${i}.jsonl`,  "owners");
for (let i = 1; i <= 7; i++)  await loadJsonl(`data/parcels-00${i}.jsonl`, "parcels");
await loadJsonl("data/owner_aliases.jsonl",   "owner_aliases");
```

For parcels (composite PK `(region_id, parcel_id)`), prefer a compound doc
ID like `${region_id}__${parcel_id}` instead of just `parcel_id` so cross-
region collisions are impossible.

## Loading into Postgres / Supabase

The Postgres schema lives in `../scripts/supabase_schema.sql`. To load the
JSONL chunks, use `psql` `\copy` with a small jsonb staging table, or write
a script using `psycopg2.execute_values` (see
`../scripts/migrate_to_supabase.py` for the SQLite-to-Postgres equivalent).
