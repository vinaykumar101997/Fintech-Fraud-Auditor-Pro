"""Behavioural features for the statistical funnel.

The original build fitted Isolation Forest on `amount` alone. Structuring is
defined by amounts that look ordinary, so amount-only anomaly detection is blind
to it by construction. These features describe how an account *behaves* across
the batch, which is where the signal actually lives.

Every feature is computed from the batch itself, so this stays unsupervised.

Point in time: these are batch features. Counts, volumes, fan-in and the
pass-through chain all use every row in the batch, including rows dated after
the transaction being scored (hop_depth follows the money forward as well as
back). That is fine for auditing a closed ledger. Scoring payments as they
arrive would need each feature computed only from rows before the payment.
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
    "hop_depth",
    "has_pass_through",
    "dwell_hours",
    "pass_through_ratio",
]

PASS_THROUGH_COLUMNS = ["hop_depth", "has_pass_through", "dwell_hours", "pass_through_ratio"]


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
    work[PASS_THROUGH_COLUMNS] = pass_through_features(df)

    return work[FEATURE_COLUMNS].replace([np.inf, -np.inf], 0.0).fillna(0.0)


def pass_through_features(df: pd.DataFrame) -> pd.DataFrame:
    """Money that comes in and goes straight back out, at almost the same amount.

    For each payment, the predecessor is the latest payment the sender received
    in the PASS_THROUGH_WINDOW_HOURS before it, of an amount the payment is
    (1 - PASS_THROUGH_TOLERANCE) to 100% of. Linking each payment to its
    predecessor gives chains:

      hop_depth          - legs in the chain this payment belongs to (1 alone)
      has_pass_through   - 1 if this payment has a predecessor
      dwell_hours        - hours the money sat with the sender (0 if none)
      pass_through_ratio - this amount / the predecessor's amount (0 if none)

    This looks at accounts' timing and amounts, not at their age, so it applies
    to established accounts as much as new ones. Without timestamps every row
    gets hop_depth 1 and zeros.
    """
    n = len(df)
    out = pd.DataFrame(
        {"hop_depth": 1.0, "has_pass_through": 0.0, "dwell_hours": 0.0, "pass_through_ratio": 0.0},
        index=df.index,
    )
    if "timestamp" not in df.columns or n == 0:
        return out
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().all():
        return out

    pred = _pass_through_predecessors(df, ts)
    succ = _first_successors(pred, ts)

    depth = 1 + _chain_length(pred) + _chain_length(succ)
    out["hop_depth"] = depth.astype(float)
    linked = pred >= 0
    if linked.any():
        rows, sources = np.flatnonzero(linked), pred[linked]
        ts_ns = ts.to_numpy("datetime64[ns]")
        amount = df["amount_float"].astype(float).to_numpy()
        out.iloc[rows, out.columns.get_loc("has_pass_through")] = 1.0
        out.iloc[rows, out.columns.get_loc("dwell_hours")] = (
            (ts_ns[rows] - ts_ns[sources]) / np.timedelta64(1, "s") / 3600.0
        )
        out.iloc[rows, out.columns.get_loc("pass_through_ratio")] = amount[rows] / amount[sources]
    return out


def _pass_through_predecessors(df: pd.DataFrame, ts: pd.Series) -> np.ndarray:
    """Position of each payment's predecessor, or -1.

    Candidates for payment i are payments j received by i's sender with
    ts[i] - window <= ts[j] < ts[i]. Each sender's received payments are
    sorted by time, so the window is found by binary search rather than by
    scanning every payment the sender ever received. Of the candidates whose
    amount qualifies, the latest wins; ties go to the earlier row.
    """
    n = len(df)
    amount = df["amount_float"].astype(float).to_numpy()
    valid = ~ts.isna().to_numpy()
    ts_ns = ts.to_numpy("datetime64[ns]").astype("int64")
    window_ns = int(pd.Timedelta(hours=config.PASS_THROUGH_WINDOW_HOURS).value)
    low = 1.0 - config.PASS_THROUGH_TOLERANCE

    accounts = pd.Index(pd.unique(np.concatenate([
        df["sender"].astype(str).to_numpy(), df["receiver"].astype(str).to_numpy()])))
    sender = accounts.get_indexer(df["sender"].astype(str))
    receiver = accounts.get_indexer(df["receiver"].astype(str))

    # Received payments that can be a predecessor, sorted by (account, time).
    inbound = np.flatnonzero(valid & (amount > 0))
    pred = np.full(n, -1, dtype=np.int64)
    if inbound.size == 0:
        return pred

    # Rank every time value used in a comparison so (account, time) packs into
    # one exact int64 key, whatever the time range.
    queries = np.flatnonzero(valid)
    times = np.concatenate([ts_ns[inbound], ts_ns[queries] - window_ns, ts_ns[queries]])
    _, rank = np.unique(times, return_inverse=True)
    scale = np.int64(len(times) + 1)
    in_rank, lo_rank, hi_rank = np.split(rank.astype(np.int64), [inbound.size, inbound.size + queries.size])

    in_key = receiver[inbound].astype(np.int64) * scale + in_rank
    order = np.argsort(in_key, kind="stable")
    in_key, inbound = in_key[order], inbound[order]

    q_account = sender[queries].astype(np.int64) * scale
    start = np.searchsorted(in_key, q_account + lo_rank, side="left")
    stop = np.searchsorted(in_key, q_account + hi_rank, side="left")
    counts = stop - start
    if counts.sum() == 0:
        return pred

    # One row per (payment, candidate) pair inside the window.
    i = np.repeat(queries, counts)
    offsets = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    j = inbound[np.repeat(start, counts) + offsets]

    keep = j != i
    i, j = i[keep], j[keep]
    ratio = amount[i] / amount[j]
    keep = (low <= ratio) & (ratio <= 1.0)
    i, j = i[keep], j[keep]
    if i.size == 0:
        return pred

    pairs = pd.DataFrame({"i": i, "j": j, "ts": ts_ns[j]})
    best = pairs.sort_values(["i", "ts", "j"], ascending=[True, False, True], kind="stable")
    best = best.drop_duplicates("i", keep="first")
    pred[best["i"].to_numpy()] = best["j"].to_numpy()
    return pred


def _first_successors(pred: np.ndarray, ts: pd.Series) -> np.ndarray:
    """For each payment, the earliest payment that passes it on, or -1. Ties go to the earlier row."""
    succ = np.full(len(pred), -1, dtype=np.int64)
    rows = np.flatnonzero(pred >= 0)
    if rows.size == 0:
        return succ
    links = pd.DataFrame({"j": pred[rows], "i": rows,
                          "ts": ts.to_numpy("datetime64[ns]").astype("int64")[rows]})
    first = links.sort_values(["j", "ts", "i"], kind="stable").drop_duplicates("j", keep="first")
    succ[first["j"].to_numpy()] = first["i"].to_numpy()
    return succ


def _chain_length(links: np.ndarray) -> np.ndarray:
    """Steps from each row along `links` until -1. Links always move strictly in time, so they never loop."""
    length = np.zeros(len(links), dtype=np.int64)
    current = links.copy()
    while (current >= 0).any():
        step = current >= 0
        length += step
        current = np.where(step, links[np.where(step, current, 0)], -1)
    return length


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
