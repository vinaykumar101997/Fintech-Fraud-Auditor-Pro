"""Single source of truth for paths, thresholds, and model settings.

Compliance thresholds change without warning. Keeping them here means a policy
change is a config edit, not a code edit.
"""

import os
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
AUDIT_DB_PATH = Path(os.getenv("AUDIT_DB_PATH", DATA_DIR / "audit_log.sqlite3"))
SANCTIONS_CSV = DATA_DIR / "sanctions_list.csv"
SAMPLE_LEDGER = DATA_DIR / "sample_ledger.csv"
GUIDELINES_PDF = DATA_DIR / "aml_guidelines.pdf"

# --- Model provider -------------------------------------------------------
# "bedrock" (AWS) or "vertex" (GCP). The pipeline is identical either way.
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "bedrock").strip().lower()

# --- AWS / Amazon Bedrock ---
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Must support tool use, because verdicts come back through
# with_structured_output. Anthropic and Nova models do; Titan text does not.
#
# The "us." prefix is a cross-region inference profile. Most current Claude
# models on Bedrock are only invocable through a profile, not a bare model ID,
# and calling the bare ID returns a confusing ValidationException.
BEDROCK_CHAT_MODEL_ID = os.getenv(
    "BEDROCK_CHAT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_EMBED_MODEL_ID = os.getenv("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")

# --- Google Vertex AI ---
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
VERTEX_CHAT_MODEL = os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash")
VERTEX_EMBED_MODEL = os.getenv("VERTEX_EMBED_MODEL", "text-embedding-004")

# --- Shared inference settings ---
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))

# Titan Embed v2 defaults to 1024; Vertex text-embedding-004 is 768. The
# ingestion script sizes the Qdrant collection from the first vector it
# produces, so this only needs to be right for the provider in use. Changing
# providers means re-ingesting into a fresh collection.
_DEFAULT_DIMS = {"bedrock": 1024, "vertex": 768}
EMBEDDING_DIMENSIONS = int(
    os.getenv("EMBEDDING_DIMENSIONS", _DEFAULT_DIMS.get(MODEL_PROVIDER, 1024))
)


def chat_model_name() -> str:
    """Model identifier for audit-log provenance."""
    return BEDROCK_CHAT_MODEL_ID if MODEL_PROVIDER == "bedrock" else VERTEX_CHAT_MODEL

# --- Qdrant ---
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "aml_compliance_docs")
RETRIEVER_K = int(os.getenv("RETRIEVER_K", "5"))

# --- Tier 1: deterministic rules ---
# Currency amounts are Decimal throughout. Never float.
REPORTING_THRESHOLD = Decimal(os.getenv("REPORTING_THRESHOLD", "10000"))

# The structuring band sits just BELOW the reporting threshold. This is the
# band the original build discarded, and it is where structuring lives.
STRUCTURING_FLOOR = Decimal(os.getenv("STRUCTURING_FLOOR", "8000"))

HIGH_RISK_COUNTRIES = [
    c.strip().lower()
    for c in os.getenv(
        "HIGH_RISK_COUNTRIES",
        "Russia,North Korea,Iran,Syria,Belarus,Myanmar,Afghanistan",
    ).split(",")
    if c.strip()
]

# Jurisdictions that are not sanctioned but carry elevated secrecy risk.
SECRECY_JURISDICTIONS = [
    c.strip().lower()
    for c in os.getenv(
        "SECRECY_JURISDICTIONS",
        "Cayman Islands,Panama,Seychelles,British Virgin Islands,Vanuatu",
    ).split(",")
    if c.strip()
]

PLACEHOLDER_ENTITIES = {"unknown_entity", "internal_wallet", "", "nan", "none"}

# --- Tier 0: statistical funnel ---
# contamination is deliberately NOT used. A fixed quota flags 15% of a clean
# batch and drops 85% of a dirty one.
#
# Instead: a robust z-score on the anomaly score, measured against the batch's
# own median and MAD. A homogeneous batch produces no outliers and flags nothing;
# a batch with genuine outliers flags them without a quota forcing the count.
# The sign of decision_function alone is not usable, as it is not calibrated
# across differently-shaped batches.
ANOMALY_Z_THRESHOLD = float(os.getenv("ANOMALY_Z_THRESHOLD", "2.5"))

# Ceiling only, never a target. Protects the LLM budget if a batch is pathological.
MAX_ML_FLAG_RATE = float(os.getenv("MAX_ML_FLAG_RATE", "0.30"))
MIN_ROWS_FOR_ML = int(os.getenv("MIN_ROWS_FOR_ML", "25"))
ML_RANDOM_STATE = 42

# --- Tier 3: topology scoring ---
CIRCULAR_FLOW_POINTS = int(os.getenv("CIRCULAR_FLOW_POINTS", "25"))
CIRCULAR_FLOW_CAP = int(os.getenv("CIRCULAR_FLOW_CAP", "50"))
# Cycles of length 2..CYCLE_MAX_LENGTH through the sender count as circular flow.
CYCLE_MAX_LENGTH = int(os.getenv("CYCLE_MAX_LENGTH", "6"))
# Hard ceiling on search steps per audit (legs tried in the fund-flow search,
# or cycles enumerated in the structural fallback), so a dense graph cannot
# hang it.
CYCLE_SEARCH_LIMIT = int(os.getenv("CYCLE_SEARCH_LIMIT", "10000"))
# A cycle is fund flow, not graph shape: its legs must run forward in time,
# complete within this window, and carry similar amounts, and it must include
# the audited transaction. Businesses that trade both ways form graph cycles
# constantly; round-tripped money does not look like that.
#
# PROVISIONAL: the 1-day default was tuned on the synthetic sample ledger
# only (7 days still left 259/420 clean rows flagged there). Validate both
# values on the IBM AML dataset in a later validation pass before relying on them.
CYCLE_MAX_WINDOW_DAYS = float(os.getenv("CYCLE_MAX_WINDOW_DAYS", "1"))
# Max spread of leg amounts, as (largest - smallest) / largest. Also
# provisional, as above.
CYCLE_AMOUNT_TOLERANCE = float(os.getenv("CYCLE_AMOUNT_TOLERANCE", "0.2"))
OBFUSCATION_POINTS = int(os.getenv("OBFUSCATION_POINTS", "50"))
MISSING_DATA_POINTS = int(os.getenv("MISSING_DATA_POINTS", "10"))
RAG_SUSPICIOUS_POINTS = int(os.getenv("RAG_SUSPICIOUS_POINTS", "20"))
HITL_REVIEW_THRESHOLD = int(os.getenv("HITL_REVIEW_THRESHOLD", "70"))

# --- Screening ---
SCREENING_MATCH_THRESHOLD = int(os.getenv("SCREENING_MATCH_THRESHOLD", "88"))

# --- Runtime ---
LLM_MAX_WORKERS = int(os.getenv("LLM_MAX_WORKERS", "4"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.5"))
MAX_LEDGER_ROWS = int(os.getenv("MAX_LEDGER_ROWS", "50000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
