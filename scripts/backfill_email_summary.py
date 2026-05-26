#!/usr/bin/env python3
"""
One-shot backfill: compute crm_email_outbound_count / crm_email_inbound_count /
crm_email_last_at / crm_email_last_outbound_at / crm_email_last_inbound_at /
crm_email_last_direction for every lead with at least one emailActivity doc.

Also runs the auto-DNC pass: any lead with >=4 outbound + 0 inbound + last
outbound >=14 days ago gets moved to Do Not Contact with reason='unspecified'.

Idempotent — re-running produces the same Firestore state.

Run from project root:
    python3 scripts/backfill_email_summary.py --dry-run
    python3 scripts/backfill_email_summary.py
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore

FIREBASE_KEY = "propagentic-crm-firebase-adminsdk-fbsvc-b1b48509ba.json"
AUTO_DNC_GRACE_DAYS = 14


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="No writes; summary only")
    args = ap.parse_args()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_KEY))
    db = firestore.client()

    # Load crm_users registry → lookup map for canonical email resolution
    # (handles aliases like weinba23@wfu.edu -> ben@propagenticai.com).
    crm_users_lookup = {}
    for d in db.collection("crm_users").stream():
        data = d.to_dict()
        primary = (data.get("email") or d.id).lower()
        crm_users_lookup[primary] = primary
        for alias in (data.get("aliases") or []):
            if alias:
                crm_users_lookup[alias.lower()] = primary
    print(f"Loaded {len(crm_users_lookup)} crm_users entries (primary + aliases)", file=sys.stderr)

    # Walk every emailActivity doc once, aggregating per matched_lead_id.
    print("Reading emailActivity collection…", file=sys.stderr)
    summaries: dict[str, dict] = defaultdict(lambda: {
        "inbound": 0, "outbound": 0,
        "last_at": None, "last_dir": None,
        "last_outbound_at": None, "last_inbound_at": None,
        "last_outbound_from": None,
    })
    n_docs = 0
    for d in db.collection("emailActivity").stream():
        n_docs += 1
        data = d.to_dict()
        direction = data.get("direction") or "outbound"
        sent_at = data.get("sent_at")
        from_email = (data.get("from_email") or "").lower()
        for lid in (data.get("matched_lead_ids") or []):
            s = summaries[lid]
            if direction == "inbound":
                s["inbound"] += 1
                if sent_at and (s["last_inbound_at"] is None or sent_at > s["last_inbound_at"]):
                    s["last_inbound_at"] = sent_at
            else:
                s["outbound"] += 1
                if sent_at and (s["last_outbound_at"] is None or sent_at > s["last_outbound_at"]):
                    s["last_outbound_at"] = sent_at
                    s["last_outbound_from"] = from_email
            if sent_at and (s["last_at"] is None or sent_at > s["last_at"]):
                s["last_at"] = sent_at
                s["last_dir"] = direction
    print(f"  read {n_docs} emailActivity docs; computed summaries for {len(summaries)} leads", file=sys.stderr)

    # Write summary fields back to each lead doc.
    if args.dry_run:
        print(f"[DRY RUN] would write summary fields to {len(summaries)} leads")
        # Show distribution of buckets
        buckets = defaultdict(int)
        owners = defaultdict(int)
        for s in summaries.values():
            inb, out, ldir = s["inbound"], s["outbound"], s["last_dir"]
            if inb > 0 and ldir == "inbound": buckets["needs_response"] += 1
            elif inb > 0 and ldir == "outbound": buckets["in_conversation"] += 1
            elif inb == 0 and out == 1: buckets["awaiting_first"] += 1
            elif inb == 0 and out >= 2: buckets["following_up"] += 1
            else: buckets["other"] += 1
            canonical = crm_users_lookup.get((s["last_outbound_from"] or "").lower())
            owners[canonical or "(no resolve)"] += 1
        print("Bucket distribution:", file=sys.stderr)
        for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}", file=sys.stderr)
        print("Auto-assignment preview (most recent outbound sender → owner):", file=sys.stderr)
        for k, v in sorted(owners.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}", file=sys.stderr)
    else:
        written = 0
        for lid, s in summaries.items():
            update = {
                "crm_email_outbound_count": s["outbound"],
                "crm_email_inbound_count": s["inbound"],
                "crm_email_last_at": s["last_at"],
                "crm_email_last_outbound_at": s["last_outbound_at"],
                "crm_email_last_inbound_at": s["last_inbound_at"],
                "crm_email_last_direction": s["last_dir"],
            }
            canonical = crm_users_lookup.get((s["last_outbound_from"] or "").lower())
            if canonical:
                update["crm_owner"] = canonical
            db.collection("leads").document(lid).set(update, merge=True)
            written += 1
            if written % 100 == 0:
                print(f"  wrote {written}/{len(summaries)}", file=sys.stderr)
        print(f"Wrote summaries to {written} leads", file=sys.stderr)

    # Auto-DNC pass
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_DNC_GRACE_DAYS)
    candidates = []
    for lid, s in summaries.items():
        if s["outbound"] < 4 or s["inbound"] > 0:
            continue
        if not s["last_outbound_at"] or s["last_outbound_at"] > cutoff:
            continue
        candidates.append(lid)
    print(f"\nAuto-DNC: {len(candidates)} candidates (>=4 outbound, 0 inbound, last >={AUTO_DNC_GRACE_DAYS}d ago)", file=sys.stderr)
    if args.dry_run:
        print("[DRY RUN] would mark these as DNC", file=sys.stderr)
    else:
        marked = 0
        for lid in candidates:
            ldoc = db.collection("leads").document(lid).get()
            data = ldoc.to_dict() if ldoc.exists else {}
            if data.get("crm_status") == "do_not_contact":
                continue
            db.collection("leads").document(lid).set({
                "crm_status": "do_not_contact",
                "crm_dnc_reason": "unspecified",
                "crm_dnc_auto": True,
                "crm_dnc_auto_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)
            marked += 1
        print(f"Marked {marked} leads as auto-DNC", file=sys.stderr)


if __name__ == "__main__":
    main()
