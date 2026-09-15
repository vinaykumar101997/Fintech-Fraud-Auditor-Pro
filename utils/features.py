"""Behavioural features for the statistical funnel.

The original build fitted Isolation Forest on `amount` alone. Structuring is
defined by amounts that look ordinary, so amount-only anomaly detection is blind
to it by construction. These features describe how an account *behaves* across
the batch, which is where the signal actually lives.

Every feature is computed from the batch itself, so this stays unsupervised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config

FEATURE_COLUMNS = [
    "amount_log",
    "sender_txn_count",
    "sender_volume_log",
    "sender_amount_std",
    "sender_structuring_count",
    "sender_structuring_ratio",
    "sender_distinct_receivers",
    "sender_round_ratio",
    "amount_vs_sender_mean",
    "receiver_fan_in",
    "country_rarity",
    "sender_burst_score",
]


def _is_round(series: pd.Series) -> pd.Series:
    """Round-number amounts (multiples of 1000) are a weak laundering signal."""
    return ((series > 0) & (series % 1000 == 0)).astype(float)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a numeric feature frame aligned to df's index."""
    work = pd.DataFrame(index=df.index)
    amount = df["amount_float"].astype(float)

    work["amount_log"] = np.log1p(amount)

    sender = df["sender"].astype(str)
    receiver = df["receiver"].astype(str)

    grp = amount.groupby(sender)
    work["sender_txn_count"] = sender.map(grp.count()).astype(float)
    work["sender_volume_log"] = np.log1p(sender.map(grp.sum()).astype(float))
    work["sender_amount_std"] = sender.map(grp.std()).fillna(0.0).astype(float)

    sender_mean = sender.map(grp.mean()).astype(float)
    work["amount_vs_sender_mean"] = amount / sender_mean.replace(0, np.nan)
    work["amount_vs_sender_mean"] = work["amount_vs_sender_mean"].fillna(1.0)

    # Transactions parked just under the reporting threshold.
    floor = float(config.STRUCTURING_FLOOR)
    ceiling = float(config.REPORTING_THRESHOLD)
    in_band = ((amount >= floor) & (amount < ceiling)).astype(float)
    band_by_sender = in_band.groupby(sender).sum()
    work["sender_structuring_count"] = sender.map(band_by_sender).astype(float)
    work["sender_structuring_ratio"] = (
        work["sender_structuring_count"] / work["sender_txn_count"].replace(0, np.nan)
    ).fillna(0.0)

    work["sender_distinct_receivers"] = sender.map(
        receiver.groupby(sender).nunique()
    ).astype(float)

    round_flags = _is_round(amount)
    work["sender_round_ratio"] = sender.map(
        round_flags.groupby(sender).mean()
    ).fillna(0.0).astype(float)

    work["receiver_fan_in"] = receiver.map(
        sender.groupby(receiver).nunique()
    ).fillna(1.0).astype(float)

    # Rare corridors are more interesting than common ones.
    country = df["country"].astype(str).str.lower()
    freq = country.value_counts(normalize=True)
    work["country_rarity"] = 1.0 - country.map(freq).fillna(0.0).astype(float)

    work["sender_burst_score"] = _burst_score(df, sender)

    return work[FEATURE_COLUMNS].replace([np.inf, -np.inf], 0.0).fillna(0.0)


def _burst_score(df: pd.DataFrame, sender: pd.Series) -> pd.Series:
    """Transactions per active hour for the sender. Zero when no timestamps.

    A sender with exactly one transaction has no span to measure a rate over
    and is not a burst by definition; it scores 0.0 rather than being divided
    by a substituted 1-hour window, which previously made single-transaction
    senders the "burstiest" accounts in the batch. Multi-transaction senders
    whose whole span is under an hour still have their span floored at one
    hour, so a handful of transactions minutes apart doesn't produce an
    inflated rate.
    """
    if "timestamp" not in df.columns:
        return pd.Series(0.0, index=df.index)

    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().all():
        return pd.Series(0.0, index=df.index)

    frame = pd.DataFrame({"sender": sender.values, "ts": ts.values}, index=df.index)
    spans = frame.groupby("sender")["ts"].agg(["min", "max", "count"])
    hours = (spans["max"] - spans["min"]).dt.total_seconds() / 3600.0
    rate = spans["count"] / hours.clip(lower=1.0)
    rate = rate.where(spans["count"] > 1, 0.0)
    return sender.map(rate).fillna(0.0).astype(float)
