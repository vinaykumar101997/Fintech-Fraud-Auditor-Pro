"""Screening worker: payment.screening.requested in, payment.screening.completed out.

    python -m screening_worker

One request at a time. Each verdict is written to the audit log before it is
published, and the offset is committed only after the publish is confirmed,
so a redelivered request is answered with the stored verdict, byte for byte.
The worker never sends REJECT: only an officer rejects a payment.

Verdicts (first match wins):
  ERROR  the message is not a valid request, the key does not match paymentId,
         the rules failed, or the review failed or ran past the deadline
  FLAG   sanctions match or high-risk / secrecy jurisdiction (no review);
         review SUSPICIOUS; review CLEAR with screening incomplete (holder
         name or country missing) or a currency other than the rule currency
  CLEAR  review CLEAR, screening complete, rule currency

Request text (account ids, holder names, paymentId) is untrusted. It never
reaches a log line, and reasons are scrubbed and HTML-escaped.
"""

from __future__ import annotations

import functools
import html
import json
import logging
import re
import signal
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker
from langchain_core.callbacks import get_usage_metadata_callback

import config
from utils import audit_log
from utils.jurisdictions import resolve_country
from utils.rule_ids import rule_id, rule_ids
from utils.rules import evaluate_row
from utils.sanitize import scrub
from utils.screening import load_watchlist, normalize_name, rule_screener
from utils.trace import billed_tokens, digest
from utils.verdicts import AuditOutcome, Verdict

logger = logging.getLogger("screening_worker")

REQUEST_TOPIC = "payment.screening.requested"
RESULT_TOPIC = "payment.screening.completed"
CONTRACTS = Path(__file__).resolve().parent / "contracts"
# Verdict eventIds are uuid5(namespace, request eventId): a redelivery maps to the same id.
VERDICT_NAMESPACE = uuid.UUID("5d2c8a51-6f0e-4b8e-9a43-0c7e2f1b9d60")
AUDIT_EVENT = "screening_verdict"
MAX_REASONS = 10
BACKOFF_CAP_SECONDS = 30.0
SUMMARY_EVERY = 50
# Charged against SCREENING_MAX_COST_USD for a review whose usage or price is unknown: the
# costliest single fixed review measured with the output limit on (docs/EVALUATION.md).
UNKNOWN_REVIEW_COST_USD = 0.09
NO_REVIEW_COST = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

# --- contracts ---------------------------------------------------------------
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
_FORMATS = FormatChecker()


# jsonschema skips date-time unless rfc3339-validator is installed; check it here instead.
@_FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return bool(_RFC3339.match(value)) and datetime.fromisoformat(value).tzinfo is not None


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=_FORMATS)


REQUEST_SCHEMA = _validator("payment.screening.requested.v1.schema.json")
VERDICT_SCHEMA = _validator("payment.screening.completed.v1.schema.json")


def parse_request(value: Optional[bytes]) -> Tuple[Optional[dict], Optional[str]]:
    """(request, None) when valid, else (whatever parsed as an object, problem).

    Unknown fields are dropped before validation, so a field the payment
    service adds later is ignored rather than rejected. The problem text names
    only schema keywords and known field names, never request content.
    """
    try:
        data = json.loads(value.decode("utf-8")) if value is not None else None
    except (ValueError, RecursionError):
        return None, "body is not UTF-8 JSON"
    if not isinstance(data, dict):
        return None, "body is not a JSON object"
    known = REQUEST_SCHEMA.schema["properties"]
    data = {k: v for k, v in data.items() if k in known}
    errors = sorted(REQUEST_SCHEMA.iter_errors(data), key=lambda e: e.json_path)
    if errors:
        return data, f"{errors[0].json_path} failed the {errors[0].validator!r} check"
    try:
        # JSON escapes can carry lone surrogates, which pass the schema but cannot be encoded.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return data, "a field is not valid UTF-8 text"
    return data, None


