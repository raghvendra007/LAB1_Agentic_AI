"""Autonomous coding agent loop for Task 2."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add repository root to path for common imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient
from common.site_checker import check_site
from common.embed_images import embed_images
from task2_own_agent.rag import StyleGuideRAG

SYSTEM_PROMPT = """You are an autonomous web-development agent. Your objective is to build a standalone, single-file HTML website introducing a software engineering team.

You must interact strictly through a JSON action protocol. In every turn, respond with exactly one JSON object:
{"thought": "concise explanation of your immediate next step", "action": "", "args": {}}

Available actions:
1. search_style_guide: {"query": ""}
   Retrieve guidelines, requirements, and color/formatting rules from the style guide.
2. read_profiles: {}
   Read member profile contents and discover available image files.
3. write_site: {}
   Provide the full HTML code in a ```html code block immediately following your JSON object.
   - Do NOT embed raw base64 data.
   - Use placeholders for images: <img src="{{image:filename.png}}" alt="...">.
   - Embed all CSS and JS directly inside the single HTML file.
4. finish: {"summary": "<brief summary of what was built>"}
   Only call this when the site has been written and all checker requirements have passed.

Rules:
- Never guess style guide specifications. Use search_style_guide to look up navigation, typography, breakpoints, footer, and day/night requirements.
- Notice: Some documents are deprecated versions; always follow the newest specifications.
"""


def parse_action_response(text: str) -> tuple[dict[str, Any], str | None]:
    """Extracts the JSON action and any trailing ```html ... ``` block."""
    text = text.strip()
    html_block = None

    # Check for trailing ```html ... ``` block
    html_match = re.search(r"```html\s*([\s\S]*?)\s*```", text)
    if html_match:
        html_block = html_match.group(1).strip()
        # Remove the code block from the string to safely parse the remaining JSON
        text = text[:html_match.start()] + text[html_match.end():]
        text = text.strip()

    # Locate first '{' and matching outermost '}'
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace == -1:
        raise ValueError("No valid JSON object found in response.")

    json_str = text[first_brace : last_brace + 1]
    parsed = json.loads(json_str)

    if not isinstance(parsed, dict) or "action" not in parsed:
        raise ValueError("Response must be a JSON object containing an 'action' key.")

    return parsed, html_block


class OwnCodingAgent:
    def __init__(
        self,
        content_dir: Path,
        out_file: Path,
        style_guide_dir: Path,
        log_path: Path,
        max_steps: int = 25,
    ) -> None:
        self.content_dir = Path(content_dir)
        self.out_file = Path(out_file)
        self.out_file.parent.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.content_dir / "images"

        self.max_steps = max_steps
        self.llm = LLMClient(log_path=log_path)
        print("Indexing style guide documents...")
        self.rag = StyleGuideRAG(style_guide_dir, llm=self.llm)
        print(f"Indexed {len(self.rag.chunks)} chunks from style guide.")

        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Build the complete standalone team website. "
                    "Start by reading profiles and searching the style guide for layout, navigation, and color rules."
                ),
            },
        ]
        self.last_checker_passed = False
        self.last_checker_report = "Site has not been written or checked yet."

    def execute_step(self, step_idx: int) -> tuple[bool, str]:
        """Executes one step in the loop. Returns (is_finished, observation)."""
        print(f"\n--- [Step {step_idx}/{self.max_steps}] Calling Model ---")
        response_text = self.llm.chat(self.messages, max_tokens=16000)
        self.messages.append({"role": "assistant", "content": response_text})

        # Parse action
        try:
            action_data, html_block = parse_action_response(response_text)
            action = action_data.get("action")
            args = action_data.get("args", {})
            thought = action_data.get("thought", "")
            print(f"Thought: {thought}")
            print(f"Action:  {action}")
        except Exception as e:
            obs = f"Error: Failed to parse action JSON: {e}. Respond strictly with {{\"thought\": \"...\", \"action\": \"...\", \"args\": {{}}}}"
            print(f"Observation: {obs}")
            self.messages.append({"role": "user", "content": f"Observation: {obs}"})
            self.llm.log_event({"step": step_idx, "error": str(e), "observation": obs})
            return False, obs

        # Dispatch action
        if action == "search_style_guide":
            query = args.get("query", "")
            if not query:
                obs = "Error: 'search_style_guide' requires a non-empty 'query' argument."
            else:
                chunks = self.rag.search(query, k=4)
                obs_lines = [f"Found {len(chunks)} style guide excerpts:"]
                for c in chunks:
                    obs_lines.append(f"\n--- Document {c.doc_id}: {c.title} ---\n{c.text}")
                obs = "\n".join(obs_lines)

        elif action == "read_profiles":
            md_files = [f for f in sorted(self.content_dir.glob("*.md")) if not f.name.startswith("_")]
            profiles_text = []
            for p in md_files:
                profiles_text.append(f"### {p.name}\n{p.read_text(encoding='utf-8')}")

            img_files = [f.name for f in sorted(self.images_dir.glob("*"))] if self.images_dir.exists() else []
            obs = (
                f"Available images in content/images/: {img_files}\n\n"
                f"Team Member Profiles:\n" + "\n\n".join(profiles_text)
            )

        elif action == "write_site":
            html_content = html_block or args.get("html", "")
            if not html_content:
                obs = "Error: No HTML provided. Supply it in args['html'] or in a ```html code block after the JSON."
            else:
                try:
                    # Write initial HTML
                    self.out_file.write_text(html_content, encoding="utf-8")
                    # Embed images
                    if self.images_dir.exists():
                        embed_images(self.out_file, self.images_dir)
                        
                    # changed=>   (self.out_file, self.images_dir, self.out_file)

                    # Run check_site in full mode
                    report = check_site(
                        self.out_file,
                        content_dir=self.content_dir,
                        mode="full",
                    )
                    self.last_checker_passed = report.passed
                    self.last_checker_report = report.format()
                    obs = f"Checker results (passed={report.passed}):\n{report.format()}"
                except Exception as e:
                    obs = f"Error during write_site or verification: {e}"
                    self.last_checker_passed = False

        elif action == "finish":
            if not self.last_checker_passed:
                obs = f"Action rejected: check_site has not passed yet. Report:\n{self.last_checker_report}"
                print("Finish rejected: site check has failing rules.")
            else:
                obs = f"Finish accepted: all checker constraints passed. Summary: {args.get('summary', 'Done')}"
                print(obs)
                self.messages.append({"role": "user", "content": f"Observation: {obs}"})
                self.llm.log_event({"step": step_idx, "action": action, "observation": obs})
                return True, obs

        else:
            obs = f"Error: Unknown action '{action}'. Valid actions: search_style_guide, read_profiles, write_site, finish."

        print(f"Observation:\n{obs[:300]}..." if len(obs) > 300 else f"Observation:\n{obs}")
        self.messages.append({"role": "user", "content": f"Observation: {obs}"})
        self.llm.log_event({"step": step_idx, "action": action, "args": args, "observation": obs})
        return False, obs

    def run(self) -> None:
        step = 1
        while step <= self.max_steps:
            finished, _ = self.execute_step(step)
            step += 1

            if finished:
                # Terminal Human in the loop feedback
                user_feedback = input("\n[Agent finished] Enter feedback to continue, or press Enter to terminate: ").strip()
                if not user_feedback:
                    print("Run completed successfully.")
                    break
                print(f"Applying user feedback: {user_feedback}")
                self.messages.append({"role": "user", "content": f"User Feedback: {user_feedback}"})
                self.llm.log_event({"step": step, "type": "human_feedback", "feedback": user_feedback})
                self.last_checker_passed = False

        if step > self.max_steps:
            print(f"\nStopped: Maximum step limit ({self.max_steps}) reached.")


def main():
    parser = argparse.ArgumentParser(description="Run the custom coding agent for Task 2.")
    parser.add_argument("--content", default="task1_opencode/content", help="Path to profile markdown directory")
    parser.add_argument("--out", default="task2_own_agent/site/index.html", help="Path to output index.html")
    parser.add_argument("--style-guide", default="task2_own_agent/style_guide", help="Path to style guide folder")
    parser.add_argument("--max-steps", type=int, default=25, help="Maximum agent steps")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = REPO_ROOT / "task2_own_agent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{stamp}.jsonl"

    agent = OwnCodingAgent(
        content_dir=REPO_ROOT / args.content,
        out_file=REPO_ROOT / args.out,
        style_guide_dir=REPO_ROOT / args.style_guide,
        log_path=log_path,
        max_steps=args.max_steps,
    )
    agent.run()


if __name__ == "__main__":
    main()