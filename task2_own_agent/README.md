# Task 2 – Build your own (small) coding agent

There is no starter code for this task, only this specification and some helpers.
Read the task description on the lab sheet first.

## What's in this folder

| File | Purpose |
|---|---|
| `style_guide/` | The fictional "UPB Agentic SE Web Style Guide" (55 Markdown documents) |
| `check_site.py` | Checks a website against the basic requirements AND the style guide (`--basic` for basic only) |
| `embed_images.py` | Replaces `{{image:<file>}}` placeholders with embedded base64 images |

Helpers you can import from `common/` (run your code from the repository root, or add it to `sys.path`):

```python
from common.llm_client import LLMClient, count_tokens
from common.site_checker import check_site, member_names_from_content
from common.embed_images import embed_images
```

## Specification

### Allowed dependencies
Python standard library, `numpy`, `rank-bm25`, `tiktoken`, the provided `common/` helpers,
optionally `sentence-transformers` for local embeddings.
Not allowed: LangChain, LlamaIndex, Haystack, agent SDKs, vector databases.

### RAG component – `rag.py`
- Loads all documents from `style_guide/` and splits them into chunks.
- Embeds the chunks, keeps the vectors in memory (a numpy matrix is enough).
- Provides `search(query: str, k: int) -> list[Chunk]`, returning the most similar chunks by cosine similarity.
  Each chunk carries its text and the id of its source document (e.g. `SG-13`).
- Context budget rules:
  - one `search_style_guide` observation may contain at most **1,000 tokens** of style guide text
    (measure with `count_tokens` from `common/llm_client.py`)
  - your system prompt and task description must not contain style guide text

### Agent loop – `agent.py`
The model answers with exactly one JSON object per step:

```json
{"thought": "short reasoning", "action": "<action name>", "args": {}}
```

Required actions:

