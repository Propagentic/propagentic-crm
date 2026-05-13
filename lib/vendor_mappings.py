"""Skip-trace vendor column mappings.

Each entry maps a vendor's column-header names → the unified schema field.
Used by both the browser-side import UI and any CLI/Store-based import
to auto-detect which vendor a file came from and pre-populate the
column-mapping UI.

Header names are matched case-insensitively with whitespace and underscores
normalized (so "Mailing Address", "mailing_address", "MAILING_ADDRESS" all match).
"""
from __future__ import annotations

# fields:
#   owner_name        — required for matching
#   mailing_addr      — line 1 of the owner's mailing address
#   mailing_city, mailing_state, mailing_zip
#   phone1..3         — phone numbers (in order of vendor confidence)
#   phone1_type, phone1_confidence, phone1_dnc, phone1_source
#   email1..2         — email addresses
#   email1_confidence, email1_source
#   source_url        — vendor's source link (if provided)
#   source_label      — defaults to vendor key
SKIPTRACE_VENDORS: dict[str, dict[str, list[str]]] = {
    "batchdata": {
        "owner_name":    ["full_name", "owner_name", "fullname", "owner", "name"],
        "mailing_addr":  ["mailing_address", "mail_address_line_1", "address_line_1", "address1"],
        "mailing_city":  ["mailing_city", "mail_city", "city"],
        "mailing_state": ["mailing_state", "state"],
        "mailing_zip":   ["mailing_zip", "mail_zip", "zip", "postal_code"],
        "phone1":        ["phone_1", "phone1", "primary_phone", "best_phone"],
        "phone1_type":   ["phone_1_type", "phone1_type"],
        "phone1_dnc":    ["phone_1_dnc", "phone1_dnc", "dnc"],
        "phone2":        ["phone_2", "phone2"],
        "phone3":        ["phone_3", "phone3"],
        "email1":        ["email_1", "email1", "primary_email", "email"],
        "email2":        ["email_2", "email2"],
    },
    "tracerfy": {
        "owner_name":    ["owner_name", "full_name", "name"],
        "mailing_addr":  ["mailing_address", "address1"],
        "mailing_city":  ["mailing_city"],
        "mailing_state": ["mailing_state"],
        "mailing_zip":   ["mailing_zip"],
        "phone1":        ["phone_1", "phone1"],
        "phone2":        ["phone_2", "phone2"],
        "email1":        ["email_1", "email1"],
    },
    "reiskip": {
        "owner_name":    ["owner_first_last", "full_name", "owner_name"],
        "mailing_addr":  ["mailing_street", "address"],
        "mailing_city":  ["mailing_city"],
        "mailing_state": ["mailing_state"],
        "mailing_zip":   ["mailing_zip"],
        "phone1":        ["phone_number", "mobile_1", "phone1"],
        "phone2":        ["mobile_2", "phone2"],
        "email1":        ["email_address", "email1"],
    },
    "datazapp": {
        "owner_name":    ["fullname", "full_name", "owner_name"],
        "mailing_addr":  ["mailing_address", "address"],
        "mailing_city":  ["city"],
        "mailing_state": ["state"],
        "mailing_zip":   ["zip"],
        "phone1":        ["phone1", "cell_phone", "landline_phone"],
        "phone2":        ["phone2", "additional_phone"],
        "email1":        ["email1", "email"],
    },
}


def _norm_header(h: str) -> str:
    """Normalize a header for matching: lower, strip, replace separators with underscore."""
    if not h:
        return ""
    s = h.strip().lower()
    s = s.replace(" ", "_").replace("-", "_").replace(".", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def detect_vendor(headers: list[str]) -> str | None:
    """Score each vendor by how many of its 'owner_name' aliases match.
    Then break ties by total fields matched. Returns vendor key or None."""
    norm = [_norm_header(h) for h in headers]
    norm_set = set(norm)
    scores: dict[str, tuple[int, int]] = {}
    for vendor, fields in SKIPTRACE_VENDORS.items():
        # required: owner_name match
        on_aliases = [_norm_header(a) for a in fields.get("owner_name", [])]
        if not any(a in norm_set for a in on_aliases):
            continue
        total = sum(
            1 for aliases in fields.values()
              for a in aliases
              if _norm_header(a) in norm_set
        )
        scores[vendor] = (1, total)
    if not scores:
        return None
    return max(scores.items(), key=lambda kv: kv[1])[0]


def auto_map_columns(headers: list[str]) -> tuple[str | None, dict[str, str | None]]:
    """Pick the best vendor and produce a {schema_field: source_header} map.

    Returns (vendor_or_None, mapping). Schema fields with no detected source
    header have value None in the mapping. The vendor key is provided so the
    UI can label the file ('Detected BatchData export — accept defaults?').
    """
    vendor = detect_vendor(headers)
    header_norm = {h: _norm_header(h) for h in headers}
    norm_to_orig = {v: k for k, v in header_norm.items()}
    mapping: dict[str, str | None] = {}
    fields_to_map = [
        "owner_name", "mailing_addr", "mailing_city", "mailing_state", "mailing_zip",
        "phone1", "phone1_type", "phone1_dnc", "phone2", "phone3",
        "email1", "email2", "source_url",
    ]
    if vendor:
        vfields = SKIPTRACE_VENDORS[vendor]
        for f in fields_to_map:
            for alias in vfields.get(f, []):
                if _norm_header(alias) in norm_to_orig:
                    mapping[f] = norm_to_orig[_norm_header(alias)]
                    break
            mapping.setdefault(f, None)
    else:
        for f in fields_to_map:
            mapping[f] = None
    return vendor, mapping
