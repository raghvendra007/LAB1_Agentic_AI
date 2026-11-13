"""Offline check of the agent with a scripted fake model (no gateway, no key). Reproducible.

    python task2_own_agent/test_agent.py
"""

import base64
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import context  # noqa: E402
from agent import MAX_TOKENS, State, parse_reply, run_turn  # noqa: E402
from common.llm_client import LLMClient  # noqa: E402
from context import Context  # noqa: E402
from rag import Embedder, StyleGuideRAG  # noqa: E402

# A page that passes every check (only used here, never shown to the model).
GOOD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Team Test | Agentic SE Lab</title>
<style>:root{--bg:#FFF8E7;--fg:#1D1D1F;--accent:#E4007C}[data-theme="dark"]{--bg:#14142B;--fg:#F4F1DE}
body{font-family:system-ui,sans-serif;font-size:1rem;background:var(--bg);color:var(--fg)}
@media (max-width: 700px){body{font-size:1.1rem}}</style></head><body>
<h1>Team Test</h1><button id="theme-toggle" aria-label="Toggle night mode">night</button>
<section id="team"><article class="member-card"><img src="{{image:anna.png}}" alt="Anna smiling">
<h2>Anna</h2><span class="hot-take">AI writes, we review.</span></article></section>
<section id="future-of-se"><h2>Future</h2><p>Robots.</p></section>
<footer>Built by an agent, supervised by humans.</footer>
<script>const t=document.getElementById('theme-toggle');t.onclick=()=>{const d=document.documentElement;
d.setAttribute('data-theme',d.getAttribute('data-theme')==='dark'?'light':'dark');
localStorage.setItem('upb-theme',d.getAttribute('data-theme'))};</script></body></html>"""
WRITE = '{"thought": "write", "action": "write_site", "args": {}}\n```html\n' + GOOD_HTML + "\n```"


class FakeOpenAI:
    """Returns the scripted replies in order; embeddings are random but fixed per text."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.embeddings = SimpleNamespace(create=self.embed)
        self.requests = []

    def create(self, **params):
        self.requests.append(params["messages"])
        self.max_tokens = params.get("max_tokens")
        msg = SimpleNamespace(role="assistant", content=self.replies.pop(0), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    def embed(self, model, input):
        vecs = [np.random.default_rng(sum(map(ord, t))).random(8).tolist() for t in input]
        return SimpleNamespace(data=[SimpleNamespace(embedding=v) for v in vecs])


def setup(replies, tmp, full_history=False):
    content = Path(tmp) / "content"
    (content / "images").mkdir(parents=True)
    (content / "anna.md").write_text("# Anna\n## Fun fact\nLikes tests.")
    (content / "images" / "anna.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
    fake = FakeOpenAI(replies)
    llm = LLMClient(model="fake", embedding_model="fake", log_path=Path(tmp) / "log.jsonl", openai_client=fake)
    ctx = Context(llm, "SYSTEM", full_history=full_history)
    rag = StyleGuideRAG(Embedder(llm, cache_dir=Path(tmp) / "cache"))
    state = State(ctx, rag, content, Path(tmp) / "site" / "index.html", ["Anna"])
    ctx.add_prompt("Build the site.")
    return fake, llm, state


def test_full_turn():
    replies = [
        "I will start now.",                                                          # 1 no JSON
        '{"action": "dance", "args": {}}',                                            # 2 unknown action
        '{"thought": "x", "action": "search_style_guide", "args": {"query": "night colors"}}',  # 3
        '{"thought": "x", "action": "remember", "args": {"fact": "night bg #14142B", "source": "SG-13"}}',
        '{"thought": "x", "action": "read_profiles", "args": {}}',                    # 5
        '{"thought": "x", "action": "say", "args": {"text": "writing now"}}',         # 6
        '{"thought": "early", "action": "finish", "args": {"summary": "done"}}',      # 7 rejected
        '{"action": "write_site", "args": {}}\n```html\n<img src="{{image:bob.png}}">\n```',  # 8 bad image
        WRITE,                                                                        # 9 passes
        '{"thought": "ok", "action": "finish", "args": {"summary": "done"}}',         # 10 accepted
    ]
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(replies, tmp)
        finished, step = run_turn(llm, state, max_steps=25, step=0)
        assert finished and step == 10
        obs = [e["full"] for e in state.ctx.entries if e["kind"] == "obs"]
        assert "No JSON" in obs[0] and "Unknown" in obs[1] and "[SG-" in obs[2] and "Saved" in obs[3]
        assert "anna.png" in obs[4] and "Rejected" in obs[6] and "not found" in obs[7]
        assert "All good!" in obs[8] and obs[9].endswith("Accepted.")
        assert state.ctx.facts == ["night bg #14142B (SG-13)"]
        # the last request: system, memory (facts + summary), pinned profiles, prompt, last 2 steps in full
        last = fake.requests[-1]
        memory = last[1]["content"]
        assert "night bg #14142B" in memory and "step 3: search_style_guide" in memory
        assert last[2]["content"].startswith("PINNED (the profiles)") and "Likes tests" in last[2]["content"]
        assert last[3]["content"] == "Build the site." and len(last) == 1 + 1 + 1 + 1 + 4
        assert "data:image/png;base64" in state.out.read_text()


def test_summary_compression():
    context.SUMMARY_LIMIT = 30  # tiny, so compression happens
    try:
        replies = ['{"thought": "look", "action": "read_profiles", "args": {}}'] * 4
        replies.insert(4, "KEY POINTS: profiles were read 3 times.")  # the compression call
        with tempfile.TemporaryDirectory() as tmp:
            fake, llm, state = setup(replies + ['{"action": "read_profiles", "args": {}}'], tmp)
            run_turn(llm, state, max_steps=5, step=0)
            assert state.ctx.compressed.startswith("KEY POINTS")
            assert "KEY POINTS" in fake.requests[-1][1]["content"]
    finally:
        context.SUMMARY_LIMIT = 400


def test_fold_after_feedback():
    """After compression, a new user prompt must not lose or duplicate summary lines."""
    context.SUMMARY_LIMIT = 30
    try:
        step = '{"thought": "look", "action": "read_profiles", "args": {}}'
        with tempfile.TemporaryDirectory() as tmp:
            fake, llm, state = setup([step] * 4 + ["KEY POINTS A"] + [step] * 3, tmp)
            run_turn(llm, state, max_steps=5, step=0)
            state.ctx.add_prompt("Make it funnier.")
            context.SUMMARY_LIMIT = 10_000  # no second compression: look at the plain summary
            msgs, _ = state.ctx.build()
            summary = msgs[1]["content"]
            compress_request = fake.requests[4][0]["content"]
            assert "user asked: Build the site." in compress_request  # the old prompt went into the key points
            assert "KEY POINTS A" in summary
            assert summary.count("step 5:") == 1 and msgs[-1]["content"] == "Make it funnier."
    finally:
        context.SUMMARY_LIMIT = 400


def test_bad_json_and_tool_crash():
    from tools import say
    replies = ['{"action": ["a"]}',                                                        # unhashable action
               'I think {maybe} {"thought": "x", "action": "read_profiles", "args": {}}',  # junk before JSON
               '{"action": "say", "args": {"text": "hi"}}']                                # tool crashes
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(replies, tmp)
        original, say.run = say.run, lambda args, st: 1 / 0
        try:
            run_turn(llm, state, max_steps=3, step=0)
        finally:
            say.run = original
        obs = [e["full"] for e in state.ctx.entries if e["kind"] == "obs"]
        assert "Unknown or missing action" in obs[0] and "anna.md" in obs[1] and "ZeroDivisionError" in obs[2]


def test_chat_loop():
    """chat.talk end to end: build, a question (reply), commands, a second chat, switching back, saved files."""
    import json
    import chat
    replies = [WRITE, '{"thought": "ok", "action": "finish", "args": {"summary": "built"}}',          # turn 1
               '{"thought": "question", "action": "reply", "args": {"text": "Night bg is #14142B."}}',  # turn 2
               '{"thought": "chat", "action": "reply", "args": {"text": "Hi!"}}',                       # other chat
               '{"thought": "page failing", "action": "write_site", "args": {}}\n```html\n<p>x</p>\n```',
               '{"thought": "dodge", "action": "reply", "args": {"text": "done"}}']                       # rejected
    said = iter(["/facts", "Which night colors?", "/switch other", "hello", "/switch main", "/chats",
                 "Break the page.", ""])
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(replies, tmp)
        state.ctx.reset()
        old, chat.CHATS = chat.CHATS, Path(tmp) / "sessions"
        try:
            chat.talk(llm, state, "main", "Build the site.", run_turn, max_steps=2, ask=lambda _: next(said))
            main = json.loads((chat.CHATS / "main.json").read_text())
            other = json.loads((chat.CHATS / "other.json").read_text())
            assert [e["full"] for e in main["context"]["entries"] if e["kind"] == "prompt"] == [
                "Build the site.", "Which night colors?", "Break the page."]
            assert any('replied "Night bg' in e["short"] for e in main["context"]["entries"])
            assert "hello" in json.dumps(other) and "Night" not in json.dumps(other)   # chats are separate
            assert main["step"] == 5 and main["logs"] == [str(llm.log_path)]
            assert "reply rejected: page failing" in main["context"]["entries"][-1]["short"]
            rows = {c["name"]: c for c in chat.list_chats()}
            assert set(rows) == {"main", "other"} and rows["main"]["last"] == "Break the page."
            # resume: a fresh start continues where we stopped
            fake2, llm2, state2 = setup(['{"action": "reply", "args": {"text": "still here"}}'], tmp + "/b")
            chat.talk(llm2, state2, "main", "IGNORED first prompt", run_turn, 1, ask=lambda _, it=iter(["again?", ""]): next(it))
            prompts = [e["full"] for e in state2.ctx.entries if e["kind"] == "prompt"]
            assert prompts[-1] == "again?" and "IGNORED first prompt" not in prompts
        finally:
            chat.CHATS = old


def test_main_cli(monkeypatch=None):
    """agent.main() as typed on the command line: options, profiles, chat, quit. Fake model, fake keyboard."""
    import builtins
    import agent
    import chat
    replies = [WRITE, '{"thought": "ok", "action": "finish", "args": {"summary": "built"}}']
    with tempfile.TemporaryDirectory() as tmp:
        _, _, state = setup([], tmp)  # creates tmp/content with anna.md + image
        fake = FakeOpenAI(replies)
        saved = (agent.LLMClient, builtins.input, sys.argv, chat.CHATS)
        agent.LLMClient = lambda model=None, log_path=None: LLMClient(
            model="fake", embedding_model="fake", log_path=log_path, openai_client=fake)
        builtins.input = lambda _: ""
        chat.CHATS = Path(tmp) / "sessions"
        sys.argv = ["agent.py", "--content", str(state.content), "--out", f"{tmp}/site/index.html",
                    "--log", f"{tmp}/run_01.jsonl", "--chat", "cli"]
        try:
            agent.main()
        finally:
            agent.LLMClient, builtins.input, sys.argv, chat.CHATS = saved
        assert (Path(tmp) / "site" / "index.html").exists() and (Path(tmp) / "sessions" / "cli.json").exists()
        assert '"user_prompt"' in (Path(tmp) / "run_01.jsonl").read_text()


def test_stop_conditions():
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(["not json"] * 3, tmp)
        assert run_turn(llm, state, max_steps=25, step=0) == (False, 3)  # 3 invalid in a row
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(['{"action": "read_profiles", "args": {}}'] * 2, tmp)
        assert run_turn(llm, state, max_steps=2, step=0) == (False, 2)  # step limit
        assert "repeated" in state.ctx.entries[-1]["full"]


def test_full_history_sends_everything():
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(['{"action": "read_profiles", "args": {}}'] * 4, tmp, full_history=True)
        run_turn(llm, state, max_steps=4, step=0)
        assert len(fake.requests[-1]) == 1 + 1 + 6  # system + prompt + 3 full steps, no memory/pins


def test_parse():
    assert parse_reply('```json\n{"action": "finish", "args": {"summary": "s"}}\n```')[0] == "finish"
    assert parse_reply('{"action": "write_site", "args": {"html": "<p>x</p>"}}')[1]["html"] == "<p>x</p>"
    # forgiving: one } missing (step 3 of a real run), and the name(key='value') format (step 2)
    cut = '{"thought": "t", "action": "search_style_guide", "args": {"query": "night mode"}'
    assert parse_reply(cut)[:2] == ("search_style_guide", {"query": "night mode"})
    native = "<|tool_call_start|>[search_style_guide(query='team website night mode')]<|tool_call_end|>"
    assert parse_reply(native)[:2] == ("search_style_guide", {"query": "team website night mode"})
    assert parse_reply("read_profiles()")[0] == "read_profiles"
    css = '{"action": "write_site", "args": {}}\n```html\n<style>a{color:red}</style>\n```'
    assert "color:red" in parse_reply(css)[1]["html"]
    for bad in ['{"action": "finish"', "search_style_guide(query=open('x'))", "[1]", '{"action": "read_profiles", "args": {"x": 1}}',
                '{"action": "remember", "args": {"fact": "f"}}']:
        try:
            parse_reply(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_real_run_failures():
    """Replies seen in real Agent lab runs (openrouter/free, run_03/run_04): none may break the turn or bloat the context."""
    step = '{"thought": "t", "action": "search_style_guide", "args": {"query": "footer"}}'
    mcp = '\n\nI will read them.\n\n<mcp-tool>\n{"name": "read_profiles", "input": {}}\n</mcp-tool>'
    fenced = json.dumps({"thought": "w", "action": "write_site", "args": {"html": "\n```html\n" + GOOD_HTML + "\n```"}})
    replies = [mcp, (step + "\n") * 2000, "", fenced, '{"action": "finish", "args": {"summary": "ok"}}']
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup(replies, tmp)
        assert run_turn(llm, state, max_steps=10, step=0) == (True, 5)
        obs = [e["full"] for e in state.ctx.entries if e["kind"] == "obs"]
        assert "anna.md" in obs[0] and "only the first one was run" in obs[1] and "empty" in obs[2]
        assert "All good!" in obs[3]  # the ```html fence inside the JSON string was removed
        assert state.out.read_text().startswith("<!DOCTYPE html>")
        assert max(len(e["full"]) for e in state.ctx.entries if e["kind"] == "reply") < len(fenced) + 100
        assert all(count < 3000 for count in (sum(len(m["content"]) for m in r) // 4 for r in fake.requests))
        assert fake.max_tokens == MAX_TOKENS
    # run_05: a free model searched for all 25 steps and never wrote. After 6 searches in a row it gets a nudge.
    with tempfile.TemporaryDirectory() as tmp:
        fake, llm, state = setup([step] * 7, tmp)
        run_turn(llm, state, max_steps=7, step=0)
        obs = [e["full"] for e in state.ctx.entries if e["kind"] == "obs"]
        assert "Write the page now" not in obs[4] and "6 searches in a row" in obs[5]
    # run_06/07: JSON only, page forgotten (3 times -> stopped). Now the page may come in the next message.
    with tempfile.TemporaryDirectory() as tmp:
        only = '{"thought": "w", "action": "write_site", "args": {}}'
        fake, llm, state = setup([only, only, "```html\n" + GOOD_HTML + "\n```", only, GOOD_HTML,
                                  '{"action": "finish", "args": {"summary": "ok"}}'], tmp)
        assert run_turn(llm, state, max_steps=6, step=0) == (True, 6)
        obs = [e["full"] for e in state.ctx.entries if e["kind"] == "obs"]
        assert "ONLY the complete page" in obs[0] and "All good!" in obs[2] and "All good!" in obs[4]
        pinned = [e for e in state.ctx.entries if e["pin"] == "page"]
        assert all(GOOD_HTML[:40] in e["full"] for e in pinned)  # only real pages are pinned, never "page follows"
    for after in ("```\n" + GOOD_HTML + "\n```", GOOD_HTML):
        assert parse_reply('{"action": "write_site", "args": {}}\n' + after)[1]["html"].strip() == GOOD_HTML
    # page inside broken JSON quotes (run_03): the page is still saved
    broken = "<|tool_call_start|>[write_site(args='{\"html\": \"" + GOOD_HTML.replace("\n", "\\n") + "\"}')]"
    action, args, _, _ = parse_reply(broken)
    assert action == "write_site" and args["html"] == GOOD_HTML


if __name__ == "__main__":
    for test in (test_parse, test_chat_loop, test_main_cli, test_bad_json_and_tool_crash, test_fold_after_feedback, test_stop_conditions, test_full_history_sends_everything, test_summary_compression,
                 test_full_turn, test_real_run_failures):
        test()
    print("all agent checks passed")
