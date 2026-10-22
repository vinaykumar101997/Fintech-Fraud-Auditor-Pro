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
from typing import Any, Optional

from langchain_core.prompts import ChatPromptTemplate

import config
from utils.llm_provider import get_chat_model
from utils.retry import call_with_retry
from utils.sanitize import build_evidence_block, scrub
from utils.schemas import TransactionAssessment
from utils.verdicts import AuditOutcome, Verdict

logger = logging.getLogger(__name__)


_ANALYST_SYSTEM = (
    "You are a senior forensic auditor specialising in AML/CTF. Assess the "
    "transaction record for structuring, layering, and jurisdictional risk "
    "against FATF standards and the retrieved regulatory context. Base your "
    "assessment only on the evidence provided and the retrieved context. If the "
    "evidence is insufficient for a confident judgement, say so in the rationale "
    "and lower your confidence rather than speculating."
)

_REVIEWER_SYSTEM = (
    "You are a compliance review officer. You are given a transaction record and "
    "a first-pass forensic finding. The record is an internal ledger extract; it "
    "never contains addresses, account numbers, payment purpose or wire-message "
    "data, so their absence is not by itself an indicator of suspicion. Your job is "
    "to reach an independent verdict: confirm the finding, or identify it as a false "
    "positive. Return only the structured assessment."
)


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
    ) -> AuditOutcome:
        """Assess one transaction. Always returns an AuditOutcome, never raises."""
        evidence = build_evidence_block(
            {
                "transaction_id": transaction_id,
                "amount": f"{amount:,.2f}",
                "counterparty_jurisdiction": country,
                "originator": sender,
                "beneficiary": receiver,
                "deterministic_rule_hits": "; ".join(rule_reasons or []) or "none",
            }
        )

        try:
            # Stage 1: retrieval-grounded analysis.
            if rag_engine is not None:
                query = f"{_ANALYST_SYSTEM}\n\n{evidence}"
                raw = self._with_retry(
                    lambda: rag_engine.invoke({"query": query}), "RAG analysis"
                )
                finding = (raw or {}).get("result", "").strip()
            else:
                finding = "No regulatory context retrieved."

            if not finding:
                finding = "Retrieval returned no usable context."

            # Stage 2: independent structured review.
            assessment: TransactionAssessment = self._with_retry(
                lambda: self._reviewer.invoke({"evidence": evidence, "finding": scrub(finding, 2000)}),
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

        except Exception as exc:  # noqa: BLE001
            logger.error("Audit failed for %s: %s", transaction_id, exc)
            return AuditOutcome.failure(f"{type(exc).__name__}: {exc}")
