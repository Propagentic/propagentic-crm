"""Store interface — thin data-access layer for the parcel explorer.

Two implementations:
  - LocalStore: backed by the SQLite file at enrichment.db (current default).
  - SupabaseStore: stub for the future hosted-backend migration.

Per CLAUDE.md backend-abstraction section: the UI should go through this
interface so the migration to Supabase/Firebase is a swap, not a rewrite.

NOTE: v1 of the parcel explorer is still a static single-page HTML with
inlined data — it doesn't actually call into LocalStore at runtime. This
interface exists so that build.py and future CLI tooling have a single
data-access seam to evolve.
"""
from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class Store(ABC):
    """Abstract data-access interface. Implementations bind to a region or
    span all regions per the method contract."""

    @abstractmethod
    def get_regions(self) -> list[dict]:
        """Return [{region_id, display_name, state, county, center_lat, center_lon, last_ingested}]."""

    @abstractmethod
    def get_owners(
        self,
        region_id: Optional[str] = None,
        filters: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Return owners matching filters (icp, ptype, own, hoa_only, exclude_gov, search).
        region_id=None means all regions."""

    @abstractmethod
    def get_owner(self, owner_id: int) -> Optional[dict]:
        """Return a single owner row by primary key."""

    @abstractmethod
    def get_owner_parcels(self, owner_id: int) -> list[dict]:
        """Return all parcels owned by a single owner."""

    @abstractmethod
    def get_owner_aliases(self, owner_id: int) -> list[dict]:
        """Return audit-trail rows (collapsed names + addresses) for this owner."""

    @abstractmethod
    def update_owner_status(self, owner_id: int, status: str, notes: str) -> None:
        """Set the outreach status and notes on an owner row."""

    @abstractmethod
    def upsert_contact(self, owner_id: int, contact: dict) -> None:
        """Add a phone or email to an owner's record per the additive merge rules
        from CLAUDE.md (write next-available slot, never overwrite differing values)."""

    @abstractmethod
    def get_dedup_queue(self, region_id: str) -> list[dict]:
        """Return tier-2 pairs awaiting human review for a region."""

    @abstractmethod
    def resolve_dedup(self, alias_id: int, action: str) -> None:
        """Apply a 'confirm' (merge) or 'separate' (keep apart) decision."""

    @abstractmethod
    def log_import(self, summary: dict) -> int:
        """Insert a row into contact_imports. Returns import_id."""


# ---------------------------------------------------------------------------
# LocalStore — SQLite-backed implementation
# ---------------------------------------------------------------------------
class LocalStore(Store):
    """SQLite-backed Store, talking to enrichment.db directly.

    Reads are eager (returns list[dict] per call). Writes commit per call.
    Thread safety: a single LocalStore is not safe across threads; create one
    per thread. SQLite's own locking handles process-level concurrency.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- regions --
    def get_regions(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM regions ORDER BY region_id").fetchall()
        return [dict(r) for r in rows]

    # -- owners --
    def get_owners(self, region_id=None, filters=None, limit=None) -> list[dict]:
        filters = filters or {}
        where = []
        args: list = []
        if region_id:
            where.append("region_id = ?")
            args.append(region_id)
        if (icp := filters.get("icp")) is not None:
            if icp == "any-multi":
                where.append("n_parcels >= 2")
            elif icp == "custom":
                if (lo := filters.get("custom_min")) is not None:
                    where.append("n_parcels >= ?"); args.append(int(lo))
                if (hi := filters.get("custom_max")) is not None:
                    where.append("n_parcels <= ?"); args.append(int(hi))
            elif str(icp).isdigit():
                where.append("icp = ?"); args.append(int(icp))
        if filters.get("hoa_only"):
            where.append("is_hoa = 1")
        if (search := filters.get("search")):
            where.append("owner_name LIKE ?")
            args.append(f"%{search}%")
        sql = "SELECT * FROM owners"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n_parcels DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_owner(self, owner_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM owners WHERE owner_id = ?", (owner_id,)).fetchone()
        return dict(row) if row else None

    def get_owner_parcels(self, owner_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM parcels WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_owner_aliases(self, owner_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM owner_aliases WHERE owner_id = ? ORDER BY alias_id",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- outreach --
    def update_owner_status(self, owner_id, status, notes):
        with self._conn() as c:
            c.execute(
                "UPDATE owners SET status = ?, notes = ?, last_touched = ? WHERE owner_id = ?",
                (status, notes, time.strftime("%Y-%m-%dT%H:%M:%S"), owner_id),
            )
            c.commit()

    # -- contact import --
    def upsert_contact(self, owner_id, contact):
        """Apply CLAUDE.md merge rules per phone/email slot.

        contact shape: {
          phones: [{value, type, confidence, source, dnc}, ...],
          emails: [{value, confidence, source}, ...],
        }
        """
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            row = c.execute(
                """SELECT phone1, phone2, phone3, email1, email2 FROM owners WHERE owner_id = ?""",
                (owner_id,),
            ).fetchone()
            if not row:
                return
            phones = [row[k] for k in ("phone1", "phone2", "phone3")]
            emails = [row[k] for k in ("email1", "email2")]
            updates: list[tuple[str, object]] = []

            for new in contact.get("phones", []) or []:
                v = new.get("value")
                if not v: continue
                if v in phones: continue   # already present — no-op
                # write to first empty slot
                for i, slot in enumerate(phones):
                    if not slot:
                        n = i + 1
                        phones[i] = v
                        updates += [
                            (f"phone{n}", v),
                            (f"phone{n}_type", new.get("type")),
                            (f"phone{n}_confidence", new.get("confidence")),
                            (f"phone{n}_source", new.get("source")),
                            (f"phone{n}_dnc", 1 if new.get("dnc") else 0),
                            (f"phone{n}_updated", ts),
                        ]
                        break
                # if all 3 slots full and the new phone is different, drop it
                # (would require conflict-review UI to resolve)

            for new in contact.get("emails", []) or []:
                v = new.get("value")
                if not v: continue
                if v in emails: continue
                for i, slot in enumerate(emails):
                    if not slot:
                        n = i + 1
                        emails[i] = v
                        updates += [
                            (f"email{n}", v),
                            (f"email{n}_confidence", new.get("confidence")),
                            (f"email{n}_source", new.get("source")),
                            (f"email{n}_updated", ts),
                        ]
                        break

            if updates:
                set_clause = ", ".join(f"{k} = ?" for k, _ in updates)
                args = [v for _, v in updates] + [owner_id]
                c.execute(f"UPDATE owners SET {set_clause} WHERE owner_id = ?", args)
                c.commit()

    # -- dedup --
    def get_dedup_queue(self, region_id):
        """Recomputed at call time from current owners — caller is expected
        to filter against the persisted 'resolved' decisions (alias rows with
        match_method='keep_separate' or 'fuzzy_name_same_address')."""
        from .dedup import find_tier2_pairs  # avoid circular import at module load
        with self._conn() as c:
            pairs = find_tier2_pairs(c, region_id)
        return [
            {"addr_norm": p.addr_norm,
             "a": p.a, "b": p.b,
             "score": p.score}
            for p in pairs
        ]

    def resolve_dedup(self, alias_id, action):
        # TODO: implement once UI is wired. The shape: action ∈ {'confirm','separate'}.
        # confirm → call apply_tier1_merges-style logic for the specific pair.
        # separate → write an owner_aliases row with match_method='keep_separate'.
        raise NotImplementedError("resolve_dedup is pending UI wiring (step 3 follow-up)")

    # -- imports --
    def log_import(self, summary):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO contact_imports
                     (filename, imported_at, rows_total, rows_matched, rows_unmatched,
                      rows_updated, rows_overwritten, source_label, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    summary.get("filename"),
                    ts,
                    summary.get("rows_total", 0),
                    summary.get("rows_matched", 0),
                    summary.get("rows_unmatched", 0),
                    summary.get("rows_updated", 0),
                    summary.get("rows_overwritten", 0),
                    summary.get("source_label"),
                    summary.get("notes"),
                ),
            )
            c.commit()
            return cur.lastrowid


# ---------------------------------------------------------------------------
# SupabaseStore — stub
# ---------------------------------------------------------------------------
class SupabaseStore(Store):
    """Stub. The future hosted-backend implementation. Method contracts are
    identical to LocalStore; the swap is intentionally one-day.

    To wire this up later:
      1. Create a Supabase project. Get URL + service-role key.
      2. Create matching tables in Postgres (regions, owners, parcels,
         owner_aliases, contact_imports). Same column names as the SQLite
         schema in lib/db.py.
      3. Replace the NotImplementedError bodies below with POSTGREST calls
         (requests.get/post against /rest/v1/<table>).
      4. In production: swap which Store implementation build.py / the
         future API server constructs.
    """

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key
        # TODO: import postgrest-py or httpx and stash a session here.

    def get_regions(self) -> list[dict]:
        raise NotImplementedError("SupabaseStore: not yet wired. See file docstring.")

    def get_owners(self, region_id=None, filters=None, limit=None):
        raise NotImplementedError

    def get_owner(self, owner_id):
        raise NotImplementedError

    def get_owner_parcels(self, owner_id):
        raise NotImplementedError

    def get_owner_aliases(self, owner_id):
        raise NotImplementedError

    def update_owner_status(self, owner_id, status, notes):
        raise NotImplementedError

    def upsert_contact(self, owner_id, contact):
        raise NotImplementedError

    def get_dedup_queue(self, region_id):
        raise NotImplementedError

    def resolve_dedup(self, alias_id, action):
        raise NotImplementedError

    def log_import(self, summary):
        raise NotImplementedError
