"""Resolve free-text country values to ISO 3166-1 alpha-2 codes.

Matching is exact on a normalised form (case, accents and punctuation
ignored), never by substring: "Prussia" must not match "Russia", and
"Panamanian Way" must not match "Panama". The index holds every alpha-2,
alpha-3 and numeric code and every name pycountry knows, plus ALIASES for
common names it does not carry.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Dict, FrozenSet, Optional

import pycountry

# Everyday names and abbreviations that pycountry's names and codes miss.
# Keys are written in normalised form (see _normalise).
ALIASES: Dict[str, str] = {
    "russia": "RU",
    "russian fed": "RU",
    "dprk": "KP",
    "korea north": "KP",
    "korea dprk": "KP",
    "korea south": "KR",
    "republic of korea": "KR",
    "rok": "KR",
    "burma": "MM",
    "byelorussia": "BY",
    "belorussia": "BY",
    "persia": "IR",
    "uk": "GB",
    "great britain": "GB",
    "britain": "GB",
    "america": "US",
    "uae": "AE",
    "turkey": "TR",
    "ivory coast": "CI",
    "macedonia": "MK",
    "swaziland": "SZ",
    "cape verde": "CV",
    "east timor": "TL",
    "holland": "NL",
    "deutschland": "DE",
    "brunei": "BN",
    "macau": "MO",
    "vatican": "VA",
    "vatican city": "VA",
    "palestine": "PS",
    "micronesia": "FM",
    "drc": "CD",
    "dr congo": "CD",
    "democratic republic of the congo": "CD",
    "congo kinshasa": "CD",
    "congo brazzaville": "CG",
    "bvi": "VG",
}

# Values that mean "no country given" rather than an unknown country.
_EMPTY = {"", "nan", "none", "null", "n a", "na"}

_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def _normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value if value is not None else ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NON_WORD.sub(" ", text.casefold().replace("&", " and "))
    return _SPACES.sub(" ", text).strip()


@lru_cache(maxsize=1)
def _index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for country in pycountry.countries:
        for attr in ("alpha_2", "alpha_3", "numeric", "name", "official_name", "common_name"):
            value = getattr(country, attr, None)
            if value:
                index[_normalise(value)] = country.alpha_2
    index.update(ALIASES)
    return index


def is_blank(value: object) -> bool:
    """True when the value carries no country at all (empty, NaN, 'none')."""
    return _normalise(value) in _EMPTY


def resolve_country(value: object) -> Optional[str]:
    """Return the alpha-2 code for a country name or code, or None if unresolved."""
    key = _normalise(value)
    if key in _EMPTY:
        return None
    index = _index()
    if key in index:
        return index[key]
    if key.startswith("the "):
        return index.get(key[4:])
    return None


def parse_jurisdiction_list(raw: str) -> FrozenSet[str]:
    """Parse a comma-separated list of codes or names into alpha-2 codes.

    Raises ValueError naming any entry that does not resolve, so a typo in a
    configured list fails at startup instead of silently dropping a country.
    """
    codes, unknown = set(), []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        code = resolve_country(item)
        if code is None:
            unknown.append(item)
        else:
            codes.add(code)
    if unknown:
        raise ValueError(
            "Unrecognised jurisdiction(s): " + ", ".join(unknown)
            + ". Use ISO 3166-1 alpha-2 codes such as RU or KP."
        )
    return frozenset(codes)
