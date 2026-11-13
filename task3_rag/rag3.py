"""Task 3: retrieval configurations R1–R5 on PaderQueue, plus answer generation.

  R1  BM25 on the same recursive chunks as R3 (identifier-aware tokenizer)
  R2  fixed-size token windows + dense embeddings
  R3  recursive chunking (headings > paragraphs > lines > sentences > words) + dense embeddings
  R4  hybrid: alpha * minmax(dense R3) + (1 - alpha) * minmax(BM25 R1), per query
  R5  R4 + cross-encoder re-ranking of the top RERANK_POOL chunks

Every chunk starts with a metadata line "title (type, version)"; it is not counted in the chunk size.

Usage (from the repository root):
  python task3_rag/rag3.py retrieve r3 --size 128            -> results/retrieval_r3_128_dev.jsonl + timing
  python task3_rag/rag3.py sweep-alpha --size 128            -> recall@5 per alpha (tuned on dev, see README)
  python task3_rag/rag3.py all                               -> every configuration + evaluate.py metrics
  python task3_rag/rag3.py answer results/retrieval_r4_128_dev.jsonl --sample 50
  python task3_rag/rag3.py predict r5 --size 128             -> predictions_test.jsonl
Add --local to embed with all-MiniLM-L6-v2 instead of the gateway model.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import tiktoken
from rank_bm25 import BM25Okapi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task2_own_agent"))
from common.llm_client import LLMClient  # noqa: E402
from rag import Embedder, _normalize  # noqa: E402  (Task 2 code, reused)

DATA, RESULTS, CACHE = HERE / "data", HERE / "results", HERE / ".cache"
ENC = tiktoken.get_encoding("cl100k_base")
OVERLAP = 16
ALPHA = 0.5  # dense weight in R4/R5; chosen with sweep-alpha, see README
RERANK_POOL = 30
CROSS_ENCODER = "cross-encoder/ms-marco-MiniLM-L-6-v2"
KEEP_DOCS, KEEP_CONTEXTS = 20, 5
ABSTAIN = "The documentation does not contain this information."


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def ntok(text: str) -> int:
    return len(ENC.encode(text))


# ---------------------------------------------------------------- chunking
def chunk_fixed(text: str, size: int, overlap: int = OVERLAP) -> list[str]:
    ids = ENC.encode(text)
    step = size - overlap
    return [ENC.decode(ids[i:i + size]) for i in range(0, max(len(ids) - overlap, 1), step)]


SEPARATORS = [r"\n(?=#{1,6} )", r"\n\n", r"\n", r"(?<=[.!?]) ", r" "]


def _split(text: str, size: int, level: int = 0) -> list[str]:
    """Split at the coarsest separator until every piece fits into `size` tokens."""
    if ntok(text) <= size or level == len(SEPARATORS):
        return [text]
    parts = [p for p in re.split(SEPARATORS[level], text) if p.strip()]
    return [piece for p in parts for piece in _split(p, size, level + 1)]


def chunk_recursive(text: str, size: int, overlap: int = OVERLAP) -> list[str]:
    """Recursive split, then merge neighbouring pieces up to `size`; each chunk repeats the
    last `overlap` tokens of the previous one."""
    chunks, current = [], ""
    for piece in _split(text, size):
        joined = f"{current}\n{piece}" if current else piece
        if current and ntok(joined) > size:
            chunks.append(current)
            room = max(0, min(overlap, size - ntok(piece)))  # the overlap never pushes a chunk over `size`
            tail = ENC.decode(ENC.encode(current)[-room:]) if room else ""
            current = f"{tail}\n{piece}" if tail else piece
        else:
            current = joined
    return chunks + [current] if current else chunks


def build_chunks(docs: list[dict], method: str, size: int) -> tuple[list[str], list[str]]:
    chunker = chunk_fixed if method == "fixed" else chunk_recursive
    texts, doc_ids = [], []
    for d in docs:
        meta = f"{d['title']} ({d['type']}" + (f", PaderQueue {d['version']}" if d["version"] else "") + ")"
        for c in chunker(d["text"], size):
            texts.append(f"{meta}\n{c}")
            doc_ids.append(d["doc_id"])
    return texts, doc_ids


# ------------------------------------------------------------------ scoring
def tokenize(text: str) -> list[str]:
    """Lowercase words; identifiers like pq-e4663 or spool.buffer_bytes are kept whole AND split."""
    out = []
    for t in re.findall(r"[a-z0-9]+(?:[._\-][a-z0-9]+)*", text.lower()):
        out.append(t)
        parts = re.split(r"[._\-]", t)
        if len(parts) > 1:
            out.extend(parts)
    return out


def minmax(m: np.ndarray) -> np.ndarray:
    lo, hi = m.min(axis=1, keepdims=True), m.max(axis=1, keepdims=True)
    return (m - lo) / np.maximum(hi - lo, 1e-12)


class Retriever:
    """config: r1 | r2 | r3 | r4 | r5. Holds chunks and the index; scores all queries at once."""

    def __init__(self, config: str, size: int, embedder: Embedder, alpha: float = ALPHA):
        self.config, self.size, self.embed, self.alpha = config, size, embedder, alpha
        start = time.time()
        docs = load_jsonl(DATA / "corpus.jsonl")
        self.texts, self.doc_ids = build_chunks(docs, "fixed" if config == "r2" else "recursive", size)
        self.bm25 = BM25Okapi([tokenize(t) for t in self.texts]) if config in ("r1", "r4", "r5") else None
        self.matrix = _normalize(embedder.cached(self.texts)) if config != "r1" else None
        self.ce = None
        if config == "r5":
            from sentence_transformers import CrossEncoder
            self.ce = CrossEncoder(CROSS_ENCODER)
        self.index_s = time.time() - start
        self.index_cache_hit = getattr(embedder, "last_cache_hit", False)

    def scores(self, queries: list[str], cache: bool = True) -> np.ndarray:
        if self.config == "r1":
            return np.array([self.bm25.get_scores(tokenize(q)) for q in queries])
        q = self.embed.cached(queries) if cache else self.embed(queries)
        dense = _normalize(q) @ self.matrix.T
        if self.config in ("r2", "r3"):
            return dense
        bm = np.array([self.bm25.get_scores(tokenize(q)) for q in queries])
        return self.alpha * minmax(dense) + (1 - self.alpha) * minmax(bm)

    def search(self, queries: list[str], cache: bool = True) -> list[list[int]]:
        """Chunk indices in rank order for every query."""
        s = self.scores(queries, cache)
        if self.ce is None:
            return [list(np.argsort(-row)[:200]) for row in s]
        ranked = []
        for q, row in zip(queries, s):
            pool = list(np.argsort(-row)[:RERANK_POOL])
            ce = self.ce.predict([(q, self.texts[i]) for i in pool])
            ranked.append([pool[i] for i in np.argsort(-ce)])
        return ranked

    def run(self, questions: list[dict]) -> tuple[list[dict], float]:
        ranked = self.search([q["question"] for q in questions])
        # latency: 10 single queries, query embedding NOT from the disk cache (the gateway may still cache)
        start = time.time()
        for q in questions[:10]:
            self.search([q["question"] + " "], cache=False)
        per_query_ms = (time.time() - start) / 10 * 1000
        rows = []
        for q, idx in zip(questions, ranked):
            docs = list(dict.fromkeys(self.doc_ids[i] for i in idx))[:KEEP_DOCS]
            rows.append({"id": q["id"], "retrieved_doc_ids": docs,
                         "contexts": [self.texts[i] for i in idx[:KEEP_CONTEXTS]]})
        return rows, per_query_ms


# --------------------------------------------------------------- generation
ANSWER_PROMPT = """Answer the question about PaderQueue using ONLY the context below.
- Be short and exact: give the value, name, code or command the question asks for.
- Pay attention to versions: the context can contain pages for different PaderQueue versions.
- Forum answers can be wrong or outdated; prefer the reference documentation.
- If the context does not contain the answer, reply exactly: {abstain}

