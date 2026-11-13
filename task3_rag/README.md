# Task 3 – RAG under the microscope

Read the task description on the lab sheet first. There is no starter code for the retrievers, only the
dataset and the evaluation scripts.

## The dataset: PaderQueue

PaderQueue is a fictional message queue. No model has seen its documentation during training.

`data/corpus.jsonl` – 932 documents, one JSON object per line:

```json
{"doc_id": "d0132", "type": "config", "title": "ingress.interval_s (PaderQueue 2.x)", "version": "2.x", "text": "# `ingress.interval_s` ..."}
```

| type | count | content |
|---|---|---|
| `config` | 170 | configuration reference, one page per option. Some options also have a page for an old major version |
| `error` | 77 | error catalogue (codes like `PQ-E4663`) |
| `cli` | 36 | command line reference |
| `changelog` | 14 | release notes per version (long) |
| `incident` | 120 | postmortems of incidents |
| `forum` | 500 | forum threads. Answers can be wrong or outdated |
| `runbook` | 15 | troubleshooting runbooks |

The `type`, `title` and `version` fields are metadata. You may use them (e.g. put the title into each chunk).

`data/questions_dev.jsonl` – 150 questions with ground truth:

```json
{"id": "dev001", "category": "version", "question": "What was the default value of `ingress.interval_s` in PaderQueue 2.x?",
 "answer": "`300`", "relevant_doc_ids": [["d0132", "d0406"]]}
```

- `relevant_doc_ids` is a list of groups. Each group is one piece of information the answer needs; any
  document in a group provides it. `[["a", "b"], ["c"]]` means: needs (a or b) and c.
- Categories (30 questions each): `identifier`, `paraphrase`, `multi-hop`, `version`, `unanswerable`.
- Unanswerable questions have `relevant_doc_ids: []` and the answer "The documentation does not contain this information."

`data/questions_test.jsonl` – 100 questions, only `id` and `question`.

## Evaluation scripts

### Retrieval: `evaluate.py`

Write your retrieval results as JSONL, one line per question, documents in rank order:

```json
{"id": "dev001", "retrieved_doc_ids": ["d0132", "d0406", "d0017"]}
```

Your retriever returns chunks. Map chunks to their `doc_id` in rank order; duplicates are removed by the script.

```bash
uv run python task3_rag/evaluate.py results/retrieval_r1.jsonl --k 5 --json results/metrics_r1.json
```

Prints context recall and context precision (definitions in the file header), overall and per category.

### End-to-end: `judge.py`

Write your generated answers as JSONL:

```json
{"id": "dev001", "answer": "In PaderQueue 2.x the default was 300.", "contexts": ["chunk text 1", "chunk text 2"]}
```

`contexts` are exactly the chunk texts you gave to the generator.

```bash
uv run python task3_rag/judge.py results/answers_r4.jsonl --sample 50 --out results/judge_r4.jsonl
```

- Scores faithfulness, answer relevance and correctness (definitions in the file header).
- If your system can't answer, it should reply exactly `The documentation does not contain this information.`
  The judge recognizes this sentence as an abstention: it counts it in the `abstained` column and doesn't score its
  faithfulness (an abstention makes no claim about the context). Correctness tells you whether abstaining was right.
- `--sample 50` judges the same stratified 50 questions every time. That's enough for the lab.
- By default the judge uses `$LAB_CHAT_MODEL` and the gateway embedding model. `--local-embeddings` uses a local model for the answer relevance metric instead.

## Test set predictions

Submit `task3_rag/predictions_test.jsonl` with your best configuration, one line per test question:

```json
{"id": "test001", "answer": "...", "retrieved_doc_ids": ["d0001", "d0002", "d0003", "d0004", "d0005"]}
```

We evaluate context recall@5 of `retrieved_doc_ids` and the correctness of `answer` on the hidden ground truth.

## Practical hints

- Embedding models: the gateway embedding model (`$LAB_EMBEDDING_MODEL`, free, send batches of texts per request),
  or locally `sentence-transformers/all-MiniLM-L6-v2` (`uv sync --extra local`, runs fine on a laptop CPU:
  indexing the whole corpus takes well under a minute). The gateway model has 4096 dimensions, so a cache of the whole corpus
  is large: don't submit it.
- Latency: the gateway caches identical requests, so a second run of the same questions is much faster.
  Measure latency on the first run, or report cache hits (`cache_hit` in the log) separately.
