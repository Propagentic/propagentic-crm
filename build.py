#!/usr/bin/env python3
"""Build the multi-region parcel-explorer HTML from enrichment.db.

For every region in the regions table, emits:
  - owners[]  (positional array, JS-friendly index = local owner_id)
  - parcels[] (positional array, owner_id field is the local JS index)
  - dedup_queue[] (precomputed tier-2 pairs awaiting review)

Inlines everything into app_template.html (forked from the legacy template),
writes app.html.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from lib.db import connect  # noqa: E402
from lib.dedup import find_tier2_pairs  # noqa: E402

DB_PATH = PROJECT / "enrichment.db"
TEMPLATE_PATH = PROJECT / "app_template.html"
OUT = PROJECT / "app.html"

# Property-type classification at build time. Each parcel's land_use_code is
# the source-native value (Nashville integer string, Baltimore text like
# "RESIDENTIAL", Fayette single-letter class like "R"). We re-classify here so
# the JS only sees 0=home/1=vacant/2=commercial.
RES_HOME_CODES = {11, 12, 13, 14, 15, 16, 17, 18, 19, 38, 39, 81, 88}
RES_VACANT_CODES = {10, 80}
PT_HOME, PT_VACANT_RES, PT_COMMERCIAL = 0, 1, 2


def classify_property_type(region_id: str, code_str: str | None, land_use: str | None) -> int:
    """Per-region classification. Nashville=integer codes, Baltimore=text,
    Fayette=single-letter CLASS."""
    if not code_str:
        return PT_COMMERCIAL
    c = code_str.strip().upper()
    if region_id == "nashville_tn":
        try:
            n = int(float(c))
        except ValueError:
            digits = "".join(ch for ch in c if ch.isdigit())
            n = int(digits) if digits else -1
        if n in RES_HOME_CODES: return PT_HOME
        if n in RES_VACANT_CODES: return PT_VACANT_RES
        return PT_COMMERCIAL
    if region_id == "baltimore_md":
        text = (land_use or c or "").upper()
        if "RESID" in text or "DWELL" in text or "TOWNHOUSE" in text or "CONDO" in text:
            if "VACANT" in text: return PT_VACANT_RES
            return PT_HOME
        if "VACANT" in text: return PT_COMMERCIAL
        return PT_COMMERCIAL
    if region_id == "fayette_ky":
        # Single-letter PVA class: R/M=home, V=vacant, others=commercial
        if c in ("R", "M"): return PT_HOME
        if c == "V": return PT_VACANT_RES
        return PT_COMMERCIAL
    return PT_COMMERCIAL


def absentee_flag(state: str | None, expected_state: str, mailing_city: str | None, parcel_city: str | None) -> bool:
    """Same per-parcel heuristic as the legacy app: state-mismatch OR city-mismatch."""
    s = (state or "").strip().upper()
    if s and s != expected_state:
        return True
    mc = (mailing_city or "").strip().upper()
    pc = (parcel_city or "").strip().upper()
    if mc and pc and mc != pc:
        return True
    return False


# Region-specific notes shown in a banner under the region selector
REGION_NOTES = {
    "fayette_ky": (
        "Fayette/Lexington source files (LFUCG GIS) do not include owner names or mailing data. "
        "Owners are synthesized as 'PVA #<num>' placeholders — owner-centric filters (ICP / value / "
        "absentee) are degraded for this region until skip-trace data is imported."
    ),
}

REGION_EXPECTED_STATE = {
    "nashville_tn": "TN",
    "baltimore_md": "MD",
    "fayette_ky":   "KY",
}


def build_region_block(conn: sqlite3.Connection, region_meta: dict) -> dict:
    region_id = region_meta["region_id"]
    expected_state = REGION_EXPECTED_STATE.get(region_id, "")
    print(f"  [{region_id}] reading owners ...")
    owner_rows = conn.execute(
        """
        SELECT owner_id, owner_name,
               n_parcels, n_homes, n_vacant, n_commercial,
               total_value, home_value,
               mailing_state, mailing_city, mailing_zip, mailing_addr1,
               icp, is_hoa, absentee_pct
          FROM owners
         WHERE region_id = ?
         ORDER BY n_parcels DESC, owner_id ASC
        """,
        (region_id,),
    ).fetchall()

    db_id_to_js: dict[int, int] = {}
    js_owners: list[list] = []
    owner_mailing: dict[int, tuple[str, str]] = {}
    skipped_solo_owners = 0
    for row in owner_rows:
        (oid, name, n_par, n_hm, n_va, n_co, tv, hv,
         m_state, m_city, m_zip, m_addr,
         icp, is_hoa, absent) = row
        # --trim drops solo-property owners — they're not prospects and they're
        # ~60-100% of the data depending on region. Their parcels get dropped too.
        if TRIM_MODE and (n_par or 0) < 2:
            skipped_solo_owners += 1
            continue
        js_idx = len(js_owners)
        db_id_to_js[oid] = js_idx
        owner_mailing[js_idx] = ((m_state or "").strip().upper(), (m_city or "").strip().upper())
        js_owners.append([
            name,
            int(n_par or 0),
            int(n_hm or 0),
            int(n_va or 0),
            int(n_co or 0),
            int(tv or 0),
            int(hv or 0),
            m_state or "",
            m_city or "",
            m_zip or "",
            m_addr or "",
            int(icp or 0),
            bool(is_hoa),
            round(float(absent or 0.0), 4),
        ])
    if skipped_solo_owners:
        print(f"  [{region_id}]   --trim dropped {skipped_solo_owners:,} single-property owners")

    print(f"  [{region_id}] reading parcels ...")
    parcel_rows = conn.execute(
        """
        SELECT owner_id, parcel_addr, parcel_city,
               land_use, land_use_code, zoning,
               acres, total_value, sale_date, sale_price,
               council_district, lat, lon, apn
          FROM parcels
         WHERE region_id = ?
        """,
        (region_id,),
    )

    js_parcels: list[list] = []
    skipped = 0
    dropped_zero = 0
    for r in parcel_rows:
        (db_oid, parcel_addr, parcel_city,
         land_use, land_use_code, zoning,
         acres, total_value, sale_date, sale_price,
         council, lat, lon, apn) = r
        js_oid = db_id_to_js.get(db_oid)
        if js_oid is None:
            skipped += 1
            continue
        # When --trim is on: drop $0-value parcels for regions that have value data.
        # Fayette has no value data (all $0) so we keep its parcels regardless.
        if TRIM_MODE and region_id != "fayette_ky":
            if total_value is None or total_value == 0:
                dropped_zero += 1
                continue
        ptype = classify_property_type(region_id, land_use_code, land_use)
        mailing_state, mailing_city = owner_mailing[js_oid]
        absent = absentee_flag(mailing_state, expected_state, mailing_city, parcel_city)
        # Precision: --trim uses 5 decimals (~1m), normal uses 6.
        coord_prec = 5 if TRIM_MODE else 6
        if TRIM_MODE:
            # Trim mode: drop fields rarely populated or shown.
            # Keep: lat, lon, owner_id, addr, total_val, property_type, absentee, land_use, apn
            # Drop: sale_date, sale_price, acres, zoning, district
            js_parcels.append([
                round(float(lat), coord_prec),
                round(float(lon), coord_prec),
                js_oid,
                parcel_addr or "",
                int(total_value) if total_value is not None else None,
                "",                   # sale_date
                None,                 # sale_price
                None,                 # acres
                "",                   # zoning
                None,                 # district
                ptype,
                bool(absent),
                land_use or "",
                apn or "",
            ])
        else:
            js_parcels.append([
                round(float(lat), coord_prec),
                round(float(lon), coord_prec),
                js_oid,
                parcel_addr or "",
                int(total_value) if total_value is not None else None,
                sale_date or "",
                int(sale_price) if sale_price is not None else None,
                round(float(acres), 2) if acres is not None else None,
                zoning or "",
                int(council) if (council not in (None, "") and str(council).strip().isdigit()) else None,
                ptype,
                bool(absent),
                land_use or "",
                apn or "",
            ])
    extra = f", dropped {dropped_zero:,} \$0 parcels" if dropped_zero else ""
    print(f"  [{region_id}]   {len(js_owners):,} owners, {len(js_parcels):,} parcels (skipped {skipped}{extra})")

    # Dedup queue (tier-2 pairs). Skip for synthetic-owner regions where it
    # would be noise.
    if region_id == "fayette_ky":
        dedup_queue: list[dict] = []
    else:
        print(f"  [{region_id}] computing tier-2 dedup pairs ...")
        pairs = find_tier2_pairs(conn, region_id)
        dedup_queue = []
        for p in pairs:
            a_js = db_id_to_js.get(p.a["owner_id"])
            b_js = db_id_to_js.get(p.b["owner_id"])
            if a_js is None or b_js is None:
                continue
            dedup_queue.append({
                "a_oid": a_js,
                "b_oid": b_js,
                "score": round(p.score, 4),
                "addr_norm": p.addr_norm,
            })
        print(f"  [{region_id}]   {len(dedup_queue):,} tier-2 pairs queued")

    return {
        "id": region_id,
        "display_name": region_meta["display_name"],
        "state": region_meta["state"],
        "county": region_meta["county"],
        "center_lat": region_meta["center_lat"],
        "center_lon": region_meta["center_lon"],
        "notes": REGION_NOTES.get(region_id),
        "owners": js_owners,
        "parcels": js_parcels,
        "dedup_queue": dedup_queue,
    }


TRIM_MODE = False


def main() -> None:
    import argparse
    global TRIM_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="",
                    help="Comma-separated region IDs to include "
                         "(e.g. 'nashville_tn' for a slim ~60 MB single-region build). "
                         "Default = all regions in DB.")
    ap.add_argument("--out", default=None,
                    help="Output filename (default: app.html for all-regions, "
                         "or app_<region>.html for single-region)")
    ap.add_argument("--trim", action="store_true",
                    help="Drop $0-value parcels and single-property owners. "
                         "Cuts 70%%+ of file size. Useful for prospect-only views.")
    ap.add_argument("--lazy", action="store_true",
                    help="Emit per-region JSON files (dist/data/&lt;region&gt;.json) and a small "
                         "shell index.html that fetches them on demand. Full data, fast load.")
    args = ap.parse_args()
    TRIM_MODE = bool(args.trim)

    if not DB_PATH.exists():
        sys.exit(f"ERROR: {DB_PATH} missing")
    if not TEMPLATE_PATH.exists():
        sys.exit(f"ERROR: {TEMPLATE_PATH} missing")

    conn = connect(DB_PATH)
    rows = conn.execute(
        """SELECT region_id, display_name, state, county, center_lat, center_lon
             FROM regions ORDER BY region_id"""
    ).fetchall()
    if not rows:
        sys.exit("ERROR: no regions in DB")
    region_metas = [
        dict(zip(["region_id","display_name","state","county","center_lat","center_lon"], r))
        for r in rows
    ]
    if args.regions:
        wanted = set(s.strip() for s in args.regions.split(",") if s.strip())
        region_metas = [m for m in region_metas if m["region_id"] in wanted]
        if not region_metas:
            sys.exit(f"ERROR: none of {wanted!r} match available regions")

    out_path = Path(args.out) if args.out else (
        OUT if not args.regions or len(region_metas) > 1
        else PROJECT / f"app_{region_metas[0]['region_id']}.html"
    )

    print(f"Building {out_path.name} for {len(region_metas)} region(s):")
    for r in region_metas:
        print(f"  - {r['region_id']}  ({r['display_name']}, {r['state']})")
    print()

    region_blocks = [build_region_block(conn, m) for m in region_metas]
    conn.close()

    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")

    if args.lazy:
        # Split data: each region → its own JSON file under dist/data/.
        # The HTML inlines only the manifest (region metadata + counts), then the
        # JS lazy-fetches `data/<region>.json` when a region is first used.
        out_dir = out_path.parent
        data_dir = out_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for r in region_blocks:
            per_region = {
                "owners":      r["owners"],
                "parcels":     r["parcels"],
                "dedup_queue": r["dedup_queue"],
            }
            per_region_path = data_dir / f"{r['id']}.json"
            per_region_text = json.dumps(per_region, separators=(",", ":"), ensure_ascii=False)
            per_region_path.write_text(per_region_text, encoding="utf-8")
            print(f"  wrote {per_region_path.name}: {per_region_path.stat().st_size / 1e6:.1f} MB")
            manifest.append({
                "id":           r["id"],
                "display_name": r["display_name"],
                "state":        r["state"],
                "county":       r["county"],
                "center_lat":   r["center_lat"],
                "center_lon":   r["center_lon"],
                "notes":        r["notes"],
                "owner_count":  len(r["owners"]),
                "parcel_count": len(r["parcels"]),
                "dedup_count":  len(r["dedup_queue"]),
                # owners/parcels/dedup_queue start EMPTY — populated on fetch
                "owners":       [],
                "parcels":      [],
                "dedup_queue":  [],
            })
        payload = {"regions": manifest, "lazy": True}
        data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        html = tpl.replace("__DATA__", data_json)
        out_path.write_text(html, encoding="utf-8")
        total = out_path.stat().st_size + sum((data_dir / f"{r['id']}.json").stat().st_size for r in region_blocks)
        print(f"\nDone. {out_path.name}: {out_path.stat().st_size / 1e6:.2f} MB (shell)")
        print(f"      total payload across all files: {total / 1e6:.1f} MB")
        print(f"\nDeploy `{out_dir}` to any static host (Netlify, GitHub Pages, S3).")
        print(f"The shell loads instantly; regions fetch on first click.")
    else:
        payload = {"regions": region_blocks}
        print("\nSerializing JSON ...")
        data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        print(f"  total JSON size: {len(data_json) / 1e6:.1f} MB")
        print(f"Writing {out_path} ...")
        html = tpl.replace("__DATA__", data_json)
        out_path.write_text(html, encoding="utf-8")
        print(f"Done. File size: {out_path.stat().st_size / 1e6:.1f} MB")
        print(f"\nTo open with reliable performance, run:")
        print(f"  python3 serve.py --file {out_path.name}")


if __name__ == "__main__":
    main()
