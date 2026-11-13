"""Task 2: a small coding agent that builds the team website. MAIN FILE.

    context.build()  ->  LLM  ->  one JSON reply  ->  parse  ->  run the tool  ->  observation  ->  context
    repeat until: finish accepted (stop signal) | step limit | 3 invalid replies in a row

Files:  agent.py (this loop)  chat.py (chats: many turns, saved)  context.py (memory, summaries, token counts)
        tools/*.py (one per tool)  rag.py

Run from the repository root:
    uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from common.llm_client import LLMClient  # noqa: E402
from common.site_checker import member_names_from_content  # noqa: E402
import chat  # noqa: E402
from context import Context  # noqa: E402
from rag import Embedder, StyleGuideRAG  # noqa: E402
from tools import TOOLS  # noqa: E402

SYSTEM_PROMPT = """You are a coding agent. You build a single-file team website (index.html) step by step.

Reply with exactly ONE JSON object per message:
{{"thought": "one short sentence", "action": "<tool>", "args": {{...}}}}

Tools:
{tools}

Rules:
- The page must follow the team's web style guide. You do not know it: search it (one topic per query) before
  you write, and save each rule you need with remember.
- Some style guide documents are DEPRECATED. Always follow the ACTIVE version.
- Never write base64 image data. Use placeholders: <img src="{{{{image:anna.jpg}}}}" alt="...">
- One standalone file: HTML + CSS + JavaScript only, nothing loaded from the internet.
- Do not over-research: after about 6 searches, write a first page. The checker report tells you what is
  still wrong; then search only those topics.
