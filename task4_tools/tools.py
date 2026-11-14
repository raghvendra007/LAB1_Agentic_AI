"""Tool layer for Task 4.

Read / execute / write are separate tools so we can apply different permission
rules. There is no generic shell tool. Schema checks only cover argument shape;
we still check paths and other semantics ourselves because the model can send
bad or malicious calls (and the mock client does).

edit_file is not idempotent: running the same edit twice fails because old_str
is already gone.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import jsonschema

from tool_contract import (
    DEFAULT_TIMEOUT_S,
    MAX_LIST_ENTRIES,
    MAX_OUTPUT_CHARS,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    OPTIONAL_TOOLS,
    REQUIRED_TOOLS,
    ToolRegistryBase,
    ToolResult,
)

SKIP_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
BINARY_SNIFF = 8192
MAX_LINE_CHARS = 500
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TEST_TARGET_RE = re.compile(
    r"^tests/[A-Za-z0-9_][A-Za-z0-9_./\-]*\.py(?:::[A-Za-z_][A-Za-z0-9_]*)?$"
)
CLI_COMMANDS = {"report", "balances", "settle"}
SENSITIVE_ENV_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|APIKEY)[A-Z0-9_]*)\s*=\s*([^\s\"']+)"
)
ABS_TMP_RE = re.compile(r"(?:/var)?/folders/[^\s\"']+|/tmp/[^\s\"']+")
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?s\b")

ToolKind = Literal["read", "execute", "write"]
TOOL_POLICY: dict[str, ToolKind] = {
    "list_files": "read",
    "read_file": "read",
    "search_code": "read",
    "run_tests": "execute",
    "run_cli": "execute",
    "edit_file": "write",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_forbidden_path_string(path: str) -> str | None:
    if not isinstance(path, str) or path == "":
        return "path must be a non-empty string"
    if "\x00" in path:
        return "path contains a NUL byte"
    if path == "~" or path[:2] == "~/" or path[0:1] == "~":
        return "home-directory paths (~) are not allowed"
    if path[0:1] == "/":
        return "absolute paths are not allowed"
    if re.match(r"^[A-Za-z]:[\\/]", path):
        return "drive-letter / absolute Windows paths are not allowed"
    if path[:2] in ("\\\\", "//"):
        return "UNC / absolute paths are not allowed"
    return None


class ToolRegistry(ToolRegistryBase):
    def __init__(
        self,
        workspace: Path,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        trace_path: Path | None = None,
    ) -> None:
        super().__init__(workspace=workspace, timeout_s=timeout_s)
        self.workspace = Path(workspace)
        self._root = self.workspace.resolve()
        self._src_root = (self._root / "src").resolve()
        self._tests_root = (self._root / "tests").resolve()
        if trace_path is None:
            trace_path = Path(__file__).resolve().parent / "logs" / "trace.jsonl"
        self.trace_path = Path(trace_path)
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._validators = {
            name: jsonschema.Draft202012Validator(schema["function"]["parameters"])
            for name, schema in ((s["function"]["name"], s) for s in self.schemas())
        }
        self._sensitive_values = self._collect_sensitive_values()
        self._root_str = str(self._root)

    # ------------------------------------------------------------------ schemas

    def schemas(self) -> list[dict]:
        return [
            self._schema(
                "list_files",
                "List files under a workspace-relative directory. Optional glob filter.",
                {
                    "path": {"type": "string", "description": "Directory relative to workspace.", "default": ".", "maxLength": 512},
                    "pattern": {"type": "string", "description": "Optional glob, e.g. **/*.py", "maxLength": 256},
                },
                required=[],
            ),
            self._schema(
                "read_file",
                "Read a text file from the workspace. Returns numbered lines (at most 200).",
                {
                    "path": {"type": "string", "maxLength": 512},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                required=["path"],
            ),
            self._schema(
                "search_code",
                "Search workspace files with a regular expression. Returns path:line: text matches.",
                {
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 500},
                    "glob": {"type": "string", "maxLength": 256},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                },
                required=["pattern"],
            ),
            self._schema(
                "run_tests",
                "Run pytest in the workspace. Returns a structured pass/fail summary.",
                {
                    "target": {
                        "type": "string",
                        "description": "tests/<file>.py or tests/<file>.py::test_name",
                        "maxLength": 256,
                    }
                },
                required=[],
            ),
            self._schema(
                "edit_file",
                "Replace exactly one occurrence of old_str in an existing file under src/.",
                {
                    "path": {"type": "string", "maxLength": 512},
                    "old_str": {"type": "string", "minLength": 1, "maxLength": 50_000},
                    "new_str": {"type": "string", "maxLength": 50_000},
                },
                required=["path", "old_str", "new_str"],
            ),
            self._schema(
                "run_cli",
                "Run python -m tinyledger with report, balances, or settle.",
                {
                    "args": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 512},
                    }
                },
                required=["args"],
            ),
        ]

    @staticmethod
    def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    # parse -> authorise -> dispatch -> finalize -> trace

    def execute(self, name: str, arguments: str) -> ToolResult:
        started = time.monotonic()
        result: ToolResult
        try:
            parsed = self._parse(name, arguments)
            if isinstance(parsed, ToolResult):
                result = parsed
            else:
                auth = self._authorise(name, parsed)
                if auth is not None:
                    result = auth
                else:
                    result = self._dispatch(name, parsed)
        except Exception as exc:  # never raise
            result = ToolResult(ok=False, error=f"internal error: {type(exc).__name__}: {exc}")
        result = self._finalize(result)
        self._trace(name, arguments, result, time.monotonic() - started)
        return result

    def _parse(self, name: str, arguments: str) -> dict[str, Any] | ToolResult:
        if name not in self._validators and name not in TOOL_POLICY:
            known = ", ".join(REQUIRED_TOOLS + OPTIONAL_TOOLS)
            return ToolResult(ok=False, error=f"unknown tool {name!r}. Known tools: {known}")

        if not isinstance(arguments, str):
            return ToolResult(ok=False, error="arguments must be a JSON string")

        try:
            raw = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, error=f"invalid JSON arguments: {exc.msg}")

        if not isinstance(raw, dict):
            return ToolResult(ok=False, error="arguments JSON must be an object, not a list/string/number")

        try:
            json.dumps(raw)
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"arguments not JSON-serializable: {exc}")

        if name not in self._validators:
            known = ", ".join(REQUIRED_TOOLS + OPTIONAL_TOOLS)
            return ToolResult(ok=False, error=f"unknown tool {name!r}. Known tools: {known}")

        validator = self._validators[name]
        errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            err = errors[0]
            where = ".".join(str(p) for p in err.path) or "(root)"
            return ToolResult(ok=False, error=f"schema violation at {where}: {err.message}")
        return raw

    def _authorise(self, name: str, args: dict[str, Any]) -> ToolResult | None:
        """Check tool permissions. Returns an error result, or None to continue."""
        kind = TOOL_POLICY.get(name)
        if kind is None:
            return ToolResult(ok=False, error=f"authorisation denied: unknown tool {name!r}")

        if kind == "read":
            path = args.get("path")
            if isinstance(path, str) and path:
                try:
                    self._resolve_in_workspace(path, must_exist=False)
                except ValueError as exc:
                    return ToolResult(ok=False, error=f"authorisation denied (read): {exc}")
            return None

        if kind == "execute":
            if name == "run_tests":
                target = args.get("target")
                if target:
                    if not isinstance(target, str) or not TEST_TARGET_RE.match(target):
                        return ToolResult(
                            ok=False,
                            error="authorisation denied (execute): target must look like tests/<file>.py[::name]",
                        )
                    file_part = target.split("::", 1)[0]
                    try:
                        resolved = self._resolve_in_workspace(file_part, must_exist=True)
                        resolved.relative_to(self._tests_root)
                    except ValueError as exc:
                        return ToolResult(ok=False, error=f"authorisation denied (execute): {exc}")
            if name == "run_cli":
                cli_args = args.get("args") or []
                if not cli_args or cli_args[0] not in CLI_COMMANDS:
                    return ToolResult(
                        ok=False,
                        error=f"authorisation denied (execute): first arg must be one of {sorted(CLI_COMMANDS)}",
                    )
            return None

        if kind == "write":
            path = args.get("path")
            if not isinstance(path, str):
                return ToolResult(ok=False, error="edit_file may only modify existing files under src/")
            try:
                resolved = self._resolve_in_workspace(path, must_exist=True)
                resolved.relative_to(self._src_root)
            except ValueError:
                return ToolResult(ok=False, error="edit_file may only modify existing files under src/")
            return None

        return ToolResult(ok=False, error=f"authorisation denied: unsupported policy {kind!r}")

    def _dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "run_tests": self._run_tests,
            "edit_file": self._edit_file,
            "run_cli": self._run_cli,
        }
        return handlers[name](args)

    # ------------------------------------------------------------------ path helpers

    def _resolve_in_workspace(self, path: str, *, must_exist: bool = False) -> Path:
        err = _is_forbidden_path_string(path)
        if err:
            raise ValueError(err)
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("path resolves outside the workspace") from exc
        if must_exist and not candidate.exists():
            raise ValueError(f"path does not exist: {path}")
        return candidate

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self._root).as_posix()

    def _recheck_inside(self, path: Path, *, under: Path | None = None) -> Path:
        """Resolve again right before open/write."""
        resolved = path.resolve()
        root = under or self._root
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("path resolves outside the allowed root (re-check)") from exc
        return resolved

    def _safe_walk(self, start: Path):
        """Yield files under start, skipping out-of-workspace symlinks and junk dirs."""
        if start.is_symlink():
            resolved = start.resolve()
            try:
                resolved.relative_to(self._root)
            except ValueError:
                return
            if resolved.is_file():
                yield resolved
            elif resolved.is_dir():
                yield from self._safe_walk(resolved)
            return

        if start.is_file():
            yield start
            return

        if not start.is_dir():
            return

        for root, dirs, files in os.walk(start, followlinks=False):
            root_path = Path(root)
            keep_dirs = []
            for d in dirs:
                if d in SKIP_DIR_NAMES:
                    continue
                child = root_path / d
                if child.is_symlink():
                    try:
                        child.resolve().relative_to(self._root)
                    except ValueError:
                        continue
                keep_dirs.append(d)
            dirs[:] = keep_dirs
            for name in files:
                child = root_path / name
                if child.is_symlink():
                    try:
                        child.resolve().relative_to(self._root)
                    except ValueError:
                        continue
                yield child

    # ------------------------------------------------------------------ tools

    def _list_files(self, args: dict[str, Any]) -> ToolResult:
        rel = args.get("path", ".")
        pattern = args.get("pattern")
        try:
            base = self._resolve_in_workspace(rel, must_exist=True)
            base = self._recheck_inside(base)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not base.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {rel}")

        matches: list[str] = []
        for path in self._safe_walk(base):
            rel_path = self._rel(path)
            if pattern:
                if (
                    Path(rel_path).match(pattern)
                    or path.name == pattern
                    or Path(path.name).match(pattern)
                ):
                    matches.append(rel_path)
            else:
                matches.append(rel_path)

        matches = sorted(set(matches))
        truncated = False
        omitted = 0
        if len(matches) > MAX_LIST_ENTRIES:
            omitted = len(matches) - MAX_LIST_ENTRIES
            matches = matches[:MAX_LIST_ENTRIES]
            truncated = True
        result = ToolResult(ok=True, output=matches, truncated=truncated)
        if omitted:
            result._omitted = omitted  # type: ignore[attr-defined]
        return result

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        try:
            path = self._resolve_in_workspace(args["path"], must_exist=True)
            path = self._recheck_inside(path)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        if path.is_symlink():
            try:
                path = self._recheck_inside(path.resolve())
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {args['path']}")

        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW") and not path.is_symlink():
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError:
                fd = os.open(path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as f:
                sample = f.read(BINARY_SNIFF)
                rest = f.read()
            data = sample + rest
        except OSError as exc:
            return ToolResult(ok=False, error=f"cannot read file: {exc}")
        if b"\x00" in sample:
            return ToolResult(ok=False, error="refusing to read binary file")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, error="file is not valid UTF-8 text")

        lines = text.splitlines()
        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", len(lines)))
        if start < 1:
            return ToolResult(ok=False, error="start_line must be >= 1")
        if end < start:
            return ToolResult(ok=False, error="end_line must be >= start_line")

        sliced = lines[start - 1 : end]
        truncated = False
        notes: list[str] = []
        if len(sliced) > MAX_READ_LINES:
            omitted = len(sliced) - MAX_READ_LINES
            next_line = start + MAX_READ_LINES
            sliced = sliced[:MAX_READ_LINES]
            truncated = True
            notes.append(
                f"[{omitted} more lines not shown; continue with start_line={next_line}]"
            )

        out_lines = []
        for i, line in enumerate(sliced, start=start):
            if len(line) > MAX_LINE_CHARS:
                cut = len(line) - MAX_LINE_CHARS
                line = line[:MAX_LINE_CHARS] + f"…[{cut} more chars not shown]"
                truncated = True
            out_lines.append(f"{i:>6}|{line}")
        out_lines.extend(notes)
        return ToolResult(ok=True, output="\n".join(out_lines), truncated=truncated)

    def _search_code(self, args: dict[str, Any]) -> ToolResult:
        pattern = args["pattern"]
        try:
            re.compile(pattern)
        except re.error as exc:
            return ToolResult(ok=False, error=f"invalid regex: {exc}")

        max_results = int(args.get("max_results", MAX_SEARCH_RESULTS))
        max_results = min(max_results, MAX_SEARCH_RESULTS)
        glob_pat = args.get("glob")

        # Run regex in a child process so a bad pattern cannot hang us.
        script = """
