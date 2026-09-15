"""RAG provider over the regulatory corpus.

The original singleton assigned `cls._instance` before running initialisation,
so a failure (bad Qdrant key, missing credentials) left a half-built object
cached. The first caller saw the exception; every caller after that got a broken
object with no `.engine` and no error. `_instance` is now only set once
initialisation has fully succeeded.

`query()` also passed a bare string to RetrievalQA where the chain expects
{"query": ...}, which agents.py was already doing correctly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_classic.chains import RetrievalQA
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient

import config
from utils.llm_provider import ProviderConfigError, get_models

logger = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """Raised when the RAG stack cannot be reached. Never swallowed silently."""


class ComplianceIntelligenceProvider:
    """Singleton over the Vertex + Qdrant retrieval stack."""

    _instance: Optional["ComplianceIntelligenceProvider"] = None

    def __new__(cls):
        if cls._instance is None:
            candidate = super().__new__(cls)
            candidate._initialize_provider()  # may raise; nothing cached if it does
            cls._instance = candidate
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the cached instance so the next call reconnects."""
        cls._instance = None

    def _initialize_provider(self) -> None:
        if not config.QDRANT_URL:
            raise ProviderUnavailable("QDRANT_URL is not set.")

        try:
            self.llm, self.embeddings = get_models()
            self.client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY)
            self.vector_store = QdrantVectorStore(
                client=self.client,
                collection_name=config.QDRANT_COLLECTION,
                embedding=self.embeddings,
                vector_name="text-dense",
            )
            self.engine = RetrievalQA.from_chain_type(
                llm=self.llm,
                chain_type="stuff",
                retriever=self.vector_store.as_retriever(
                    search_kwargs={"k": config.RETRIEVER_K}
                ),
            )
        except ProviderConfigError as exc:
            logger.error("Model provider misconfigured: %s", exc)
            raise ProviderUnavailable(str(exc)) from exc
        except Exception as exc:
            logger.error("Compliance provider initialisation failed: %s", exc)
            raise ProviderUnavailable(str(exc)) from exc

        logger.info("Compliance intelligence provider initialised.")

    def health(self) -> tuple[bool, str]:
        """Real reachability check, for the status indicators in the UI.

        The original sidebar rendered "Qdrant Vector DB: Connected" as a static
        string whether or not Qdrant was reachable.
        """
        try:
            names = {c.name for c in self.client.get_collections().collections}
        except Exception as exc:
            return False, f"Qdrant unreachable: {exc}"
        if config.QDRANT_COLLECTION not in names:
            return False, f"Collection '{config.QDRANT_COLLECTION}' not found. Run the ingestion script."
        try:
            count = self.client.count(config.QDRANT_COLLECTION, exact=False).count
        except Exception as exc:
            return False, f"Collection unreadable: {exc}"
        if count == 0:
            return False, "Collection is empty. Run the ingestion script."
        return True, f"{count:,} indexed passages"

    def query(self, user_input: str) -> str:
        """Grounded query against the compliance corpus."""
        try:
            response = self.engine.invoke({"query": user_input})
            return (response or {}).get("result", "No definitive compliance data found.")
        except Exception as exc:
            logger.error("Query execution error: %s", exc)
            return f"Retrieval unavailable: {exc}"


def _cli() -> None:
    logging.basicConfig(level=config.LOG_LEVEL)
    try:
        provider = ComplianceIntelligenceProvider()
    except ProviderUnavailable as exc:
        print(f"[ERROR] {exc}")
        return

    ok, detail = provider.health()
    print(f"\n[SYSTEM] AML compliance interface. Corpus: {'ready' if ok else 'DEGRADED'} - {detail}\n")

    while True:
        try:
            cmd = input("auditor@system:~$ ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[SYSTEM] Shutting down.")
            return
        if cmd.lower() in {"exit", "quit", "q"}:
            return
        if not cmd:
            continue
        print(f"\nREGULATORY FINDING:\n{provider.query(cmd)}\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    _cli()
