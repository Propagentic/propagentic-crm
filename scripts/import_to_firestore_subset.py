"""Subset import: load a representative slice of the explorer dataset into Firestore.

Stays under the Firebase free-tier 20k-writes/day quota so the pipeline can
be validated end-to-end before committing to Blaze billing for the full
1.3M-doc import.

What gets written:
    - regions               (3 docs)
    - owner_aliases         (~3.3k docs)
    - contact_imports       (0 docs by design)
    - owners (sampled)      (1,000 docs, proportional across regions)
    - parcels for those     (all parcels belonging to the sampled owners)

Usage:
    .venv/bin/python3 scripts/import_to_firestore_subset.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
KEY_PATH = ROOT / "secrets" / "firebase-key.json"

OWNER_SAMPLE_SIZE = 1000
BATCH_SIZE = 500  # Firestore's hard limit per batched write
RANDOM_SEED = 42


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def commit_batch(db, batch, count):
    if count == 0:
        return 0
    batch.commit()
    return count


def write_collection(db, collection_name, docs, id_fn):
    """Write an iterable of dicts to a collection, batched. Returns count written."""
    batch = db.batch()
    n_in_batch = 0
    total = 0
    for doc in docs:
        doc_id = id_fn(doc)
        if doc_id is None:
            continue
        ref = db.collection(collection_name).document(str(doc_id))
        batch.set(ref, doc)
        n_in_batch += 1
        total += 1
        if n_in_batch >= BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            n_in_batch = 0
            print(f"  ...{collection_name}: {total} so far", file=sys.stderr)
    if n_in_batch > 0:
        batch.commit()
    return total


def main():
    random.seed(RANDOM_SEED)

    if not KEY_PATH.exists():
        print(f"ERROR: service account key not found at {KEY_PATH}", file=sys.stderr)
        sys.exit(1)

    cred = credentials.Certificate(str(KEY_PATH))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    # ---------- regions ----------
    print("regions...")
    n = write_collection(
        db,
        "regions",
        read_jsonl(DATA_DIR / "regions.jsonl"),
        id_fn=lambda d: d.get("region_id"),
    )
    print(f"  wrote {n} regions\n")

    # ---------- contact_imports (likely empty) ----------
    print("contact_imports...")
    n = write_collection(
        db,
        "contact_imports",
        read_jsonl(DATA_DIR / "contact_imports.jsonl"),
        id_fn=lambda d: d.get("import_id"),
    )
    print(f"  wrote {n} contact_imports\n")

    # ---------- owner_aliases (small, full load) ----------
    print("owner_aliases...")
    n = write_collection(
        db,
        "owner_aliases",
        read_jsonl(DATA_DIR / "owner_aliases.jsonl"),
        id_fn=lambda d: d.get("alias_id"),
    )
    print(f"  wrote {n} owner_aliases\n")

    # ---------- sample 1,000 owners proportionally across regions ----------
    print(f"sampling {OWNER_SAMPLE_SIZE} owners across regions...")
    all_owners = []
    for i in range(1, 6):
        owner_file = DATA_DIR / f"owners-00{i}.jsonl"
        if owner_file.exists():
            all_owners.extend(read_jsonl(owner_file))
    print(f"  total owners in source: {len(all_owners)}")

    by_region = defaultdict(list)
    for o in all_owners:
        by_region[o.get("region_id", "unknown")].append(o)

    sampled = []
    total = len(all_owners)
    for region, owners_in_region in by_region.items():
        share = max(1, round(OWNER_SAMPLE_SIZE * len(owners_in_region) / total))
        share = min(share, len(owners_in_region))
        sampled.extend(random.sample(owners_in_region, share))
        print(f"  {region}: {share} sampled (of {len(owners_in_region)})")

    print(f"\nwriting {len(sampled)} sampled owners...")
    n = write_collection(
        db,
        "owners",
        iter(sampled),
        id_fn=lambda d: d.get("owner_id"),
    )
    print(f"  wrote {n} owners\n")

    # ---------- parcels for those sampled owners only ----------
    sampled_owner_ids = {o["owner_id"] for o in sampled}
    print(f"loading parcels for {len(sampled_owner_ids)} sampled owners...")

    def parcel_iter():
        for i in range(1, 8):
            pfile = DATA_DIR / f"parcels-00{i}.jsonl"
            if not pfile.exists():
                continue
            for p in read_jsonl(pfile):
                if p.get("owner_id") in sampled_owner_ids:
                    yield p

    def parcel_id(p):
        # Compound ID to avoid cross-region collisions.
        region = p.get("region_id", "unknown")
        pid = p.get("parcel_id", "")
        return f"{region}__{pid}"

    n = write_collection(db, "parcels", parcel_iter(), id_fn=parcel_id)
    print(f"  wrote {n} parcels\n")

    print("DONE.")


if __name__ == "__main__":
    main()