- Cache your embeddings (e.g. `np.save`), so you don't re-embed the corpus for every experiment.
- Cross-encoder for re-ranking: `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers).
- BM25: `rank_bm25.BM25Okapi`. Think about how you tokenize identifiers like `PQ-E4663` or `spool.buffer_bytes`.
- Hybrid search: BM25 scores and cosine similarities have completely different ranges. Normalize before you combine them.
- Don't tune on the questions you report results on without saying so.

---

## Our RAG system (team 16)

Code: [`rag3.py`](rag3.py) (no frameworks: numpy, rank-bm25, tiktoken; the Task 2 `Embedder` is reused).
Embeddings: gateway `kiconnect.qwen3-embedding-8b` (4096 dims, cached in `.cache/`, not submitted). Generator and
judge: `openai.gpt-6-luna`. Every chunk starts with a metadata line `title (type, version)`; overlap 16 tokens; k = 5.

```bash
uv run python task3_rag/rag3.py all                       # R1–R4 + chunk sizes + metrics (R5 needs sentence-transformers)
uv run python task3_rag/rag3.py retrieve r5 --size 128    # cross-encoder re-ranking (we ran it in a venv with sentence-transformers)
uv run python task3_rag/rag3.py predict r4 --size 128     # -> predictions_test.jsonl
uv run python task3_rag/rag3.py answer task3_rag/results/retrieval_r4_128_dev.jsonl --sample 50
uv run python task3_rag/judge.py task3_rag/results/answers_r4_128_dev.jsonl --sample 50 --out task3_rag/results/judge_r4.jsonl
```

| Config | Idea |
|---|---|
| R1 | BM25 with an identifier-aware tokenizer (`PQ-E4663`, `spool.buffer_bytes` stay whole and are also split into parts) |
| R2 | fixed token windows + dense |
| R3 | recursive chunks (headings > paragraphs > lines > sentences > words) + dense |
| R4 | hybrid: 0.5 · minmax(dense) + 0.5 · minmax(BM25), per query (alpha picked with `sweep-alpha` on dev, so slightly optimistic) |
| R5 | R4 top 30 re-ranked by `cross-encoder/ms-marco-MiniLM-L-6-v2` |

### Retrieval on dev (k = 5, recall / precision, 120 answerable questions)

| config | chunks | identifier R/P | paraphrase R/P | multi-hop R/P | version R/P | overall R/P | index s | query ms |
|---|---|---|---|---|---|---|---|---|
| r1_128 | 1324 | 0.90/0.74 | 0.23/0.08 | 0.47/0.71 | 0.63/0.63 | **0.56/0.54** | 0.75 | 20.19 |
| r2_128 | 1317 | 0.57/0.51 | 0.87/0.66 | 0.03/0.03 | 0.77/0.52 | **0.56/0.43** | 47.49 | 209.73 |
| r3_128 | 1324 | 0.60/0.55 | 0.90/0.77 | 0.02/0.02 | 0.73/0.58 | **0.56/0.48** | 31.84 | 121.45 |
| r3_256 | 958 | 0.57/0.54 | 0.93/0.70 | 0.08/0.10 | 0.70/0.54 | **0.57/0.47** | 0.18 (cached) | 212.29 |
| r3_64 | 3243 | 0.57/0.53 | 0.90/0.80 | 0.03/0.04 | 0.83/0.51 | **0.58/0.47** | 89.04 | 190.6 |
| r4_128 | 1324 | 0.93/0.73 | 0.60/0.40 | 0.43/0.68 | 0.67/0.64 | **0.66/0.61** | 0.79 (cached) | 179.93 |
| r5_128 | 1324 | 0.63/0.59 | 0.67/0.49 | 0.27/0.53 | 0.97/0.62 | **0.63/0.56** | 39.69 (cached) | 2531.25 |

Query ms includes the gateway call for the query embedding (R5: plus the cross-encoder on our laptop CPU).
Old numbers with local `all-MiniLM-L6-v2` embeddings: [`results/local_minilm/`](results/local_minilm/) (R4: 0.62 / 0.58).

**Which approach wins where**
- **Identifiers**: BM25 (0.90) far ahead of dense (0.60). Exact tokens like error codes have no "meaning" an embedding can catch.
- **Paraphrases**: dense (0.90) far ahead of BM25 (0.23). Different words, same meaning.
- **Hybrid R4** keeps most of both and is best overall (0.66 / 0.61), so we use it for the test predictions.
- **Multi-hop** is weak everywhere (≤ 0.47): one query vector can't find two different documents.
- **Re-ranking R5** helps on version questions (0.97) but hurts identifiers (the cross-encoder was trained on web search, not
  config names) and is 14× slower. Not worth it here.
- **Chunk size** (R3: 64 / 128 / 256 tokens) changes little (0.56–0.58); 128 is the middle ground for index size and context length.

### Why the R4 weight is 0.5 (alpha sweep)

`python task3_rag/rag3.py sweep-alpha --size 128`: score = alpha · minmax(dense) + (1 − alpha) · minmax(BM25), on the dev questions.

| alpha (dense weight) | recall@5 | precision@5 | identifier recall | paraphrase recall |
|---|---|---|---|---|
| 0.0 | 0.56 | 0.54 | 0.90 | 0.23 |
| 0.1 | 0.57 | 0.54 | 0.90 | 0.27 |
| 0.2 | 0.57 | 0.55 | 0.90 | 0.30 |
| 0.3 | 0.61 | 0.57 | 0.90 | 0.47 |
| 0.4 | 0.62 | 0.58 | 0.90 | 0.50 |
| 0.5 | 0.66 | 0.61 | 0.93 | 0.60 |
| 0.6 | 0.65 | 0.63 | 0.87 | 0.67 |
| 0.7 | 0.64 | 0.54 | 0.83 | 0.80 |
| 0.8 | 0.61 | 0.52 | 0.77 | 0.87 |
| 0.9 | 0.58 | 0.51 | 0.67 | 0.93 |
| 1.0 | 0.56 | 0.48 | 0.60 | 0.90 |

The ends show the trade-off: BM25 alone (alpha 0) is best on identifiers, dense alone (alpha 1) on paraphrases. **0.5 has the best
recall@5 (0.66)** and is close to the best precision (0.63 at 0.6), so both kinds of question keep most of their score.
Honest note: alpha was chosen on the same dev questions we report, so the R4 numbers are slightly optimistic; the test predictions are the fair check.

### End-to-end (judge.py, R4, the stratified 50-question sample)

| category | n | abstained | faithful | relevance | correct |
|---|---|---|---|---|---|
| paraphrase | 10 | 0.20 | 1.00 | 0.59 | 0.80 |
| identifier | 10 | 0.10 | 1.00 | 0.48 | 0.90 |
| version | 10 | 0.00 | 0.75 | 0.48 | 1.00 |
| unanswerable | 10 | 1.00 | – | 0.41 | 1.00 |
| multi-hop | 10 | 1.00 | – | 0.39 | 0.00 |
| **overall** | 50 | 0.46 | 0.91 | 0.47 | **0.74** |

Unanswerable questions are always handled correctly, but **the model also abstains on every multi-hop question**:
when only one of the two needed facts is in the top 5, our strict prompt ("if the context does not contain the answer,
abstain") makes it give up. Better next steps: query decomposition (one search per sub-question) or a larger k for the
generator. Cost: test predictions $0.009 (100 questions), dev answers $0.004, judge a few cents.

### End-to-end: R4 vs R5 (the two best configurations, same 50 questions)

| category | R4 correct | R5 correct | R4 abstained | R5 abstained | R4 faithful | R5 faithful |
|---|---|---|---|---|---|---|
| paraphrase | 0.80 | 0.80 | 0.20 | 0.20 | 1.00 | 1.00 |
| identifier | 0.90 | 0.50 | 0.10 | 0.40 | 1.00 | 1.00 |
| version | 1.00 | 0.90 | 0.00 | 0.10 | 0.75 | 1.00 |
| unanswerable | 1.00 | 1.00 | 1.00 | 1.00 | – | – |
| multi-hop | 0.00 | 0.00 | 1.00 | 1.00 | – | – |
| **overall** | **0.74** | **0.64** | 0.46 | 0.54 | 0.91 | 1.00 |

R5 (re-ranking) is **worse end to end (0.64 vs 0.74)**. The loss is almost all in identifiers (0.90 → 0.50): the cross-encoder
pushes the exact error-code page out of the top 5, the model then abstains (R5 abstains on 40 % of identifier questions). R5's
answers are fully faithful (1.00), so it does not hallucinate either; it just finds the right page less often. Judge logs:
`results/judge_r4.jsonl`, `results/judge_r5.jsonl`. Hand check of 10 judge decisions: `results/judge_check.md`
(**10 of 10 agreed**: the judge scored the 6 right answers 1 and the 4 misses 0).

### Cost and latency per configuration

| config | chunks | index s | query ms | query tokens | context tokens (top 5) |
|---|---|---|---|---|---|
| r1_128 | 1324 | 0.75 | 20.19 | 18.4 | 587.2 |
| r2_128 | 1317 | 47.49 | 209.73 | 18.4 | 591.2 |
| r3_128 | 1324 | 31.84 | 121.45 | 18.4 | 554.6 |
| r3_256 | 958 | 0.18 (cached) | 212.29 | 18.4 | 761.2 |
| r3_64 | 3243 | 89.04 | 190.6 | 18.4 | 323.5 |
| r4_128 | 1324 | 0.55 (cached) | 531.41 | 18.8 | 578.7 |
| r5_128 | 1324 | 39.69 (cached) | 2531.25 | 18.4 | 588.1 |

Query ms = one query on the first run (query embedding from the gateway; R5 adds the cross-encoder on our laptop CPU).
Tokens: approximate (`tiktoken` cl100k). The generator then gets the question + the 5 chunks, so about 550–750 prompt tokens per
answer for the 128-token configs. Test predictions cost $0.009 for 100 questions (gpt-6-luna).

### Failure analysis: where R4 goes wrong (judge sample)

R4 got 13 of the 50 sample questions wrong. **All 13 are retrieval failures**: a needed document was not in the top 5, and the
generator then answered "The documentation does not contain this information." The generator never invented an answer
(0 generation failures; faithfulness 0.91 on the answers with claims). Five typical cases
(rank = position of the needed document in R4's top 20, `results/retrieval_r4_128_dev.jsonl`):

| question | category | needed docs (rank) | why it failed |
|---|---|---|---|
| dev062 "What should I do when PaderQueue logs PQ-E3177?" | identifier | error page d0227 (**rank 7**) | Just outside k=5. Our guess (not measured): other chunks that repeat "connection" outrank the short error chunk after min-max normalisation. |
| dev004 "Which setting controls how many times the mechanism that parks messages nobody could process tries again…" | paraphrase | `dlq.max_retries` d0032 (not in 20), d0303 (rank 17) | Pure paraphrase: "parks messages nobody could process" = dead-letter queue. BM25 finds no shared words and pulls the hybrid score down. |
| dev087 "Which setting limits how many clients can talk to the part that holds back messages…" | paraphrase | `scheduler.max_connections` d0153 (not in 20) | Same pattern: "the part that holds back messages" = scheduler. The embedding alone does not bridge it. |
| dev025 "Which permanent fix … for the error that caused incident INC-2024-171?" | multi-hop | incident d0253 (rank 1), error PQ-E1915 d0183 (not in 20) | Hop 1 works. The error code only appears inside the incident text, never in the question, so one search cannot find hop 2. |
| dev047 "What is the current default value of the setting that the error catalogue recommends changing for PQ-E2425?" | multi-hop | error d0522 (rank 3), config `acktracker.max_retries` d0749 (not in 20) | Same: the setting name is only known after reading the error page. |

Pattern: 10 of the 13 failures are multi-hop (all 10 multi-hop sample questions), 2 are paraphrases, 1 is an identifier just outside the top 5.

### Recommendation

**Deploy R4 (hybrid, 0.5/0.5, recursive 128-token chunks).** It has the best recall@5 (0.66) and precision (0.61), indexing is
cheap and a query costs one embedding call. R5's cross-encoder is ~14× slower and loses on identifiers (0.63 vs 0.93), so it
is not worth it here. The system abstains instead of guessing, which is the safe failure mode.

Next fixes, in order of value:
1. **Two-step retrieval for multi-hop**: search, extract the error code / setting name from the top hit, search again
   (or let the model write the second query). This targets 10 of the 13 failures.
2. **k = 8 for the generator** (keep k = 5 for the metric): dev062 and dev042-style cases have the document at rank 7–10.
3. **Query rewriting for paraphrases** (expand "parks messages nobody could process" to "dead letter queue").

### What went wrong
Our first gateway run gave r3_256 a recall of 0.03. The cause: the gateway sometimes returns an embedding batch with a
duplicate `index` (63 vectors for 64 texts), so every later vector belonged to another chunk. Fix in
`common/llm_client.py: embed` (sort by index, split and retry a broken batch). We found it by comparing cached vectors with fresh single embeddings.
