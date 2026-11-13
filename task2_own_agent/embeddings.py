"""Embeddings: text -> vector of numbers. Similar meaning -> similar vectors (cosine close to 1).

One wrapper, two backends, same interface:
    emb = Embedder(llm)               # API model ($LAB_EMBEDDING_MODEL via LLMClient)
    emb = Embedder(local=True)        # sentence-transformers MiniLM on this laptop (free, offline after first download)
    vectors = emb(["a text", "another"])       # numpy array (2, dim)
    vectors = emb.cached(texts)                # same, but saved in .cache/ (keyed by model + texts)
    similarity("night mode", "dark theme", emb)  # cosine, -1..1

Try it:  python task2_own_agent/embeddings.py "night mode colors" "dark theme palette" "team photo" [--api]
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # 384 numbers per text, ~90 MB


class Embedder:
    """Call it with a list of texts, get one row per text. Picks the backend once, so vectors always fit together."""

    def __init__(self, llm=None, local: bool = False, cache_dir: Path = CACHE_DIR):
        self.llm, self.local, self.cache_dir = llm, local, Path(cache_dir)
        self.name = LOCAL_MODEL if local else (llm.embedding_model if llm else "none")
        self._st = None  # the local model, loaded on first use
        self.last_cache_hit = False

    def __call__(self, texts: list[str]) -> np.ndarray:
        if self.local:
            if self._st is None:
                from sentence_transformers import SentenceTransformer

                self._st = SentenceTransformer(LOCAL_MODEL)
            return np.asarray(self._st.encode(texts), dtype=np.float32)
        return self.llm.embed(texts)

    def cached(self, texts: list[str]) -> np.ndarray:
        """Like calling it, but reuse vectors saved on disk (the style guide is embedded once, not every run)."""
        key = hashlib.sha256((self.name + "\0" + "\0".join(texts)).encode()).hexdigest()[:16]
        path = self.cache_dir / f"emb_{key}.npy"
        self.last_cache_hit = path.exists()
        if self.last_cache_hit:
            return np.load(path)
        vectors = self(texts)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(path, vectors)
        return vectors


def normalize(m: np.ndarray) -> np.ndarray:
    """Divide every vector by its length. Afterwards cosine similarity is just the dot product."""
    return m / np.maximum(np.linalg.norm(m, axis=-1, keepdims=True), 1e-12)


def similarity(a: str, b: str, emb: Embedder) -> float:
    va, vb = normalize(emb([a, b]))
    return float(va @ vb)


if __name__ == "__main__":
    texts = [t for t in sys.argv[1:] if not t.startswith("--")] or ["night mode colors", "dark theme palette", "team photo"]
    if "--api" in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from common.llm_client import LLMClient

        emb = Embedder(LLMClient())
    else:
        emb = Embedder(local=True)
    v = normalize(emb(texts))
    print(f"model {emb.name}: {len(texts)} texts -> {v.shape[1]} numbers each\n")
    print("cosine".ljust(8) + "".join(f"{i:>8}" for i in range(len(texts))))
    for i, row in enumerate(v @ v.T):
        print(f"{i:<8}" + "".join(f"{x:8.2f}" for x in row) + f"   {texts[i][:40]}")
