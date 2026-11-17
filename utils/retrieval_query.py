"""Build the text that is embedded to search the regulatory corpus.

The review used to search with its whole instruction text followed by the
fenced transaction record. Most of that text is the same for every case, so
the same passages came back whatever the transaction was.

The query built here says what the case is about instead: the typologies its
rule hits point to, the regulatory topics that go with each, and a few facts
(amount band, counterparty jurisdiction, unidentified parties).

Nothing a counterparty wrote is copied into it. Party names and the
transaction id are left out, and the jurisdiction appears only when it
resolves to a known country, under that country's own name. A hostile field
therefore cannot steer the search. Pure Python: no model, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Optional, Tuple

import config
from utils.jurisdictions import country_name, resolve_country
from utils.rule_ids import rule_ids

# Rule id -> (typology, regulatory topics to look for). Rules that only add a
# note (unresolved jurisdiction, self-transfer, non-cash channel) have no entry.
TYPOLOGIES = {
    "structuring_band": (
        "structuring",
        "payments split or kept just below a reporting or customer due diligence threshold, "
        "linked transactions, reporting suspicious and attempted transactions whatever the amount",
    ),
    "reporting_threshold": (
        "large-value transaction",
        "occasional transactions above the designated threshold, customer due diligence, "
        "examining the background and purpose of unusually large transactions, "
        "large cash transaction reporting",
    ),
    "high_risk_country": (
        "higher-risk jurisdiction",
        "enhanced due diligence and countermeasures for transactions with persons from "
        "higher-risk countries, country and geographic risk factors",
    ),
    "secrecy_jurisdiction": (
        "opaque ownership in a secrecy jurisdiction",
        "beneficial ownership and control of legal persons, shell companies, nominee "
        "shareholders and directors, bearer shares, complex ownership structures",
    ),
    "sanctions_name": (
        "sanctions evasion",
        "targeted financial sanctions, freezing funds and prohibiting transactions with "
        "designated persons and entities, reporting attempted transactions",
    ),
    "unidentified_parties": (
        "unidentified counterparties",
        "missing originator or beneficiary information on a payment, anonymous transactions, "
        "identifying the customer and the beneficial owner",
    ),
}

# Used when no rule with a typology fired (a row forwarded by the behaviour
# model alone): the general monitoring duties, since nothing narrower is known.
GENERAL_TOPICS = (
    "ongoing customer due diligence, whether a transaction is consistent with the customer's "
    "business and risk profile, unusual transactions with no apparent economic or lawful "
    "purpose, reporting of suspicious transactions"
)

MAX_QUERY_CHARS = 1600


@dataclass(frozen=True)
class RetrievalQuery:
    text: str                     # what gets embedded
    rule_ids: Tuple[str, ...]     # every rule behind the reasons, notes included
    typologies: Tuple[str, ...]   # from TYPOLOGIES, in rule order
    facts: Tuple[str, ...]


def _is_placeholder(name: object) -> bool:
    """A blank or placeholder name; None means the field was not supplied."""
    return name is not None and str(name).strip().lower() in config.PLACEHOLDER_ENTITIES


def _amount_fact(amount: object) -> Optional[str]:
    try:
        magnitude = abs(Decimal(str(amount)))
    except (InvalidOperation, ValueError):
        return None
    if not magnitude.is_finite():
        return None
    if magnitude >= config.REPORTING_THRESHOLD:
        return "amount at or above the reporting threshold"
    if magnitude >= config.STRUCTURING_FLOOR:
        return "amount just below the reporting threshold"
    return "amount below the reporting threshold"


def _jurisdiction_fact(country: object, ids: List[str]) -> Optional[str]:
    name = country_name(resolve_country(country))
    if name is None:
        return None
    if "high_risk_country" in ids:
        return f"counterparty jurisdiction {name}, on the high-risk list"
    if "secrecy_jurisdiction" in ids:
        return f"counterparty jurisdiction {name}, a secrecy jurisdiction"
    return f"counterparty jurisdiction {name}"


def build_retrieval_query(
    *,
    amount: object = None,
    country: object = "",
    sender: object = None,
    receiver: object = None,
    rule_reasons: Optional[Iterable[str]] = None,
) -> RetrievalQuery:
    """Query for one transaction, from its rule hits and key facts.

    Takes the same fields the review receives. The same inputs always give
    the same text.
    """
    ids = rule_ids(rule_reasons or [])
    matched = [rid for rid in ids if rid in TYPOLOGIES]
    facts = _facts(amount, country, sender, receiver, ids)
    lead = None if matched else "AML/CFT requirements for monitoring a payment that no rule flagged: "
    return RetrievalQuery(text=_compose(matched, lead, facts), rule_ids=tuple(ids),
                          typologies=tuple(TYPOLOGIES[rid][0] for rid in matched), facts=facts)


# Topic id for the general monitoring duties, next to the TYPOLOGIES keys.
GENERAL_TOPIC = "general_monitoring"
TOPIC_IDS = tuple(TYPOLOGIES) + (GENERAL_TOPIC,)


def build_topic_query(
    topics: Iterable[str],
    *,
    amount: object = None,
    country: object = "",
    sender: object = None,
    receiver: object = None,
    rule_reasons: Optional[Iterable[str]] = None,
) -> RetrievalQuery:
    """Query for chosen topics, with the same facts build_retrieval_query states.

    `topics` are ids from TOPIC_IDS, chosen by the caller instead of read off
    the rule hits. The facts still come from the transaction and its rule
    hits, so a topic choice cannot add a fact that is not true (choosing
    sanctions_name does not claim a sanctions match). Party names stay out of
    the text, as above. An unknown topic id raises ValueError.
    """
    chosen = list(dict.fromkeys(topics or []))
    unknown = [t for t in chosen if t not in TOPIC_IDS]
    if unknown:
        raise ValueError(f"Unknown topic id(s): {', '.join(map(str, unknown))}")
    matched = [t for t in TYPOLOGIES if t in chosen]
    ids = rule_ids(rule_reasons or [])
    facts = _facts(amount, country, sender, receiver, ids)
    if matched:
        lead = None
    elif any(rid in TYPOLOGIES for rid in ids):
        lead = "AML/CFT requirements for monitoring a payment: "
    else:
        lead = "AML/CFT requirements for monitoring a payment that no rule flagged: "
    text = _compose(matched, lead, facts, also_general=bool(matched) and GENERAL_TOPIC in chosen)
    return RetrievalQuery(text=text, rule_ids=tuple(ids),
                          typologies=tuple(TYPOLOGIES[t][0] for t in matched), facts=facts)


def _facts(amount: object, country: object, sender: object, receiver: object,
           ids: List[str]) -> Tuple[str, ...]:
    facts = [_amount_fact(amount), _jurisdiction_fact(country, ids)]
    if "sanctions_name" in ids:
        facts.append("a party name matches a sanctions list entry")
    if _is_placeholder(sender):
        facts.append("originator not identified")
    if _is_placeholder(receiver):
        facts.append("beneficiary not identified")
    return tuple(fact for fact in facts if fact)


def _compose(typology_ids: List[str], lead: Optional[str], facts: Tuple[str, ...],
             also_general: bool = False) -> str:
    """Query text: typologies and their topics, or the general duties after `lead`."""
    if typology_ids:
        topics = [TYPOLOGIES[t][1] for t in typology_ids] + ([GENERAL_TOPICS] if also_general else [])
        parts = [
            "AML/CFT requirements and risk indicators for a payment showing: "
            + "; ".join(TYPOLOGIES[t][0] for t in typology_ids) + ".",
            "Topics: " + "; ".join(topics) + ".",
        ]
    else:
        parts = [(lead or "") + GENERAL_TOPICS + "."]
    if facts:
        parts.append("Facts: " + "; ".join(facts) + ".")

    text = " ".join(parts)
    if len(text) > MAX_QUERY_CHARS:
        text = text[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return text
