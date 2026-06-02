#!/usr/bin/env python3
"""
One-shot import of the 40 third-party property managers from the GMass —
AppFolio Outreach XLSX into the `leads` collection with
crm_entity_type="property_manager" and PM-specific fields.

Doc IDs are deterministic: `pm__<state_lower>__<sha1(company_norm)[:12]>` —
so re-running the script with the same XLSX is idempotent (updates in place).

Run from project root:
    python3 scripts/import_pms.py --dry-run
    python3 scripts/import_pms.py
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import firebase_admin
import pandas as pd
from firebase_admin import credentials, firestore

FIREBASE_KEY = "propagentic-crm-firebase-adminsdk-fbsvc-b1b48509ba.json"
XLSX_PATH = Path.home() / "Desktop" / "GMass — AppFolio Outreach (40 PMs) 2026-05-29.xlsx"
SOURCE_FILE_LABEL = "GMass — AppFolio Outreach (40 PMs) 2026-05-29"


def _normalize_for_id(s: str) -> str:
    """Lowercase + collapse non-alphanumeric to spaces + single-space + strip."""
    s = re.sub(r"[^a-zA-Z0-9]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def _doc_id_for_pm(company: str, state: str) -> str:
    company_norm = _normalize_for_id(company)
    state_norm = (state or "xx").strip().lower()
    h = hashlib.sha1(company_norm.encode("utf-8")).hexdigest()[:12]
    return f"pm__{state_norm}__{h}"


def _clean_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s else None


def _clean_phone(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    digits = re.sub(r"\D", "", str(v))
    return digits or None


def _parse_units(units_str: str):
    """Extract a numeric units count from strings like '300+ units' or '3,300+ units'."""
    if not units_str:
        return None
    s = str(units_str).replace(",", "")
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't write; print what would happen")
    ap.add_argument("--xlsx", default=str(XLSX_PATH), help="Path to the XLSX file")
    args = ap.parse_args()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_KEY))
    db = firestore.client()

    df = pd.read_excel(args.xlsx)
    print(f"Read {len(df)} rows from {args.xlsx}", file=sys.stderr)

    written = 0
    for i, row in df.iterrows():
        company = _clean_str(row.get("Company"))
        state = _clean_str(row.get("State"))
        if not company or not state:
            print(f"  row {i}: skipping — missing Company or State", file=sys.stderr)
            continue
        doc_id = _doc_id_for_pm(company, state)
        units_raw = _clean_str(row.get("Units"))
        units_int = _parse_units(units_raw)

        # market matches the existing landlord format (<county>_<state_lower>);
        # PMs don't have county, so we use the city slug instead.
        city = _clean_str(row.get("City"))
        market_state = state.upper()
        market_key = f"{(city or 'unknown').lower().replace(' ', '_')}_{state.lower()}"

        payload = {
            "crm_entity_type": "property_manager",
            "third_party_pm": True,
            # Display name on cards = Company (the PM brand).
            "owner_name": company,
            "first_name": _clean_str(row.get("FirstName")),
            "company_name": company,
            "decision_maker": _clean_str(row.get("DecisionMaker")),
            "title": _clean_str(row.get("Title")),
            # PM-specific enrichment
            "units_raw": units_raw,
            "units_count": units_int,
            "software": _clean_str(row.get("Software")),
            "website": _clean_str(row.get("Website")),
            "appfolio_portal": _clean_str(row.get("AppFolioPortal")),
            "hook": _clean_str(row.get("Hook")),
            # Contact
            "email": _clean_str(row.get("Email")),
            "phone": _clean_phone(row.get("Phone")),
            # Location — PMs are company offices; use the city/state for both
            # property_* and mailing_* so existing UI keeps rendering them.
            "property_city": city,
            "property_state": state.upper(),
            "mailing_city": city,
            "mailing_state": state.upper(),
            # Market routing fields (matches landlord convention)
            "market": market_key,
            "market_state": market_state,
            "market_county": None,
            # Provenance
            "source_file": SOURCE_FILE_LABEL,
            "source_row": int(i) + 2,  # +2 for header row + 1-indexed
            "imported_at": firestore.SERVER_TIMESTAMP,
            "imported_by": "scripts/import_pms.py",
        }
        # Strip None values so merge=True doesn't overwrite existing fields
        # with None when this is re-run.
        for k in list(payload.keys()):
            if payload[k] is None:
                del payload[k]

        if args.dry_run:
            print(f"  [DRY] {doc_id}  · {company} ({state})  units={units_int}  hook=\"{(payload.get('hook') or '')[:60]}…\"")
        else:
            db.collection("leads").document(doc_id).set(payload, merge=True)
        written += 1

    print(f"\n{'[DRY RUN] Would write' if args.dry_run else 'Wrote'} {written} PM docs to leads collection.", file=sys.stderr)


if __name__ == "__main__":
    main()
