"""Chats: the conversation around the agent. agent.py = one turn of work, chat.py = many turns, saved.

A chat is a file sessions/<name>.json with the context (history full + short, facts, summary), the step counter,
the page version, and the log files of every run. agent.py starts a chat (new or resumed) and hands over to talk().

In the chat, type a task, feedback or a question. Commands:
    /help  /chats  /switch NAME  /facts  /history  /tokens  /quit   (empty line = quit)

Manage chats without starting the model:
    python task2_own_agent/chat.py list | show NAME | delete NAME
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

CHATS = Path(__file__).resolve().parent / "sessions"
HELP = """Commands: /chats (list) · /switch NAME (other chat) · /facts · /history · /tokens · /quit
Anything else is a message to the agent: a task, feedback ("make it funnier") or a question."""


def chat_path(name: str) -> Path:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("chat names use letters, digits, - and _ only")
    return CHATS / f"{name}.json"


def new_name() -> str:
    return datetime.now().strftime("chat-%Y%m%d-%H%M%S")


def list_chats() -> list[dict]:
    rows = []
    for p in sorted(CHATS.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        prompts = [e["full"] for e in d["context"]["entries"] if e["kind"] == "prompt"]
        rows.append({"name": p.stem, "updated": d["updated"], "steps": d["step"], "facts": len(d["context"]["facts"]),
                     "last": (prompts[-1] if prompts else "")[:60]})
    return rows


def save(name: str, state, step: int, log_path: Path) -> None:
    path = chat_path(name)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    logs = old.get("logs", [])
    if str(log_path) not in logs:
        logs.append(str(log_path))
    CHATS.mkdir(exist_ok=True)
    path.write_text(json.dumps({"name": name, "created": old.get("created", datetime.now().isoformat(timespec="seconds")),
                                "updated": datetime.now().isoformat(timespec="seconds"), "step": step,
                                "version": state.version, "logs": logs, "context": state.ctx.to_dict()},
                               ensure_ascii=False), encoding="utf-8")


def load(name: str, state) -> int:
    """Load a saved chat into state. Returns the step counter (0 for a new chat)."""
    path = chat_path(name)
    state.ctx.reset()
    state.version, state.last_report = 0, None
    if not path.exists():
        return 0
    d = json.loads(path.read_text(encoding="utf-8"))
    state.ctx.load(d["context"])
    state.version = d["version"]
    return d["step"]


def talk(llm, state, name: str, first_prompt: str | None, run_turn, max_steps: int, ask=None) -> None:
    """The chat loop: user message -> agent turn -> save -> next message. `ask` is input() (or a fake in tests)."""
    ask = ask or input  # looked up at call time, so tests can replace input()
    step = load(name, state)
    print(f"Chat '{name}': " + (f"resumed, {step} steps, {len(state.ctx.facts)} facts." if step else "new.")
          + "  (/help for commands)")
    message = first_prompt if not step else None
    while True:
        if message is None:
            try:
                message = ask("\nYou: ").strip()
            except EOFError:  # Ctrl-D
                message = ""
        if message in ("", "/quit"):
            break
        if message.startswith("/"):
            name, step = command(message, llm, state, name, step, run_turn)
        else:
            state.ctx.add_prompt(message)
            llm.log_event({"step": step, "chat": name, "user_prompt": message})
            state.done, state.last_report = False, None  # a page task must end with a freshly checked page
            _, step = run_turn(llm, state, max_steps, step)
            save(name, state, step, llm.log_path)
        message = None
    save(name, state, step, llm.log_path)
    print(f"Chat '{name}' saved ({chat_path(name).relative_to(CHATS.parent)}). {llm.usage_summary()}")


def command(text: str, llm, state, name: str, step: int, run_turn) -> tuple[str, int]:
    cmd, _, arg = text.partition(" ")
    if cmd == "/chats":
        for c in list_chats():
            print(f"  {'*' if c['name'] == name else ' '} {c['name']:24} {c['updated']}  {c['steps']:3} steps  "
                  f"{c['facts']} facts  last: {c['last']}")
    elif cmd == "/switch" and arg:
        save(name, state, step, llm.log_path)
        name, step = arg.strip(), load(arg.strip(), state)
        print(f"Switched to chat '{name}' ({step} steps).")
    elif cmd == "/facts":
        print("\n".join(f"  - {f}" for f in state.ctx.facts) or "  (no facts yet)")
    elif cmd == "/history":
        print("\n".join(e["short"] for e in state.ctx.entries) or "  (empty)")
    elif cmd == "/tokens":
        _, tokens = state.ctx.build() if state.ctx.entries else (None, {})
        print("  next request:", " · ".join(f"{k} {v}" for k, v in tokens.items()) or "empty chat")
    else:
        print(HELP)
    return name, step


if __name__ == "__main__":
    cmd, arg = (sys.argv[1:] + ["list", ""])[:2]
    if cmd == "list":
        for c in list_chats():
            print(f"{c['name']:24} {c['updated']}  {c['steps']:3} steps  {c['facts']} facts  last: {c['last']}")
    elif cmd == "show" and arg:
        d = json.loads(chat_path(arg).read_text(encoding="utf-8"))
        print(f"{d['name']}  created {d['created']}  logs: {', '.join(d['logs'])}\nfacts: {d['context']['facts']}")
        print("\n".join(e["short"] for e in d["context"]["entries"]))
    elif cmd == "delete" and arg:
        chat_path(arg).unlink()
        print(f"deleted {arg}")
    else:
        print(__doc__)
