"""Record of one investigation: every model call and tool call, in order.

The investigation loop (utils/investigation.py) appends a TraceStep for each
model call, each tool call and the final review, and hands each step to a
Tracer as it happens. The default tracer does nothing. An external tracing
backend plugs in either as a Tracer here or as a LangChain callback handler
passed in the run config, which sees the same model and tool calls.

Free of third-party imports, like utils/verdicts.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple


@dataclass
class TraceStep:
    kind: str                       # "model", "tool" or "review"
    name: str                       # model step label or tool name
    args: Dict[str, Any] = field(default_factory=dict)
    result_chars: int = 0           # length of what went back to the model
    result_digest: Optional[str] = None  # SHA-256 of that text, first 16 hex chars
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: Optional[str] = None


def billed_tokens(usage: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """(input, output) tokens of one model call as billed.

    Gemini reports reasoning ("thinking") tokens outside output_tokens, in
    output_token_details["reasoning"], and counts them in total_tokens; they
    are billed as output. Output is therefore taken as total minus input
    when the total is larger than input plus output.
    """
    usage = usage or {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0)
    return tokens_in, max(tokens_out, total - tokens_in)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Tracer(Protocol):
    def on_step(self, step: TraceStep) -> None: ...


class NullTracer:
    def on_step(self, step: TraceStep) -> None:
        return None


@dataclass
class Trace:
    steps: List[TraceStep] = field(default_factory=list)
    stop_reason: str = ""           # "answered", or why the loop was stopped

    def add(self, step: TraceStep, tracer: Optional[Tracer] = None) -> TraceStep:
        self.steps.append(step)
        if tracer is not None:
            tracer.on_step(step)
        return step

    def _of(self, kind: str) -> List[TraceStep]:
        return [s for s in self.steps if s.kind == kind]

    @property
    def model_calls(self) -> int:
        """Model calls made, the final review included."""
        return len(self._of("model")) + len(self._of("review"))

    @property
    def investigation_calls(self) -> int:
        return len(self._of("model"))

    @property
    def tool_calls(self) -> int:
        return len(self._of("tool"))

    @property
    def tools_called(self) -> List[str]:
        """Distinct tool names, in the order first called."""
        return list(dict.fromkeys(s.name for s in self._of("tool")))

    @property
    def input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)

    @property
    def output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {"stop_reason": self.stop_reason, "steps": [asdict(s) for s in self.steps]}
