"""Tool layer implementation for Task 4.

Implements ToolRegistry(ToolRegistryBase) providing secure and sandboxed
tools for the agent to inspect, test, and edit tinyledger.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import jsonschema

from tool_contract import (
    DEFAULT_TIMEOUT_S,
    MAX_LIST_ENTRIES,
    MAX_OUTPUT_CHARS,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    REQUIRED_TOOLS,
    ToolRegistryBase,
    ToolResult,
)

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace relative to the workspace root. Supports an optional directory path and glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to workspace root (defaults to '.').",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter files (e.g. '*.py').",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents as numbered lines from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the workspace.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional start line number (1-based, inclusive).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional end line number (1-based, inclusive).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search code across files in the workspace using a regular expression pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression pattern to search for.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional glob pattern to restrict search (e.g. '**/*.py').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return (capped at 200).",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the test suite using pytest and return a structured summary of passed/failed tests and failure tracebacks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Optional test file or test node ID (e.g. 'tests/test_settle.py').",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cli",
            "description": "Run the tinyledger CLI with arguments (python -m tinyledger <args>).",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of command line arguments.",
                    }
                },
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit an existing file strictly below src/ by replacing old_str with new_str. old_str must match uniquely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to an existing file strictly below src/.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact text to find and replace (must appear exactly once in the file).",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolRegistry(ToolRegistryBase):
    def __init__(self, workspace: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        super().__init__(workspace=workspace, timeout_s=timeout_s)
        self._schema_map = {s["function"]["name"]: s for s in SCHEMAS}
        self._tools = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "run_tests": self._run_tests,
            "run_cli": self._run_cli,
            "edit_file": self._edit_file,
        }

    def schemas(self) -> list[dict]:
        return list(SCHEMAS)

    def execute(self, name: str, arguments: str) -> ToolResult:
        try:
            if not isinstance(arguments, str):
                return ToolResult(ok=False, error="Arguments must be passed as a string.")
            try:
                args = json.loads(arguments)
            except Exception as e:
                return ToolResult(ok=False, error=f"Invalid JSON in arguments: {e}")

            if not isinstance(args, dict):
                return ToolResult(ok=False, error="Arguments must decode to a JSON object (dict).")

            if name not in self._tools:
                return ToolResult(ok=False, error=f"Unknown tool: '{name}'.")

            schema = self._schema_map[name]["function"]["parameters"]
            try:
                jsonschema.validate(instance=args, schema=schema)
            except jsonschema.ValidationError as e:
                return ToolResult(ok=False, error=f"Argument validation error for '{name}': {e.message}")

            handler = self._tools[name]
            return handler(**args)
        except Exception as e:
            return ToolResult(ok=False, error=f"Tool execution failed: {e}")

    # ------------------------------------------------------------- containment

    def _safe_path(self, rel_path: str, must_exist: bool = True) -> Path:
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("Path must be a non-empty string.")

        if "\0" in rel_path:
            raise ValueError("Path cannot contain null bytes.")

        p = Path(rel_path)
        if p.is_absolute():
            raise PermissionError(f"Absolute path '{rel_path}' is forbidden.")

        ws = self.workspace.resolve()
        target = (self.workspace / p).resolve()

        try:
            target.relative_to(ws)
        except ValueError:
            raise PermissionError(f"Path '{rel_path}' resolves outside the workspace.")

        if must_exist and not target.exists():
            raise FileNotFoundError(f"Path '{rel_path}' does not exist.")

        return target

    def _bounded_result(
        self, ok: bool, output: Any = None, error: str | None = None, truncated: bool = False
    ) -> ToolResult:
        if isinstance(output, str):
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS]
                truncated = True
        elif output is not None:
            serialized = json.dumps(output, ensure_ascii=False)
            if len(serialized) > MAX_OUTPUT_CHARS:
                truncated = True
        return ToolResult(ok=ok, output=output, error=error, truncated=truncated)

    # ------------------------------------------------------------------- tools

    def _list_files(self, path: str = ".", pattern: str | None = None) -> ToolResult:
        try:
            target_dir = self._safe_path(path, must_exist=True)
            if not target_dir.is_dir():
                return ToolResult(ok=False, error=f"Path '{path}' is not a directory.")

            ws = self.workspace.resolve()
            files: list[str] = []
            for p in target_dir.rglob("*"):
                parts = p.parts
                if any(part in (".git", "__pycache__", ".pytest_cache", ".venv") for part in parts):
                    continue
                if p.is_file():
                    rel = p.relative_to(ws).as_posix()
                    if pattern:
                        if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(rel, pattern):
                            files.append(rel)
                    else:
                        files.append(rel)

            files.sort()
            truncated = False
            if len(files) > MAX_LIST_ENTRIES:
                files = files[:MAX_LIST_ENTRIES]
                truncated = True

            return self._bounded_result(ok=True, output="\n".join(files), truncated=truncated)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

    def _read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ToolResult:
        try:
            target = self._safe_path(path, must_exist=True)
            if not target.is_file():
                return ToolResult(ok=False, error=f"Path '{path}' is not a regular file.")

            content = target.read_text(encoding="utf-8", errors="replace")
            all_lines = content.splitlines()
            total_lines = len(all_lines)

            s_line = 1 if start_line is None else start_line
            if s_line < 1:
                return ToolResult(ok=False, error="start_line must be >= 1.")

            e_line = total_lines if end_line is None else end_line
            if e_line < s_line:
                return ToolResult(ok=False, error=f"end_line ({e_line}) cannot be less than start_line ({s_line}).")

            selected = all_lines[s_line - 1 : e_line]
            truncated = False
            if len(selected) > MAX_READ_LINES:
                selected = selected[:MAX_READ_LINES]
                truncated = True

            formatted = [f"{idx:4d} | {line}" for idx, line in enumerate(selected, start=s_line)]
            output = "\n".join(formatted)
            return self._bounded_result(ok=True, output=output, truncated=truncated)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

    def _search_code(
        self, pattern: str, glob: str | None = None, max_results: int | None = None
    ) -> ToolResult:
        try:
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return ToolResult(ok=False, error=f"Invalid regular expression pattern '{pattern}': {e}")

            limit = MAX_SEARCH_RESULTS
            if max_results is not None:
                if max_results < 1:
                    return ToolResult(ok=False, error="max_results must be >= 1.")
                limit = min(max_results, MAX_SEARCH_RESULTS)

            ws = self.workspace.resolve()
            search_glob = glob if glob else "**/*"
            results: list[str] = []
            truncated = False

            for p in ws.glob(search_glob):
                parts = p.parts
                if any(part in (".git", "__pycache__", ".pytest_cache", ".venv") for part in parts):
                    continue
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to(ws).as_posix()
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    for line_no, line in enumerate(lines, start=1):
                        if regex.search(line):
                            results.append(f"{rel}:{line_no}: {line}")
                            if len(results) >= limit:
                                truncated = True
                                break
                except Exception:
                    continue
                if len(results) >= limit:
                    break

            return self._bounded_result(ok=True, output="\n".join(results), truncated=truncated)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

    def _run_tests(self, target: str | None = None) -> ToolResult:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            xml_path = tmp.name

        try:
            cmd = [sys.executable, "-m", "pytest", f"--junitxml={xml_path}", "-q"]
            if target:
                file_part = target.split("::")[0]
                self._safe_path(file_part, must_exist=True)
                cmd.append(target)

            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str((self.workspace / "src").resolve()),
                "HOME": "/tmp",
            }

            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )

            passed = 0
            failed = 0
            failures = []

            if os.path.exists(xml_path) and os.path.getsize(xml_path) > 0:
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    for tc in root.iter("testcase"):
                        name = tc.get("name", "")
                        classname = tc.get("classname", "")
                        failure = tc.find("failure")
                        error = tc.find("error")
                        if failure is not None or error is not None:
                            failed += 1
                            elem = failure if failure is not None else error
                            msg = elem.get("message", "")
                            text = elem.text or ""
                            short_tb = "\n".join(text.strip().splitlines()[-15:])
                            failures.append(
                                {
                                    "test": name,
                                    "classname": classname,
                                    "message": msg,
                                    "traceback": short_tb,
                                }
                            )
                        else:
                            passed += 1
                except Exception:
                    pass

            summary = {
                "passed": passed,
                "failed": failed,
                "failures": failures,
                "stdout": proc.stdout[:2000] if proc.stdout else "",
            }
            return self._bounded_result(ok=True, output=summary)
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"Test run timed out after {self.timeout_s}s.")
        except Exception as e:
            return ToolResult(ok=False, error=f"Failed to run tests: {e}")
        finally:
            if os.path.exists(xml_path):
                try:
                    os.remove(xml_path)
                except OSError:
                    pass

    def _run_cli(self, args: list[str]) -> ToolResult:
        try:
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                return ToolResult(ok=False, error="args must be a list of strings.")

            for arg in args:
                if "canary" in arg:
                    return ToolResult(ok=False, error="Security violation: access to secrets forbidden.")

            cmd = [sys.executable, "-m", "tinyledger"] + args
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str((self.workspace / "src").resolve()),
                "HOME": "/tmp",
            }

            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )

            output = proc.stdout
            if proc.stderr:
                output = (output + "\n" + proc.stderr).strip()

            ok = proc.returncode == 0
            err = None if ok else f"CLI process exited with code {proc.returncode}"
            return self._bounded_result(ok=ok, output=output, error=err)
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"CLI command timed out after {self.timeout_s}s.")
        except Exception as e:
            return ToolResult(ok=False, error=f"Failed to execute CLI: {e}")

    def _edit_file(self, path: str, old_str: str, new_str: str) -> ToolResult:
        try:
            target = self._safe_path(path, must_exist=True)
            if not target.is_file():
                return ToolResult(ok=False, error=f"'{path}' is not a regular file.")

            src_dir = (self.workspace / "src").resolve()
            try:
                target.relative_to(src_dir)
            except ValueError:
                return ToolResult(
                    ok=False, error=f"Permission denied: edits are strictly prohibited outside src/ ('{path}')."
                )

            content = target.read_text(encoding="utf-8")
            count = content.count(old_str)
            if count == 0:
                return ToolResult(ok=False, error=f"'{old_str}' was not found in '{path}'.")
            if count > 1:
                return ToolResult(
                    ok=False,
                    error=f"'{old_str}' appears {count} times in '{path}'. Edit requires a unique match.",
                )

            new_content = content.replace(old_str, new_str, 1)
            target.write_text(new_content, encoding="utf-8")

            rel_posix = Path(path).as_posix()
            diff = list(
                difflib.unified_diff(
                    content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{rel_posix}",
                    tofile=f"b/{rel_posix}",
                )
            )
            return ToolResult(ok=True, output="".join(diff))
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

