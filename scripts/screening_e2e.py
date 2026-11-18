"""Optional end-to-end check of the screening worker against a live local stack.

Not part of the test suite. Needs:
  - payment-event-service running with docker compose, PAYMENTS_SCREENING_ENABLED=true
    and PAYMENTS_API_KEY set in the shell (the same key is read here);
  - `python -m screening_worker` running, with model provider credentials, a Qdrant
    collection and data/sanctions_list.csv;
  - confluent-kafka installed (requirements.txt).

Each payment reviewed by the model costs two model calls, so --count is capped.

    python scripts/screening_e2e.py --count 5
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from screening_worker import RESULT_TOPIC  # noqa: E402

MAX_COUNT = 30  # keeps a run under the live-cost budget at the worst measured review cost


def call(method: str, url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--amount", default="125.50")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for all verdicts")
    args = parser.parse_args()
    if not 1 <= args.count <= MAX_COUNT:
        parser.error(f"--count must be 1 to {MAX_COUNT}")
    base = os.getenv("PAYMENTS_BASE_URL", "http://localhost:8080")
    key = os.getenv("PAYMENTS_API_KEY")
    if not key:
        parser.error("set PAYMENTS_API_KEY to the key the payment service was started with")

    from confluent_kafka import Consumer

    consumer = Consumer({"bootstrap.servers": config.SCREENING_BOOTSTRAP_SERVERS,
                         "group.id": f"screening-e2e-{uuid.uuid4().hex[:8]}",
                         "auto.offset.reset": "latest", "enable.auto.commit": False})
    consumer.subscribe([RESULT_TOPIC])
    consumer.poll(5.0)  # join the group before posting, so no verdict is missed

    run = uuid.uuid4().hex[:8]
    sent: dict[str, datetime] = {}
    for i in range(args.count):
        payment_id = f"e2e-{run}-{i:03d}"
        call("POST", f"{base}/payments", key, {"paymentId": payment_id, "fromAccount": "ACC-1001",
                                              "toAccount": "ACC-1002", "amount": float(args.amount),
                                              "currency": "USD"})
        sent[payment_id] = datetime.now(timezone.utc)

    verdicts: dict[str, dict] = {}
    deadline = time.monotonic() + args.timeout
    while len(verdicts) < len(sent) and time.monotonic() < deadline:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        event = json.loads(msg.value())
        if event.get("paymentId") in sent:
            verdicts[event["paymentId"]] = event
    consumer.close()

    latencies = []
    for payment_id, posted in sent.items():
        event = verdicts.get(payment_id)
        status = call("GET", f"{base}/payments/{payment_id}", key).get("status")
        if event is None:
            print(f"{payment_id}  no verdict  status={status}")
            continue
        seconds = (datetime.fromisoformat(event["decidedAt"]) - posted).total_seconds()
        latencies.append(seconds)
        print(f"{payment_id}  {event['verdict']:<5}  {seconds:7.1f} s  status={status}")

    if len(latencies) >= 2:
        p95 = statistics.quantiles(latencies, n=20, method="inclusive")[18]
        print(f"post to verdict: p50 {statistics.median(latencies):.1f} s, p95 {p95:.1f} s")
    print(f"{len(verdicts)}/{len(sent)} verdicts received")
    return 0 if len(verdicts) == len(sent) else 1


if __name__ == "__main__":
    sys.exit(main())
