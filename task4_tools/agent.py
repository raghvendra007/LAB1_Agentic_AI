"""Autonomous agent loop for Task 4.

Uses native tool calling via LLMClient.chat_with_tools to diagnose and fix
tinyledger issues and ensure all tests pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root and task directory to sys.path
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient, message_to_dict
from tools import ToolRegistry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Task 4 Autonomous Agent Loop")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use scripted MockOpenAIClient instead of the real UPB AI Gateway model.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=HERE / "workspace",
        help="Path to tinyledger workspace repository (default: task4_tools/workspace).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Maximum number of turns in the agent loop (default: 30).",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        default=HERE / "logs" / "trace.jsonl",
        help="Path for tool execution trace log (default: task4_tools/logs/trace.jsonl).",
    )
    return parser.parse_args()


def log_trace(
    trace_path: Path,
    name: str,
    arguments: str,
    ok: bool,
    error: str | None,
    truncated: bool,
    duration_s: float,
) -> None:
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "arguments": arguments,
        "ok": ok,
        "error": error,
        "truncated": truncated,
        "duration_s": round(duration_s, 4),
    }
    with trace_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_agent(args: argparse.Namespace) -> None:
    workspace = args.workspace.resolve()
    if not workspace.exists():
        print(f"Workspace {workspace} does not exist. Initializing with reset_workspace...")
        import reset_workspace

        reset_workspace.main()

    registry = ToolRegistry(workspace=workspace, timeout_s=60)
    args.trace.parent.mkdir(parents=True, exist_ok=True)

    # Clean trace log for fresh run
    if args.trace.exists():
        args.trace.unlink()

    if args.mock:
        from mock_model import MockOpenAIClient

        mock_log = HERE / "logs" / "mock_run.jsonl"
        mock_log.parent.mkdir(parents=True, exist_ok=True)
        llm = LLMClient(model="mock", log_path=mock_log, openai_client=MockOpenAIClient())
        print("Running agent with MockOpenAIClient (scripted sequence)...")
    else:
        real_log = HERE / "logs" / "real_run.jsonl"
        real_log.parent.mkdir(parents=True, exist_ok=True)
        llm = LLMClient(log_path=real_log)
        print(f"Running agent with Gateway model: {llm.model}...")

    tools = registry.schemas()
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are an expert autonomous software engineer fixing bugs in the tinyledger project.\n"
                "Rules:\n"
                "- All source code edits must be strictly below src/ using edit_file.\n"
                "- Never modify any tests, data files, or configuration.\n"
                "- Use run_tests to check test results and tracebacks.\n"
                "- When all tests pass and the issue is resolved, provide a concise final summary without tool calls."
            ),
        },
        {
            "role": "user",
            "content": "Make all tests pass and fix the problem described in ISSUE.md. Do not modify tests.",
        },
    ]

    for step in range(1, args.max_steps + 1):
        print(f"\n[Step {step}/{args.max_steps}] Calling model...")
        message = llm.chat_with_tools(messages, tools=tools)
        messages.append(message_to_dict(message))

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            print("\n[Agent Completed] Model finished without further tool calls.")
            if message.content:
                print(f"Final response:\n{message.content}")
            break

        for call in tool_calls:
            tool_name = call.function.name
            tool_args = call.function.arguments
            print(f"  -> Tool call: {tool_name}({tool_args[:80]}...)")

            start_t = time.time()
            result = registry.execute(tool_name, tool_args)
            duration_s = time.time() - start_t

            log_trace(
                trace_path=args.trace,
                name=tool_name,
                arguments=tool_args,
                ok=result.ok,
                error=result.error,
                truncated=result.truncated,
                duration_s=duration_s,
            )

            status = "OK" if result.ok else f"ERR: {result.error}"
            print(f"     <- [{status}] ({duration_s:.2f}s)")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.to_content(),
                }
            )
    else:
        print(f"\nReached maximum step limit ({args.max_steps}). Stopped.")

    print("\nSummary:")
    print(f"Tool trace log: {args.trace}")
    print(llm.usage_summary())


if __name__ == "__main__":
    run_agent(parse_args())

