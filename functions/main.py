"""
gmail_poll — scheduled Cloud Function that ingests recent email activity
from the 5 team mailboxes every 2 minutes.

Per invocation:
  1. Load all leads' email addresses from Firestore (used as the contact-match
     gate).
  2. For each of the 5 mailboxes, impersonate via domain-wide delegation and
     fetch messages newer than (now − LOOKBACK_MINUTES).
  3. For each message, check whether any participant's address matches a CRM
     lead. If none, skip — non-CRM email is never persisted.
  4. Write matched messages to `emailActivity/{sha1(message_id)}` (idempotent
     across runs and across mailboxes — same email seen in two mailboxes
     collapses to one doc).
  5. Update each matched lead with crm_emailed=true, and crm_email_outcome=
     "responded" if the message is inbound from the lead's own email.

The LOOKBACK window (5 min) overlaps the SCHEDULE (every 2 min) so that any
single late message is picked up on the next run, not lost. Idempotency
guarantees re-processing a message is a no-op.
"""

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from firebase_admin import initialize_app, firestore
from firebase_functions import https_fn, options
from firebase_functions.params import SecretParam
from google.oauth2 import service_account
from googleapiclient.discovery import build

initialize_app()

# Secret containing the gmail-readonly-watcher service account JSON.
# Set with: firebase functions:secrets:set GMAIL_SA_KEY < <path-to-key.json>
GMAIL_SA_KEY = SecretParam("GMAIL_SA_KEY")

MAILBOXES = [
    "jackson@propagenticai.com",
    "ben@propagenticai.com",
    "brantley@propagenticai.com",
    "brian@propagenticai.com",
    "zach@propagenticai.com",
]
TEAM_DOMAINS = {"propagenticai.com"}
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LOOKBACK_MINUTES = 5      # window we re-scan each run; > schedule interval for safety
MAX_BODY_BYTES = 4096     # cap stored body_text per message
MAX_PER_MAILBOX = 100     # safety cap on messages per mailbox per run


def _normalize_email(addr: str) -> str:
    if not addr:
        return ""
    m = re.search(r"<([^>]+)>", addr)
    if m:
        addr = m.group(1)
    return addr.strip().lower()


def _extract_display_name(addr: str) -> str:
    if not addr:
        return ""
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', addr)
    return m.group(1).strip() if m else ""


def _parse_header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_email_list(field_value):
    if not field_value:
        return []
    out = []
    for part in re.split(r",\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", field_value):
        addr = _normalize_email(part)
        if addr:
            out.append(addr)
    return out


def _extract_body_text(payload) -> str:
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if mime == "text/plain" and data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = payload.get("parts", []) or []
    for p in parts:
        if p.get("mimeType", "").startswith("text/plain"):
            text = _extract_body_text(p)
            if text:
                return text
    for p in parts:
        text = _extract_body_text(p)
        if text:
            if "<" in text and ">" in text:
                text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
                text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
                text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
                text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
            return text
    if mime == "text/html" and data:
        try:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            html = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
            html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
            html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
            html = re.sub(r"</p>", "\n\n", html, flags=re.IGNORECASE)
            html = re.sub(r"<[^>]+>", "", html)
            return re.sub(r"\n{3,}", "\n\n", html)
        except Exception:
            return ""
    return ""


def _has_attachments(payload) -> bool:
    if not payload:
        return False
    for p in payload.get("parts", []) or []:
        if p.get("filename"):
            return True
        if _has_attachments(p):
            return True
    return False