- write_site: send the JSON and the page (```html block) in one message, or the page alone in the next one.
- When a check fails: search that topic, then write the whole page again.
- If the user only asks a question or chats, answer with reply (no page needed).
  If the user asks for a change to the page, write it, get all checks passing, then finish.
"""

FIRST_PROMPT = """Build the website of our team. It introduces the team and every member (with picture), is
responsive (phone and desktop), has a day/night mode toggle, a section with the team's shared view on AI and the
future of software engineering, and is funny and creative."""

MAX_TOKENS = 24_000  # per reply: a page + reasoning fit; stops a model that repeats itself for minutes
SEARCH_NUDGE = 6  # searches in a row -> remind the model to write (free models can search forever)
KEEP_INVALID = 2_000  # characters of a broken reply kept in the context (the full reply stays in the log)
PINS = {"write_site": ("page", None), "read_profiles": (None, "profiles")}  # always kept visible (context.py)
PAGE = re.compile(r"<!DOCTYPE html.*</html>", re.S | re.I)
HTML_BLOCK = re.compile(r"```(?:html)?\s*\n(\s*<.*?)(?:```|\Z)", re.S | re.I)  # ```html or a plain ``` block


@dataclass
class State:
    """Everything the tools may read or change."""
    ctx: Context
    rag: StyleGuideRAG
    content: Path
    out: Path
    members: list[str]
    k: int = 5
    last_report: object = None  # checker report of the last write_site
    version: int = 0  # number of write_site calls
    done: bool = False  # set by finish when accepted


CALL = re.compile(r"\b(" + "|".join(TOOLS) + r")\((.*)\)", re.S)  # name(key='value') style that some models use


ALIASES = ("args", "arguments", "input", "parameters")  # {"name": tool, "input": {...}} styles of other agents


def _normalize(obj: object) -> object:
    """{"name": "read_profiles", "input": {}} (MCP / OpenAI style) -> {"action": "read_profiles", "args": {}}."""
    if isinstance(obj, dict) and "action" not in obj and (name := obj.get("name") or obj.get("tool")) in TOOLS:
        args = next((obj[k] for k in ALIASES if k in obj), {})
        return {"thought": obj.get("thought", ""), "action": name, "args": args}
    return obj


def _find_object(text: str) -> tuple[object, int, str]:
    """First JSON object with an "action" (else the first valid one). Closes up to 3 missing } at the end:
    free models often stop one brace short. Returns (object or None, end position, last parse error)."""
    first, error = None, "no complete object"
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        for tail in ("", "}", "}}", "}}}"):
            try:
                obj, end = json.JSONDecoder().raw_decode(text[start:] + tail)
            except json.JSONDecodeError as exc:
                error = exc.msg
                continue
            end, obj = start + min(end, len(text) - start), _normalize(obj)
            if isinstance(obj, dict) and "action" in obj:
                return obj, end, error
            first = first or (obj, end)
            break
    return (*first, error) if first else (None, 0, error)


def _call_format(text: str) -> dict | None:
    """search_style_guide(query='night mode') -> {"action": ..., "args": {...}} (only literal values, via ast)."""
    if not (m := CALL.search(text)):
        return None
    try:
        call = ast.parse(f"{m.group(1)}({m.group(2)})", mode="eval").body
        return {"action": m.group(1), "args": {k.arg: ast.literal_eval(k.value) for k in call.keywords}}
    except (SyntaxError, ValueError, AttributeError):
        return None


class NeedPage(ValueError):
    """write_site without a page: not an error, the page may come in the next message."""


def page_in(text: str) -> str | None:
    """The page in a reply: a ```html (or plain ```) code block, or a bare <!DOCTYPE html> ... </html>."""
    m = HTML_BLOCK.search(text) or PAGE.search(text)
    return (m.group(1) if m.re is HTML_BLOCK else m.group(0)) if m else None


def parse_reply(text: str) -> tuple[str, dict, str, str]:
    """Reply text -> (action, args, thought, kept). Raises ValueError with a message the model can act on.
    kept = the part of the reply worth keeping in the context (the JSON, plus the page for write_site);
    junk after it (e.g. the same action repeated 500 times) is dropped.
    Forgiving on purpose: a missing closing brace, a name(key='value') call or {"name", "input"} still work."""
    if not text.strip():
        raise ValueError("Your reply was empty (maybe you thought for too long). Reply now with one short JSON "
                         'object: {"thought": ..., "action": ..., "args": {...}}')
    obj, end, error = _find_object(text) if "{" in text else (None, 0, "")
    if not (isinstance(obj, dict) and "action" in obj) and (call := _call_format(text)):
        obj, end = call, len(text)
    if not (isinstance(obj, dict) and "action" in obj) and "write_site" in text and (page := PAGE.search(text)):  # page in broken JSON quotes
        html = page.group(0)
        if "\n" not in html:  # it was a JSON string: undo the escapes
            html = html.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        obj, end = {"action": "write_site", "args": {"html": html}}, page.end()
    if obj is None:
        if "{" not in text:
            raise ValueError('No JSON found. Reply with one JSON object: {"thought": ..., "action": ..., "args": {...}}')
        raise ValueError(f"Invalid JSON ({error}). Send exactly one valid JSON object.")
    if isinstance(obj, dict) and "action" not in obj and '"action"' in text:
        raise ValueError("Your JSON is not valid (check quotes and that every { has its }). Send it again.")
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str) or obj["action"] not in TOOLS:
        raise ValueError(f"Unknown or missing action. Allowed: {', '.join(TOOLS)}.")
    action, args = obj["action"], obj.get("args", {})
    if not isinstance(args, dict):
        raise ValueError('"args" must be a JSON object.')
    kept = text[:end]
    if action == "write_site" and "html" not in args:
        if m := HTML_BLOCK.search(text, end) or PAGE.search(text, end):  # code block, or a bare page, after the JSON
            args["html"] = m.group(1) if m.re is HTML_BLOCK else m.group(0)
            kept, end = text[:m.end()], m.end()
        else:
            raise NeedPage("No page yet. Now reply with ONLY the complete page in a ```html ... ``` code block.")
    if (more := text[end:].count('"action"')) > 0:
        kept += f"\n[{more} more action(s) in this reply were ignored: one action per message]"

    expected = TOOLS[action].ARGS
    if missing := [a for a in expected if not isinstance(args.get(a), expected[a])]:
        raise ValueError(f"{action}: missing or wrong args {missing}. Usage: {TOOLS[action].DESCRIPTION}")
    if extra := set(args) - set(expected):
        raise ValueError(f"{action}: unexpected args {sorted(extra)}. Usage: {TOOLS[action].DESCRIPTION}")
    return action, args, str(obj.get("thought", "")), kept


def run_turn(llm: LLMClient, state: State, max_steps: int, step: int) -> tuple[bool, int]:
    """Agent loop for one user prompt. Returns (finished, last step number)."""
    invalid, previous, searches, awaiting = 0, None, 0, False
    for step in range(step + 1, step + max_steps + 1):
        messages, tokens = state.ctx.build()
        print(f"[step {step:2}] tokens: " + " · ".join(f"{k} {v}" for k, v in tokens.items()))
        try:
            reply = llm.chat(messages, max_tokens=MAX_TOKENS)
        except Exception as exc:  # gateway down, budget used up, ... (LLMClient already retried)
            print(f"Stopped: model call failed: {type(exc).__name__}: {exc}")
            return False, step
        try:
            page, awaiting = (page_in(reply) if awaiting else None), False
            if page:  # the page that write_site announced in the previous message
                action, args, thought, kept = "write_site", {"html": page}, "the page", reply
            else:
                action, args, thought, kept = parse_reply(reply)
            observation, obs_short = TOOLS[action].run(args, state)
            searches = searches + 1 if action == "search_style_guide" else 0
            if searches >= SEARCH_NUDGE:
                observation += (f"\nNote: {searches} searches in a row. Write the page now with write_site; "
                                "the checker will tell you what is still missing.")
            if (action, args) == previous:
                observation += "\nNote: you repeated your previous action exactly. Do something different."
            previous, invalid = (action, args), 0
            query = f' "{args["query"]}"' if "query" in args else ""
            reply_short = f"step {step}: {action}{query} ({thought[:80]})"
        except NeedPage as exc:  # models told "one JSON per message" often stop after the JSON: ask for the page
            action, observation, obs_short, kept, awaiting = "write_site", str(exc), "waiting for the page", reply, True
            reply_short = f"step {step}: write_site (page follows)"
        except Exception as exc:  # bad reply (ValueError) or a tool error: never crash, tell the model
            msg = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            action, observation, obs_short = "invalid", f"Error: {msg}", f"error: {msg[:60]}"
            kept = reply if len(reply) <= KEEP_INVALID else reply[:KEEP_INVALID] + f"\n[... cut, {len(reply)} chars]"
            reply_short, invalid = f"step {step}: invalid reply", invalid + 1
        print(f"          {action}: {obs_short}")
        if "more action(s)" in kept:
            observation += "\nNote: you sent several actions; only the first one was run. Send one per message."
        pins = (None, None) if awaiting else PINS.get(action, (None, None))  # "page follows" is not the page
        state.ctx.add_step(kept, reply_short, observation, obs_short, *pins)
        llm.log_event({"step": step, "action": action, "tokens": tokens, "reply": reply, "observation": observation,
                       "short": obs_short, "facts": list(state.ctx.facts)})
        if state.done:
            return True, step
        if invalid >= 3:
            print("Stopped: 3 invalid replies in a row.")
            return False, step
    print(f"Stopped: step limit of {max_steps} reached.")
    return False, step


def make_state(llm, content: Path, out: Path, k=5, local_embeddings=False, full_history=False,
               cache_dir: Path | None = None) -> State:
    """Wire everything together: system prompt (with the tool list), context, RAG, state."""
    tools = "\n".join(f"- {name}  {t.DESCRIPTION}" for name, t in TOOLS.items())
    ctx = Context(llm, SYSTEM_PROMPT.format(tools=tools), full_history=full_history)
    cache = {"cache_dir": cache_dir} if cache_dir else {}  # e.g. a writable folder inside a sandbox
    rag = StyleGuideRAG(Embedder(llm, local=local_embeddings, **cache))
    return State(ctx, rag, content, out, member_names_from_content(content), k=k)


def next_log_path() -> Path:
    numbers = [int(m.group(1)) for p in (HERE / "logs").glob("run_*.jsonl") if (m := re.match(r"run_(\d+)$", p.stem))]
    return HERE / "logs" / f"run_{max(numbers, default=0) + 1:02d}.jsonl"


def main() -> None:
    p = argparse.ArgumentParser(description="Small coding agent for the team website (Task 2).")
    p.add_argument("--content", type=Path, default=Path("task1_opencode/content"))
    p.add_argument("--out", type=Path, default=HERE / "site" / "index.html")
    p.add_argument("--model", help="chat model (default $LAB_CHAT_MODEL)")
    p.add_argument("--max-steps", type=int, default=25, help="per user prompt")
    p.add_argument("-k", type=int, default=5, help="chunks per search")
    p.add_argument("--local-embeddings", action="store_true", help="embed locally instead of via the gateway")
    p.add_argument("--full-history", action="store_true", help="no summaries: send every message as-is")
    p.add_argument("--log", type=Path, help="log file (default logs/run_XX.jsonl)")
    p.add_argument("--chat", help="chat name: resume it if it exists (default: a new chat). See chat.py")
    a = p.parse_args()

    try:
        llm = LLMClient(model=a.model, log_path=a.log or next_log_path())
    except RuntimeError as exc:
        sys.exit(f"{exc}\nRun `python setup_api.py` once to create .env.")
    state = make_state(llm, a.content, a.out, a.k, a.local_embeddings, a.full_history)
    if not state.members:
        sys.exit(f"No profiles in {a.content} (need <firstname>.md files + images/). Nothing to build.")
    print(f"Log: {llm.log_path} | members: {state.members}")

    name = a.chat or chat.new_name()
    chat.talk(llm, state, name, FIRST_PROMPT, run_turn, a.max_steps)


if __name__ == "__main__":
    main()
