"""Ledger ingestion: validate the schema before anything touches the data.

The original build assumed `amount`, `country`, and `transaction_id` existed and
crashed with a raw KeyError when they did not. It also called float() on values
like "$50,000", which raises. Both are handled here, once, at the boundary.

Money is parsed to Decimal. A parallel float column exists only because
scikit-learn cannot consume Decimal; it is never used for comparisons.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple

import pandas as pd

import config

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("transaction_id", "amount", "country")

COLUMN_ALIASES: Dict[str, str] = {
    "from": "sender",
    "source": "sender",
    "originator": "sender",
    "sender_name": "sender",
    "to": "receiver",
    "destination": "receiver",
    "beneficiary": "receiver",
    "receiver_name": "receiver",
    "id": "transaction_id",
    "txn_id": "transaction_id",
    "reference": "transaction_id",
    "value": "amount",
    "amt": "amount",
    "jurisdiction": "country",
    "counterparty_country": "country",
    "date": "timestamp",
    "datetime": "timestamp",
    "booked_at": "timestamp",
}

_CURRENCY_NOISE = re.compile(r"[^\d.\-]")
_PAREN_NEGATIVE = re.compile(r"^\((.*)\)$")


class LedgerSchemaError(ValueError):
    """Raised when an uploaded ledger cannot be used. Message is user-facing."""


def parse_money(raw) -> Decimal:
    """Parse a currency cell into Decimal.

    Handles "$50,000", "1,200.00", "(500)" for negatives, and plain numerics.
    Raises InvalidOperation on anything unparseable so the caller can report the
    offending row rather than silently coercing it to zero.
    """
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int,)):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))

    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        raise InvalidOperation("empty amount")

    negative = False
    paren = _PAREN_NEGATIVE.match(text)
    if paren:
        negative = True
        text = paren.group(1)

    cleaned = _CURRENCY_NOISE.sub("", text)
    if not cleaned or cleaned in {"-", "."}:
        raise InvalidOperation(f"unparseable amount: {raw!r}")

    value = Decimal(cleaned)
    return -value if negative else value


def normalize_entity(raw) -> str:
    """Collapse blanks and placeholders to a single canonical unknown marker."""
    text = str(raw).strip() if raw is not None else ""
    if text.lower() in config.PLACEHOLDER_ENTITIES:
        return "Unknown_Entity"
    return text


def load_ledger(source) -> Tuple[pd.DataFrame, List[str]]:
    """Read and validate a ledger CSV.

    Returns the normalized frame plus a list of non-fatal warnings.
    Raises LedgerSchemaError with an actionable message on anything fatal.
    """
    try:
        df = pd.read_csv(source, dtype=str, keep_default_na=False)
    except Exception as exc:  # pragma: no cover - pandas surfaces many types
        raise LedgerSchemaError(f"Could not read the CSV: {exc}") from exc

    if df.empty:
        raise LedgerSchemaError("The uploaded file contains no rows.")
    if len(df) > config.MAX_LEDGER_ROWS:
        raise LedgerSchemaError(
            f"Ledger has {len(df):,} rows; the limit is {config.MAX_LEDGER_ROWS:,}. "
            "Split the file and audit in batches."
        )

    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    df = df.rename(columns=COLUMN_ALIASES)
    df = df.loc[:, ~df.columns.duplicated()]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise LedgerSchemaError(
            "The ledger is missing required column(s): "
            + ", ".join(missing)
            + ". Found: "
            + ", ".join(df.columns)
            + ". Accepted aliases include "
            + ", ".join(sorted(COLUMN_ALIASES)[:6])
            + "."
        )

    warnings: List[str] = []

    # Amounts. Collect every bad row rather than dying on the first.
    parsed, bad_rows = [], []
    for idx, raw in enumerate(df["amount"]):
        try:
            parsed.append(parse_money(raw))
        except (InvalidOperation, ValueError, ArithmeticError):
            parsed.append(None)
            bad_rows.append((idx + 2, raw))  # +2 for header and 1-indexing

    if len(bad_rows) == len(df):
        sample = ", ".join(repr(v) for _, v in bad_rows[:3])
        raise LedgerSchemaError(
            f"No amount in the file could be parsed as currency. Examples: {sample}"
        )
    if bad_rows:
        preview = ", ".join(f"line {ln} ({v!r})" for ln, v in bad_rows[:5])
        warnings.append(
            f"{len(bad_rows)} row(s) had an unparseable amount and were dropped: {preview}"
        )

    df["amount"] = parsed
    df = df[df["amount"].notna()].reset_index(drop=True)

    negatives = sum(1 for a in df["amount"] if a < 0)
    if negatives:
        warnings.append(
            f"{negatives} row(s) have a negative amount; screened on absolute value."
        )

    # Float mirror for scikit-learn only.
    df["amount_float"] = [float(abs(a)) for a in df["amount"]]

    # Identity columns.
    df["transaction_id"] = df["transaction_id"].astype(str).str.strip()
    blank_ids = df["transaction_id"].eq("")
    if blank_ids.any():
        df.loc[blank_ids, "transaction_id"] = [
            f"ROW-{i + 1}" for i in df.index[blank_ids]
        ]
        warnings.append(f"{int(blank_ids.sum())} row(s) had no ID; synthetic IDs assigned.")

    if df["transaction_id"].duplicated().any():
        dupes = int(df["transaction_id"].duplicated().sum())
        df["transaction_id"] = [
            f"{tid}#{i}" if dup else tid
            for i, (tid, dup) in enumerate(
                zip(df["transaction_id"], df["transaction_id"].duplicated(keep=False))
            )
        ]
        warnings.append(f"{dupes} duplicate transaction ID(s) were suffixed to stay unique.")

    df["country"] = df["country"].astype(str).str.strip()

    for col in ("sender", "receiver"):
        if col not in df.columns:
            df[col] = "Unknown_Entity"
            warnings.append(f"No '{col}' column found; topology analysis will be limited.")
        else:
            df[col] = df[col].map(normalize_entity)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", format="mixed")
        if df["timestamp"].isna().all():
            warnings.append("No timestamp parsed; velocity features are disabled.")
            df = df.drop(columns=["timestamp"])
    else:
        warnings.append("No timestamp column; velocity features are disabled.")

    logger.info("Ledger loaded: %d rows, %d warnings", len(df), len(warnings))
    return df, warnings
