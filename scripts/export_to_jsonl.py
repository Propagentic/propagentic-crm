#!/usr/bin/env python3
"""Export enrichment.db tables to JSONL for git/Firestore portability.

For each table, writes data/<table>.jsonl with one JSON object per line.
Files larger than ~50 MB are split into chunks named <table>-001.jsonl,
<table>-002.jsonl, ... Files larger than 1 GB cause the script to bail out
with a recommendation to use Firebase Storage instead of git.

Also emits data/schema.json describing every field per table with its
inferred type (string / number / boolean / null), and prints a summary.

Re-run anytime — output is fully replaced. Idempotent.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT / "enrichment.db"
OUT_DIR = PROJECT / "data"
CHUNK_TARGET_BYTES = 50 * 1024 * 1024     # 50 MB target per chunk
HARD_LIMIT_BYTES   = 1024 * 1024 * 1024   # 1 GB hard cap → bail with warning

TABLES = ["regions", "owners", "parcels", "owner_aliases", "contact_imports"]


def infer_field_types(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Walk the whole table once to determine each column's actual type.

    SQLite is dynamically typed; we trust the data, not the declared types.
    Returns {col: 'string'|'number'|'boolean'|'null'}.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    seen: dict[str, set[str]] = {c: set() for c in cols}
    for row in conn.execute(f"SELECT * FROM {table}"):
        for i, v in enumerate(row):
            if v is None:
                continue
            t = type(v).__name__
            if t == "int":
                seen[cols[i]].add("integer")
            elif t == "float":
                seen[cols[i]].add("number")
            elif t == "str":
                seen[cols[i]].add("string")
            elif t == "bytes":
                seen[cols[i]].add("bytes")
            else:
                seen[cols[i]].add(t)
    schema = {}
    for col in cols:
        if not seen[col]:
            schema[col] = "null"
        elif seen[col] == {"integer"}:
            schema[col] = "integer"
        elif seen[col] <= {"integer", "number"}:
            schema[col] = "number"
        else:
            schema[col] = sorted(seen[col])[0] if len(seen[col]) == 1 else sorted(seen[col])
    return schema


def export_table(conn: sqlite3.Connection, table: str) -> dict:
    """Write data/<table>.jsonl. Returns summary dict."""
    out_single = OUT_DIR / f"{table}.jsonl"
    # Stream rows directly to a chunk-aware writer.
    chunk_idx = 1
    current_path = OUT_DIR / f"{table}.jsonl.partial"
    current_bytes = 0
    current_file = open(current_path, "w", encoding="utf-8")
    chunk_paths: list[Path] = [current_path]

    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows_written = 0
    total_bytes = 0

    def flush_close():
        nonlocal current_file
        current_file.close()

    def rotate():
        nonlocal chunk_idx, current_path, current_bytes, current_file
        flush_close()
        chunk_idx += 1
        current_path = OUT_DIR / f"{table}-{chunk_idx:03d}.jsonl.partial"
        chunk_paths.append(current_path)
        current_bytes = 0
        current_file = open(current_path, "w", encoding="utf-8")

    for row in cur:
        # Drop null fields per-record (standard JSONL convention). The full
        # field list lives in schema.json so nothing is lost — a missing key
        # round-trips back to NULL. Saves ~50% on tables with many empty cols.
        obj = {k: v for k, v in zip(cols, row) if v is not None}
        line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
        b = line.encode("utf-8")
        if current_bytes > 0 and current_bytes + len(b) > CHUNK_TARGET_BYTES:
            rotate()
        current_file.write(line)
        current_bytes += len(b)
        total_bytes += len(b)
        rows_written += 1
    flush_close()

    if total_bytes > HARD_LIMIT_BYTES:
        # bail — caller should decide what to do
        return {
            "table": table,
            "rows": rows_written,
            "bytes": total_bytes,
            "chunks": [],
            "over_hard_limit": True,
        }

    # Finalize: rename .partial files to final names.
    # If we ended up with exactly one chunk AND it's under target, name it <table>.jsonl.
    # Otherwise rename each <table>.jsonl.partial → <table>-001.jsonl, etc.
    final_paths: list[Path] = []
    if len(chunk_paths) == 1 and total_bytes <= CHUNK_TARGET_BYTES:
        final = OUT_DIR / f"{table}.jsonl"
        chunk_paths[0].rename(final)
        final_paths = [final]
    else:
        for i, p in enumerate(chunk_paths, 1):
            final = OUT_DIR / f"{table}-{i:03d}.jsonl"
            p.rename(final)
            final_paths.append(final)

    return {
        "table": table,
        "rows": rows_written,
        "bytes": total_bytes,
        "chunks": [str(p.relative_to(PROJECT)) for p in final_paths],
        "over_hard_limit": False,
    }


def main() -> None:
    if not DB_PATH.exists():
        sys.exit(f"ERROR: {DB_PATH} not found")
    OUT_DIR.mkdir(exist_ok=True)
    # wipe prior output so we never mix old + new
    for old in OUT_DIR.glob("*.jsonl"):
        old.unlink()
    for old in OUT_DIR.glob("*.jsonl.partial"):
        old.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    schema: dict[str, dict] = {
        "_meta": {
            "source": str(DB_PATH.relative_to(PROJECT)),
            "extracted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chunk_target_mb": CHUNK_TARGET_BYTES / 1e6,
            "extraction_script": "scripts/export_to_jsonl.py",
        }
    }
    summaries: list[dict] = []
    for t in TABLES:
        print(f"[{t}] exporting ...")
        schema[t] = {"fields": infer_field_types(conn, t)}
        summary = export_table(conn, t)
        summaries.append(summary)
        if summary["over_hard_limit"]:
            print(f"  !! {t} exceeds 1 GB ({summary['bytes']/1e9:.1f} GB) — "
                  f"too large for git. Use Firebase Storage or split further.")
            continue
        size_mb = summary["bytes"] / 1e6
        print(f"  {summary['rows']:>9,} rows, {size_mb:>6.1f} MB, "
              f"{len(summary['chunks'])} file(s)")

    conn.close()

    (OUT_DIR / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_DIR / 'schema.json'}")

    # Save extraction summary for the README to read.
    (OUT_DIR / ".extraction_summary.json").write_text(
        json.dumps(summaries, indent=2),
        encoding="utf-8",
    )

    total_rows = sum(s["rows"] for s in summaries)
    total_mb   = sum(s["bytes"] for s in summaries) / 1e6
    print(f"\nTotal: {total_rows:,} rows across {len(summaries)} tables, {total_mb:.1f} MB")


if __name__ == "__main__":
    main()
