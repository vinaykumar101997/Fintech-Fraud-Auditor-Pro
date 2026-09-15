"""Verdict vocabulary, deliberately free of third-party imports.

ERROR is a first-class verdict. The original build returned an error string
that failed a keyword check and was then recorded as CLEAR, which meant an API
outage produced a clean audit run. Nothing in this codebase may map a failure
to CLEAR.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Verdict(str, Enum):
    SUSPICIOUS = "SUSPICIOUS"
    CLEAR = "CLEAR"
    ERROR = "ERROR"

    @property
    def needs_attention(self) -> bool:
        """True for anything an analyst must look at. ERROR is never silently cleared."""
        return self in (Verdict.SUSPICIOUS, Verdict.ERROR)


@dataclass
class AuditOutcome:
    """Result of a Tier 2 audit. Always carries a verdict, never a bare string."""

    verdict: Verdict
    rationale: str
    confidence: float = 0.0
    typology: Optional[str] = None
    initial_finding: str = ""
    reviewer_note: str = ""
    error_detail: Optional[str] = None

    @classmethod
    def failure(cls, detail: str) -> "AuditOutcome":
        return cls(
            verdict=Verdict.ERROR,
            rationale="Automated analysis did not complete. Manual review required.",
            confidence=0.0,
            error_detail=detail,
        )


@dataclass
class RuleResult:
    """Outcome of the deterministic Tier 1 pass."""

    flagged: bool = False
    reasons: List[str] = field(default_factory=list)
    force_review: bool = False  # bypasses the statistical funnel entirely
