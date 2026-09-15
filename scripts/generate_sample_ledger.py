"""Generate a labelled synthetic ledger with known laundering patterns.

Produces `data/sample_ledger.csv` (no label column, for the app) and
`data/sample_ledger_labelled.csv` (with `is_laundering` and `pattern`, for the
evaluation harness). Patterns injected:

  structuring  - one sender splits a large sum into legs just under $10k
  circular     - funds leave an account and return through intermediaries
  sanctioned   - ordinary-sized transfers to a high-risk jurisdiction
  fan_in       - many senders converge on one collection account

The structuring and sanctioned patterns are deliberately *small in value*.
They are the cases an amount-only funnel discards.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

CLEAN_COUNTRIES = ["USA", "Canada", "Germany", "France", "Japan", "Australia", "Brazil"]
BUSINESSES = [
    "Northwind Retail", "Acme Logistics", "Bluepeak Foods", "Corvus Media",
    "Delta Print Works", "Eastgate Supplies", "Fairline Travel", "Granite Tools",
]


def generate(n_clean: int = 420, seed: int = 7):
    rng = random.Random(seed)
    start = datetime(2026, 3, 2, 9, 0)
    rows = []

    def add(tid, sender, receiver, amount, country, ts, laundering, pattern):
        rows.append({
            "transaction_id": tid,
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "receiver": receiver,
            "amount": f"{amount:,.2f}",
            "country": country,
            "is_laundering": int(laundering),
            "pattern": pattern,
        })

    # --- Background: ordinary business traffic ---
    for i in range(n_clean):
        sender = rng.choice(BUSINESSES)
        receiver = rng.choice([b for b in BUSINESSES if b != sender])
        amount = round(rng.lognormvariate(6.6, 1.05), 2)
        ts = start + timedelta(minutes=rng.randint(0, 60 * 24 * 20))
        add(f"TXN-{i:05d}", sender, receiver, amount, rng.choice(CLEAN_COUNTRIES), ts, False, "clean")

    # --- Structuring: $86k split into 10 legs just under the threshold ---
    ts = start + timedelta(days=4)
    for i in range(10):
        amount = round(rng.uniform(8600, 9850), 2)
        add(f"STR-{i:03d}", "Halcyon Trading", f"Shell Holdings {i % 3}", amount,
            rng.choice(["USA", "Cayman Islands"]), ts + timedelta(hours=i * 3), True, "structuring")

    # --- Circular flow: money returns to origin through three hops ---
    ts = start + timedelta(days=9)
    hops = ["Meridian Capital", "Vertex Nominees", "Orion Trust", "Meridian Capital"]
    for i in range(len(hops) - 1):
        add(f"CIR-{i:03d}", hops[i], hops[i + 1], round(rng.uniform(24000, 26000), 2),
            rng.choice(["Panama", "Seychelles", "USA"]), ts + timedelta(hours=i * 8), True, "circular")

    # --- Sanctioned jurisdiction, deliberately low value ---
    ts = start + timedelta(days=12)
    for i in range(6):
        add(f"SAN-{i:03d}", "Kestrel Imports", "Unknown_Entity", round(rng.uniform(300, 1400), 2),
            rng.choice(["Russia", "Iran", "North Korea"]), ts + timedelta(hours=i * 5), True, "sanctioned")

    # --- Fan-in: eleven senders converge on one account ---
    ts = start + timedelta(days=15)
    for i in range(11):
        add(f"FAN-{i:03d}", f"Courier Agent {i}", "Pinnacle Collections",
            round(rng.uniform(4200, 6800), 2), "USA", ts + timedelta(minutes=i * 40), True, "fan_in")

    df = pd.DataFrame(rows).sample(frac=1, random_state=seed).reset_index(drop=True)
    return df


def main():
    df = generate()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(config.DATA_DIR / "sample_ledger_labelled.csv", index=False)
    df.drop(columns=["is_laundering", "pattern"]).to_csv(config.SAMPLE_LEDGER, index=False)
    counts = df["pattern"].value_counts()
    print(f"Wrote {len(df)} rows to {config.SAMPLE_LEDGER}")
    for pattern, n in counts.items():
        print(f"  {pattern:<14} {n}")


if __name__ == "__main__":
    main()