def _payment_id(value: object) -> Optional[str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _uuid_text(value: object) -> Optional[str]:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return value


def _key_text(key: Optional[bytes]) -> Optional[str]:
    try:
        return _payment_id(key.decode("utf-8")) if key is not None else None
    except UnicodeDecodeError:
        return None


# --- decision ----------------------------------------------------------------
def safe_reason(text: object) -> str:
    """One verdict reason: scrubbed, HTML-escaped, within the 500-character limit."""
    return html.escape(scrub(text, 400), quote=True)[:500]


def cap_reasons(reasons: List[str]) -> List[str]:
    cleaned = [r for r in (safe_reason(x) for x in reasons) if r]
    if len(cleaned) <= MAX_REASONS:
        return cleaned
    keep = MAX_REASONS - 1
    return cleaned[:keep] + [f"{len(cleaned) - keep} more reason(s) omitted."]


def run_with_deadline(fn: Callable[[], AuditOutcome],
                      seconds: float) -> Tuple[AuditOutcome, Optional[Dict[str, Any]]]:
    """(outcome, token usage by model) of fn() run in a daemon thread.

    ERROR if fn raises or is still running after `seconds`; usage is None then.
    shortcut: an overrun review keeps running in the background and can briefly
    double model concurrency; move to a cancellable call if quota errors appear.
    """
    box: Dict[str, Any] = {}

    def target() -> None:
        # Inside the thread: a new thread does not inherit the caller's callback context.
        with get_usage_metadata_callback() as meter:
            try:
                outcome = fn()
            except Exception as exc:  # noqa: BLE001
                outcome = AuditOutcome.failure(type(exc).__name__)
        box["usage"] = dict(meter.usage_metadata)
        box["outcome"] = outcome  # last, so a finished review always carries its usage

    thread = threading.Thread(target=target, name="screening-review", daemon=True)
    thread.start()
    thread.join(seconds)
    if "outcome" in box:
        return box["outcome"], box["usage"]
    overrun = f"Review did not finish within {seconds:g} s. Manual review required."
    return AuditOutcome(verdict=Verdict.ERROR, rationale=overrun, error_detail=overrun), None


def review_cost(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Billed tokens and USD cost of one review; cost_usd is None when usage or a price is unknown.

    Prices come from the evaluation harness's table. Embedding calls and replies cut
    off at the output limit (raised, not returned) are not counted.
    """
    from scripts.eval_llm_tier import PRICES_PER_MILLION

    tokens_in = tokens_out = 0
    cost: Optional[float] = 0.0 if usage else None
    for model, metadata in (usage or {}).items():
        used_in, used_out = billed_tokens(metadata)
        tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out
        prices = PRICES_PER_MILLION.get(model)
        if prices is None or cost is None:
            cost = None
        else:
            cost += (used_in * prices[0] + used_out * prices[1]) / 1e6
    return {"tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": None if cost is None else round(cost, 6)}


def decide(request: dict, review: Callable[..., AuditOutcome],
           deadline: float) -> Tuple[str, List[str], Dict[str, Any]]:
    """(verdict, reasons, facts) for a valid request. Raises only if the rules fail."""
    from_acct, to_acct = request["fromAccount"], request["toAccount"]
    from_country = request.get("fromAccountCountry", "")
    to_country = request.get("toAccountCountry", "")
    currency = request["currency"]
    amount = Decimal(request["amount"])
    rule_currency = currency == config.SCREENING_RULE_CURRENCY

    def screenable(name: object) -> bool:
        # "   " or "Ltd" passes the schema but normalises to nothing, so it can never match.
        return bool(name) and bool(normalize_name(str(name)))

    # Rules key on account ids (self-transfer must not depend on names);
    # sanctions screening looks up the holder names behind each account.
    holders: Dict[str, List[str]] = {}
    for acct, name in ((from_acct, request.get("fromAccountHolderName")),
                       (to_acct, request.get("toAccountHolderName"))):
        if screenable(name):
            holders.setdefault(acct, []).append(name)

    def screener(acct: str) -> Optional[str]:
        for name in holders.get(acct, []):
            hit = rule_screener(name)
            if hit:
                return hit
        return None

    main = evaluate_row(amount if rule_currency else Decimal(0), to_country,
                        sender=from_acct, receiver=to_acct, screener=screener)
    origin = evaluate_row(Decimal(0), from_country, sender=from_acct, receiver=to_acct)
    reasons = list(main.reasons) + [
        f"Originator: {r}" for r in origin.reasons
        if rule_id(r) in ("high_risk_country", "secrecy_jurisdiction")
    ]

    gaps = []
    for side, name_key, country_key in (
        ("originator", "fromAccountHolderName", "fromAccountCountry"),
        ("beneficiary", "toAccountHolderName", "toAccountCountry"),
    ):
        missing = []
        if not screenable(request.get(name_key)):
            missing.append("screenable holder name")
        if resolve_country(request.get(country_key)) is None:  # "XX" passes the schema pattern
            missing.append("recognised country")
        if missing:
            gaps.append(f"Screening incomplete: no {' or '.join(missing)} for the {side}.")
    if not rule_currency:
        gaps.append(f"Amount rules not applied: {currency} is not the rule currency "
                    f"{config.SCREENING_RULE_CURRENCY}.")

    facts: Dict[str, Any] = {"rule_ids": rule_ids(reasons), "reviewed": False, "model_s": None,
                             "rationale": None, **NO_REVIEW_COST}
    # Decided on structured results, never on reason text: a reason can carry request text.
    risky = config.HIGH_RISK_COUNTRIES | config.SECRECY_JURISDICTIONS
    if main.sanctions_hits or any(resolve_country(c) in risky for c in (from_country, to_country) if c):
        return "FLAG", reasons + gaps, facts

    started = time.perf_counter()
    outcome, usage = run_with_deadline(lambda: review(
        transaction_id=request["eventId"], amount=amount, country=to_country,
        sender=from_acct, receiver=to_acct, rule_reasons=reasons,
        behaviour_signals={"currency": currency},
    ), deadline)
    facts.update(reviewed=True, model_s=round(time.perf_counter() - started, 3),
                 rationale=outcome.rationale, **review_cost(usage))

    if outcome.verdict == Verdict.ERROR:
        return "ERROR", reasons + gaps + [outcome.rationale], facts
    if outcome.verdict == Verdict.SUSPICIOUS:
        return "FLAG", reasons + gaps + [f"Review: {outcome.rationale}"], facts
    verdict = "FLAG" if gaps else "CLEAR"
    return verdict, reasons + gaps + [f"Review: {outcome.rationale}"], facts


def decided_at(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_verdict(seed: str, payment_id: str, verdict: str, reasons: List[str],
                  run_id: str, now: Optional[datetime] = None) -> dict:
    """A contract-valid verdict. Falls back to a fixed ERROR if the first one is not."""
    now = now or datetime.now(timezone.utc)
    event = {
        "schemaVersion": 1,
        "eventId": str(uuid.uuid5(VERDICT_NAMESPACE, seed)),
        "paymentId": payment_id,
        "verdict": verdict,
        "reasons": cap_reasons(reasons),
        "decidedAt": decided_at(now),
        "screeningRunId": run_id,
    }
    if VERDICT_SCHEMA.is_valid(event):
        return event
    logger.error(json.dumps({"event": "verdict_invalid", "verdict": verdict}))
    event.update(verdict="ERROR", reasons=["Verdict failed its contract check. Manual review required."])
    VERDICT_SCHEMA.validate(event)  # raises: the worker stops rather than publish a broken verdict
    return event


def encode(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- loop --------------------------------------------------------------------
class _Stopped(Exception):
    """Shutdown was requested before the verdict could be published."""


class Worker:
    """Consume, decide, record, publish, commit. Clients are duck-typed, so tests pass fakes."""

    def __init__(self, consumer: Any, producer: Any, review: Callable[..., AuditOutcome], *,
                 run_id: str, deadline: float, db_path: Optional[Path] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_cost_usd: Optional[float] = None):
        self.consumer, self.producer, self.review = consumer, producer, review
        self.run_id, self.deadline, self.db_path, self.sleep = run_id, deadline, db_path, sleep
        self.max_cost_usd = config.SCREENING_MAX_COST_USD if max_cost_usd is None else max_cost_usd
        self.spent_usd = 0.0
        self.stopping = False
        self.counts: Counter = Counter()
        self.e2e_ms: List[float] = []
        self.model_s: List[float] = []

    def run(self, max_polls: Optional[int] = None) -> None:
        backoff, polls = 1.0, 0
        try:
            while not self.stopping and (max_polls is None or polls < max_polls):
                polls += 1
                msg = self.consumer.poll(1.0)
                if msg is None:
                    continue
                err = msg.error()
                if err is not None:
                    if err.fatal():
                        raise RuntimeError(f"Kafka fatal error: {err.name()}")
                    # Includes unknown-topic: the topics appear once the payment service starts.
                    self.counts["kafka_error"] += 1
                    logger.warning(json.dumps({"event": "kafka_error", "error": err.name(),
                                               "retry_in_s": backoff}))
                    self.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_CAP_SECONDS)
                    continue
                backoff = 1.0
                self.handle(msg)
        except _Stopped:
            pass
        finally:
            self.summary()

    def close(self) -> None:
        self.producer.flush(10)
        self.consumer.close()

    def handle(self, msg: Any) -> None:
        data, problem = parse_request(msg.value())
        key = _key_text(msg.key())
        body_id = _payment_id((data or {}).get("paymentId"))
        if problem is None and key != body_id:  # a missing key counts as a mismatch
            problem = "message key does not match paymentId"
        payment_id = body_id or key
        event_id = _uuid_text((data or {}).get("eventId"))
        seed = event_id or f"{msg.topic()}/{msg.partition()}/{msg.offset()}"
        raw_key = msg.key()
        evidence = {"value": (msg.value() or b"").hex(),
                    "key": None if raw_key is None else raw_key.hex()}
        line: Dict[str, Any] = {"event": "screened", "event_id": event_id or "-",
                                "payment_ref": digest(payment_id) if payment_id else "-",
                                "partition": msg.partition(), "offset": msg.offset()}

        if payment_id is None:
            # Nothing to address a verdict to; the payment service flags it as overdue.
            self.counts["unaddressable"] += 1
            logger.error(json.dumps({**line, "outcome": "skipped", "problem": problem}))
            self.commit(msg)
            return

        stored = self.stored(payment_id, seed)
        if stored is not None and stored["evidence_hash"] != audit_log.evidence_hash(evidence):
            # Same eventId, different body or key: not a redelivery. Answer it with its own
            # ERROR (own eventId), never with the verdict stored for the other message.
            problem = "eventId was already screened with a different body or key"
            seed = f"{seed}/{audit_log.evidence_hash(evidence)}"
            stored = self.stored(payment_id, seed)
        if stored is not None:
            value, line["deduped"] = json.loads(stored["detail"])["published"].encode("utf-8"), True
        else:
            value = self.decide_and_record(data, problem, payment_id, seed, line, evidence)
        self.publish(payment_id, value)
        self.commit(msg)
        line["verdict"] = json.loads(value)["verdict"]
        self.counts[line["verdict"]] += 1
        self.counts["deduped" if line.get("deduped") else "decided"] += 1
        logger.info(json.dumps({**line, "outcome": "published"}))
        if line.get("reviewed"):  # replays and skipped reviews cost nothing
            cost = line["cost_usd"]
            self.spent_usd += UNKNOWN_REVIEW_COST_USD if cost is None else cost
        if self.max_cost_usd and self.spent_usd >= self.max_cost_usd and not self.stopping:
            # This message is already published and committed; polling stops here.
            logger.warning(json.dumps({"event": "cost_limit_reached",
                                       "spent_usd": round(self.spent_usd, 6),
                                       "limit_usd": self.max_cost_usd}))
            self.stopping = True
        if sum(self.counts[v] for v in ("CLEAR", "FLAG", "ERROR")) % SUMMARY_EVERY == 0:
            self.summary()

    def decide_and_record(self, data: Optional[dict], problem: Optional[str], payment_id: str,
                          seed: str, line: Dict[str, Any], evidence: Dict[str, Any]) -> bytes:
        facts: Dict[str, Any] = {"rule_ids": [], "reviewed": False, "model_s": None, "rationale": None,
                                 **NO_REVIEW_COST}
        if problem is not None:
            verdict, reasons = "ERROR", [f"Invalid screening request: {problem}."]
            line["problem"] = problem
        else:
            try:
                verdict, reasons, facts = decide(data, self.review, self.deadline)
            except Exception as exc:  # noqa: BLE001
                verdict, reasons = "ERROR", [f"Rules could not run ({type(exc).__name__}). "
                                             "Manual review required."]
                line["problem"] = type(exc).__name__
        event = build_verdict(seed, payment_id, verdict, reasons, self.run_id)
        value = encode(event)

        e2e_ms = None
        created = (data or {}).get("createdAt") if problem is None else None
        if created:
            decided = datetime.fromisoformat(event["decidedAt"])
            e2e_ms = round((decided - datetime.fromisoformat(created)).total_seconds() * 1000, 1)
            self.e2e_ms.append(e2e_ms)
        if facts["model_s"] is not None:
            self.model_s.append(facts["model_s"])
        cost = {k: facts[k] for k in NO_REVIEW_COST}
        line.update(rule_ids=facts["rule_ids"], reviewed=facts["reviewed"],
                    model_s=facts["model_s"], e2e_ms=e2e_ms, **cost)

        # Raises AuditWriteError: nothing is published or committed, so the request comes back.
        audit_log.record(
            session_id=self.run_id, batch_id=seed, transaction_id=payment_id, stage=AUDIT_EVENT,
            verdict=event["verdict"], rule_reasons=event["reasons"],
            rationale=facts["rationale"],
            model_name=config.chat_model_name() if facts["reviewed"] else None,
            evidence=evidence,  # stored as evidence_hash, compared on replay
            detail=json.dumps({"published": value.decode("utf-8"),
                               "created_at": created, "model_s": facts["model_s"],
                               "e2e_ms": e2e_ms, **cost}),
            db_path=self.db_path,
        )
        return value

    def stored(self, payment_id: str, seed: str) -> Optional[Dict[str, Any]]:
        for row in audit_log.history(payment_id, db_path=self.db_path):
            if row["stage"] == AUDIT_EVENT and row["batch_id"] == seed:
                return row
        return None

    def publish(self, payment_id: str, value: bytes) -> None:
        """Produce and wait for the broker's acknowledgement, retrying until it comes."""
        backoff = 1.0
        while True:
            report: Dict[str, Any] = {}
            try:
                self.producer.produce(RESULT_TOPIC, key=payment_id.encode("utf-8"), value=value,
                                      on_delivery=lambda err, _msg: report.update(err=err))
                self.producer.flush(10)
            except Exception as exc:  # noqa: BLE001
                report["err"] = exc
            if "err" in report and report["err"] is None:
                return
            err = report.get("err", "no delivery report")
            if getattr(err, "fatal", lambda: False)():
                raise RuntimeError(f"Kafka fatal error while publishing: {err.name()}")
            self.counts["publish_retry"] += 1
            name = err.name() if hasattr(err, "name") else type(err).__name__
            logger.warning(json.dumps({"event": "publish_failed", "error": name,
                                       "retry_in_s": backoff}))
            if self.stopping:
                raise _Stopped
            self.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP_SECONDS)

    def commit(self, msg: Any) -> None:
        try:
            self.consumer.commit(message=msg, asynchronous=False)
        except Exception as exc:  # noqa: BLE001
            # After a rebalance the message comes back and is answered from the audit log.
            self.counts["commit_failed"] += 1
            logger.warning(json.dumps({"event": "commit_failed", "error": type(exc).__name__}))

    def summary(self) -> None:
        def pct(values: List[float]) -> Dict[str, Optional[float]]:
            if len(values) < 2:
                return {"p50": values[0] if values else None, "p95": values[0] if values else None}
            q = statistics.quantiles(values, n=20, method="inclusive")
            return {"p50": round(statistics.median(values), 3), "p95": round(q[18], 3)}

        logger.info(json.dumps({"event": "summary", "counts": dict(self.counts),
                                "spent_usd": round(self.spent_usd, 6),
                                "e2e_ms": pct(self.e2e_ms), "model_s": pct(self.model_s)}))


# --- entry point --------------------------------------------------------------
def new_run_id() -> str:
    return f"screening-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def main() -> int:
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if config.REVIEW_MODE != "fixed":
        logger.error("REVIEW_MODE must be 'fixed': the agent review needs a ledger a single payment lacks.")
        return 2
    _, real_list = load_watchlist()
    if not real_list:
        logger.error("data/sanctions_list.csv is missing or empty; refusing to screen against the demo list.")
        return 2

    # Imported here so the module and its tests load without the Kafka client or provider stack.
    from confluent_kafka import Consumer, Producer

    from utils.agents import ComplianceAuditEngine
    from utils.chat_agent import ComplianceIntelligenceProvider, ProviderUnavailable

    try:
        provider = ComplianceIntelligenceProvider()
    except ProviderUnavailable as exc:
        logger.error("Review backend unavailable: %s", exc)
        return 2
    # A tracing backend (e.g. Langfuse) hooks in here, as a LangChain callback handler on provider.llm.
    engine = ComplianceAuditEngine(llm=provider.llm)
    review = functools.partial(engine.assess_transaction, rag_engine=provider.engine)
    audit_log.initialize()

    deadline = config.SCREENING_DEADLINE_SECONDS
    consumer = Consumer({
        "bootstrap.servers": config.SCREENING_BOOTSTRAP_SERVERS,
        "group.id": config.SCREENING_GROUP_ID,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "allow.auto.create.topics": False,
        "max.poll.interval.ms": int((deadline + 120) * 1000),
        "topic.metadata.refresh.interval.ms": 10000,
    })
    producer = Producer({
        "bootstrap.servers": config.SCREENING_BOOTSTRAP_SERVERS,
        "enable.idempotence": True,
        "acks": "all",
    })
    consumer.subscribe([REQUEST_TOPIC])
    worker = Worker(consumer, producer, review, run_id=new_run_id(), deadline=deadline)

    def stop(*_: Any) -> None:
        worker.stopping = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    logger.info(json.dumps({"event": "started", "run_id": worker.run_id, "topic": REQUEST_TOPIC}))
    try:
        worker.run()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
