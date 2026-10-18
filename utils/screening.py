"""Sanctions and PEP screening.

Kept deterministic on purpose: an LLM must never be the thing that decides
whether a name is on a sanctions list.

Changes from the original build:
  - The list comes from data/sanctions_list.csv (with aliases) rather than six
    names hardcoded in the UI file. Refresh it from the OFAC SDN publication.
  - Names are normalised (accents, punctuation, corporate suffixes, ordering)
    before comparison, so "PUTIN, Vladimir" and "Vladimir Putin" match.
  - Each name is also compared with its suffixes kept, and the higher score
    counts, so a misspelt suffix ("Lazarus Groop") is not left as a stray
    name token that the stripped comparison penalises.
  - extractOne's None return is handled rather than unpacked blindly.
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

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
    full: str = ""


@dataclass(frozen=True)
class ScreeningHit:
    query: str
    matched_name: str
    score: float
    entry_type: str


def _tokens(raw: str) -> List[str]:
    text = unicodedata.normalize("NFKD", str(raw or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _PUNCT.sub(" ", text.lower()).split()


def normalize_name(raw: str) -> str:
    """Canonical form for comparison: accent-folded, depunctuated, sorted tokens."""
    tokens = [t for t in _tokens(raw) if t not in _SUFFIXES]
    # Sorting makes "Putin Vladimir" and "Vladimir Putin" identical.
    return " ".join(sorted(tokens))


def full_name(raw: str) -> str:
    """normalize_name with corporate suffixes kept."""
    return " ".join(sorted(_tokens(raw)))


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
                entries.append(WatchlistEntry(canonical, norm, entry_type, full_name(variant)))

    return tuple(entries), using_real_list


@lru_cache(maxsize=4)
def _choices(entries: Tuple[WatchlistEntry, ...]) -> Tuple[str, ...]:
    """Normalised watchlist names, built once per loaded list."""
    return tuple(e.normalized for e in entries)


@lru_cache(maxsize=4)
def _full_choices(entries: Tuple[WatchlistEntry, ...]) -> Tuple[str, ...]:
    """Watchlist names with suffixes kept, built once per loaded list."""
    return tuple(e.full for e in entries)


def _best_matches(names: Iterable[str], cutoff: float) -> Dict[str, Optional[ScreeningHit]]:
    """Best watchlist entry per name at or above cutoff, or None.

    Each distinct name is scored against the whole watchlist twice, suffixes
    stripped and kept, with one rapidfuzz cdist call each; an entry's score is
    the higher of the two. Ties go to the first watchlist entry. A name made
    only of suffixes ("The Group") is not screened.
    """
    from rapidfuzz import fuzz, process  # imported lazily so tests can run without it
    import numpy as np

    unique = list(dict.fromkeys(names))
    results: Dict[str, Optional[ScreeningHit]] = {name: None for name in unique}

    entries, _ = load_watchlist()
    by_query: Dict[Tuple[str, str], List[str]] = {}
    for name in unique:
        query = normalize_name(name)
        if query:
            by_query.setdefault((query, full_name(name)), []).append(name)
    if not entries or not by_query:
        return results

    queries = list(by_query)
    # float64 so scores compare exactly with the threshold.
    stripped = process.cdist([q for q, _ in queries], _choices(entries),
                             scorer=fuzz.token_sort_ratio, dtype=np.float64)
    kept = process.cdist([f for _, f in queries], _full_choices(entries),
                         scorer=fuzz.token_sort_ratio, dtype=np.float64)
    for query, row in zip(queries, np.maximum(stripped, kept)):
        index = int(row.argmax())
        score = float(row[index])
        if score < cutoff:
            continue
        entry = entries[index]
        for name in by_query[query]:
            results[name] = ScreeningHit(
                query=name, matched_name=entry.canonical, score=score, entry_type=entry.entry_type
            )
    return results


def screen(name: str, threshold: Optional[int] = None) -> Optional[ScreeningHit]:
    """Fuzzy-match a name against the watchlist. Returns None below threshold."""
    cutoff = config.SCREENING_MATCH_THRESHOLD if threshold is None else threshold
    return _best_matches([name], cutoff)[name]


def screen_many(
    names: Iterable[str], threshold: Optional[int] = None
) -> Dict[str, Optional[ScreeningHit]]:
    """Screen many names in one pass; the result for each name equals screen(name)."""
    cutoff = config.SCREENING_MATCH_THRESHOLD if threshold is None else threshold
    return _best_matches(names, cutoff)


def best_effort_match(name: str) -> Optional[Tuple[str, float]]:
    """Highest-scoring candidate regardless of threshold, for UI transparency."""
    hit = _best_matches([name], 0)[name]
    return (hit.matched_name, hit.score) if hit else None


class ScreeningUnavailable(RuntimeError):
    """Sanctions screening cannot run. Raised instead of reporting 'no match'."""


def _unavailable(exc: ImportError) -> ScreeningUnavailable:
    return ScreeningUnavailable(
        "Sanctions screening is unavailable: rapidfuzz is not installed. "
        "Run `pip install -r requirements.txt`. The batch cannot be audited "
        "without screening."
    )


def rule_screener(name: str) -> Optional[str]:
    """Adapter matching utils.rules.Screener.

    Fails closed: if the matcher cannot run, raise rather than return None,
    because None means "screened and clear" to the rules engine.
    """
    try:
        hit = screen(name)
    except ImportError as exc:
        raise _unavailable(exc) from exc
    return hit.matched_name if hit else None


def _rule_screen_many(names: Iterable[str]) -> Dict[str, Optional[str]]:
    """Bulk form of rule_screener, used by utils.rules.apply_rules. Fails closed too."""
    try:
        hits = screen_many(names)
    except ImportError as exc:
        raise _unavailable(exc) from exc
    return {name: (hit.matched_name if hit else None) for name, hit in hits.items()}


rule_screener.screen_many = _rule_screen_many  # type: ignore[attr-defined]


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