def _doc_id(rfc_message_id: str, fallback: str) -> str:
    raw = (rfc_message_id or "").strip().strip("<>")
    if not raw:
        return f"gmail__{fallback}"
    return "mid__" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _run_poll() -> dict:
    """The actual ingest logic. Returns a summary dict suitable for HTTP response."""
    db = firestore.client()

    # Heartbeat — write a doc on every invocation so we can verify externally.
    db.collection("_function_heartbeats").document("gmail_poll").set({
        "last_run_at": firestore.SERVER_TIMESTAMP,
    })

    # Build the leads email index (one-shot per invocation).
    email_to_lead_ids: dict[str, list[str]] = {}
    n_leads = 0
    for d in db.collection("leads").stream():
        n_leads += 1
        email = (d.to_dict().get("email") or "").strip().lower()
        if email:
            email_to_lead_ids.setdefault(email, []).append(d.id)
    print(f"loaded {n_leads} leads ({len(email_to_lead_ids)} with email)")

    # Load the crm_users registry so we can resolve outbound sender emails
    # (including aliases like weinba23@wfu.edu) back to the canonical team
    # member's email for auto-assignment.
    crm_users_lookup = _load_crm_users_lookup(db)

    sa_info = json.loads(GMAIL_SA_KEY.value)
    after_ts = int(
        (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)).timestamp()
    )
    gmail_query = f"after:{after_ts}"

    total_seen = 0
    total_matched = 0
    affected_leads: set[str] = set()

    for mailbox in MAILBOXES:
        try:
            creds = service_account.Credentials.from_service_account_info(
                sa_info, scopes=SCOPES
            )
            delegated = creds.with_subject(mailbox)
            gmail = build("gmail", "v1", credentials=delegated, cache_discovery=False)
        except Exception as e:
            print(f"[{mailbox}] auth/build failed: {e}")
            continue

        try:
            resp = gmail.users().messages().list(
                userId="me", q=gmail_query, maxResults=MAX_PER_MAILBOX
            ).execute()
        except Exception as e:
            print(f"[{mailbox}] list failed: {e}")
            continue

        msg_ids = [m["id"] for m in resp.get("messages", [])]
        if not msg_ids:
            continue

        mailbox_matched = 0
        for mid in msg_ids:
            total_seen += 1
            try:
                msg = gmail.users().messages().get(
                    userId="me", id=mid, format="full"
                ).execute()
            except Exception as e:
                print(f"[{mailbox}] get({mid}) failed: {e}")
                continue

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            from_raw = _parse_header(headers, "From")
            to_raw = _parse_header(headers, "To")
            cc_raw = _parse_header(headers, "Cc")
            subject = _parse_header(headers, "Subject") or "(no subject)"
            rfc_msgid = _parse_header(headers, "Message-ID") or _parse_header(
                headers, "Message-Id"
            )

            from_email = _normalize_email(from_raw)
            from_name = _extract_display_name(from_raw)
            to_emails = _parse_email_list(to_raw)
            cc_emails = _parse_email_list(cc_raw)

            participants = {from_email} | set(to_emails) | set(cc_emails)
            participants.discard("")
            matched = sorted(
                {lid for addr in participants for lid in email_to_lead_ids.get(addr, [])}
            )
            if not matched:
                continue

            total_matched += 1
            mailbox_matched += 1

            # Inbound only when sender is a known CRM lead — keeps non-team-domain
            # senders (e.g. weinba23@wfu.edu, Ben's student account) from
            # triggering false "responded" flags.
            from_is_lead = from_email in email_to_lead_ids
            direction = "inbound" if from_is_lead else "outbound"

            sent_at = datetime.fromtimestamp(
                int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc
            )
            snippet = (msg.get("snippet") or "").strip()
            body = _extract_body_text(payload)
            if len(body.encode("utf-8")) > MAX_BODY_BYTES:
                body = body.encode("utf-8")[:MAX_BODY_BYTES].decode(
                    "utf-8", errors="ignore"
                ) + "…"

            mailbox_label = mailbox.split("@")[0].capitalize()
            doc_id = _doc_id(rfc_msgid, mid)
            doc = {
                "message_id": rfc_msgid or None,
                "thread_id": msg.get("threadId"),
                "from_email": from_email,
                "from_name": from_name or None,
                "to_emails": to_emails,
                "cc_emails": cc_emails,
                "subject": subject,
                "snippet": snippet,
                "body_text": body,
                "has_attachments": _has_attachments(payload),
                "sent_at": sent_at,
                "ingested_at": firestore.SERVER_TIMESTAMP,
                "matched_lead_ids": matched,
                "direction": direction,
                "source_mailbox": mailbox,
                "source_label": mailbox_label,
            }
            db.collection("emailActivity").document(doc_id).set(doc, merge=True)

            for lid in matched:
                affected_leads.add(lid)
                update = {
                    "crm_emailed": True,
                    "crm_emailed_at": firestore.SERVER_TIMESTAMP,
                }
                if direction == "inbound":
                    update["crm_email_outcome"] = "responded"
                db.collection("leads").document(lid).set(update, merge=True)

        if mailbox_matched:
            print(f"[{mailbox}] matched {mailbox_matched} of {len(msg_ids)}")

    # Recompute per-lead email summary fields (counts + last-message timing +
    # direction) for every lead touched this cycle. The kanban Emailed sub-
    # sections (Needs Response / In Conversation / Awaiting First Response /
    # Following Up) read these fields directly — no client-side aggregation.
    # Also re-runs auto-assignment: whoever sent the most recent outbound is
    # the lead's owner (crm_owner field).
    for lid in affected_leads:
        try:
            _recompute_lead_email_summary(db, lid, crm_users_lookup)
        except Exception as e:
            print(f"summary recompute failed for {lid}: {e}")

    # Auto-DNC pass: any lead with >=4 outbound and 0 inbound and the most
    # recent outbound was >=14 days ago (the chosen grace window) gets moved
    # to Do Not Contact with reason="unspecified". Runs every poll cycle so a
    # lead that crosses the threshold is caught within minutes.
    dnc_count = _auto_dnc_pass(db)
    if dnc_count:
        print(f"auto-DNC: marked {dnc_count} leads")

    print(f"poll complete: matched {total_matched} of {total_seen}; touched {len(affected_leads)} leads")
    return {"seen": total_seen, "matched": total_matched, "leads_touched": len(affected_leads), "auto_dnc": dnc_count}


