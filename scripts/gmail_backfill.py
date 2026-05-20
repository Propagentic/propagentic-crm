#!/usr/bin/env python3
"""
One-shot backfill of email activity from the 5 team mailboxes.

For each mailbox, pulls messages within the time window, runs the
contact-match gate against the `leads` collection (matches if any
participant's email equals a lead's `email` field), and writes matched
messages to `emailActivity` in Firestore. Also updates each matched lead's
`crm_emailed = true` and, for inbound mail, `crm_email_outcome = "responded"`.

Idempotent: uses RFC 822 Message-ID (sanitized) as the Firestore doc ID, so
re-running the script overwrites docs in place rather than producing
duplicates — and the same email seen in two mailboxes (e.g. Ben → Brantley)
collapses to one doc.

Run from project root:
    python3 scripts/gmail_backfill.py --days 30 --dry-run
    python3 scripts/gmail_backfill.py --days 30
    python3 scripts/gmail_backfill.py --mailbox jackson --days 90
"""

import argparse
import base64
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_KEY = "propagentic-crm-d76880ca6b63.json"  # Gmail readonly watcher
FIREBASE_KEY = "propagentic-crm-firebase-adminsdk-fbsvc-b1b48509ba.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MAILBOXES = [
    ("jackson",  "jackson@propagenticai.com"),
    ("ben",      "ben@propagenticai.com"),
    ("brantley", "brantley@propagenticai.com"),
    ("brian",    "brian@propagenticai.com"),
    ("zach",     "zach@propagenticai.com"),
]
TEAM_DOMAINS = {"propagenticai.com"}

MAX_BODY_BYTES = 4096  # truncate body_text per message


def normalize_email(addr: str) -> str:
    if not addr:
        return ""
    m = re.search(r"<([^>]+)>", addr)
    if m:
        addr = m.group(1)
    return addr.strip().lower()


def extract_display_name(addr: str) -> str:
    if not addr:
        return ""
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', addr)
    return m.group(1).strip() if m else ""


def parse_header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_email_list(field_value):
    if not field_value:
        return []
    out = []
    # Split on commas not inside quotes — good enough for header fields
    for part in re.split(r",\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", field_value):
        addr = normalize_email(part)
        if addr:
            out.append(addr)
    return out


