"""RAG over the style guide: chunk, embed, cosine search.

Chunking: each document is split at its `## ` headings, and long sections at blank lines,
so a chunk stays below MAX_CHUNK_TOKENS. Every chunk starts with a header line
"[SG-13] Night mode palette (version 2) | Status: ACTIVE (version 2)", so the model always sees
the source id and whether the rule is deprecated, even when it only gets one section.

Embeddings: see embeddings.py (API model by default, local MiniLM with --local, cached in .cache/).

Quick check:  python task2_own_agent/rag.py "night mode colors" [--local]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.llm_client import count_tokens  # noqa: E402
from embeddings import CACHE_DIR, LOCAL_MODEL, Embedder, normalize as _normalize  # noqa: E402,F401  (wrapper: embeddings.py)

GUIDE_DIR = Path(__file__).resolve().parent / "style_guide"
MAX_CHUNK_TOKENS = 250


@dataclass
class Chunk:
    doc_id: str  # e.g. "SG-13"
    text: str  # header line + section text
    score: float = 0.0


def load_chunks(guide_dir: Path = GUIDE_DIR) -> list[Chunk]:
    chunks = []
    for path in sorted(guide_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        doc_id = path.name.split("_")[0]
        title = text.splitlines()[0].lstrip("# ").split(":", 1)[-1].strip()
        status = re.search(r"^Status:\s*(.+)$", text, re.M)
        header = f"[{doc_id}] {title}" + (f" | Status: {status.group(1).strip()}" if status else "")
        body = text.split("\n", 1)[1] if "\n" in text else ""
        for section in re.split(r"\n(?=## )", body):
            for piece in _split_long(section.strip()):
                if piece:
                    chunks.append(Chunk(doc_id, f"{header}\n{piece}"))
    return chunks


def _split_long(text: str) -> list[str]:
    """Merge paragraphs into pieces of at most MAX_CHUNK_TOKENS (a single huge paragraph stays whole)."""
    pieces, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if current and count_tokens(candidate) > MAX_CHUNK_TOKENS:
            pieces.append(current)
            current = para
        else:
            current = candidate
    return pieces + [current]


class StyleGuideRAG:
    def __init__(self, embedder: Embedder, guide_dir: Path = GUIDE_DIR):
        self.embed = embedder
        self.chunks = load_chunks(guide_dir)
        self.matrix = _normalize(embedder.cached([c.text for c in self.chunks]))

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        q = _normalize(self.embed([query])[0])
        scores = self.matrix @ q
        top = np.argsort(-scores)[:k]
        return [Chunk(self.chunks[i].doc_id, self.chunks[i].text, float(scores[i])) for i in top]


def format_observation(chunks: list[Chunk], max_tokens: int = 1000) -> str:
    """Join chunks best-first; stop before the style guide text exceeds max_tokens."""
    parts, used = [], 0
    for c in chunks:
        n = count_tokens(c.text)
        if used + n > max_tokens:
            break
        parts.append(c.text)
        used += n
    if not parts:
        return "No style guide text found for this query."
    return "\n\n---\n\n".join(parts)


# query -> (expected ACTIVE document, its DEPRECATED older version or None)
CHECKS = [
    ("night mode colors", "SG-13", "SG-12"),
    ("day mode background and text color", "SG-11", "SG-10"),
    ("what must the footer say", "SG-40", "SG-39"),
    ("mobile breakpoint width", "SG-50", "SG-49"),
    ("remember the chosen theme after reload", "SG-21", "SG-19"),
    ("theme toggle button", "SG-22", None),
    ("page title format", "SG-02", None),
    ("member card markup", "SG-30", None),
    ("which font family", "SG-51", None),
    ("hot take badge", "SG-31", None),
]


def check(rag: "StyleGuideRAG", k: int) -> None:
    """Retrieval check: rank of the expected document (doc rank, first chunk counts) for each query."""
    def rank(ids, doc):
        return ids.index(doc) + 1 if doc in ids else None
    hits = 0
    print(f"{'query':40} expected  rank  deprecated  rank")
    for query, active, old in CHECKS:
        ids = list(dict.fromkeys(c.doc_id for c in rag.search(query, len(rag.chunks))))
        r, ro = rank(ids, active), rank(ids, old) if old else None
        hits += r <= k
        flag = "  <- deprecated ranks higher" if ro and ro < r else ""
        print(f"{query:40} {active:8} {r:>4}  {old or '-':10} {ro or '-':>4}{flag}")
    print(f"expected document in top {k}: {hits}/{len(CHECKS)} (chunk size {MAX_CHUNK_TOKENS}, {len(rag.chunks)} chunks)")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("query", nargs="?", help="a search query (or use --check)")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--check", action="store_true", help="retrieval check table (CHECKS)")
    p.add_argument("--chunk", type=int, default=MAX_CHUNK_TOKENS, help="max tokens per chunk")
    p.add_argument("--local", action="store_true")
    a = p.parse_args()
    MAX_CHUNK_TOKENS = a.chunk
    llm = None
    if not a.local:
        from common.llm_client import LLMClient

        llm = LLMClient(log_path=CACHE_DIR / "rag_cli.jsonl")
    rag = StyleGuideRAG(Embedder(llm, local=a.local))
    if a.check:
        check(rag, a.k)
    else:
        print(f"{len(rag.chunks)} chunks")
        for c in rag.search(a.query, a.k):
            print(f"\n{c.score:.3f} {c.text[:300]}")
