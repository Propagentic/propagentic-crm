#!/usr/bin/env python3
"""Run dedup on any region.

Usage:
  python scripts/dedup_region.py REGION_ID [--apply] [--limit-show N]

Examples:
  python scripts/dedup_region.py nashville_tn --apply
  python scripts/dedup_region.py baltimore_md
  python scripts/dedup_region.py fayette_ky
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

DB_PATH = Path(__file__).resolve().parent.parent / "enrichment.db"


def fmt_money(v):
    if v is None: return "—"
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("region_id", help="e.g. nashville_tn, baltimore_md, fayette_ky")
    ap.add_argument("--apply", action="store_true",
                    help="Actually apply tier-1 merges. Default is dry-run.")
    ap.add_argument("--limit-show", type=int, default=10)
    args = ap.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"ERROR: {DB_PATH} missing")
    conn = connect(DB_PATH)

    n_before = conn.execute(
        "SELECT COUNT(*) FROM owners WHERE region_id = ?", (args.region_id,),
    ).fetchone()[0]
    if n_before == 0:
        sys.exit(f"ERROR: no owners with region_id={args.region_id!r}")
    print(f"Region: {args.region_id}")
    print(f"Owners before: {n_before:,}")

    print("\n[1/3] Normalizing ...")
    n_norm = compute_norm_columns(conn, args.region_id)
    print(f"  populated _norm on {n_norm:,} rows")

    print("\n[2/3] Tier-1 candidates (exact name + address) ...")
    groups = find_tier1_groups(conn, args.region_id)
    rows_collapsed = sum(len(g.members) - 1 for g in groups)
    print(f"  {len(groups):,} groups → {rows_collapsed:,} rows would collapse")

    sorted_groups = sorted(groups, key=lambda g: -sum(m["n_parcels"] for m in g.members))
    for i, g in enumerate(sorted_groups[: args.limit_show]):
        total = sum(m["n_parcels"] for m in g.members)
        print(f"  #{i+1}  {g.name_norm!r}  @ {g.addr_norm!r}  ({len(g.members)} members, {total} parcels)")

    print("\n[3/3] Tier-2 candidates (same address, jaccard > 0.85) ...")
    pairs = find_tier2_pairs(conn, args.region_id)
    print(f"  {len(pairs):,} pairs (need human review in Dedup Review tab)")

    if args.apply:
        print("\n>>> APPLYING tier-1 merges ...")
        summary = apply_tier1_merges(conn, args.region_id, groups, dry_run=False)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM owners WHERE region_id = ?", (args.region_id,),
        ).fetchone()[0]
        print(f"  rows merged:           {summary['rows_merged']:,}")
        print(f"  parcels reassigned:    {summary['parcels_reassigned']:,}")
        print(f"  conflicts skipped:     {summary['conflicts_skipped']:,}")
        print(f"  owners after:          {n_after:,}  (was {n_before:,})")
    else:
        summary = apply_tier1_merges(conn, args.region_id, groups, dry_run=True)
        print(f"\n>>> DRY-RUN (use --apply to commit)")
        print(f"  would merge {summary['rows_merged']:,} rows, reassign {summary['parcels_reassigned']:,} parcels")

    conn.close()


if __name__ == "__main__":
    main()