import json, os, re, sys
from pathlib import Path
payload = json.loads(sys.stdin.read())
root = Path(payload["root"])
pattern = re.compile(payload["pattern"])
glob_pat = payload.get("glob")
max_results = payload["max_results"]
max_line = payload["max_line"]
skip = set(payload["skip"])
matches = []
omitted = 0

def ok_symlink(p: Path) -> bool:
    try:
        p.resolve().relative_to(root)
        return True
    except Exception:
        return False

for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirnames[:] = [d for d in dirnames if d not in skip and ok_symlink(Path(dirpath)/d)]
    for name in filenames:
        path = Path(dirpath) / name
        if path.is_symlink() and not ok_symlink(path):
            continue
        rel = path.resolve().relative_to(root).as_posix()
        if glob_pat and not (Path(rel).match(glob_pat) or Path(name).match(glob_pat)):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\\x00" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                if len(matches) >= max_results:
                    omitted += 1
                    continue
                if len(line) > max_line:
                    cut = len(line) - max_line
                    line = line[:max_line] + f"…[{cut} more chars not shown]"
                matches.append(f"{rel}:{i}: {line}")
print(json.dumps({"matches": matches, "omitted": omitted, "truncated": omitted > 0}))
"""
        try:
            completed = self._run_subprocess(
                [sys.executable, "-c", script],
                cwd=self._root,
                stdin_data=json.dumps(
                    {
                        "root": str(self._root),
                        "pattern": pattern,
                        "glob": glob_pat,
                        "max_results": max_results,
                        "max_line": MAX_LINE_CHARS,
                        "skip": sorted(SKIP_DIR_NAMES),
                    }
                ),
                timeout_s=min(self.timeout_s, 30),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error="search_code timed out (regex too expensive?)")
        except Exception as exc:
            return ToolResult(ok=False, error=f"search_code failed: {exc}")

        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()[:500]
            return ToolResult(ok=False, error=f"search_code child failed: {err or completed.returncode}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ToolResult(ok=False, error="search_code returned unreadable output")
        matches = payload.get("matches") or []
        omitted = int(payload.get("omitted") or 0)
        truncated = bool(payload.get("truncated"))
        if not matches:
            return ToolResult(ok=True, output="(no matches)", truncated=False)
        text = "\n".join(matches)
        if omitted:
            text += f"\n[{omitted} more matches not shown]"
            truncated = True
        return ToolResult(ok=True, output=text, truncated=truncated)

    def _edit_file(self, args: dict[str, Any]) -> ToolResult:
        """Replace one occurrence of old_str. Same edit twice fails (old_str gone)."""
        rel = args["path"]
        old_str = args["old_str"]
        new_str = args["new_str"]
        try:
            path = self._resolve_in_workspace(rel, must_exist=True)
            path = self._recheck_inside(path, under=self._src_root)
        except ValueError:
            return ToolResult(ok=False, error="edit_file may only modify existing files under src/")

        if path.is_symlink():
            return ToolResult(ok=False, error="edit_file may only modify existing files under src/")

        if not path.is_file():
            return ToolResult(ok=False, error="edit_file target is not a file")

        try:
            path = self._recheck_inside(path, under=self._src_root)
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"cannot read file: {exc}")

        count = original.count(old_str)
        if count == 0:
            return ToolResult(ok=False, error="old_str not found in file")
        if count > 1:
            return ToolResult(ok=False, error=f"old_str must occur exactly once (found {count} times)")

        updated = original.replace(old_str, new_str, 1)
        try:
            path = self._recheck_inside(path, under=self._src_root)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".edit_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(updated)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=f"write failed: {exc}")

        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{self._rel(path)}",
                tofile=f"b/{self._rel(path)}",
            )
        )
        return ToolResult(ok=True, output=diff or "(no textual diff)")

    def _run_tests(self, args: dict[str, Any]) -> ToolResult:
        target = args.get("target")
        pytest_args = [sys.executable, "-m", "pytest", "-q"]
        if target:
            if not isinstance(target, str) or not TEST_TARGET_RE.match(target):
                return ToolResult(
                    ok=False,
                    error="target must look like tests/<file>.py or tests/<file>.py::test_name",
                )
            file_part = target.split("::", 1)[0]
            try:
                resolved = self._resolve_in_workspace(file_part, must_exist=True)
                resolved.relative_to(self._tests_root)
            except ValueError as exc:
                return ToolResult(ok=False, error=f"invalid test target: {exc}")
            pytest_args.append(target)

        with tempfile.TemporaryDirectory(prefix="tinyledger_pytest_") as tmp:
            junit = Path(tmp) / "junit.xml"
            pytest_args.extend(["--junitxml", str(junit)])
            env_extra = {"PYTEST_ADDOPTS": ""}
            try:
                completed = self._run_subprocess(
                    pytest_args,
                    cwd=self._root,
                    timeout_s=self.timeout_s,
                    env_extra=env_extra,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(ok=False, error=f"run_tests timed out after {self.timeout_s}s")

            summary = self._parse_junit(junit)
            if summary["passed"] == 0 and summary["failed"] == 0 and summary["errors"] == 0:
                if completed.returncode not in (0, 1):
                    tail = self._sanitize_text((completed.stderr or completed.stdout or "")[-800:])
                    return ToolResult(
                        ok=False,
                        error=f"pytest exited with code {completed.returncode}: {tail}",
                    )
            return ToolResult(ok=True, output=summary)

    def _parse_junit(self, junit: Path) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "failures": [],
            "failures_omitted": 0,
        }
        if not junit.is_file():
            return summary
        try:
            root = ET.parse(junit).getroot()
        except ET.ParseError:
            return summary

        suites = root.findall("testsuite")
        if root.tag == "testsuite":
            suites = [root]

        all_failures: list[dict[str, Any]] = []
        for suite in suites:
            for case in suite.findall("testcase"):
                fail = case.find("failure")
                err = case.find("error")
                skip = case.find("skipped")
                classname = case.attrib.get("classname", "")
                name = case.attrib.get("name", "")
                nodeid = f"{classname}::{name}" if classname else name
                if skip is not None:
                    summary["skipped"] += 1
                elif fail is not None or err is not None:
                    if fail is not None:
                        summary["failed"] += 1
                        node = fail
                    else:
                        summary["errors"] += 1
                        node = err
                    message = self._sanitize_text((node.attrib.get("message") or "")[:300])
                    raw_tb = self._sanitize_text(node.text or "")
                    tb_lines = raw_tb.strip().splitlines()
                    trace_tail = tb_lines[-12:] if tb_lines else []
                    at_loc = self._extract_at(trace_tail) or ""
                    all_failures.append(
                        {
                            "test": nodeid,
                            "assertion": message,
                            "at": at_loc,
                            "trace_tail": trace_tail,
                        }
                    )
                else:
                    summary["passed"] += 1

        kept: list[dict[str, Any]] = []
        for item in all_failures:
            candidate = {**summary, "failures": kept + [item], "failures_omitted": 0}
            if len(json.dumps(candidate, ensure_ascii=False)) > MAX_OUTPUT_CHARS - 120:
                item = dict(item)
                item["trace_tail"] = item["trace_tail"][-4:]
                candidate = {**summary, "failures": kept + [item], "failures_omitted": 0}
                if len(json.dumps(candidate, ensure_ascii=False)) > MAX_OUTPUT_CHARS - 120:
                    break
            kept.append(item)
        summary["failures"] = kept
        summary["failures_omitted"] = max(0, len(all_failures) - len(kept))
        return summary

    @staticmethod
    def _extract_at(trace_tail: list[str]) -> str:
        for line in reversed(trace_tail):
            m = re.search(r"(src/[^\s:\"]+\.py):(\d+)", line)
            if m:
                return f"{m.group(1)}:{m.group(2)}"
            m = re.search(r"([^/\s\"]+\.py):(\d+)", line)
            if m:
                return f"{m.group(1)}:{m.group(2)}"
        return ""

    def _run_cli(self, args: dict[str, Any]) -> ToolResult:
        cli_args = args["args"]
        if not cli_args:
            return ToolResult(ok=False, error="args must be a non-empty list")
        cmd = cli_args[0]
        if cmd not in CLI_COMMANDS:
            return ToolResult(
                ok=False,
                error=f"first arg must be one of {sorted(CLI_COMMANDS)}, got {cmd!r}",
            )

        rest = cli_args[1:]
        csv_path = None
        month = None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "--month":
                if i + 1 >= len(rest):
                    return ToolResult(ok=False, error="--month requires a YYYY-MM value")
                month = rest[i + 1]
                if not MONTH_RE.match(month):
                    return ToolResult(ok=False, error="--month must be YYYY-MM")
                i += 2
                continue
            if tok[:1] == "-":
                return ToolResult(ok=False, error=f"unsupported option: {tok}")
            if csv_path is not None:
                return ToolResult(ok=False, error="only one positional CSV path is allowed")
            try:
                self._resolve_in_workspace(tok, must_exist=True)
            except ValueError as exc:
                return ToolResult(ok=False, error=f"csv path rejected: {exc}")
            csv_path = tok
            i += 1

        if csv_path is None:
            return ToolResult(ok=False, error="missing CSV file argument")

        argv = [sys.executable, "-m", "tinyledger", cmd, csv_path]
        if month:
            argv.extend(["--month", month])

        try:
            completed = self._run_subprocess(argv, cwd=self._root, timeout_s=self.timeout_s)
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"run_cli timed out after {self.timeout_s}s")

        output = {
            "exit_code": completed.returncode,
            "stdout": self._sanitize_text(completed.stdout or ""),
            "stderr": self._sanitize_text(completed.stderr or ""),
        }
        return ToolResult(ok=True, output=output)

    # ------------------------------------------------------------------ subprocess / env

    def _minimal_env(self, env_extra: dict[str, str] | None = None) -> dict[str, str]:
        path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
        home = tempfile.mkdtemp(prefix="tinyledger_home_")
        env = {
            "PATH": path,
            "HOME": home,
            "PYTHONPATH": str(self._root / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "TERM": "dumb",
            "TMPDIR": tempfile.gettempdir(),
        }
        if env_extra:
            env.update(env_extra)
        return env

    def _run_subprocess(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        stdin_data: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        env = self._minimal_env(env_extra)
        with tempfile.TemporaryDirectory(prefix="tool_subproc_") as tmp:
            stdout_path = Path(tmp) / "stdout.txt"
            stderr_path = Path(tmp) / "stderr.txt"
            stdin_path = Path(tmp) / "stdin.txt"
            if stdin_data is not None:
                stdin_path.write_text(stdin_data, encoding="utf-8")
                stdin_f = open(stdin_path, "rb")
            else:
                stdin_f = subprocess.DEVNULL

            stdout_f = open(stdout_path, "wb")
            stderr_f = open(stderr_path, "wb")
            try:
                def _preexec():
                    try:
                        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                    except (ValueError, OSError):
                        pass
                    try:
                        resource.setrlimit(
                            resource.RLIMIT_CPU,
                            (max(1, int(timeout_s)), max(1, int(timeout_s)) + 5),
                        )
                    except (ValueError, OSError, AttributeError):
                        pass
                    try:
                        as_bytes = 512 * 1024 * 1024
                        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
                    except (ValueError, OSError, AttributeError):
                        pass

                popen_kwargs: dict[str, Any] = {
                    "cwd": str(cwd),
                    "env": env,
                    "stdin": stdin_f,
                    "stdout": stdout_f,
                    "stderr": stderr_f,
                    "shell": False,
                    "start_new_session": True,
                }
                if sys.platform != "win32":
                    popen_kwargs["preexec_fn"] = _preexec

                proc = subprocess.Popen(argv, **popen_kwargs)
                try:
                    proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self._kill_process_group(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise
            finally:
                if stdin_f is not subprocess.DEVNULL:
                    stdin_f.close()
                stdout_f.close()
                stderr_f.close()

            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            if len(stdout) > MAX_OUTPUT_CHARS:
                omitted = len(stdout) - (MAX_OUTPUT_CHARS // 2)
                stdout = stdout[: MAX_OUTPUT_CHARS // 2] + f"\n…[{omitted} more stdout chars not shown]\n"
            if len(stderr) > MAX_OUTPUT_CHARS:
                omitted = len(stderr) - (MAX_OUTPUT_CHARS // 2)
                stderr = stderr[: MAX_OUTPUT_CHARS // 2] + f"\n…[{omitted} more stderr chars not shown]\n"
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        try:
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    # ------------------------------------------------------------------ finalize / redact / trace

    def _collect_sensitive_values(self) -> list[str]:
        values = []
        for key, value in os.environ.items():
            upper = key.upper()
            if any(tok in upper for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
                if value and len(value) >= 4:
                    values.append(value)
        return values

    def _sanitize_text(self, text: str) -> str:
        redacted = text.replace(self._root_str, ".")
        redacted = ABS_TMP_RE.sub("<tmpdir>", redacted)
        redacted = DURATION_RE.sub("<duration>", redacted)
        return self._redact(redacted)

    def _redact(self, text: str) -> str:
        redacted = text
        for value in self._sensitive_values:
            if value in redacted:
                redacted = redacted.replace(value, "[REDACTED]")
        redacted = SENSITIVE_ENV_RE.sub(r"\1=[REDACTED]", redacted)
        return redacted

    def _finalize(self, result: ToolResult) -> ToolResult:
        omitted_note = getattr(result, "_omitted", None)

        if isinstance(result.output, str):
            result.output = self._sanitize_text(result.output)
        elif isinstance(result.output, list):
            result.output = [
                self._sanitize_text(x) if isinstance(x, str) else x for x in result.output
            ]
            if omitted_note:
                # Keep list type; public tests look for paths inside the serialized list.
                # Explicit truncation is signaled via truncated=True; note appended as last entry.
                result.truncated = True
                note = f"[{omitted_note} more entries not shown]"
                # Only append note if it still fits later; finalize size clamp may drop it.
                result.output = list(result.output) + [note]
        elif isinstance(result.output, dict):
            try:
                blob = self._sanitize_text(json.dumps(result.output, ensure_ascii=False))
                result.output = json.loads(blob)
            except (TypeError, ValueError):
                result.output = self._sanitize_text(str(result.output))
        elif result.output is not None:
            result.output = self._sanitize_text(str(result.output))

        if result.error:
            result.error = self._sanitize_text(result.error)

        content = result.to_content()
        if len(content) <= MAX_OUTPUT_CHARS:
            return result

        result.truncated = True
        overhead = len(json.dumps({"ok": result.ok, "truncated": True}, ensure_ascii=False)) + 40
        budget = max(0, MAX_OUTPUT_CHARS - overhead)

        if result.ok:
            if isinstance(result.output, list):
                original_len = len(result.output)
                while result.output and len(result.to_content()) > MAX_OUTPUT_CHARS:
                    drop = max(1, len(result.output) // 10)
                    result.output = result.output[: max(0, len(result.output) - drop)]
                dropped = original_len - len(result.output)
                if dropped > 0:
                    result.output = list(result.output) + [f"[{dropped} more entries not shown]"]
                    while result.output and len(result.to_content()) > MAX_OUTPUT_CHARS:
                        # drop note or last path until it fits
                        result.output = result.output[:-1]
            elif isinstance(result.output, str):
                omitted = max(0, len(result.output) - budget + 40)
                result.output = result.output[: max(0, budget - 40)] + f"…[{omitted} more chars not shown]"
            elif isinstance(result.output, dict):
                if isinstance(result.output.get("failures"), list):
                    while len(result.to_content()) > MAX_OUTPUT_CHARS and result.output["failures"]:
                        result.output["failures"].pop()
                        result.output["failures_omitted"] = int(result.output.get("failures_omitted") or 0) + 1
                if len(result.to_content()) > MAX_OUTPUT_CHARS:
                    text = json.dumps(result.output, ensure_ascii=False)
                    omitted = max(0, len(text) - budget + 40)
                    result.output = text[: max(0, budget - 40)] + f"…[{omitted} more chars not shown]"
            else:
                text = json.dumps(result.output, ensure_ascii=False)
                omitted = max(0, len(text) - budget + 40)
                result.output = text[: max(0, budget - 40)] + f"…[{omitted} more chars not shown]"
        else:
            omitted = max(0, len(result.error or "") - budget + 40)
            result.error = (result.error or "")[: max(0, budget - 40)] + (
                f"…[{omitted} more chars not shown]" if omitted else ""
            )

        while len(result.to_content()) > MAX_OUTPUT_CHARS:
            result.truncated = True
            if result.ok and isinstance(result.output, list) and result.output:
                result.output = result.output[:-1]
            elif result.ok and isinstance(result.output, str) and result.output:
                result.output = result.output[: max(0, len(result.output) - 200)]
            elif result.error:
                result.error = result.error[: max(0, len(result.error) - 200)]
            else:
                result.output = None
                result.error = "output truncated"
                break
        return result

    def _trace(self, name: str, arguments: str, result: ToolResult, duration: float) -> None:
        entry = {
            "timestamp": _now_iso(),
            "tool": name,
            "arguments": arguments[:2000] if isinstance(arguments, str) else str(arguments)[:2000],
            "ok": result.ok,
            "error": result.error,
            "truncated": result.truncated,
            "duration_s": round(duration, 4),
        }
        try:
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
