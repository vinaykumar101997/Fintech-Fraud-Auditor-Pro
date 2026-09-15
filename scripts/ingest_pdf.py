"""Index regulatory PDFs into the vector store.

Changes from the original: paths come from config rather than walking two
directories up from __file__, embeddings are batched so a large document does
not become one enormous request, and the target file is a CLI argument.

    python scripts/ingest_pdf.py
    python scripts/ingest_pdf.py --file data/fatf_2025.pdf
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_community.document_loaders import PyMuPDFLoader  # noqa: E402
from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, PointStruct, VectorParams  # noqa: E402

import config  # noqa: E402
from utils.llm_provider import get_embeddings  # noqa: E402

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 64


class DocumentIngestionEngine:
    def __init__(self, collection_name: str = None):
        self.collection_name = collection_name or config.QDRANT_COLLECTION

        if not config.QDRANT_URL:
            raise SystemExit("QDRANT_URL is not set. Configure .env first.")

        self.embeddings = get_embeddings()
        self.client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY)

    def _ensure_collection(self, vector_size: int) -> None:
        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection_name in existing:
            return
        logger.info("Creating collection '%s' (dim=%d)", self.collection_name, vector_size)
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={"text-dense": VectorParams(size=vector_size, distance=Distance.COSINE)},
        )

    def _embed_in_batches(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            vectors.extend(self.embeddings.embed_documents(batch))
            logger.info("Embedded %d/%d fragments", len(vectors), len(texts))
        return vectors

    def process_pdf(self, file_path: Path, chunk_size: int = 1200, overlap: int = 150) -> int:
        if not file_path.exists():
            raise SystemExit(f"Source file not found: {file_path}")

        logger.info("Processing %s", file_path)
        documents = PyMuPDFLoader(str(file_path)).load()
        chunks = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=overlap,
            separators=["\n\n", "\n", ". ", " "],
        ).split_documents(documents)

        texts = [d.page_content for d in chunks if d.page_content.strip()]
        metadatas = [d.metadata for d in chunks if d.page_content.strip()]
        if not texts:
            raise SystemExit("No extractable text. Is this a scanned PDF? Run OCR first.")

        vectors = self._embed_in_batches(texts)
        self._ensure_collection(len(vectors[0]))

        points = [
            PointStruct(
                # Deterministic ID from content, so re-ingestion updates rather
                # than duplicating.
                id=str(uuid.uuid5(uuid.NAMESPACE_DNS, text)),
                vector={"text-dense": vector},
                payload={"page_content": text, "metadata": meta, "source_file": file_path.name},
            )
            for text, vector, meta in zip(texts, vectors, metadatas)
        ]

        for start in range(0, len(points), 128):
            self.client.upsert(collection_name=self.collection_name, points=points[start : start + 128])

        logger.info("Indexed %d fragments into '%s'", len(points), self.collection_name)
        return len(points)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=config.GUIDELINES_PDF)
    parser.add_argument("--collection", default=config.QDRANT_COLLECTION)
    args = parser.parse_args()

    engine = DocumentIngestionEngine(args.collection)
    count = engine.process_pdf(args.file)
    print(f"Indexed {count} fragments from {args.file.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
