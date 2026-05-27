"""
Knowledge Retriever
====================
Provides semantic search over the ChromaDB fitness knowledge base.
Used by ai/coach.py to inject relevant knowledge into Claude prompts.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "fitness_knowledge"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class KnowledgeRetriever:
    """Semantic search over the fitness knowledge base."""

    def __init__(self):
        self._client = None
        self._collection = None
        self._model = None
        self._initialized = False

    def _initialize(self):
        """Lazy initialization — only loads models when first queried."""
        if self._initialized:
            return

        try:
            import chromadb
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(EMBEDDING_MODEL)
            self._client = chromadb.PersistentClient(path=str(CHROMA_PATH))
            self._collection = self._client.get_collection(COLLECTION_NAME)
            self._initialized = True
            logger.info(f"Knowledge base loaded: {self._collection.count()} chunks")

        except Exception as e:
            logger.warning(f"Knowledge base not available: {e}. Run ingest_knowledge.py first.")
            self._initialized = False

    def retrieve(self, query: str, n_results: int = 4) -> str:
        """
        Search the knowledge base for content relevant to the query.
        Returns a formatted string ready to inject into a prompt.
        Returns empty string if knowledge base is unavailable.
        """
        self._initialize()

        if not self._initialized or self._collection is None:
            return ""

        try:
            embedding = self._model.encode([query]).tolist()
            results = self._collection.query(
                query_embeddings=embedding,
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )

            docs = results["documents"][0]
            metas = results["metadatas"][0]
            distances = results["distances"][0]

            if not docs:
                return ""

            # Only include sufficiently relevant results (cosine distance < 0.6)
            relevant = [
                (doc, meta, dist)
                for doc, meta, dist in zip(docs, metas, distances)
                if dist < 0.6
            ]

            if not relevant:
                return ""

            parts = ["RELEVANT FITNESS KNOWLEDGE:"]
            for doc, meta, dist in relevant:
                source = meta.get("source", "unknown").replace("_", " ").title()
                section = meta.get("section", "")
                parts.append(f"\n[{source} — {section}]\n{doc.strip()}")

            return "\n".join(parts)

        except Exception as e:
            logger.warning(f"Knowledge retrieval failed: {e}")
            return ""
