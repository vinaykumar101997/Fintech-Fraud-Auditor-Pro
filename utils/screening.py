"""Sanctions and PEP screening.

Kept deterministic on purpose: an LLM must never be the thing that decides
whether a name is on a sanctions list.

Changes from the original build:
  - The list comes from data/sanctions_list.csv (with aliases) rather than six
    names hardcoded in the UI file. Refresh it from the OFAC SDN publication.
  - Names are normalised (accents, punctuation, corporate suffixes, ordering)
    before comparison, so "PUTIN, Vladimir" and "Vladimir Putin" match.
  - extractOne's None return is handled rather than unpacked blindly.
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

import config

logger = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^\w\s]")
_SUFFIXES = {
    "ltd", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "plc", "gmbh", "sa", "ag", "bv", "nv", "pty", "limited", "group", "holdings",
    "trust", "foundation", "the", "and",
}

# Used only when data/sanctions_list.csv is absent. Not a substitute for the
# real publication; the app warns when it falls back to this.
FALLBACK_ENTRIES = [
    ("Vladimir Putin", "Vladimir Vladimirovich Putin|Putin, Vladimir", "PEP"),
    ("Kim Jong Un", "Kim Jong-un|Kim Jong Un", "PEP"),
    ("Lazarus Group", "APT38|Hidden Cobra|Guardians of Peace", "Entity"),
    ("Sinaloa Cartel", "Cartel de Sinaloa|CDS", "Entity"),
    ("Viktor Bout", "Viktor Anatolyevich Bout|Bout, Viktor", "Individual"),
    ("Tornado Cash", "TornadoCash", "Entity"),
]


@dataclass(frozen=True)
class WatchlistEntry:
    canonical: str
    normalized: str
    entry_type: str


@dataclass(frozen=True)
class ScreeningHit:
    query: str
    matched_name: str
    score: float
    entry_type: str


def normalize_name(raw: str) -> str:
    """Canonical form for comparison: accent-folded, depunctuated, sorted tokens."""
    text = unicodedata.normalize("NFKD", str(raw or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text.lower())
    tokens = [t for t in text.split() if t and t not in _SUFFIXES]
    # Sorting makes "Putin Vladimir" and "Vladimir Putin" identical.
    return " ".join(sorted(tokens))


@lru_cache(maxsize=1)
def load_watchlist() -> Tuple[Tuple[WatchlistEntry, ...], bool]:
    """Return (entries, using_real_list)."""
    rows: List[Tuple[str, str, str]] = []
    using_real_list = False

    if config.SANCTIONS_CSV.exists():
        try:
            with config.SANCTIONS_CSV.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    name = (row.get("name") or "").strip()
                    if name:
                        rows.append(
                            (name, (row.get("aliases") or "").strip(), (row.get("type") or "Unknown").strip())
                        )
            using_real_list = bool(rows)
        except Exception as exc:
            logger.error("Could not read sanctions list: %s", exc)

    if not rows:
        logger.warning("Falling back to the built-in demo watchlist.")
        rows = list(FALLBACK_ENTRIES)

    entries: List[WatchlistEntry] = []
    for canonical, aliases, entry_type in rows:
        variants = [canonical] + [a for a in aliases.split("|") if a.strip()]
        for variant in variants:
            norm = normalize_name(variant)
            if norm:
                entries.append(WatchlistEntry(canonical, norm, entry_type))

    return tuple(entries), using_real_list


def screen(name: str, threshold: Optional[int] = None) -> Optional[ScreeningHit]:
    """Fuzzy-match a name against the watchlist. Returns None below threshold."""
    from rapidfuzz import fuzz, process  # imported lazily so tests can run without it

    cutoff = config.SCREENING_MATCH_THRESHOLD if threshold is None else threshold
    query = normalize_name(name)
    if not query:
        return None

    entries, _ = load_watchlist()
    if not entries:
        return None

    choices: Sequence[str] = [e.normalized for e in entries]
    match = process.extractOne(query, choices, scorer=fuzz.token_sort_ratio, score_cutoff=cutoff)
    if match is None:
        return None

    _, score, index = match
    entry = entries[index]
    return ScreeningHit(
        query=name, matched_name=entry.canonical, score=float(score), entry_type=entry.entry_type
    )


def best_effort_match(name: str) -> Optional[Tuple[str, float]]:
    """Highest-scoring candidate regardless of threshold, for UI transparency."""
    from rapidfuzz import fuzz, process

    query = normalize_name(name)
    entries, _ = load_watchlist()
    if not query or not entries:
        return None
    match = process.extractOne(query, [e.normalized for e in entries], scorer=fuzz.token_sort_ratio)
    if match is None:
        return None
    _, score, index = match
    return entries[index].canonical, float(score)


def rule_screener(name: str) -> Optional[str]:
    """Adapter matching utils.rules.Screener."""
    try:
        hit = screen(name)
    except ImportError:
        return None
    return hit.matched_name if hit else None


FREEZE_DIRECTIVE_TEMPLATE = """\
COMPLIANCE DIRECTIVE - IMMEDIATE ACTION REQUIRED

Reference:      {reference}
Issued:         {issued_at} UTC
Screened name:  {query}
List match:     {matched_name} ({entry_type})
Match score:    {score:.1f}% (threshold {threshold}%)

1. Place an immediate hold on all pending and future transactions associated
   with the screened party pending manual adjudication.
2. Preserve all related records, correspondence, and onboarding documentation.
   Do not notify the customer of this hold or of any resulting filing.
3. Escalate to the nominated compliance officer for verification against the
   source list and for a filing determination within the applicable deadline.

This directive is generated from a deterministic name-matching rule. It is a
screening alert, not a confirmed identification, and requires human review
before any account action becomes permanent.
"""


def render_freeze_directive(hit: ScreeningHit, reference: str, issued_at: str) -> str:
    """Fixed template with slot filling.

    Deliberately not model-generated. A legal instruction assembled by an LLM is
    a hallucination surface in exactly the place that can least tolerate one.
    """
    return FREEZE_DIRECTIVE_TEMPLATE.format(
        reference=reference,
        issued_at=issued_at,
        query=hit.query,
        matched_name=hit.matched_name,
        entry_type=hit.entry_type,
        score=hit.score,
        threshold=config.SCREENING_MATCH_THRESHOLD,
    )
