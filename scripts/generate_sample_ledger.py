"""Generate a labelled synthetic ledger with known laundering patterns.

Produces `data/sample_ledger.csv` (no label column, for the app) and
`data/sample_ledger_labelled.csv` (with `is_laundering` and `pattern`, for the
evaluation harness). Patterns injected:

  structuring    - one sender splits a large sum into legs just under $10k
  circular       - funds leave an account and return through intermediaries,
                   in legs large enough to trip the reporting threshold
  circular_small       - the same shape, in rings of 3-4 accounts, but every
                         leg is under $7,000 - below the structuring band,
                         so no rule threshold reaches it; each hop forwards
                         90-100% of the previous leg
  circular_camouflaged - the same ring shape again, but built from businesses
                         that already have ordinary repeat activity in the
                         "clean" background, at the same amount distribution
                         as that background traffic, spread over ~10 days
  sanctioned           - ordinary-sized transfers to a high-risk jurisdiction
  fan_in               - many senders converge on one collection account

The structuring, sanctioned, and circular_small patterns are deliberately
*small in value*. They are the cases an amount-only funnel discards.
circular_camouflaged is deliberately indistinguishable from the background on
amount, account age, and per-account volume - see the README for whether
Tier 0 catches it and what that does and does not prove.

generate(seed, hard=True) appends harder patterns after the default rows,
drawn from a separate random generator so the default rows are unchanged:

  established_pass_through   - money passed along a chain of businesses that
                               each have 20+ earlier clean transactions, so
                               account newness gives nothing away
  sanctioned_name_variant    - watchlist names with changed casing,
                               punctuation, Ltd/Limited, token order, or a
                               one-character typo
  sanctioned_country_variant - low-value transfers to high-risk countries
                               written as ISO codes, lowercase names,
                               official names or aliases (DPRK, Russian
                               Federation)
  near_miss_clean            - clean parties whose names resemble watchlist
                               entries but are different (false positives)
  clean_country_variant      - clean traffic with countries written as
                               codes, lowercase names or aliases (UK, GB)
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


NAME_VARIANTS = [
    ("casing", "VIKTOR BOUT"), ("casing", "lazarus group"),
    ("punctuation", "Bout, Viktor."), ("punctuation", "Tornado-Cash"),
    ("suffix", "Wagner Group Limited"), ("suffix", "Sinaloa Cartel Ltd"),
    ("reorder", "Iran Bank Melli"), ("reorder", "Un Jong Kim"),
    ("typo", "Viktor Bouf"), ("typo", "Lazarus Groop"),
]
NEAR_MISS_NAMES = [
    "Victor Boutros", "Viktoria Boutique", "Tornado Sports Ltd", "Wagner Plumbing",
    "Lazarus Dental Group", "Sinaloa Grill", "Melli Bakery", "Kimberly Jones",
    "Putnam Valley Farms", "Cash Tornado Car Wash",
]
RISKY_COUNTRY_FORMS = ["RU", "ru", "Russian Federation", "russia", "KP", "DPRK",
                       "Korea, North", "IR", "Iran, Islamic Republic of", "sy"]
CLEAN_COUNTRY_FORMS = ["GB", "UK", "united kingdom", "Great Britain", "DE", "germany",
                       "US", "usa", "FR", "Deutschland"]


def _hard_rows(seed: int, start: datetime) -> list:
    """Harder patterns, from their own random generator (see module docstring)."""
    rng = random.Random(seed * 1000 + 1)
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

    # Pass-through chains after the 20-day background window, so every
    # business on a chain already has its full clean history behind it.
    ts = start + timedelta(days=21)
    for chain in range(3):
        accounts = rng.sample(BUSINESSES, 4)
        amount = None
        for i in range(3):
            draw = rng.uniform(2500, 4500) if amount is None else amount * rng.uniform(0.95, 1.0)
            amount = round(draw, 2)
            add(f"ESTP-{chain}-{i:02d}", accounts[i], accounts[i + 1], amount,
                rng.choice(CLEAN_COUNTRIES), ts + timedelta(hours=i * 3), True,
                "established_pass_through")
        ts += timedelta(days=1)

    ts = start + timedelta(days=5)
    for i, (kind, name) in enumerate(NAME_VARIANTS):
        add(f"NAMEV-{kind}-{i:02d}", name, rng.choice(BUSINESSES), round(rng.uniform(300, 900), 2),
            "USA", ts + timedelta(hours=i * 7), True, "sanctioned_name_variant")

    for i, name in enumerate(NEAR_MISS_NAMES):
        add(f"NEAR-{i:03d}", name, rng.choice(BUSINESSES), round(rng.lognormvariate(6.6, 1.05), 2),
            "USA", ts + timedelta(hours=i * 9 + 2), False, "near_miss_clean")

    ts = start + timedelta(days=13)
    for i, country in enumerate(RISKY_COUNTRY_FORMS):
        add(f"CTRV-{i:03d}", "Harrowgate Freight", "Unknown_Entity", round(rng.uniform(300, 1400), 2),
            country, ts + timedelta(hours=i * 5), True, "sanctioned_country_variant")

    for i, country in enumerate(CLEAN_COUNTRY_FORMS):
        sender = rng.choice(BUSINESSES)
        receiver = rng.choice([b for b in BUSINESSES if b != sender])
        add(f"CTRC-{i:03d}", sender, receiver, round(rng.lognormvariate(6.6, 1.05), 2),
            country, ts + timedelta(hours=i * 6 + 1), False, "clean_country_variant")

    return rows


def generate(n_clean: int = 420, seed: int = 7, hard: bool = False):
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

    # Laundering parties must NOT appear on data/sanctions_list.csv, or name
    # screening detects the label rather than the behaviour (label leakage).
    # --- Structuring: $86k split into 10 legs just under the threshold ---
    ts = start + timedelta(days=4)
    for i in range(10):
        amount = round(rng.uniform(8600, 9850), 2)
        add(f"STR-{i:03d}", "Larkspur Trading", f"Shell Holdings {i % 3}", amount,
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
        add(f"SAN-{i:03d}", "Wrenfield Imports", "Unknown_Entity", round(rng.uniform(300, 1400), 2),
            rng.choice(["Russia", "Iran", "North Korea"]), ts + timedelta(hours=i * 5), True, "sanctioned")

    # --- Circular flow (small): rings of 3-4 accounts, legs below the
    # structuring band. Unlike the pattern above, no leg here exceeds any
    # rule threshold - only a feature that recognises the cycle itself,
    # rather than the amount, could catch this one. ---
    ts = start + timedelta(days=18)
    for ring in range(rng.randint(2, 3)):
        size = rng.randint(3, 4)
        accounts = [f"Loop{ring}-{j}" for j in range(size)]
        amount = None
        for i in range(size):
            # Each hop forwards 90-100% of what it received (fees, skims),
            # as round-tripped money does. Benchmark change: legs used to be
            # independent draws, so a "ring" could grow 57% in one lap. One
            # draw per leg either way, so every later pattern is unchanged.
            draw = rng.uniform(3000, 7000) if amount is None else amount * rng.uniform(0.9, 1.0)
            amount = round(draw, 2)
            add(f"CIRS-{ring}-{i:02d}", accounts[i], accounts[(i + 1) % size], amount,
                rng.choice(CLEAN_COUNTRIES), ts + timedelta(hours=i * 6), True, "circular_small")
        ts += timedelta(days=2)

    # --- Circular flow (camouflaged): the same ring shape, but built from
    # businesses that already appear throughout the "clean" background with
    # normal repeat activity, at that same amount distribution, spread over
    # ~10 days rather than clustered hours. Nothing here is statistically new
    # about the accounts themselves - only the ring structure is - and Tier 0
    # has no feature that looks at structure. ---
    ts = start + timedelta(days=25)
    for ring in range(2):
        size = rng.randint(3, 4)
        accounts = rng.sample(BUSINESSES, size)
        for i in range(size):
            amount = round(rng.lognormvariate(6.6, 1.05), 2)
            add(f"CIRC-{ring}-{i:02d}", accounts[i], accounts[(i + 1) % size], amount,
                rng.choice(CLEAN_COUNTRIES),
                ts + timedelta(days=i * 2, hours=rng.randint(0, 12)),
                True, "circular_camouflaged")
        ts += timedelta(days=10)

    # --- Fan-in: eleven senders converge on one account ---
    ts = start + timedelta(days=15)
    for i in range(11):
        add(f"FAN-{i:03d}", f"Courier Agent {i}", "Pinnacle Collections",
            round(rng.uniform(4200, 6800), 2), "USA", ts + timedelta(minutes=i * 40), True, "fan_in")

    df = pd.DataFrame(rows).sample(frac=1, random_state=seed).reset_index(drop=True)
    if hard:
        extra = pd.DataFrame(_hard_rows(seed, start)).sample(frac=1, random_state=seed)
        df = pd.concat([df, extra], ignore_index=True)
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
