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

    sa_info = json.loads(GMAIL_SA_KEY.value)
    after_ts = int(
        (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)).timestamp()
    )
    gmail_query = f"after:{after_ts}"

    total_seen = 0
    total_matched = 0

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
                update = {
                    "crm_emailed": True,
                    "crm_emailed_at": firestore.SERVER_TIMESTAMP,
                }
                if direction == "inbound":
                    update["crm_email_outcome"] = "responded"
                db.collection("leads").document(lid).set(update, merge=True)

        if mailbox_matched:
            print(f"[{mailbox}] matched {mailbox_matched} of {len(msg_ids)}")

    print(f"poll complete: matched {total_matched} of {total_seen}")
    return {"seen": total_seen, "matched": total_matched}


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
