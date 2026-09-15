"""Verify every external dependency before running the app.

Checks are ordered cheapest-first and each one is independent, so a single
missing credential does not mask the rest. Exit code 0 means the app will start.

    python scripts/preflight.py
    python scripts/preflight.py --skip-network
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_ICON = {PASS: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}

results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"{_ICON[status]} {name}" + (f"  -  {detail}" if detail else ""))


# --- 1. Interpreter -------------------------------------------------------
def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        check("Python version", FAIL, f"{major}.{minor} found; 3.11+ required")
    elif (major, minor) == (3, 10):
        check("Python version", WARN, f"{major}.{minor}; 3.11 recommended")
    else:
        check("Python version", PASS, f"{major}.{minor}")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    check("Virtual environment", PASS if in_venv else WARN,
          sys.prefix if in_venv else "not active; installing to the system interpreter")


# --- 2. Packages ----------------------------------------------------------
def check_packages() -> None:
    required = {
        "streamlit": "streamlit", "pandas": "pandas", "sklearn": "scikit-learn",
        "pydantic": "pydantic", "rapidfuzz": "rapidfuzz", "plotly": "plotly",
        "dotenv": "python-dotenv", "langgraph": "langgraph",
        "langchain_core": "langchain-core", "qdrant_client": "qdrant-client",
        "langchain_google_vertexai": "langchain-google-vertexai",
        "langchain_qdrant": "langchain-qdrant",
        "langchain_text_splitters": "langchain-text-splitters",
        "langchain_classic": "langchain-classic", "fitz": "PyMuPDF",
    }
    missing = []
    for module, package in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        check("Python packages", FAIL, f"missing: {', '.join(missing)}  ->  pip install -r requirements.txt")
    else:
        check("Python packages", PASS, f"all {len(required)} present")


# --- 3. Configuration -----------------------------------------------------
def check_env_file() -> None:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".env").exists():
        check(".env file", FAIL, "not found  ->  cp .env.example .env")
        return
    check(".env file", PASS, str(root / ".env"))

    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=True)
    except ImportError:
        check("dotenv load", WARN, "python-dotenv not installed; skipping")
        return

    import importlib
    import config as _cfg
    importlib.reload(_cfg)

    if _cfg.MODEL_PROVIDER == "bedrock":
        wanted = (("AWS_REGION", True), ("QDRANT_URL", True), ("QDRANT_API_KEY", False))
    else:
        wanted = (("GCP_PROJECT_ID", True), ("QDRANT_URL", True), ("QDRANT_API_KEY", False))

    for var, required in wanted:
        value = os.getenv(var, "")
        if not value:
            check(f"env {var}", FAIL if required else WARN, "not set")
        elif "your-" in value or "here" in value:
            check(f"env {var}", FAIL, "still the placeholder from .env.example")
        else:
            shown = value if len(value) < 45 else value[:40] + "..."
            check(f"env {var}", PASS, shown)


def check_secrets_not_committed() -> None:
    root = Path(__file__).resolve().parent.parent
    leaked = [p.name for p in root.glob("*.json")
              if any(k in p.name.lower() for k in ("key", "credential", "service", "account"))]
    if leaked:
        check("Credentials in repo root", FAIL,
              f"{', '.join(leaked)} - move outside the build context and use ADC")
    else:
        check("Credentials in repo root", PASS, "none found")

    if not (root / ".dockerignore").exists():
        check(".dockerignore", FAIL, "missing; docker build would copy .env into the image")
    else:
        content = (root / ".dockerignore").read_text()
        check(".dockerignore", PASS if ".env" in content else FAIL,
              "excludes .env" if ".env" in content else ".env not excluded")


# --- 4. Model provider ----------------------------------------------------
def check_provider(skip_network: bool) -> None:
    import config

    if config.MODEL_PROVIDER not in ("bedrock", "vertex"):
        check("MODEL_PROVIDER", FAIL, f"'{config.MODEL_PROVIDER}' unrecognised; use bedrock or vertex")
        return
    check("MODEL_PROVIDER", PASS, config.MODEL_PROVIDER)

    if config.MODEL_PROVIDER == "bedrock":
        _check_aws_credentials(skip_network)
        _check_bedrock(skip_network)
    else:
        _check_gcloud(skip_network)
        _check_vertex(skip_network)


def _check_aws_credentials(skip_network: bool) -> None:
    if shutil.which("aws"):
        check("AWS CLI", PASS, shutil.which("aws"))
    else:
        check("AWS CLI", WARN, "not on PATH; fine if using an IAM role or env vars")

    try:
        import boto3
        from botocore.exceptions import NoCredentialsError
    except ImportError:
        check("boto3", FAIL, "not installed  ->  pip install -r requirements.txt")
        return

    session = boto3.Session()
    creds = session.get_credentials()
    if creds is None:
        check("AWS credentials", FAIL,
              "none found  ->  aws configure sso, or aws configure, or attach an IAM role")
        return
    check("AWS credentials", PASS, f"method: {creds.method}")

    region = session.region_name or os.getenv("AWS_REGION")
    check("AWS region", PASS if region else FAIL,
          region or "not set  ->  set AWS_REGION in .env")

    if skip_network:
        return
    try:
        identity = session.client("sts").get_caller_identity()
        arn = identity["Arn"]
        check("AWS identity", PASS, f"{arn.split('/')[-1]} in account {identity['Account']}")
    except NoCredentialsError:
        check("AWS identity", FAIL, "credentials present but unusable")
    except Exception as exc:
        check("AWS identity", FAIL, f"{type(exc).__name__}: {str(exc)[:80]}")


def _check_bedrock(skip_network: bool) -> None:
    if skip_network:
        check("Bedrock reachable", WARN, "skipped")
        return

    import config
    try:
        import boto3
    except ImportError:
        return

    # Model access must be requested per account and per region. This is the
    # single most common reason a first Bedrock call fails.
    try:
        control = boto3.client("bedrock", region_name=config.AWS_REGION)
        available = {m["modelId"] for m in control.list_foundation_models()["modelSummaries"]}
        base_id = config.BEDROCK_CHAT_MODEL_ID.split(".", 1)[-1] if config.BEDROCK_CHAT_MODEL_ID.startswith(
            ("us.", "eu.", "apac.")) else config.BEDROCK_CHAT_MODEL_ID
        check("Bedrock model catalogue", PASS if base_id in available else WARN,
              f"{len(available)} models in {config.AWS_REGION}"
              + ("" if base_id in available else f"; {base_id} not listed"))
    except Exception as exc:
        check("Bedrock model catalogue", WARN, f"could not list: {str(exc)[:70]}")

    try:
        from utils.llm_provider import get_chat_model
        get_chat_model().invoke("ping")
        check("Bedrock inference", PASS, config.BEDROCK_CHAT_MODEL_ID)
    except Exception as exc:
        msg = str(exc)
        hint = ""
        if "AccessDenied" in msg or "not authorized" in msg:
            hint = "  ->  attach bedrock:InvokeModel, and request model access in the console"
        elif "ValidationException" in msg and "inference profile" in msg.lower():
            hint = "  ->  use the inference profile ID, e.g. prefix with 'us.'"
        elif "ValidationException" in msg:
            hint = "  ->  model ID wrong or not available in this region"
        elif "could not be found" in msg or "ResourceNotFound" in msg:
            hint = "  ->  model not enabled in this region; check Bedrock > Model access"
        elif "ThrottlingException" in msg:
            hint = "  ->  throttled; request a quota increase or retry"
        check("Bedrock inference", FAIL, f"{msg[:80]}{hint}")

    try:
        from utils.llm_provider import get_embeddings
        vector = get_embeddings().embed_query("ping")
        matches = len(vector) == config.EMBEDDING_DIMENSIONS
        check("Bedrock embeddings", PASS if matches else WARN,
              f"{len(vector)} dimensions"
              + ("" if matches else f"; config says {config.EMBEDDING_DIMENSIONS}"))
    except Exception as exc:
        check("Bedrock embeddings", FAIL, f"{str(exc)[:80]}")


def _check_gcloud(skip_network: bool) -> None:
    if not shutil.which("gcloud"):
        check("gcloud CLI", WARN, "not on PATH; fine if using Workload Identity")
    else:
        check("gcloud CLI", PASS, shutil.which("gcloud"))

    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        path = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        check("GOOGLE_APPLICATION_CREDENTIALS", PASS if path.exists() else FAIL,
              str(path) if path.exists() else f"set but not found: {path}")
        return

    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if os.name == "nt":
        adc = Path(os.getenv("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    check("Application Default Credentials", PASS if adc.exists() else FAIL,
          str(adc) if adc.exists() else "run: gcloud auth application-default login")


def _check_vertex(skip_network: bool) -> None:
    if skip_network:
        check("Vertex AI reachable", WARN, "skipped")
        return
    try:
        import config
        from utils.llm_provider import get_chat_model
        get_chat_model().invoke("ping")
        check("Vertex AI reachable", PASS, f"{config.VERTEX_CHAT_MODEL} @ {config.GCP_LOCATION}")
    except Exception as exc:
        msg = str(exc)
        hint = ""
        if "403" in msg or "PERMISSION" in msg.upper():
            hint = "  ->  grant roles/aiplatform.user, enable aiplatform.googleapis.com"
        elif "404" in msg:
            hint = "  ->  model unavailable in this region; try us-central1"
        elif "billing" in msg.lower():
            hint = "  ->  enable billing on the project"
        check("Vertex AI reachable", FAIL, f"{msg[:80]}{hint}")


# --- 5. Qdrant ------------------------------------------------------------
def check_qdrant(skip_network: bool) -> None:
    if skip_network:
        check("Qdrant reachable", WARN, "skipped")
        return
    try:
        import config
        from qdrant_client import QdrantClient
        client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=10)
        names = {c.name for c in client.get_collections().collections}
        check("Qdrant reachable", PASS, f"{len(names)} collection(s)")

        if config.QDRANT_COLLECTION not in names:
            check("Compliance corpus", FAIL,
                  f"'{config.QDRANT_COLLECTION}' missing  ->  python scripts/ingest_pdf.py")
        else:
            count = client.count(config.QDRANT_COLLECTION, exact=False).count
            check("Compliance corpus", PASS if count else FAIL,
                  f"{count:,} passages" if count else "collection is empty  ->  run ingestion")
    except Exception as exc:
        check("Qdrant reachable", FAIL, f"{type(exc).__name__}: {str(exc)[:90]}")


# --- 6. Local data --------------------------------------------------------
def check_data_files() -> None:
    import config

    check("Sample ledger", PASS if config.SAMPLE_LEDGER.exists() else WARN,
          str(config.SAMPLE_LEDGER) if config.SAMPLE_LEDGER.exists()
          else "run: python scripts/generate_sample_ledger.py")

    check("Regulatory PDF", PASS if config.GUIDELINES_PDF.exists() else WARN,
          str(config.GUIDELINES_PDF) if config.GUIDELINES_PDF.exists()
          else "download FATF guidance to data/aml_guidelines.pdf")

    if config.SANCTIONS_CSV.exists():
        rows = sum(1 for _ in config.SANCTIONS_CSV.open()) - 1
        real = rows > 50
        check("Sanctions watchlist", PASS if real else WARN,
              f"{rows} entries" + ("" if real else " - demo list; load an OFAC SDN export"))
    else:
        check("Sanctions watchlist", WARN, "not found; falling back to the built-in demo list")

    try:
        config.AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe = config.AUDIT_DB_PATH.parent / ".write_probe"
        probe.write_text("x"); probe.unlink()
        check("Audit log directory writable", PASS, str(config.AUDIT_DB_PATH.parent))
    except Exception as exc:
        check("Audit log directory writable", FAIL, str(exc)[:90])


# --- 7. Test suite --------------------------------------------------------
def check_tests() -> None:
    if not shutil.which("pytest"):
        check("Test suite", WARN, "pytest not installed")
        return
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
                          capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parent.parent)
    tail = (proc.stdout.strip().splitlines() or ["no output"])[-1]
    check("Test suite", PASS if proc.returncode == 0 else FAIL, tail[:90])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-network", action="store_true",
                        help="skip Vertex AI and Qdrant reachability checks")
    args = parser.parse_args()

    print("\nPreflight check\n" + "=" * 70)
    print("\n-- Environment --")
    check_python()
    check_packages()
    print("\n-- Configuration --")
    check_env_file()
    check_secrets_not_committed()
    print("\n-- Model provider --")
    check_provider(args.skip_network)
    print("\n-- Vector store --")
    check_qdrant(args.skip_network)
    print("\n-- Local data --")
    check_data_files()
    print("\n-- Tests --")
    check_tests()

    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]

    print("\n" + "=" * 70)
    print(f"{len(results) - len(failed) - len(warned)} passed, {len(warned)} warnings, {len(failed)} failures")

    if failed:
        print("\nBlocking issues:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        print("\nThe app will not start until these are resolved.")
        return 1

    if warned:
        print("\nNon-blocking. The app will start; some features are degraded:")
        for name, _, detail in warned:
            print(f"  - {name}: {detail}")

    print("\nReady. Start with:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
