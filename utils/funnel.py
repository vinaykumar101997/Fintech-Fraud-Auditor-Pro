"""Tier 0: the statistical funnel, with rules given right of way.

Two changes from the original design:

1. `contamination=0.15` is gone. It is a fixed quota: it flags 15% of a clean
   batch and drops 85% of a batch that is 90% fraudulent. We threshold on the
   decision function instead, so the flagged count follows the data.
2. The funnel cannot drop a row that a deterministic rule flagged. Cost saving
   never overrides a compliance rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

import config
from utils.features import FEATURE_COLUMNS, build_features

logger = logging.getLogger(__name__)


@dataclass
class FunnelResult:
    flagged: pd.DataFrame
    dropped: pd.DataFrame
    total_rows: int
    ml_flagged: int
    rule_flagged: int
    rescued_by_rules: int  # rows the ML called normal but a rule saved
    ml_applied: bool
    elapsed_ms: float
    top_features: List[str]

    @property
    def forwarded(self) -> int:
        return len(self.flagged)

    @property
    def drop_rate(self) -> float:
        return len(self.dropped) / self.total_rows if self.total_rows else 0.0


def run_funnel(df: pd.DataFrame) -> FunnelResult:
    """Score the batch and split it into rows to investigate and rows to drop.

    `df` must already carry `rule_flag` from utils.rules.apply_rules.
    """
    import time

    start = time.perf_counter()
    total = len(df)
    work = df.copy()

    if "rule_flag" not in work.columns:
        raise ValueError("run_funnel requires rule_flag; call apply_rules first.")

    ml_applied = total >= config.MIN_ROWS_FOR_ML
    if ml_applied:
        features = build_features(work)
        scaled = StandardScaler().fit_transform(features.values)

        model = IsolationForest(
            n_estimators=200,
            contamination="auto",
            random_state=config.ML_RANDOM_STATE,
            n_jobs=-1,
        )
        model.fit(scaled)
        scores = model.decision_function(scaled)
        work["ml_score"] = scores
        work["ml_anomaly"] = _robust_anomaly_mask(scores)
        top_features = _rank_features(features, work["ml_anomaly"])
    else:
        # Too few rows for the distribution to mean anything. Fail open into
        # review rather than guessing: everything goes to the next tier.
        logger.info("Batch of %d is below the ML minimum; skipping Tier 0.", total)
        work["ml_score"] = np.nan
        work["ml_anomaly"] = True
        top_features = []

    work["investigate"] = work["ml_anomaly"] | work["rule_flag"]
    if ml_applied:
        work["selection_reason"] = np.where(
            work["rule_flag"] & work["ml_anomaly"],
            "rule + statistical anomaly",
            np.where(work["rule_flag"], "deterministic rule", "statistical anomaly"),
        )
    else:
        # ml_anomaly is True here only from the fail-open above, not a
        # computed score, so selection_reason must never claim a statistical
        # anomaly was detected. explain_row already gets this right (it only
        # cites rule_reasons plus a "batch too small" disclaimer); this keeps
        # the two in sync.
        work["selection_reason"] = np.where(
            work["rule_flag"],
            "deterministic rule",
            "batch too small for scoring - forwarded by default",
        )

    rescued = int((work["rule_flag"] & ~work["ml_anomaly"]).sum())
    if rescued:
        logger.info("%d row(s) kept by rules despite a normal ML score.", rescued)

    flagged = work[work["investigate"]].copy().reset_index(drop=True)
    dropped = work[~work["investigate"]].copy().reset_index(drop=True)

    return FunnelResult(
        flagged=flagged,
        dropped=dropped,
        total_rows=total,
        ml_flagged=int(work["ml_anomaly"].sum()),
        rule_flagged=int(work["rule_flag"].sum()),
        rescued_by_rules=rescued,
        ml_applied=ml_applied,
        elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
        top_features=top_features,
    )


def _robust_anomaly_mask(scores: np.ndarray) -> np.ndarray:
    """Flag rows whose anomaly score sits far below the batch's own centre.

    Uses median and MAD rather than mean and standard deviation, so a handful of
    extreme outliers cannot drag the threshold out to meet them. A batch with no
    dispersion (MAD of zero) has no outliers by definition and flags nothing.
    """
    median = np.median(scores)
    mad = np.median(np.abs(scores - median))

    if mad <= 1e-12:
        return np.zeros_like(scores, dtype=bool)

    robust_z = (scores - median) / (1.4826 * mad)
    mask = robust_z < -config.ANOMALY_Z_THRESHOLD

    # Ceiling, not a quota: only ever removes flags, never adds them.
    ceiling = int(len(scores) * config.MAX_ML_FLAG_RATE)
    if mask.sum() > ceiling > 0:
        cutoff_idx = np.argsort(scores)[:ceiling]
        limited = np.zeros_like(mask)
        limited[cutoff_idx] = True
        mask = mask & limited
        logger.warning(
            "ML flag rate hit the %.0f%% ceiling; keeping the %d most anomalous rows.",
            config.MAX_ML_FLAG_RATE * 100, ceiling,
        )
    return mask


def _rank_features(features: pd.DataFrame, anomaly_mask: pd.Series) -> List[str]:
    """Which features separate the flagged rows from the rest.

    Analysts will not trust a score they cannot interrogate, so we surface the
    features that drove the split rather than only the score.
    """
    if anomaly_mask.sum() == 0 or anomaly_mask.all():
        return []
    flagged_mean = features[anomaly_mask.values].mean()
    normal_mean = features[~anomaly_mask.values].mean()
    spread = features.std().replace(0, np.nan)
    separation = ((flagged_mean - normal_mean).abs() / spread).dropna()
    return list(separation.sort_values(ascending=False).head(4).index)


def explain_row(row: pd.Series) -> List[str]:
    """Human-readable reasons this specific row was forwarded."""
    reasons = list(row.get("rule_reasons") or [])
    if row.get("ml_anomaly"):
        score = row.get("ml_score")
        if pd.notna(score):
            reasons.append(
                f"Behavioural model flagged this row (anomaly score {score:+.3f}, "
                f"more than {config.ANOMALY_Z_THRESHOLD:.1f} robust deviations below the batch median)."
            )
        else:
            reasons.append("Batch too small for statistical scoring; forwarded by default.")
    return reasons or ["No specific trigger recorded."]
