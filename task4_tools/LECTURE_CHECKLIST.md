# Task 4 design checklist

Notes for the video / write-up. Checked against our `tools.py` and `agent.py`.

Tests: `uv run pytest task4_tools/tests -q` (53 passed).  
Mock run: `uv run python task4_tools/agent.py --mock` (25/25, bad calls rejected).

## Orchestrator vs model

| Item | Status | Where |
|---|---|---|
| Model only proposes tool calls; our code runs them | MET | `agent.py` loop + `tools.execute` |
| parse → authorise → dispatch → run → format | MET | `tools.py` `execute`, `_parse`, `_authorise`, `_dispatch` |
| Per-tool policy (read / execute / write) | MET | `TOOL_POLICY` |
| Log every call | MET | `logs/trace.jsonl` via `_trace` |
| Subprocess timeout + kill process group | MET | `_run_subprocess`, `_kill_process_group` |
| CPU / memory limits where available | MET | `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE` (AS may be ignored on macOS) |

## Tool interface

| Item | Status | Where |
|---|---|---|
| `run_tests` returns structured summary | MET | `_parse_junit` (`passed`/`failed`/`failures`/…) |
| Truncation says what is missing | MET | read/search/list/`_finalize` messages |
| Stable, parseable fields | MET | dict/list outputs |
| Strip volatile paths/timings from output | MET | `_sanitize_text` |
| `edit_file` not idempotent (2nd identical edit fails) | MET | `old_str not found` |
| Separate tools by permission (no shell tool) | MET | schemas + module docstring |
| Least privilege at runtime | MET | path checks, `src/`-only writes, minimal env, no `shell=True` |

## Validation

| Item | Status | Where |
|---|---|---|
| jsonschema + `additionalProperties: false` | MET | `_schema` / `_parse` |
| Extra semantic checks (paths, unique `old_str`) | MET | `_authorise`, `_edit_file` |
| Why validate if schema exists? | MET | schema ≠ “safe path / unique edit”; mock sends broken calls |

## Untrusted content

| Item | Status | Where |
|---|---|---|
| Prompt: files/tool output are data | MET | `agent.py` system prompt |
| Secrets/canary not leaked | MET | path deny + redaction |
| Bad CSV / docstring ignored by controls | MET | mock: injection CLI rejected; test edits blocked |

## Paths / escapes

| Item | Status | Where |
|---|---|---|
| `resolve` + `relative_to` (no workspace string prefix) | MET | `_resolve_in_workspace` |
| Symlink escapes rejected | MET | resolve + walk skips |
| Re-check before open/write | PARTIAL | `_recheck_inside`, `O_NOFOLLOW` best effort; small race remains |
| No shell; allowlisted CLI/tests | MET | `run_cli` / `run_tests` |
| Regex search in child with timeout | MET | `_search_code` |

## Agent loop

| Item | Status | Where |
|---|---|---|
| Native `chat_with_tools` | MET | `agent.py` |
| One tool message per call id | MET | `agent.py` |
| Stop on no tools / max steps / token budget | MET | `agent.py` |
| `--mock` | MET | works |
| Final `run_tests` + `final.diff` | MET | end of `run_agent` |
| Schema size / prompt tokens printed | MET | start of run + each step |

## Limits we still have

- `run_tests` still runs the project’s own test code (including `conftest.py`).
- No full network/container isolation.
- Hardlinks are not specially handled.
- Path re-check reduces but does not remove every race.
- `RLIMIT_AS` is unreliable on macOS; timeout + `killpg` matter more.