| Action | Args | What your code does | Observation returned to the model |
|---|---|---|---|
| `search_style_guide` | `{"query": str}` | calls `rag.search` | retrieved chunks with document ids (≤ 1,000 tokens) |
| `read_profiles` | `{}` | reads all `content/*.md` files (not `_template.md`) | profile texts + list of available image files |
| `write_site` | `{}` + the HTML in a ` ```html ` code block directly after the JSON (or `{"html": str}`) | resolves `{{image:...}}` placeholders, saves the file, runs `check_site(..., mode="full")` | the checker report (or an error, e.g. unknown image) |
| `finish` | `{"summary": str}` | accepted only if the last checker report passed | if rejected: the failing checks |

Why the extra code block for `write_site`? A whole HTML page inside a JSON string needs thousands of escaped
quotes and newlines, and models get that wrong surprisingly often. You may support both variants.

Behaviour your loop must have:
- A maximum number of steps (e.g. 25). Stop with a clear message when it is reached.
- If the model output is not valid JSON or uses an unknown action or wrong args: don't crash.
  Send an error observation back and continue. Count it as a step.
- The model must never write base64 image data. It uses placeholders like `<img src="{{image:anna.jpg}}" alt="...">`.
- Human in the loop: after an accepted `finish`, ask the user for feedback on the terminal.
  Non-empty feedback is appended as a new user message and the loop continues. Empty input ends the run.
- Don't use the native function-calling / `tools` API. You build the protocol yourselves.
- A complete website is several thousand output tokens, plus the model's hidden reasoning tokens. Set `max_tokens` high
  enough (or don't set it), otherwise the answer is cut off (the log shows `"finish_reason": "length"`).
- Embeddings: use the gateway embedding model via `llm.embed(...)` (free), or a local model.

### Command line
From the repository root:

```bash
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```

You may add options (model, max steps, k, ...). Document them in your README.

### Logs
- Store every message sent to and received from the model, with step numbers, in `task2_own_agent/logs/run_XX.jsonl`.
- The simplest way: use `LLMClient(log_path=...)` and add your own entries (observations, human feedback) with `llm.log_event({...})`.
- Submit the logs of at least one complete run that produced your submitted `site/index.html`.

### What your README must contain
- how to run the agent
- your design decisions: chunking, embedding model, k, prompt design, how you handle invalid output
- known limitations

---

## Our agent (team 16)

### How to run
```bash
# once: write .env (UPB gateway, OpenRouter free or OpenRouter cheap; key typed hidden) and test it
python setup_api.py
# from the repository root
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```
| Option | Default | Meaning |
|---|---|---|
| `--content` | `task1_opencode/content` | profiles (`*.md`) + `images/` |
| `--out` | `task2_own_agent/site/index.html` | where the site is written |
| `--model` | `$LAB_CHAT_MODEL` | chat model (`$LAB_FALLBACK_MODEL` if the budget is used up) |
| `--max-steps` | 25 | step limit per user prompt (invalid replies count) |
| `-k` | 5 | chunks per search |
| `--local-embeddings` | off | `all-MiniLM-L6-v2` locally instead of the gateway embedding model |
| `--full-history` | off | no summaries, every message sent as-is (to compare token use) |
| `--log` | `logs/run_XX.jsonl` | log file (next free number) |
| `--chat NAME` | new chat | resume chat NAME (or start it); every chat is saved in `sessions/NAME.json` |

After each turn the program asks for your next message: feedback ("make it funnier"), a question ("which night
colors did you use?") or a new task. Empty input ends the run. Every chat is saved after each turn; `--chat team` continues
chat "team" later (history, facts and summary are restored; each run writes its own `run_XX.jsonl`).
In the chat: `/chats`, `/switch NAME`, `/facts`, `/history`, `/tokens`, `/help`, `/quit`.
Without the model: `python task2_own_agent/chat.py list | show NAME | delete NAME`.

Offline self-check (scripted fake model, no key): `python task2_own_agent/test_agent.py`.
Retrieval check: `python task2_own_agent/rag.py "night mode colors"`. Embedding demo (cosine table): `python task2_own_agent/embeddings.py "night mode colors" "dark theme palette" "team photo"`.

### Structure
```
agent.py      MAIN: options + one turn of work (the loop). context.build() -> LLM -> parse JSON -> run tool -> observation
chat.py       chats: many turns, saved/resumed, /commands (agent.py hands over to chat.talk)
context.py    prompt processor: memory (facts), short summaries of old steps, token counting
rag.py        style guide chunking + cosine search
embeddings.py embedding wrapper: Embedder (API or local MiniLM, disk cache), normalize, similarity; demo CLI
tools/        one file per tool: NAME, ARGS, DESCRIPTION, run(args, state) -> (observation, short)
  say.py  reply.py  remember.py  search_style_guide.py  read_profiles.py  write_site.py  finish.py