def extract_body_text(payload) -> str:
    """Walk the MIME tree, prefer text/plain, fall back to text/html stripped."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if mime == "text/plain" and data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    # Walk parts, preferring plaintext
    parts = payload.get("parts", []) or []
    # First pass: find text/plain anywhere
    for p in parts:
        text = extract_body_text(p)
        if text and p.get("mimeType", "").startswith("text/plain"):
            return text
    # Second pass: anything that returns content
    for p in parts:
        text = extract_body_text(p)
        if text:
            # crude HTML strip if needed
            if "<" in text and ">" in text:
                text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
                text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
                text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
                text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
            return text
    # text/html at the root, no parts
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


def has_attachments(payload) -> bool:
    if not payload:
        return False
    for p in payload.get("parts", []) or []:
        if p.get("filename"):
            return True
        if has_attachments(p):
            return True
    return False


def doc_id_for_message_id(rfc_message_id: str, fallback_gmail_id: str) -> str:
    """
    Produce a Firestore-safe doc ID. Prefer RFC 822 Message-ID (globally unique
    across mailboxes) so a single email seen in sender+recipient mailboxes
    dedupes to one doc. Fall back to Gmail's internal ID if Message-ID is missing.
    """
    raw = (rfc_message_id or "").strip().strip("<>")
    if not raw:
        return f"gmail__{fallback_gmail_id}"
    # Firestore disallows '/' in IDs; SHA1 the raw value to get a clean 40-char hex.
    return "mid__" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="Lookback window in days")
    ap.add_argument("--dry-run", action="store_true", help="No writes; print summary only")
    ap.add_argument("--mailbox", help="Process only this mailbox label")
    ap.add_argument("--limit", type=int, default=2000, help="Cap messages per mailbox")
    args = ap.parse_args()

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_KEY))
    db = firestore.client()

    print(f"Loading leads from Firestore...", file=sys.stderr)
    email_to_lead_ids = {}
    n_leads = 0
    for d in db.collection("leads").stream():
        n_leads += 1
        email = (d.to_dict().get("email") or "").strip().lower()
        if email:
            email_to_lead_ids.setdefault(email, []).append(d.id)
    print(f"  {n_leads} leads, {len(email_to_lead_ids)} unique emails on file", file=sys.stderr)

    sa_creds = service_account.Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())
    gmail_query = f"after:{after_ts}"

    total_seen = 0
    total_matched = 0
    leads_touched = set()
    per_mailbox = {}
    docs_written = set()  # dedupe within this run too

    for label, mailbox_email in MAILBOXES:
        if args.mailbox and args.mailbox != label:
            continue
        print(f"\n=== {label} ({mailbox_email}) ===", file=sys.stderr)
        delegated = sa_creds.with_subject(mailbox_email)
        gmail = build("gmail", "v1", credentials=delegated, cache_discovery=False)

        # Page through message IDs
        msg_ids = []
        page_token = None
        while True:
            resp = gmail.users().messages().list(
                userId="me", q=gmail_query, maxResults=500, pageToken=page_token
            ).execute()
            msg_ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(msg_ids) >= args.limit:
                break
        msg_ids = msg_ids[: args.limit]
        print(f"  {len(msg_ids)} candidate messages in last {args.days}d", file=sys.stderr)

        mailbox_matched = 0
        for mid in msg_ids:
            total_seen += 1
            try:
                msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
            except Exception as e:
                print(f"  skip {mid}: {type(e).__name__}", file=sys.stderr)
                continue

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            from_raw = parse_header(headers, "From")
            to_raw = parse_header(headers, "To")
            cc_raw = parse_header(headers, "Cc")
            subject = parse_header(headers, "Subject") or "(no subject)"
            rfc_msgid = parse_header(headers, "Message-ID") or parse_header(headers, "Message-Id")

            from_email = normalize_email(from_raw)
            from_name = extract_display_name(from_raw)
            to_emails = parse_email_list(to_raw)
            cc_emails = parse_email_list(cc_raw)

            participants = {from_email} | set(to_emails) | set(cc_emails)
            participants.discard("")
            matched = []
            for addr in participants:
                if addr in email_to_lead_ids:
                    matched.extend(email_to_lead_ids[addr])
            matched = sorted(set(matched))
            if not matched:
                continue

            total_matched += 1
            mailbox_matched += 1

            # Direction: inbound only when the sender is actually a known CRM
            # lead. Anything else (team work mail, team student mail, third-party
            # cc's, etc.) is treated as outbound. This avoids false-positive
            # "responded" flags when e.g. Ben emails a lead from his wfu.edu
            # account — sender is non-team-domain but also not a lead.
            from_is_lead = from_email in email_to_lead_ids
            direction = "inbound" if from_is_lead else "outbound"
            sent_at = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc)
            snippet = (msg.get("snippet") or "").strip()
            body = extract_body_text(payload)
            if len(body.encode("utf-8")) > MAX_BODY_BYTES:
                body = body.encode("utf-8")[:MAX_BODY_BYTES].decode("utf-8", errors="ignore") + "…"

            doc_id = doc_id_for_message_id(rfc_msgid, mid)
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
                "has_attachments": has_attachments(payload),
                "sent_at": sent_at,
                "ingested_at": firestore.SERVER_TIMESTAMP,
                "matched_lead_ids": matched,
                "direction": direction,
                "source_mailbox": mailbox_email,
                "source_label": label.capitalize(),
            }

            if args.dry_run:
                print(
                    f"  [DRY] {direction:8s} {from_email[:40]:40s} → {(to_emails[:1] or [''])[0][:40]:40s} · {subject[:60]} · leads={matched}",
                    file=sys.stderr,
                )
                docs_written.add(doc_id)
                continue

            db.collection("emailActivity").document(doc_id).set(doc, merge=True)
            docs_written.add(doc_id)

            # Update each matched lead's email-related flags.
            for lid in matched:
                update = {"crm_emailed": True, "crm_emailed_at": firestore.SERVER_TIMESTAMP}
                if direction == "inbound":
                    update["crm_email_outcome"] = "responded"
                db.collection("leads").document(lid).set(update, merge=True)
                leads_touched.add(lid)

        per_mailbox[label] = (len(msg_ids), mailbox_matched)
        print(f"  matched {mailbox_matched} of {len(msg_ids)}", file=sys.stderr)

    print(f"\n=== Summary ===", file=sys.stderr)
    for label, (seen, matched) in per_mailbox.items():
        print(f"  {label:10s}: {matched:5d} matched / {seen:5d} seen", file=sys.stderr)
    print(
        f"\n  total:       {total_matched} matched of {total_seen} seen"
        f"\n  unique docs: {len(docs_written)}"
        f"\n  leads updated: {len(leads_touched)}"
        f"\n  mode: {'DRY RUN (no writes)' if args.dry_run else 'WROTE TO FIRESTORE'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
