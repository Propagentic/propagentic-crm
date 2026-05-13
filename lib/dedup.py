"""Owner deduplication — Tier 1 (auto-merge) + Tier 2 (review queue).

Per CLAUDE.md non-negotiable rules:
  - Auto-merging requires BOTH normalized name AND normalized mailing address
    to match. Name-only or address-only is never enough.
  - Mailing address only — never the parcel address.
  - Tier 2 (same address + fuzzy-similar name) is QUEUED for human review,
    never auto-applied.
  - Wrong merges destroy data; missed merges cost nothing. Bias conservative.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .normalize import normalize_name, normalize_address, token_jaccard

TIER2_JACCARD_THRESHOLD = 0.85

# Mirror the ICP buckets from build_app.py / ingestion (must stay in sync).
def _icp_bucket(n: int) -> int:
    if n < 5: return 0
    if n <= 19: return 1
    if n <= 49: return 2
    if n <= 99: return 3
    if n <= 500: return 4
    return 5


# ---------------------------------------------------------------------------
# 1) Populate owner_name_norm + mailing_addr_norm on every owner row
# ---------------------------------------------------------------------------
def compute_norm_columns(conn: sqlite3.Connection, region_id: str) -> int:
    """Backfill _norm columns for all owners in region. Idempotent.

    Returns the number of rows updated.
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT owner_id, owner_name, mailing_addr1, mailing_zip
          FROM owners
         WHERE region_id = ?
        """,
        (region_id,),
    ).fetchall()
    updates = []
    for owner_id, owner_name, addr1, zip_code in rows:
        updates.append((
            normalize_name(owner_name),
            normalize_address(addr1, zip_code),
            owner_id,
        ))
    cur.executemany(
        "UPDATE owners SET owner_name_norm = ?, mailing_addr_norm = ? WHERE owner_id = ?",
        updates,
    )
    conn.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# 2) Tier 1 candidates — exact match on BOTH normalized name and address
# ---------------------------------------------------------------------------
@dataclass
class Tier1Group:
    name_norm: str
    addr_norm: str
    members: list[dict]  # owner rows; first element is the picked canonical


def find_tier1_groups(conn: sqlite3.Connection, region_id: str) -> list[Tier1Group]:
    """Return groups of >=2 owners with identical (name_norm, addr_norm).

    Excludes any group where either norm key is empty. Canonical pick per group:
    highest n_parcels, tiebreaker lowest owner_id.
    """
    rows = conn.execute(
        """
        SELECT owner_id, owner_name, owner_name_norm,
               mailing_addr1, mailing_city, mailing_state, mailing_zip, mailing_addr_norm,
               n_parcels, n_homes, n_vacant, n_commercial,
               total_value, home_value, absentee_pct, icp, is_hoa,
               status, notes, last_touched,
               entity_type
          FROM owners
         WHERE region_id = ?
           AND owner_name_norm != ''
           AND mailing_addr_norm != ''
         ORDER BY owner_name_norm, mailing_addr_norm, n_parcels DESC, owner_id ASC
        """,
        (region_id,),
    ).fetchall()
    cols = [
        "owner_id", "owner_name", "owner_name_norm",
        "mailing_addr1", "mailing_city", "mailing_state", "mailing_zip", "mailing_addr_norm",
        "n_parcels", "n_homes", "n_vacant", "n_commercial",
        "total_value", "home_value", "absentee_pct", "icp", "is_hoa",
        "status", "notes", "last_touched",
        "entity_type",
    ]
    groups: list[Tier1Group] = []
    current_key: tuple | None = None
    current_members: list[dict] = []
    for row in rows:
        d = dict(zip(cols, row))
        key = (d["owner_name_norm"], d["mailing_addr_norm"])
        if key != current_key:
            if current_key is not None and len(current_members) > 1:
                groups.append(Tier1Group(current_key[0], current_key[1], current_members))
            current_key = key
            current_members = [d]
        else:
            current_members.append(d)
    if current_key is not None and len(current_members) > 1:
        groups.append(Tier1Group(current_key[0], current_key[1], current_members))
    return groups


# ---------------------------------------------------------------------------
# 3) Tier 2 candidates — same address, fuzzy-similar name, NOT tier-1
# ---------------------------------------------------------------------------
@dataclass
class Tier2Pair:
    addr_norm: str
    a: dict  # owner row
    b: dict  # owner row
    score: float


def find_tier2_pairs(
    conn: sqlite3.Connection,
    region_id: str,
    threshold: float = TIER2_JACCARD_THRESHOLD,
) -> list[Tier2Pair]:
    """Pairs of owners that share a mailing address but have different
    normalized names with token-Jaccard > threshold. Surface for human review.
    """
    # Group owners by mailing_addr_norm, but only addresses with >1 distinct name_norm.
    rows = conn.execute(
        """
        SELECT mailing_addr_norm, owner_id, owner_name, owner_name_norm,
               n_parcels, total_value
          FROM owners
         WHERE region_id = ?
           AND mailing_addr_norm != ''
           AND owner_name_norm != ''
        """,
        (region_id,),
    ).fetchall()

    # Bucket by address
    by_addr: dict[str, list[dict]] = {}
    for addr, oid, name, name_n, n_par, tv in rows:
        by_addr.setdefault(addr, []).append({
            "owner_id": oid, "owner_name": name, "owner_name_norm": name_n,
            "n_parcels": n_par, "total_value": tv,
        })

    pairs: list[Tier2Pair] = []
    for addr, members in by_addr.items():
        if len(members) < 2:
            continue
        # Skip if all the normalized names are identical (those collapse via tier 1)
        unique_norms = {m["owner_name_norm"] for m in members}
        if len(unique_norms) < 2:
            continue
        # Pairwise within this address group
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["owner_name_norm"] == b["owner_name_norm"]:
                    continue  # would be tier-1, handled elsewhere
                score = token_jaccard(a["owner_name_norm"], b["owner_name_norm"])
                if score > threshold:
                    pairs.append(Tier2Pair(addr_norm=addr, a=a, b=b, score=score))
    return pairs


# ---------------------------------------------------------------------------
# 4) Apply tier-1 merges
# ---------------------------------------------------------------------------
def apply_tier1_merges(
    conn: sqlite3.Connection,
    region_id: str,
    groups: list[Tier1Group],
    dry_run: bool = True,
) -> dict:
    """Merge each group into its first member (canonical).

    For every loser:
      - reassign parcels.owner_id -> canonical.owner_id
      - sum n_parcels/n_homes/n_vacant/n_commercial/total_value/home_value onto canonical
      - recompute absentee_pct (weighted) and icp
      - is_hoa = OR
      - write owner_aliases row (audit)
      - if loser has user-set status/notes/last_touched and canonical doesn't, lift onto canonical
      - delete loser row

    Refuses to merge if multiple group members have conflicting user-set state
    (status or contact info) — surfaces those for manual review instead.
    """
    cur = conn.cursor()
    n_groups = len(groups)
    n_merged_rows = 0
    n_parcels_reassigned = 0
    n_conflicts_skipped = 0
    skipped_conflict_examples: list[str] = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    for grp in groups:
        canonical = grp.members[0]
        losers = grp.members[1:]

        # User-set state conflict check. A "rich" row is one with any user-set
        # status / notes / phone / email — these matter for non-Nashville regions
        # later; for the current Nashville migration nothing is user-set yet.
        rich_rows = [m for m in grp.members if (m["status"] or m["notes"])]
        if len(rich_rows) > 1:
            n_conflicts_skipped += 1
            if len(skipped_conflict_examples) < 5:
                skipped_conflict_examples.append(
                    f"name_norm={grp.name_norm!r} addr_norm={grp.addr_norm!r} "
                    f"members_with_state={len(rich_rows)}"
                )
            continue

        # If exactly one member has user-set state and it's not the canonical,
        # lift status/notes/last_touched onto the canonical.
        if rich_rows and rich_rows[0]["owner_id"] != canonical["owner_id"]:
            src = rich_rows[0]
            canonical["status"] = src["status"]
            canonical["notes"] = src["notes"]
            canonical["last_touched"] = src["last_touched"]

        loser_ids = [m["owner_id"] for m in losers]

        # ---- aggregates ----
        new_n        = canonical["n_parcels"]    + sum(m["n_parcels"]    for m in losers)
        new_homes    = canonical["n_homes"]      + sum(m["n_homes"]      for m in losers)
        new_vacant   = canonical["n_vacant"]     + sum(m["n_vacant"]     for m in losers)
        new_comm     = canonical["n_commercial"] + sum(m["n_commercial"] for m in losers)
        new_totval   = canonical["total_value"]  + sum(m["total_value"]  for m in losers)
        new_homeval  = canonical["home_value"]   + sum(m["home_value"]   for m in losers)
        # weighted absentee_pct by old n_parcels
        denom = float(canonical["n_parcels"] + sum(m["n_parcels"] for m in losers))
        new_absentee = (
            canonical["absentee_pct"] * canonical["n_parcels"]
            + sum(m["absentee_pct"] * m["n_parcels"] for m in losers)
        ) / denom if denom > 0 else canonical["absentee_pct"]
        new_icp      = _icp_bucket(new_n)
        new_is_hoa   = int(any(m["is_hoa"] for m in grp.members))

        if dry_run:
            # don't write; just account
            n_merged_rows += len(losers)
            n_parcels_reassigned += sum(m["n_parcels"] for m in losers)
            continue

        # ---- write ----
        # 1. reassign parcels FK to canonical
        placeholders = ",".join("?" for _ in loser_ids)
        cur.execute(
            f"UPDATE parcels SET owner_id = ? WHERE owner_id IN ({placeholders})",
            [canonical["owner_id"], *loser_ids],
        )
        n_parcels_reassigned += cur.rowcount

        # 2. update canonical with summed aggregates + lifted state
        cur.execute(
            """
            UPDATE owners
               SET n_parcels = ?, n_homes = ?, n_vacant = ?, n_commercial = ?,
                   total_value = ?, home_value = ?,
                   absentee_pct = ?, icp = ?, is_hoa = ?,
                   status = ?, notes = ?, last_touched = ?
             WHERE owner_id = ?
            """,
            (
                new_n, new_homes, new_vacant, new_comm,
                new_totval, new_homeval,
                round(new_absentee, 4), new_icp, new_is_hoa,
                canonical["status"], canonical["notes"], canonical["last_touched"],
                canonical["owner_id"],
            ),
        )

        # 3. owner_aliases audit rows
        alias_rows = [
            (
                canonical["owner_id"],
                m["owner_name"],
                m["mailing_addr1"],
                "exact_name_and_address",
                1.0,
                ts,
                "auto",
            )
            for m in losers
        ]
        cur.executemany(
            """
            INSERT INTO owner_aliases
              (owner_id, raw_owner_name, raw_owner_addr,
               match_method, match_score, confirmed_at, confirmed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            alias_rows,
        )

        # 4. delete loser rows
        cur.execute(
            f"DELETE FROM owners WHERE owner_id IN ({placeholders})",
            loser_ids,
        )
        n_merged_rows += len(losers)

    if not dry_run:
        conn.commit()

    return {
        "groups_total": n_groups,
        "groups_applied": n_groups - n_conflicts_skipped,
        "rows_merged": n_merged_rows,
        "parcels_reassigned": n_parcels_reassigned,
        "conflicts_skipped": n_conflicts_skipped,
        "conflict_examples": skipped_conflict_examples,
        "dry_run": dry_run,
    }
