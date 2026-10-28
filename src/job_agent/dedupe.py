"""De-duplication of roles.

Two listings are the same role when either of these holds:

1. their canonical URLs are equal; or
2. their normalised company and normalised title are equal, and their
   normalised locations are equal or one of them is blank.

The second rule is what joins the same role seen on two sites. A role the
tracker already holds counts as known whatever its status, so a rejected or
withdrawn role is not added again. Rows are never deleted or merged: a
duplicate is reported and dropped, and the existing row is left alone.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "mc_cid", "mc_eid", "ref", "ref_src", "referrer",
    "source", "src", "campaign", "cmp", "trk", "trackingid", "refid", "from", "fromsearch",
}  # fmt: skip
COMPANY_SUFFIXES = {"ltd", "limited", "plc", "llp", "llc", "inc", "incorporated", "gmbh", "uk", "corp", "co"}
LOCATION_NOISE = {"uk", "united", "kingdom", "england", "scotland", "wales", "greater", "hybrid", "remote", "or", "and"}
TITLE_SYNONYMS = {"grad": "graduate", "jr": "junior", "sr": "senior", "intern": "internship"}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace("&", " and ")


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text).split()


def canonical_url(url: str) -> str:
    """Lower-case host, https, no fragment, no tracking parameters, sorted query."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url if "//" in url else "")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return ""
    host = host.removeprefix("www.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/")
    port = f":{parts.port}" if parts.port not in (None, 80, 443) else ""
    result = f"https://{host}{port}{path}"
    if query:
        result += "?" + urlencode(sorted(query))
    return result


def normalise_company(name: str) -> str:
    words = _words(_fold(name))
    while len(words) > 1 and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalise_title(title: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", _fold(title))
    words = [TITLE_SYNONYMS.get(w, w) for w in _words(text)]
    return " ".join(words)


def normalise_location(location: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", _fold(location))
    places = []
    for chunk in re.split(r"[/,;]", text):
        words = [w for w in _words(chunk) if w not in LOCATION_NOISE]
        if words:
            places.append(" ".join(words))
    return "; ".join(sorted(places))


@dataclass(frozen=True)
class Duplicate:
    candidate: Mapping[str, str]
    existing_id: str


class DedupeIndex:
    def __init__(self, rows: Iterable[Mapping[str, str]] = ()):
        self._by_url: dict[str, str] = {}
        self._by_role: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in rows:
            self.add(row["id"], row)

    def add(self, row_id: str, row: Mapping[str, str]) -> None:
        url = canonical_url(row.get("link", ""))
        if url:
            self._by_url.setdefault(url, row_id)
        company, title = normalise_company(row.get("company", "")), normalise_title(row.get("role", ""))
        if company and title:
            self._by_role.setdefault((company, title), []).append((normalise_location(row.get("location", "")), row_id))

    def find(self, row: Mapping[str, str]) -> str | None:
        url = canonical_url(row.get("link", ""))
        if url and url in self._by_url:
            return self._by_url[url]
        company, title = normalise_company(row.get("company", "")), normalise_title(row.get("role", ""))
        if not (company and title):
            return None
        location = normalise_location(row.get("location", ""))
        for known_location, row_id in self._by_role.get((company, title), []):
            if not location or not known_location or location == known_location:
                return row_id
        return None


def add_new_rows(tx, candidates: Iterable[Mapping[str, str]]) -> tuple[list[str], list[Duplicate]]:
    """Add each candidate that is not already in the tracker.

    ``tx`` is an open ``Tracker.edit()`` transaction. Returns the new row ids
    and the candidates that were dropped as duplicates.
    """
    index = DedupeIndex(tx.rows)
    added: list[str] = []
    duplicates: list[Duplicate] = []
    for candidate in candidates:
        existing = index.find(candidate)
        if existing is not None:
            duplicates.append(Duplicate(candidate, existing))
            continue
        row_id = tx.add_row(dict(candidate))
        index.add(row_id, candidate)
        added.append(row_id)
    return added, duplicates
