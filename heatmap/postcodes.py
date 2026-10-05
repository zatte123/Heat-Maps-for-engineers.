"""UK postcode helpers."""
from __future__ import annotations

import re

# Full UK postcode, with or without the space, anywhere in a string.
_FULL_RE = re.compile(
    r"\b(GIR ?0AA|[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKMNPR-Y]? ?[0-9][ABD-HJLNP-UW-Z]{2})\b",
    re.IGNORECASE,
)
_OUTCODE_RE = re.compile(r"^[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKMNPR-Y]?$", re.IGNORECASE)


def normalise(postcode: str | None) -> str | None:
    """'n3 1ab' / 'N31AB' -> 'N3 1AB'. Outcodes ('N3') are returned upper-cased.

    Returns None for blanks or anything that isn't postcode-shaped.
    """
    if postcode is None:
        return None
    compact = re.sub(r"\s+", "", str(postcode)).upper()
    if not compact:
        return None
    if _OUTCODE_RE.match(compact):
        return compact
    if len(compact) >= 5 and _FULL_RE.fullmatch(compact):
        return f"{compact[:-3]} {compact[-3:]}"
    return None


def extract(text: str | None) -> str | None:
    """Find the first full postcode inside free text such as an address."""
    if not text:
        return None
    match = _FULL_RE.search(str(text).upper())
    return normalise(match.group(1)) if match else None


def outcode(postcode: str | None) -> str | None:
    pc = normalise(postcode)
    return pc.split(" ")[0] if pc else None


def is_office(postcode: str | None, office_prefix: str) -> bool:
    """Same rule as the timekeeping project: any postcode starting with the prefix."""
    pc = normalise(postcode)
    if not pc or not office_prefix:
        return False
    return pc.replace(" ", "").startswith(office_prefix.replace(" ", "").upper())
