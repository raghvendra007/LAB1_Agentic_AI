"""The prompt processor: builds what the model sees in each step, and keeps it small.

Every message is stored twice:  full  (kept in the log)  and  short  (one line, made once).

What the model gets in a step:
    [system]   rules + tool list                                   fixed
    [memory]   facts the agent saved + summary of older steps (short lines)
    [pinned]   the newest page it wrote + the profiles (if they are older than the recent window)
    [prompt]   the newest user prompt, word for word
    [recent]   the last RECENT_STEPS steps in full (reply + observation)

When the summary gets longer than SUMMARY_LIMIT tokens, one LLM call compresses it into key points.
With full_history=True everything is sent as-is (to compare token use in the video).
"""

from __future__ import annotations

from common.llm_client import count_tokens

RECENT_STEPS = 2  # steps kept word for word
SUMMARY_LIMIT = 400  # tokens; above this the summary is compressed by the LLM (about step 15)
FACTS_LIMIT = 500  # tokens; above this the oldest fact is dropped
LONG_PROMPT = 60  # tokens; older user prompts longer than this are summarized by the LLM


class Context:
    def __init__(self, llm, system_prompt: str, full_history: bool = False):
        self.llm, self.system, self.full_history = llm, system_prompt, full_history
        # facts: list[str]; entries: {"role", "full", "short", "kind": "prompt"|"reply"|"obs", "pin": None|"page"|"profiles"}
        # compressed: key points of entries[:folded]; folded: absolute index
        self.reset()

    # ------------------------------------------------------------- chats (chat.py saves / loads these)
    def reset(self) -> None:
        self.facts, self.entries, self.compressed, self.folded = [], [], "", 0

    def to_dict(self) -> dict:
        return {"facts": self.facts, "entries": self.entries, "compressed": self.compressed, "folded": self.folded}

    def load(self, data: dict) -> None:
        self.facts, self.entries = data["facts"], data["entries"]
        self.compressed, self.folded = data["compressed"], data["folded"]

    # ------------------------------------------------------------- memory
    def remember(self, fact: str) -> str | None:
        """Add a fact. Returns the dropped oldest fact if memory was full."""
        self.facts.append(fact)
        if count_tokens("\n".join(self.facts)) > FACTS_LIMIT and len(self.facts) > 1:
            return self.facts.pop(0)
        return None

    # ------------------------------------------------------------- adding
    def add_prompt(self, text: str) -> None:
        short = text if count_tokens(text) <= LONG_PROMPT else self._ask(
            "Summarize this request in at most 3 short bullet points. Keep names, numbers and exact wording "
            f"of requirements.\n\n{text}")
        self._add("user", text, f"user asked: {short}", "prompt")

    def add_step(self, reply, reply_short, observation, obs_short, pin_reply=None, pin_obs=None) -> None:
        self._add("assistant", reply, reply_short, "reply", pin_reply)
        self._add("user", f"Observation:\n{observation}", f"  -> {obs_short}", "obs", pin_obs)

    def _add(self, role, full, short, kind, pin=None):
        self.entries.append({"role": role, "full": full, "short": short, "kind": kind, "pin": pin})

    # ------------------------------------------------------------- building
    def build(self) -> tuple[list[dict], dict]:
        """Return (messages for the LLM, token count per section)."""
        system = [{"role": "system", "content": self.system}]
        if self.full_history:
            recent = [{"role": e["role"], "content": e["full"]} for e in self.entries]
            return self._result(system, self._memory(""), [], [], recent)

        last_prompt = max(i for i, e in enumerate(self.entries) if e["kind"] == "prompt")
        recent_start = max(len(self.entries) - 2 * RECENT_STEPS, last_prompt + 1)
        newest_pin = {e["pin"]: i for i, e in enumerate(self.entries) if e["pin"]}  # latest page / profiles
        pinned = sorted(i for i in newest_pin.values() if i < recent_start)
        old = [i for i in range(recent_start) if i != last_prompt]

        memory = self._memory(self._summary(old, recent_start))
        pins = [{"role": "user", "content": "PINNED (" + ("your newest page" if self.entries[i]["pin"] == "page"
                 else "the profiles") + "):\n" + self.entries[i]["full"]} for i in pinned]
        prompt = [{"role": "user", "content": self.entries[last_prompt]["full"]}]
        recent = [{"role": e["role"], "content": e["full"]} for e in self.entries[recent_start:]]
        return self._result(system, memory, pins, prompt, recent)

    def _summary(self, old: list[int], recent_start: int) -> str:
        head = [self.compressed] if self.compressed else []
        text = "\n".join(head + [self.entries[i]["short"] for i in old if i >= self.folded])
        if count_tokens(text) > SUMMARY_LIMIT:  # summary of summaries
            everything = head + [e["short"] for e in self.entries[self.folded:recent_start]]  # incl. the prompt
            self.compressed = self._ask(
                "These are notes of an agent's earlier steps. Compress them into at most 10 key points "
                "(the user's requests, what was done, what was found, what is still failing).\n\n"
                + "\n".join(everything))
            self.folded = recent_start
            text = self.compressed
        return text

    def _memory(self, summary: str) -> list[dict]:
        parts = []
        if self.facts:
            parts.append("Facts you saved:\n" + "\n".join(f"- {f}" for f in self.facts))
        if summary:
            parts.append("Summary of earlier steps:\n" + summary)
        return [{"role": "user", "content": "MEMORY\n\n" + "\n\n".join(parts)}] if parts else []

    @staticmethod
    def _result(*sections):
        names = ["system", "memory", "pinned", "prompt", "recent"]
        tokens = {n: sum(count_tokens(m["content"]) for m in s) for n, s in zip(names, sections)}
        tokens["total"] = sum(tokens.values())
        return [m for s in sections for m in s], tokens

    def _ask(self, prompt: str) -> str:
        return self.llm.chat([{"role": "user", "content": prompt}]).strip()
