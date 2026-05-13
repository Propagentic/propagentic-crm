#!/usr/bin/env python3
"""Run the dedup pass on Nashville. Default is --dry-run (no writes to merges).

Usage:
  python scripts/dedup_nashville.py               # dry-run
  python scripts/dedup_nashville.py --apply       # actually merge

Behavior:
  - Always recomputes owner_name_norm and mailing_addr_norm in place (safe write).
  - Tier-1 (auto-merge) candidates: identified and reported. Applied only with --apply.
  - Tier-2 (review queue) candidates: identified and reported. Never auto-applied
    here — they need the Dedup Review UI (step 3) for human confirmation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import connect  # noqa: E402
from lib.dedup import (  # noqa: E402
    compute_norm_columns,
    find_tier1_groups,
    find_tier2_pairs,
    apply_tier1_merges,
)

REGION_ID = "nashville_tn"
DB_PATH = Path(__file__).resolve().parent.parent / "enrichment.db"


def fmt_money(v):
    if v is None: return "—"
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually apply tier-1 merges. Default is dry-run.")
    ap.add_argument("--limit-show", type=int, default=50,
                    help="Number of example merges to print")
    args = ap.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"ERROR: {DB_PATH} missing — run ingestion/nashville.py first")

    conn = connect(DB_PATH)

    n_owners_before = conn.execute(
        "SELECT COUNT(*) FROM owners WHERE region_id = ?", (REGION_ID,),
    ).fetchone()[0]
    print(f"Region: {REGION_ID}")
    print(f"Owners before pass: {n_owners_before:,}")

    print("\n[1/3] Normalizing owner_name and mailing_addr (in place) ...")
    n_norm = compute_norm_columns(conn, REGION_ID)
    print(f"  populated _norm columns on {n_norm:,} rows")

    print("\n[2/3] Finding Tier-1 groups (exact match on BOTH name_norm AND addr_norm) ...")
    groups = find_tier1_groups(conn, REGION_ID)
    n_t1_groups = len(groups)
    n_t1_rows_collapsed = sum(len(g.members) - 1 for g in groups)
    n_t1_parcels_affected = sum(
        sum(m["n_parcels"] for m in g.members[1:]) for g in groups
    )
    print(f"  {n_t1_groups:,} groups   →   {n_t1_rows_collapsed:,} owner rows would collapse")
    print(f"  parcels reassigned:    {n_t1_parcels_affected:,}")

    print(f"\n  --- first {min(args.limit_show, n_t1_groups)} tier-1 groups (sorted by size desc) ---")
    sorted_groups = sorted(groups, key=lambda g: -sum(m["n_parcels"] for m in g.members))
    for i, g in enumerate(sorted_groups[: args.limit_show]):
        total_parcels = sum(m["n_parcels"] for m in g.members)
        total_value = sum(m["total_value"] for m in g.members)
        print(f"  #{i+1}  name_norm={g.name_norm!r}  addr_norm={g.addr_norm!r}")
        print(f"        {len(g.members)} members  {total_parcels} parcels  {fmt_money(total_value)}")
        for j, m in enumerate(g.members):
            tag = "*canonical" if j == 0 else " loser    "
            print(f"        {tag}  id={m['owner_id']:>6}  n={m['n_parcels']:>4}  {m['owner_name']!r}  | "
                  f"{(m['mailing_addr1'] or '')!r}")

    print("\n[3/3] Finding Tier-2 pairs (same address, name jaccard > 0.85) ...")
    pairs = find_tier2_pairs(conn, REGION_ID)
    print(f"  {len(pairs):,} pairs flagged for review (NEVER auto-merged)")
    for i, p in enumerate(pairs[: min(20, len(pairs))]):
        print(f"  #{i+1}  score={p.score:.3f}  addr={p.addr_norm!r}")
        print(f"        A: id={p.a['owner_id']:>6}  n={p.a['n_parcels']:>4}  {p.a['owner_name']!r}")
        print(f"        B: id={p.b['owner_id']:>6}  n={p.b['n_parcels']:>4}  {p.b['owner_name']!r}")

    # --- apply or report ---
    print()
    if args.apply:
        print(">>> APPLYING tier-1 merges (this commits and is irreversible) ...")
        summary = apply_tier1_merges(conn, REGION_ID, groups, dry_run=False)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM owners WHERE region_id = ?", (REGION_ID,),
        ).fetchone()[0]
        n_aliases = conn.execute(
            """SELECT COUNT(*) FROM owner_aliases
                WHERE owner_id IN (SELECT owner_id FROM owners WHERE region_id = ?)""",
            (REGION_ID,),
        ).fetchone()[0]
        print(f"  groups applied:        {summary['groups_applied']:,}")
        print(f"  owner rows removed:    {summary['rows_merged']:,}")
        print(f"  parcels reassigned:    {summary['parcels_reassigned']:,}")
        print(f"  conflicts skipped:     {summary['conflicts_skipped']:,}")
        if summary["conflict_examples"]:
            for ex in summary["conflict_examples"]:
                print(f"    skipped: {ex}")
        print(f"  owners after pass:     {n_after:,}  (was {n_owners_before:,})")
        print(f"  owner_aliases rows:    {n_aliases:,}")
    else:
        summary = apply_tier1_merges(conn, REGION_ID, groups, dry_run=True)
        print(">>> DRY-RUN — no writes performed (use --apply to commit).")
        print(f"  Would merge {summary['rows_merged']:,} owner rows across "
              f"{summary['groups_total']:,} groups,")
        print(f"  reassigning {summary['parcels_reassigned']:,} parcels.")

    conn.close()


if __name__ == "__main__":
    main()