# Window after the 4th outbound during which we still wait for a reply.
# Aligned with the user's stated cadence (initial / 3d / 2w / 2w+3d).
AUTO_DNC_GRACE_DAYS = 14


def _natural_bucket(inbound: int, outbound: int, last_dir: str) -> str:
    """Compute the natural Emailed sub-section a lead lands in, ignoring any
    manual override. Mirrors the frontend `emailedBucket()` exactly so they
    stay in sync."""
    if inbound > 0 and last_dir == "inbound": return "needs_response"
    if inbound > 0 and last_dir == "outbound": return "in_conversation"
    if inbound == 0 and outbound == 1: return "awaiting_first"
    if inbound == 0 and outbound >= 2: return "following_up"
    return "awaiting_first"


def _load_crm_users_lookup(db) -> dict:
    """
    Build a lookup map from any team-member email (primary or alias) to the
    canonical primary email. e.g. "weinba23@wfu.edu" -> "ben@propagenticai.com".
    """
    lookup = {}
    for d in db.collection("crm_users").stream():
        data = d.to_dict()
        primary = (data.get("email") or d.id).lower()
        lookup[primary] = primary
        for alias in (data.get("aliases") or []):
            if alias:
                lookup[alias.lower()] = primary
    return lookup


def _recompute_lead_email_summary(db, lead_id: str, crm_users_lookup: dict = None) -> None:
    """
    Aggregate all emailActivity for this lead and write summary fields to the
    lead doc. Idempotent — same input always produces the same output.

    Also auto-assigns crm_owner = canonical email of the team member whose
    address appears in the most recent outbound message. If we can't resolve
    the sender to a known team member, crm_owner is left untouched.
    """
    docs = db.collection("emailActivity") \
        .where("matched_lead_ids", "array_contains", lead_id) \
        .stream()
    inbound = 0
    outbound = 0
    last_at = None
    last_dir = None
    last_outbound_at = None
    last_inbound_at = None
    last_outbound_from = None
    for d in docs:
        data = d.to_dict()
        direction = data.get("direction") or "outbound"
        sent_at = data.get("sent_at")
        if direction == "inbound":
            inbound += 1
            if sent_at and (last_inbound_at is None or sent_at > last_inbound_at):
                last_inbound_at = sent_at
        else:
            outbound += 1
            if sent_at and (last_outbound_at is None or sent_at > last_outbound_at):
                last_outbound_at = sent_at
                last_outbound_from = (data.get("from_email") or "").lower()
        if sent_at and (last_at is None or sent_at > last_at):
            last_at = sent_at
            last_dir = direction

    update = {
        "crm_email_outbound_count": outbound,
        "crm_email_inbound_count": inbound,
        "crm_email_last_at": last_at,
        "crm_email_last_outbound_at": last_outbound_at,
        "crm_email_last_inbound_at": last_inbound_at,
        "crm_email_last_direction": last_dir,
    }
    # Auto-assignment: most recent outbound's sender wins. We only write
    # crm_owner if we can resolve the sender to a known team member; otherwise
    # the existing value is left as-is.
    if last_outbound_from and crm_users_lookup:
        canonical = crm_users_lookup.get(last_outbound_from)
        if canonical:
            update["crm_owner"] = canonical

    # Manual bucket override yields to auto once natural data catches up. If
    # the lead has a crm_email_bucket_override AND the natural bucket now
    # matches that override, clear it — no longer needed.
    existing = db.collection("leads").document(lead_id).get()
    existing_data = existing.to_dict() if existing.exists else {}
    override = existing_data.get("crm_email_bucket_override")
    if override:
        natural = _natural_bucket(inbound, outbound, last_dir)
        if natural == override:
            update["crm_email_bucket_override"] = firestore.DELETE_FIELD

    db.collection("leads").document(lead_id).set(update, merge=True)