Context:
{context}

Question: {question}
Answer:"""


def answer_all(llm: LLMClient, questions: list[dict], retrieval: dict[str, dict]) -> list[dict]:
    def one(q):
        contexts = retrieval[q["id"]]["contexts"]
        prompt = ANSWER_PROMPT.format(abstain=ABSTAIN, question=q["question"],
                                      context="\n\n---\n\n".join(contexts))
        text = llm.chat([{"role": "user", "content": prompt}]).strip()
        return {"id": q["id"], "answer": text, "contexts": contexts}

    with ThreadPoolExecutor(4) as pool:  # gateway limit: 10 parallel requests per key
        return list(pool.map(one, questions))


# ---------------------------------------------------------------------- cli
def make_embedder(local: bool) -> Embedder:
    llm = None if local else LLMClient(log_path=CACHE / "embed_log.jsonl")
    return Embedder(llm, local=local, cache_dir=CACHE)


def run_config(config, size, emb, split="dev", alpha=ALPHA) -> Path:
    name = f"{config}_{size}" + (f"_a{alpha:g}" if config in ("r4", "r5") and alpha != ALPHA else "")
    qs = load_jsonl(DATA / f"questions_{split}.jsonl")
    r = Retriever(config, size, emb, alpha)
    rows, ms = r.run(qs)
    out = RESULTS / f"retrieval_{name}_{split}.jsonl"
    write_jsonl(out, rows)
    timing = {"config": name, "n_chunks": len(r.texts), "index_s": round(r.index_s, 2),
              "embedding_cache_hit": r.index_cache_hit, "query_ms": round(ms, 2),
              "query_tokens_avg": round(sum(ntok(q["question"]) for q in qs) / len(qs), 1),
              "context_tokens_avg": round(sum(ntok("\n".join(x["contexts"])) for x in rows) / len(rows), 1)}
    (RESULTS / f"timing_{name}.json").write_text(json.dumps(timing, indent=2))
    print(json.dumps(timing))
    return out


def evaluate(path: Path, k: int = 5) -> dict:
    metrics = RESULTS / path.name.replace("retrieval_", "metrics_").replace(".jsonl", f"_k{k}.json")
    subprocess.run([sys.executable, str(HERE / "evaluate.py"), str(path), "--k", str(k), "--json", str(metrics)],
                   check=True, stdout=subprocess.DEVNULL)
    return json.loads(metrics.read_text())


def summary_table(k: int = 5) -> str:
    """Markdown table: recall/precision@k per category + timing, for every evaluated configuration."""
    cats = ["identifier", "paraphrase", "multi-hop", "version"]
    lines = ["| config | chunks | " + " | ".join(f"{c} R/P" for c in cats) + " | overall R/P | index s | query ms |",
             "|---" * (len(cats) + 5) + "|"]
    for f in sorted(RESULTS.glob(f"metrics_*_dev_k{k}.json")):
        name = f.name[len("metrics_"):-len(f"_dev_k{k}.json")]
        m = json.loads(f.read_text())["summary"]
        t = json.loads((RESULTS / f"timing_{name}.json").read_text())
        rp = lambda x: f"{x['context_recall']:.2f}/{x['context_precision']:.2f}"  # noqa: E731
        lines.append(f"| {name} | {t['n_chunks']} | " + " | ".join(rp(m["per_category"][c]) for c in cats)
                     + f" | **{rp(m['overall'])}** | {t['index_s']}{' (cached)' if t['embedding_cache_hit'] else ''} | {t['query_ms']} |")
    out = "\n".join(lines)
    (RESULTS / f"summary_k{k}.md").write_text(out + "\n")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["retrieve", "sweep-alpha", "all", "answer", "predict", "table"])
    p.add_argument("arg", nargs="?", help="config (r1..r5) or, for answer, a retrieval file")
    p.add_argument("--size", type=int, default=128)
    p.add_argument("--alpha", type=float, default=ALPHA)
    p.add_argument("--sample", type=int, help="answer: only judge.py's stratified sample of N questions")
    p.add_argument("--local", action="store_true", help="local embeddings (all-MiniLM-L6-v2)")
    a = p.parse_args()

    if a.cmd == "retrieve":
        evaluate(run_config(a.arg, a.size, make_embedder(a.local), alpha=a.alpha))
    elif a.cmd == "sweep-alpha":
        emb = make_embedder(a.local)
        for alpha in np.round(np.arange(0, 1.01, 0.1), 1):
            m = evaluate(run_config("r4", a.size, emb, alpha=float(alpha)))
            print(f"alpha={alpha:.1f}  {json.dumps(m)[:200]}")
    elif a.cmd == "all":
        emb = make_embedder(a.local)
        runs = [("r1", a.size), ("r2", a.size)] + [("r3", s) for s in (64, 128, 256)] + \
               [("r4", a.size), ("r5", a.size)]
        for config, size in runs:
            path = run_config(config, size, emb)
            print(config, size, json.dumps(evaluate(path))[:300])
    elif a.cmd == "answer":
        from judge import stratified_sample
        path = Path(a.arg)
        qs = load_jsonl(DATA / "questions_dev.jsonl")
        if a.sample:
            qs = stratified_sample(qs, a.sample)
        llm = LLMClient(log_path=RESULTS / f"llm_{path.stem.replace('retrieval_', 'answers_')}.jsonl")
        rows = answer_all(llm, qs, {r["id"]: r for r in load_jsonl(path)})
        out = RESULTS / path.name.replace("retrieval_", "answers_")
        write_jsonl(out, rows)
        print(f"{out} | {llm.usage_summary()}")
    elif a.cmd == "table":
        print(summary_table())
    elif a.cmd == "predict":
        path = run_config(a.arg, a.size, make_embedder(a.local), split="test", alpha=a.alpha)
        qs = load_jsonl(DATA / "questions_test.jsonl")
        retrieval = {r["id"]: r for r in load_jsonl(path)}
        llm = LLMClient(log_path=RESULTS / "llm_predictions_test.jsonl")
        rows = answer_all(llm, qs, retrieval)
        write_jsonl(HERE / "predictions_test.jsonl",
                    [{"id": r["id"], "answer": r["answer"], "retrieved_doc_ids": retrieval[r["id"]]["retrieved_doc_ids"][:5]}
                     for r in rows])
        print(f"predictions_test.jsonl | {llm.usage_summary()}")


if __name__ == "__main__":
    main()