test_agent.py offline tests with a fake model
```

### Design decisions
- **Protocol:** one JSON object per reply `{"thought", "action", "args"}`. `json.JSONDecoder.raw_decode` reads the first
  object, so text or a ```json fence around it is fine. `write_site` takes the page from a ```html block after the JSON
  (or `args.html`). Args are checked against each tool's `ARGS` (missing, wrong type, extra).
- **Invalid output:** no JSON, broken JSON, unknown action or wrong args -> `Error: ...` observation with the tool's
  usage line. Counts as a step; the loop never crashes.
- **Two ways to end a turn:** `finish` = "the page is done" (only after all checks pass); `reply` = "here is my
  answer" for questions and chat (no page needed, but refused while a page written in this turn fails its checks).
- **Chats (`chat.py`):** agent.py does one turn of work; chat.py owns the conversation: it reads your message (or a
  /command), runs a turn, and saves the context (entries full + short, facts, summary, step, page version, log files)
  to `sessions/<name>.json` after every turn. `--chat NAME` resumes; `/switch` changes chat inside one run.
- **Stop conditions:** `finish` or `reply` accepted (only if the last `write_site` passed every check), the step limit, or 3 invalid
  replies in a row. Exactly repeated actions get a note "do something different".
- **Human in the loop:** after a turn, your feedback becomes the next user prompt. The last checker report is reset,
  so the agent must write and check the page again before it may finish.
- **Context (context.py):** every message is stored in full (log) and as one short line. The model gets: system prompt,
  MEMORY (saved facts + summary of older steps as short lines), the newest user prompt word for word, and the last 2 steps
  in full. Short lines cost nothing: the reply's own `thought` and a one-line result per tool. Only a long user prompt
  (>60 tokens) and a summary longer than 400 tokens are summarized by the LLM ("summary of summaries").
  Every step prints and logs the tokens per section.
- **Pinned:** the newest page the agent wrote and the profiles always stay visible, even when they are older
  than the recent window (otherwise the agent would rewrite the page without seeing it).
- **Memory:** `remember` saves a fact in the model's own words with its source (e.g. `SG-13`); max 500 tokens, oldest
  dropped first. Facts are not in the system prompt and are written by the model, not copied from the guide.
- **RAG (`rag.py`):** the guide is small (55 docs, ~5.5k tokens, longest doc ~190 tokens), so we split each document
  at its `## ` headings and merge paragraphs up to 250 tokens: 67 chunks, no overlap needed (a chunk never cuts a rule in
  half). Every chunk starts with `[SG-xx] title | Status: ACTIVE/DEPRECATED ...`. Embeddings: `$LAB_EMBEDDING_MODEL`
  (gateway `kiconnect.qwen3-embedding-8b`), cached in `.cache/`. Cosine similarity, `k = 5`; the observation adds chunks
  best-first and stops before 1,000 tokens of guide text (5 x max 190 tokens always fits).
- **How we checked retrieval:** `python task2_own_agent/rag.py --check` runs 10 queries with a known answer document,
  5 of them with a deprecated older version. With chunk size 250 the expected document was rank 1 for 10/10 queries,
  with chunk size 60 rank 1-2 (local MiniLM, 2026-11-10). The deprecated version always came right after it (rank 2),
  so the model always sees both: that is why the status line in every chunk and the "follow the ACTIVE version" rule matter.
- **Images:** the model writes `{{image:<file>}}`; `embed_images` inlines the base64 data before saving.
- **Logs:** `LLMClient` logs every request/response; we add one event per step (`step`, `action`, `tokens`, `observation`,
  `short`, `facts`) and every user prompt.

### Submitted run (2026-11-12, `openai.gpt-6-luna`, `--max-steps 40`)

Log: [`logs/run_01.jsonl`](logs/run_01.jsonl) · site: [`site/index.html`](site/index.html)

| Turn | Steps | Site versions (checks passed) | Result |
|---|---|---|---|
| 1: build the site | 22 | v1 15/25 → v2 23/25 → v3 **25/25** | finish accepted |
| 2: human feedback "fun fact under each card, toggle easier to see" | 1 | v4 **25/25** | finish accepted |

Total: 28 chat calls + 14 embedding calls, 131k prompt + 23k completion tokens, **$0.028**. The prompt stays at about 6–8k tokens per step
(summaries + pinned page), instead of growing with every step.

What went wrong first: some gateway embedding responses were broken (out of order, and sometimes a duplicate
`index`, 63 vectors for 64 texts), so every vector after it belonged to the wrong chunk. Search then returned the wrong
rules and the agent searched the same rules again and again (a first run stopped at 24/25). Fix in
`common/llm_client.py: embed`: sort by `index`, and if the indices aren't exactly 0…n-1, split the batch and retry.
Lesson: check retrieval itself, not only the final output.

### Known limitations
- Summaries drop detail: an old rule that was not saved with `remember` must be searched again.
- Retrieval is purely semantic: a deprecated rule can rank above the active one; we rely on the status line in each chunk.
- The whole page is rewritten on every `write_site` (thousands of output tokens); no patch action.
- The checker's messages are vague; a rule with no automatic check can be broken silently.
- `count_tokens` uses `cl100k_base`, only an estimate of the gateway model's tokenizer.
- A reasoning model without `temperature`: two runs differ. Reproducibility = logs + `test_agent.py`.
