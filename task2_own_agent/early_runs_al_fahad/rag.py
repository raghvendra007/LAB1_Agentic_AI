"""RAG component for Task 2: indexes style guide docs and performs cosine similarity search."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import numpy as np

from common.llm_client import LLMClient, count_tokens


@dataclass
class Chunk:
    doc_id: str
    title: str
    text: str
    tokens: int


class StyleGuideRAG:
    def __init__(self, style_guide_dir: str | Path, llm: LLMClient | None = None) -> None:
        self.style_guide_dir = Path(style_guide_dir)
        self.llm = llm or LLMClient()
        self.chunks: list[Chunk] = []
        self.vectors: np.ndarray | None = None
        self._build_index()

    def _extract_doc_id(self, path: Path) -> str:
        # Extracts 'SG-XX' from filenames like 'SG-35_page-structure.md'
        match = re.match(r"(SG-\d+)", path.name)
        return match.group(1) if match else path.stem

    def _build_index(self) -> None:
        md_files = sorted(self.style_guide_dir.glob("*.md"))
        if not md_files:
            raise FileNotFoundError(f"No markdown documents found in {self.style_guide_dir}")

        for file_path in md_files:
            doc_id = self._extract_doc_id(file_path)
            content = file_path.read_text(encoding="utf-8").strip()
            if not content:
                continue

            # Chunk by primary markdown sections (# or ##), fallback to full file if small
            sections = re.split(r"\n(?=#{1,2}\s+)", content)
            for sec in sections:
                sec_text = sec.strip()
                if not sec_text:
                    continue
                first_line = sec_text.splitlines()[0].strip("# ").strip()
                tokens = count_tokens(sec_text)
                self.chunks.append(
                    Chunk(
                        doc_id=doc_id,
                        title=first_line,
                        text=sec_text,
                        tokens=tokens,
                    )
                )

        texts = [f"Document {c.doc_id}: {c.title}\n{c.text}" for c in self.chunks]
        # Embed all chunks using the UPB embedding model
        self.vectors = self.llm.embed(texts)
        # Normalize vectors for cosine similarity computation
        norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        self.vectors = self.vectors / norms

    def search(self, query: str, k: int = 5, max_token_budget: int = 1000) -> list[Chunk]:
        """Returns top-k matching chunks staying within the 1,000-token budget."""
        if not self.chunks or self.vectors is None:
            return []

        # Embed query
        q_vec = self.llm.embed([query])
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # Cosine similarity via dot product against normalized vectors
        scores = np.dot(self.vectors, q_vec.T).squeeze(-1)
        ranked_indices = np.argsort(scores)[::-1]

        selected_chunks: list[Chunk] = []
        accumulated_tokens = 0

        for idx in ranked_indices:
            chunk = self.chunks[idx]
            # Enforce the strict 1,000-token per observation budget rule
            if accumulated_tokens + chunk.tokens > max_token_budget:
                if not selected_chunks:
                    # Guarantee at least one chunk even if it slightly approaches budget
                    selected_chunks.append(chunk)
                break

            selected_chunks.append(chunk)
            accumulated_tokens += chunk.tokens
            if len(selected_chunks) >= k:
                break

        return selected_chunks