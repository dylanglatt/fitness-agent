"""
RAG Knowledge Base Ingestion Script
====================================
Reads all markdown files in the knowledge/ directory,
chunks them into sections, creates embeddings using
sentence-transformers (runs locally, no API needed),
and stores in ChromaDB.

Usage:
    python ingest_knowledge.py

Run this once after adding or updating knowledge files.
ChromaDB persists to disk — no need to re-run unless files change.

Install dependencies first:
    pip install chromadb sentence-transformers
"""

import os
import re
import chromadb
from pathlib import Path
from sentence_transformers import SentenceTransformer

# ── Config ────────────────────────────────────────────────────────────────────
KNOWLEDGE_DIR = Path(__file__).parent  # same folder as this script
CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "fitness_knowledge"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # fast, good quality, ~80MB download
CHUNK_SIZE = 600      # target words per chunk
CHUNK_OVERLAP = 50    # words of overlap between chunks


# ── Chunking ──────────────────────────────────────────────────────────────────
def chunk_markdown(text: str, source: str) -> list[dict]:
    """
    Split markdown into chunks by section (##) headers.
    Falls back to word-count chunking if sections are too large.
    """
    chunks = []

    # Split by ## headers (level 2 and 3)
    sections = re.split(r"\n(?=#{2,3} )", text)

    for section in sections:
        if not section.strip():
            continue

        # Extract section title from first line
        lines = section.strip().splitlines()
        title = lines[0].lstrip("#").strip() if lines else "General"

        # If section is small enough, keep as-is
        words = section.split()
        if len(words) <= CHUNK_SIZE:
            chunks.append({
                "text": section.strip(),
                "source": source,
                "section": title,
            })
        else:
            # Word-count chunk with overlap
            for i in range(0, len(words), CHUNK_SIZE - CHUNK_OVERLAP):
                chunk_words = words[i : i + CHUNK_SIZE]
                chunks.append({
                    "text": " ".join(chunk_words),
                    "source": source,
                    "section": f"{title} (part {i // (CHUNK_SIZE - CHUNK_OVERLAP) + 1})",
                })

    return chunks


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Loading embedding model (downloads ~80MB on first run)...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    print(f"Connecting to ChromaDB at {CHROMA_PATH}...")
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # Delete and recreate collection for clean ingest
    try:
        client.delete_collection(COLLECTION_NAME)
        print("Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    # Gather all markdown files
    md_files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not md_files:
        print(f"No .md files found in {KNOWLEDGE_DIR}")
        return

    all_chunks = []
    for md_file in md_files:
        print(f"Processing {md_file.name}...")
        text = md_file.read_text(encoding="utf-8")
        chunks = chunk_markdown(text, source=md_file.stem)
        all_chunks.extend(chunks)
        print(f"  → {len(chunks)} chunks")

    print(f"\nTotal chunks: {len(all_chunks)}")
    print("Creating embeddings...")

    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts, show_progress_bar=True, batch_size=32)

    print("Storing in ChromaDB...")
    collection.add(
        ids=[f"chunk_{i}" for i in range(len(all_chunks))],
        embeddings=embeddings.tolist(),
        documents=texts,
        metadatas=[{"source": c["source"], "section": c["section"]} for c in all_chunks],
    )

    print(f"\n✅ Done! {len(all_chunks)} chunks stored in ChromaDB at {CHROMA_PATH}")
    print("You can now run the bot — CoachRex will use this knowledge base.")


if __name__ == "__main__":
    main()