def _auto_dnc_pass(db) -> int:
    """
    Find leads where: 4+ outbound, 0 inbound, latest outbound >=14 days ago,
    and not already DNC. Mark them DNC with reason='unspecified' + audit flag.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_DNC_GRACE_DAYS)
    # Single-field query to keep index requirements minimal; filter the rest in Python.
    candidates = db.collection("leads") \
        .where("crm_email_outbound_count", ">=", 4) \
        .stream()
    marked = 0
    for c in candidates:
        data = c.to_dict()
        if data.get("crm_status") == "do_not_contact":
            continue
        if (data.get("crm_email_inbound_count") or 0) > 0:
            continue
        last_out = data.get("crm_email_last_outbound_at")
        if not last_out or last_out > cutoff:
            continue
        db.collection("leads").document(c.id).set({
            "crm_status": "do_not_contact",
            "crm_dnc_reason": "unspecified",
            "crm_dnc_auto": True,
            "crm_dnc_auto_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        marked += 1
    return marked


# HTTPS-triggered ingest function. Cloud Scheduler invokes this URL on a 2-min
# cron via a job created out-of-band (see scripts/create_scheduler_job.py).
#
# We use HTTPS instead of @on_schedule because Firebase v2 Python's
# @scheduler_fn.on_schedule has an adapter issue where Cloud Scheduler's HTTP
# POST doesn't construct a valid ScheduledEvent — the trigger fires but the
# function body never runs (verified via heartbeat doc, 2026-05-20).
@https_fn.on_request(
    secrets=[GMAIL_SA_KEY],
    timeout_sec=300,
    memory=options.MemoryOption.MB_512,
    region="us-central1",
)
def gmail_poll(req: https_fn.Request) -> https_fn.Response:
    try:
        summary = _run_poll()
        return https_fn.Response(
            f"OK · seen={summary['seen']} matched={summary['matched']}", status=200
        )
    except Exception as e:
        import traceback
        return https_fn.Response(
            f"ERROR: {type(e).__name__}: {e}\n\n{traceback.format_exc()}", status=500
        )
