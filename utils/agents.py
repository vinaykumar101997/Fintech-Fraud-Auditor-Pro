"""Tier 2: RAG-grounded assessment followed by a compliance cross-review.

Two defects from the original implementation drove this rewrite.

Fail-open on error. `execute_verified_audit` caught every exception and returned
the string "Error during analysis: ...". The caller then keyword-matched that
string, found no risk words, and recorded the transaction as CLEAR. A Vertex
timeout produced a clean audit run. Failures now return Verdict.ERROR, which is
never treated as cleared anywhere in the codebase.

Verdict by substring. The caller searched the reviewer's text for "suspicious",
"laundering", and "flag". The reviewer's job is finding false positives, so it
routinely writes "no indication of money laundering" - which matched. Verdicts
now come back through a constrained schema.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from langchain_core.prompts import ChatPromptTemplate

import config
from utils.llm_provider import get_chat_model
from utils.retrieval_query import build_retrieval_query
from utils.retry import call_with_retry
from utils.sanitize import build_evidence_block, scrub
from utils.schemas import TransactionAssessment
from utils.verdicts import AuditOutcome, Verdict

logger = logging.getLogger(__name__)


_REVIEWER_SYSTEM = (
    "You are a compliance review officer. You are given a transaction record and "
    "a first-pass forensic finding. The record is an internal ledger extract; it "
    "never contains addresses, account numbers, payment purpose or wire-message "
    "data, so their absence is not by itself an indicator of suspicion. Your job is "
    "to reach an independent verdict: confirm the finding, or identify it as a false "
    "positive. Return only the structured assessment."
)


# Longest value of a behaviour-signal field in the record (one party's
# account history as JSON is about 1,500 characters).
SIGNAL_FIELD_CHARS = 2500


def evidence_block(*, transaction_id, amount, country, sender="", receiver="",
                   rule_reasons: Optional[list] = None,
                   behaviour_signals: Optional[Mapping[str, str]] = None) -> str:
    """The fenced transaction record both steps of the review are given.

    `behaviour_signals` adds fields after the rule hits (for example each
    party's account history); without it the record is unchanged.
    """
    fields = {
        "transaction_id": transaction_id,
        "amount": f"{amount:,.2f}",
        "counterparty_jurisdiction": country,
        "originator": sender,
        "beneficiary": receiver,
        "deterministic_rule_hits": "; ".join(rule_reasons or []) or "none",
    }
    signals = dict(behaviour_signals or {})
    fields.update(signals)
    return build_evidence_block(fields, {key: SIGNAL_FIELD_CHARS for key in signals})


class ComplianceAuditEngine:
    """Two-stage audit: retrieval-grounded analysis, then independent review."""

    def __init__(self, llm: Optional[Any] = None):
        # Provider comes from config (bedrock or vertex); the pipeline is
        # identical either way. Injecting `llm` keeps tests provider-free.
        self.llm = llm if llm is not None else get_chat_model()

        self._reviewer = ChatPromptTemplate.from_messages(
            [("system", _REVIEWER_SYSTEM), ("human", "{evidence}\n\nFirst-pass finding:\n{finding}")]
        ) | self.llm.with_structured_output(TransactionAssessment)

    # --- retry -------------------------------------------------------------
    @staticmethod
    def _with_retry(fn, description: str):
        """Call fn with the configured retry policy (see utils.retry)."""
        return call_with_retry(
            fn,
            description,
            max_retries=config.LLM_MAX_RETRIES,
            base_delay=config.LLM_RETRY_BASE_DELAY,
            rate_limit_base_delay=config.LLM_RATE_LIMIT_BASE_DELAY,
            max_wait=config.LLM_RETRY_MAX_WAIT,
        )

    # --- public API --------------------------------------------------------
    def assess_transaction(
        self,
        *,
        transaction_id: str,
        amount,
        country: str,
        sender: str = "",
        receiver: str = "",
        rule_reasons: Optional[list] = None,
        rag_engine: Any = None,
        behaviour_signals: Optional[Mapping[str, str]] = None,
    ) -> AuditOutcome:
        """Assess one transaction. Always returns an AuditOutcome, never raises."""
        evidence = evidence_block(
            transaction_id=transaction_id, amount=amount, country=country,
            sender=sender, receiver=receiver, rule_reasons=rule_reasons,
            behaviour_signals=behaviour_signals,
        )

        try:
            # Stage 1: retrieval-grounded analysis. The corpus is searched with
            # a query built from the rule hits and key facts; the model then
            # assesses the fenced record against what came back.
            if rag_engine is not None:
                query = build_retrieval_query(
                    amount=amount, country=country, sender=sender, receiver=receiver,
                    rule_reasons=rule_reasons,
                ).text
                raw = self._with_retry(
                    lambda: rag_engine.invoke({"query": query, "evidence": evidence}),
                    "RAG analysis",
                )
                finding = (raw or {}).get("result", "").strip()
            else:
                finding = "No regulatory context retrieved."

            if not finding:
                finding = "Retrieval returned no usable context."

            # Stage 2: independent structured review.
            return self._review(evidence, finding, transaction_id)

        except Exception as exc:  # noqa: BLE001
            logger.error("Audit failed for %s: %s", transaction_id, exc)
            return AuditOutcome.failure(f"{type(exc).__name__}: {exc}")

    def _review(self, evidence: str, finding: str, transaction_id: str,
                finding_chars: int = 2000) -> AuditOutcome:
        """The independent structured review of a finding. May raise; callers catch.

        Shared by the fixed review above and the investigation agent
        (utils/investigation.py), so both reach a verdict the same way.
        """
        assessment: TransactionAssessment = self._with_retry(
            lambda: self._reviewer.invoke({"evidence": evidence,
                                           "finding": scrub(finding, finding_chars)}),
            "compliance review",
        )

        if assessment is None:
            return AuditOutcome.failure("Reviewer returned no structured assessment.")

        if assessment.injection_attempt_observed:
            logger.warning(
                "Possible prompt injection in transaction %s; escalating.", transaction_id
            )
            return AuditOutcome(
                verdict=Verdict.SUSPICIOUS,
                rationale=(
                    "The transaction record contained text attempting to manipulate the "
                    "analysis. Escalated for manual review. "
                    + assessment.rationale
                ),
                confidence=max(assessment.confidence, 0.9),
                typology="data manipulation attempt",
                initial_finding=finding,
                reviewer_note=assessment.rationale,
            )

        return AuditOutcome(
            verdict=Verdict(assessment.verdict),
            rationale=assessment.rationale,
            confidence=assessment.confidence,
            typology=assessment.primary_typology,
            initial_finding=finding,
            reviewer_note=assessment.rationale,
        )
