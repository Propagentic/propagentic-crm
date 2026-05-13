"""Owner name and address normalization for dedup matching.

Lifted from CLAUDE.md "Normalization" section. The reference test cases in the
spec must all collapse to identical keys — see tests/test_normalize.py.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

ENTITY_SUFFIXES = [
    "LLC", "L.L.C.", "L L C", "LLLP", "LLP", "PLLC", "PC",
    "INC", "INCORPORATED", "INC.", "CORP", "CORPORATION", "CORP.",
    "LP", "L.P.", "LTD", "LTD.", "LIMITED",
    "CO", "COMPANY", "CO.",
]
TRUST_QUALIFIERS = [
    "REVOCABLE", "IRREVOCABLE", "LIVING", "FAMILY", "TESTAMENTARY",
    "GST EXEMPT", "GENERATION SKIPPING", "MARITAL", "BYPASS",
    "CHARITABLE", "GRANTOR", "QTIP", "TRUSTEE", "TRUST",
]
PERSON_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "ESQ", "MD", "PHD"}
DIRECTIONALS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
STREET_TYPES = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "TERRACE": "TER", "CIRCLE": "CIR",
    "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TRAIL": "TRL",
    "WAY": "WAY", "SQUARE": "SQ", "PLAZA": "PLZ",
    "EXPRESSWAY": "EXPY", "FREEWAY": "FWY",
}
UNIT_TYPES = {"APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT", "#": "UNIT"}


def normalize_name(name: Optional[str]) -> str:
    """Produce a normalized owner-name key for dedup matching.

    Example outputs (all members of a group produce the same key):
      'COOKEVILLE COMMONS, LP'      -> 'COMMONS COOKEVILLE'
      'COOKEVILLE COMMONS, L.P.'    -> 'COMMONS COOKEVILLE'
      'COOKEVILLE COMMONS L. P.'    -> 'COMMONS COOKEVILLE'

      'MIDTOWN REALTY LLC'          -> 'MIDTOWN REALTY'
      'MIDTOWN REALTY, LLC'         -> 'MIDTOWN REALTY'
      'MIDTOWN REALTY L.L.C.'       -> 'MIDTOWN REALTY'

      'SMITH JOHN J'                -> 'J JOHN SMITH'
      'SMITH JOHN J.'               -> 'J JOHN SMITH'

      'BARRETT, PAMELA KAYE REVOCABLE TRUST' -> 'BARRETT KAYE PAMELA'
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.upper().strip()

    # Compact dotted single-letter acronyms BEFORE punctuation removal:
    #   'L.L.C.' / 'L. L. C.' -> 'LLC'
    #   'L.P.'   / 'L. P.'    -> 'LP'
    # IMPORTANT: use (?!\w) instead of \b at the trailing edge. \b does NOT
    # match between two non-word chars (e.g. between '.' and end-of-string),
    # so the dotted form at end of string would otherwise slip through.
    s = re.sub(r"\b([A-Z])\.\s*([A-Z])\.\s*([A-Z])\.(?!\w)", r"\1\2\3", s)
    s = re.sub(r"\b([A-Z])\.\s*([A-Z])\.(?!\w)", r"\1\2", s)

    s = re.sub(r"[.,'\";:!?]", " ", s)
    s = re.sub(r"\s+", " ", s)
    for term in ENTITY_SUFFIXES + TRUST_QUALIFIERS:
        s = re.sub(rf"\b{re.escape(term)}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = [t for t in s.split() if t not in PERSON_SUFFIXES and t != "&" and t != "AND"]
    return " ".join(sorted(toks))


def normalize_address(addr: Optional[str], zip_code: Optional[str] = None) -> str:
    """Produce a normalized address key for dedup matching.

    Example outputs:
      '123 N MAIN ST APT 4B', zip='37027'   -> '123 N MAIN ST APT 4B 37027'
      '123 NORTH MAIN STREET #4B', '37027'  -> '123 N MAIN ST UNIT 4B 37027'
      'P.O. BOX 100', '37027'               -> 'PO BOX 100 37027'
    """
    if not addr:
        return ""
    s = unicodedata.normalize("NFKD", addr).encode("ascii", "ignore").decode()
    s = s.upper().strip()
    s = re.sub(r"\bP\.?\s?O\.?\s?BOX\b", "PO BOX", s)
    s = re.sub(r"\bPOBOX\b", "PO BOX", s)
    s = re.sub(r"#", " UNIT ", s)
    s = re.sub(r"[.,'\";:!?]", " ", s)
    s = re.sub(r"\s+", " ", s)
    toks = s.split()
    out = []
    for t in toks:
        out.append(DIRECTIONALS.get(t, STREET_TYPES.get(t, UNIT_TYPES.get(t, t))))
    s = " ".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    if zip_code:
        z = re.sub(r"[^0-9]", "", str(zip_code))[:5]
        if z:
            s = f"{s} {z}"
    return s


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity on whitespace-split tokens. 0..1, 1 = identical sets."""
    if not a or not b:
        return 0.0
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
