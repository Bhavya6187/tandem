# Native Tool-Call Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the summarize-to-prose policy for tool activity with native tool-call translation in both sync directions, with a semantic mapping layer (`toolmap.py`) that re-expresses common tools in the shadow harness's own vocabulary.

**Architecture:** The converter keeps its stash-on-call flow but, on each ToolResult, emits the mapped **call + result as an adjacent native pair** instead of a prose summary. A new pure module `toolmap.py` decides per call between Tier 1 (semantic re-expression: Bash↔exec_command, Edit/Write↔apply_patch, Read→cat -n, Grep/Glob→rg, TodoWrite↔update_plan) and Tier 2 (verbatim pass-through — spike-proven safe). The harness adapters gain dumb tool_call/tool_result rendering; a dangle-flush closes unpaired calls with `(tool result not recorded)` whenever a source is drained on role flip.

**Tech Stack:** Python 3.11+, pydantic v2, pytest. No new dependencies.

**Spec:** `docs/specs/2026-07-29-native-tool-call-translation-design.md` — read it before starting.

**Baseline:** Revalidated against main after the tandem-shell merge (`9f21464`). Every file Tasks 1–9 modify is unchanged by that merge; the shell (`shell.py`) and CLI both dispatch role flips through `ops.switch_session` and one-offs through `ops.run_oneoff`, so the Task-8 flush wiring points are still the single choke points. The `tandem sync` command's manual `drain_source` call (`cli.py:339`) intentionally does **not** flush — the active harness keeps running, so its in-flight calls are not dangles. CLI vocabulary for Task 10: `tandem start` no longer exists — bare `tandem [--active claude|codex]` pairs and enters a session, and `switch` is available both as a CLI verb and at the tandem shell prompt (which re-enters the new active harness immediately).

## Global Constraints

- Attribution: tool calls/results carry **no** `[via claude-code]`/`[via codex]` tag; text messages keep tags unchanged.
- No output clipping anywhere in the new path — outputs ride verbatim.
- Placeholder output wording, exactly: `(tool result not recorded)`.
- `map_pair` never raises: any mapping surprise degrades to Tier 2 pass-through.
- Honesty rule: Tier-1 only when the native rendering truthfully describes what happened (overwrite `Write`, `replace_all` Edit, multi-file patch → Tier 2).
- Codex rendering convention: mapped ToolCall with `dict` arguments → `function_call`; `str` arguments → `custom_tool_call`.
- Claude rendering convention: `ToolResult.structured` → `toolUseResult` sibling; `ToolResult.is_error` → `tool_result.is_error`.
- `docs/superpowers/` is gitignored in this repo — specs live in `docs/specs/`, plans in `docs/plans/`.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with `python3 -m pytest tests/ -q` from the repo root (`uv run pytest` if a venv isn't active).

## File Structure

- Create `src/tandem/toolmap.py` — the mapping layer (pure functions, no I/O). Owns the exec-output header parser (moves here from `summarize.py` because the mapper, not the renderer, synthesizes `toolUseResult`/`is_error` — deliberate small deviation from the spec's "keep it in summarize.py").
- Create `tests/test_toolmap.py` — unit tests for mappings.
- Modify `src/tandem/events.py` — add `SessionContext.claude_run_msg_id`.
- Modify `src/tandem/converter.py` — pair-at-result emission, orphan fallback, `flush_dangling`.
- Modify `src/tandem/harness/codex.py` — render tool events; `model_provider` fix.
- Modify `src/tandem/harness/claude_code.py` — render tool events; `message.id` runs.
- Modify `src/tandem/sync.py` — `SyncEngine.flush_dangling` + crash recovery.
- Modify `src/tandem/ops.py` — flush wiring on switch/one-off drains.
- Modify `src/tandem/summarize.py` — shrink to orphan-only.
- Modify `tests/test_converter.py`, `tests/test_sync.py` — expectations change from prose summaries to native pairs.

---

### Task 1: model_provider bug fix (independent rider)

Codex ≥0.145's interactive `thread/resume` rejects rollouts whose `session_meta` lacks `model_provider` (``Model provider `` not found``, -32600). Tandem's shadow rollouts omit it.

**Files:**
- Modify: `src/tandem/harness/codex.py` (the `meta` dict in `create_shadow_transcript`, around line 68)
- Test: `tests/test_toolmap.py` (create the file now; this test lives here to avoid touching unrelated suites)

**Interfaces:**
- Consumes: `conftest.Env` fixture (`env_factory`) which calls `create_shadow_transcript`.
- Produces: `session_meta.payload.model_provider == "openai"` in every tandem-minted rollout.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_toolmap.py
"""Tool mapping layer + related shadow-file invariants."""

import json


class TestShadowRolloutMeta:
    def test_model_provider_written(self, env_factory):
        env = env_factory()
        meta = json.loads(env.codex_shadow.read_text().splitlines()[0])
        assert meta["type"] == "session_meta"
        # codex >= 0.145 interactive thread/resume rejects rollouts without
        # this ("Model provider `` not found", -32600)
        assert meta["payload"]["model_provider"] == "openai"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: FAIL with `KeyError: 'model_provider'`

- [ ] **Step 3: Implement**

In `src/tandem/harness/codex.py`, inside `create_shadow_transcript`, add one key to the `meta["payload"]` dict after `"source": "exec",`:

```python
                "source": "exec",
                "thread_source": "user",
                # codex >= 0.145 interactive thread/resume rejects rollouts
                # without a provider id; "openai" is codex's built-in default
                "model_provider": "openai",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite (goldens must not care)**

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/tandem/harness/codex.py tests/test_toolmap.py
git commit -m "fix: write model_provider in shadow rollout session_meta

Interactive codex resume (>=0.145) rejects rollouts without it; only
codex exec resume tolerates the omission, which is how M1 validation
missed it.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: toolmap foundation — pass-through + Bash↔exec_command

**Files:**
- Create: `src/tandem/toolmap.py`
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `ToolCall`, `ToolResult` from `src/tandem/events.py` (pydantic models; `ToolCall.arguments: dict | str`; `ToolResult.output: str`, `.is_error: bool`, `.structured: dict | None`).
- Produces (later tasks rely on these exact names):
  - `toolmap.map_pair(call: ToolCall, result: ToolResult, target: str) -> tuple[ToolCall, ToolResult]` where `target` is `"claude"` or `"codex"`.
  - `toolmap.PLACEHOLDER_OUTPUT = "(tool result not recorded)"`.
  - `toolmap.parse_exec_output(raw: str) -> tuple[str | None, str]` (exit-code, body) — moved logic from `summarize._codex_exec_output`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toolmap.py`:

```python
from tandem import toolmap
from tandem.events import ToolCall, ToolResult


def call(tool, arguments, source="claude", call_id="c1"):
    return ToolCall(source=source, call_id=call_id, tool=tool, arguments=arguments)


def result(output, source="claude", call_id="c1", structured=None, is_error=False):
    return ToolResult(source=source, call_id=call_id, output=output,
                      structured=structured, is_error=is_error)


class TestBashExecCommand:
    def test_bash_to_exec_command(self):
        c, r = toolmap.map_pair(
            call("Bash", {"command": "pytest -q"}),
            result("3 failed", structured={"stdout": "3 failed", "stderr": ""}),
            "codex",
        )
        assert c.tool == "exec_command"
        assert c.arguments == {"cmd": "pytest -q"}
        assert c.call_id == "c1"          # call ids ride verbatim
        assert r.output == "3 failed"     # no fake codex exit-code header

    def test_exec_command_to_bash_strips_header(self):
        raw = ("Chunk ID: 0\nProcess exited with code 1\nWall time: 0.1 s\n"
               "Original token count: 5\nOutput:\n3 failed")
        c, r = toolmap.map_pair(
            call("exec_command", '{"cmd": "pytest -q"}', source="codex"),
            result(raw, source="codex"),
            "claude",
        )
        assert c.tool == "Bash"
        assert c.arguments == {"command": "pytest -q"}
        assert r.output == "3 failed"
        assert r.is_error is True
        assert r.structured == {"stdout": "3 failed", "exitCode": 1}

    def test_exec_command_exit_zero_not_error(self):
        raw = "Process exited with code 0\nOutput:\nok"
        _, r = toolmap.map_pair(
            call("exec_command", '{"cmd": "true"}', source="codex"),
            result(raw, source="codex"), "claude",
        )
        assert r.is_error is False
        assert r.structured == {"stdout": "ok", "exitCode": 0}


class TestPassThrough:
    def test_unknown_claude_tool_rides_verbatim_to_codex(self):
        c, r = toolmap.map_pair(
            call("AskUserQuestion", {"questions": [{"q": "a?"}]}),
            result("answer: a"), "codex",
        )
        assert c.tool == "AskUserQuestion"
        assert c.arguments == {"questions": [{"q": "a?"}]}
        assert r.output == "answer: a"

    def test_unknown_codex_tool_rides_verbatim_to_claude(self):
        c, r = toolmap.map_pair(
            call("write_stdin", '{"chars": "y\\n"}', source="codex"),
            result("ok", source="codex"), "claude",
        )
        assert c.tool == "write_stdin"
        assert c.arguments == {"chars": "y\n"}   # json args parsed for tool_use.input
        assert r.structured == {"stdout": "ok"}  # default toolUseResult

    def test_unparseable_str_args_wrapped_for_claude(self):
        c, _ = toolmap.map_pair(
            call("mystery", "not json", source="codex"),
            result("ok", source="codex"), "claude",
        )
        assert c.arguments == {"input": "not json"}

    def test_mapping_never_raises(self):
        # Bash with pathological arguments degrades to pass-through
        c, _ = toolmap.map_pair(call("Bash", "i am not a dict"), result("x"), "codex")
        assert c.tool == "Bash"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.toolmap'` (the Task-1 test still passes)

- [ ] **Step 3: Implement `src/tandem/toolmap.py`**

```python
"""Tool vocabulary mapping between harnesses.

Spec: docs/specs/2026-07-29-native-tool-call-translation-design.md.

Tier 1 re-expresses common tools in the target harness's own vocabulary so
shadow history reads as the shadow's own work; Tier 2 passes name and
arguments through verbatim (spike-validated safe on both sides, 2026-07).
Honesty rule: Tier 1 only when the native rendering truthfully describes
what happened; anything that doesn't fit degrades to Tier 2. map_pair never
raises on shape surprises.

Rendering conventions consumed by the adapters:
- codex target: ToolCall with dict arguments renders as function_call, str
  arguments as custom_tool_call input.
- claude target: ToolResult.structured becomes the toolUseResult sibling,
  ToolResult.is_error the tool_result is_error flag.
"""

from __future__ import annotations

import json
import re
import shlex

from .events import ToolCall, ToolResult

PLACEHOLDER_OUTPUT = "(tool result not recorded)"

_EXIT_RE = re.compile(r"^(?:Process exited|Exit code)[^\d-]*(-?\d+)", re.M)
_OUTPUT_RE = re.compile(r"^Output:\n", re.M)


def parse_exec_output(raw: str) -> tuple[str | None, str]:
    """codex exec_command output has a header (chunk id, wall time, exit
    code, token count) followed by 'Output:\\n<text>'."""
    exit_code = None
    m = _EXIT_RE.search(raw)
    if m:
        exit_code = m.group(1)
    m = _OUTPUT_RE.search(raw)
    return exit_code, raw[m.end():] if m else raw


def map_pair(
    call: ToolCall, result: ToolResult, target: str
) -> tuple[ToolCall, ToolResult]:
    """One completed source-native pair -> one target-native pair."""
    try:
        mapper = _TO_CODEX if target == "codex" else _TO_CLAUDE
        fn = mapper.get(call.tool)
        if fn is not None:
            mapped = fn(call, result)
            if mapped is not None:
                return mapped
    except Exception:
        pass  # honesty rule: surprises fall through to pass-through
    return _passthrough(call, result, target)


def _passthrough(
    call: ToolCall, result: ToolResult, target: str
) -> tuple[ToolCall, ToolResult]:
    if target == "claude":
        args = call.arguments
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                args = parsed if isinstance(parsed, dict) else {"input": args}
            except json.JSONDecodeError:
                args = {"input": args}
        call = call.model_copy(update={"arguments": args})
        if result.structured is None:
            result = result.model_copy(update={"structured": {"stdout": result.output}})
    return call, result


def _retool(call, result, tool, arguments, output=None, structured=None,
            is_error=None):
    new_call = call.model_copy(update={"tool": tool, "arguments": arguments})
    updates = {"tool": tool}
    if output is not None:
        updates["output"] = output
    if structured is not None:
        updates["structured"] = structured
    if is_error is not None:
        updates["is_error"] = is_error
    return new_call, result.model_copy(update=updates)


# -- claude -> codex ---------------------------------------------------------

def _bash_to_exec(call, result):
    args = call.arguments
    if not isinstance(args, dict):
        return None
    return _retool(call, result, "exec_command", {"cmd": str(args.get("command", ""))})


# -- codex -> claude ---------------------------------------------------------

def _exec_to_bash(call, result):
    args = call.arguments
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        return None
    exit_code, body = parse_exec_output(result.output)
    structured: dict = {"stdout": body}
    is_error = False
    if exit_code is not None:
        structured["exitCode"] = int(exit_code)
        is_error = exit_code != "0"
    return _retool(call, result, "Bash", {"command": str(args.get("cmd", ""))},
                   output=body, structured=structured, is_error=is_error)


_TO_CODEX = {
    "Bash": _bash_to_exec,
}

_TO_CLAUDE = {
    "exec_command": _exec_to_bash,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/toolmap.py tests/test_toolmap.py
git commit -m "feat: toolmap foundation - pass-through tiers + Bash<->exec_command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: toolmap claude→codex — Read, Write, Edit, Grep, Glob, TodoWrite

**Files:**
- Modify: `src/tandem/toolmap.py`
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `_retool`, `_TO_CODEX` from Task 2.
- Produces: Tier-1 claude→codex mappings. `apply_patch` calls are emitted with **str** arguments (the patch text) so the codex renderer picks `custom_tool_call`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toolmap.py`:

```python
class TestClaudeToCodexTier1:
    def test_read_plain(self):
        c, r = toolmap.map_pair(
            call("Read", {"file_path": "/a/b.py"}),
            result("1\timport os"), "codex",
        )
        assert c.tool == "exec_command"
        assert c.arguments == {"cmd": "cat -n /a/b.py"}
        assert r.output == "1\timport os"

    def test_read_ranged(self):
        c, _ = toolmap.map_pair(
            call("Read", {"file_path": "/a/b.py", "offset": 10, "limit": 5}),
            result("10\tx"), "codex",
        )
        assert c.arguments == {"cmd": "cat -n /a/b.py | sed -n '10,14p'"}

    def test_write_create_becomes_add_file_patch(self):
        c, _ = toolmap.map_pair(
            call("Write", {"file_path": "/p/hello.txt", "content": "hi\nthere"}),
            result("ok", structured={"type": "create", "filePath": "/p/hello.txt",
                                     "content": "hi\nthere"}),
            "codex",
        )
        assert c.tool == "apply_patch"
        assert isinstance(c.arguments, str)   # str => custom_tool_call
        assert c.arguments == (
            "*** Begin Patch\n*** Add File: /p/hello.txt\n+hi\n+there\n*** End Patch"
        )

    def test_write_overwrite_falls_back(self):
        c, _ = toolmap.map_pair(
            call("Write", {"file_path": "/p/x.txt", "content": "new"}),
            result("ok", structured={"type": "update", "filePath": "/p/x.txt"}),
            "codex",
        )
        assert c.tool == "Write"   # Tier 2: honesty rule

    def test_edit_becomes_update_file_patch(self):
        c, _ = toolmap.map_pair(
            call("Edit", {"file_path": "/p/a.py", "old_string": "x = 1",
                          "new_string": "x = 2"}),
            result("ok"), "codex",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == (
            "*** Begin Patch\n*** Update File: /p/a.py\n-x = 1\n+x = 2\n*** End Patch"
        )

    def test_edit_replace_all_falls_back(self):
        c, _ = toolmap.map_pair(
            call("Edit", {"file_path": "/p/a.py", "old_string": "x",
                          "new_string": "y", "replace_all": True}),
            result("ok"), "codex",
        )
        assert c.tool == "Edit"

    def test_grep_and_glob(self):
        # Grep's schema defaults output_mode to files_with_matches, and a
        # transcript records only the args actually passed: omitted => -l
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src"}),
            result("src/a.py"), "codex",
        )
        assert c.arguments == {"cmd": "rg -l 'def main' src"}
        c, _ = toolmap.map_pair(
            call("Glob", {"pattern": "**/*.py"}), result("a.py"), "codex",
        )
        assert c.arguments == {"cmd": "rg --files -g '**/*.py'"}

    def test_grep_content_mode_uses_line_numbers(self):
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src",
                          "output_mode": "content"}),
            result("src/a.py:1:def main"), "codex",
        )
        assert c.arguments == {"cmd": "rg -n 'def main' src"}

    def test_grep_count_mode_falls_back(self):
        # 'src/a.py:3' means "3 matches", which `rg -n` output would read as
        # line 3: honesty rule, Tier 2
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src",
                          "output_mode": "count"}),
            result("src/a.py:3"), "codex",
        )
        assert c.tool == "Grep"

    def test_grep_head_limit_falls_back(self):
        # truncated output under a command implying the full result
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "output_mode": "content",
                          "head_limit": 10}),
            result("src/a.py:1:def main"), "codex",
        )
        assert c.tool == "Grep"

    def test_todowrite_becomes_update_plan(self):
        c, _ = toolmap.map_pair(
            call("TodoWrite", {"todos": [
                {"content": "fix bug", "status": "in_progress", "activeForm": "Fixing"},
                {"content": "add test", "status": "pending", "activeForm": "Adding"},
            ]}),
            result("Todos have been modified successfully"), "codex",
        )
        assert c.tool == "update_plan"
        assert c.arguments == {"plan": [
            {"step": "fix bug", "status": "in_progress"},
            {"step": "add test", "status": "pending"},
        ]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: the new class FAILS (Read passes through as `Read`, etc.)

- [ ] **Step 3: Implement**

Add to `src/tandem/toolmap.py` (claude→codex section), and register in `_TO_CODEX`:

```python
def _sh_quote(path: str) -> str:
    q = shlex.quote(path)
    return q


def _read_to_cat(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("file_path"):
        return None
    cmd = f"cat -n {_sh_quote(args['file_path'])}"
    offset, limit = args.get("offset"), args.get("limit")
    if offset or limit:
        start = int(offset or 1)
        end = f"{start + int(limit) - 1}" if limit else "$"
        cmd += f" | sed -n '{start},{end}p'"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _patch(op: str, path: str, body: str) -> str:
    return f"*** Begin Patch\n*** {op} File: {path}\n{body}\n*** End Patch"


def _write_to_patch(call, result):
    args = call.arguments
    s = result.structured or {}
    if not isinstance(args, dict) or s.get("type") != "create":
        return None  # overwrite or unknown: honesty rule, Tier 2
    content = s.get("content") if isinstance(s.get("content"), str) else args.get("content", "")
    body = "\n".join("+" + line for line in content.splitlines())
    return _retool(call, result, "apply_patch",
                   _patch("Add", args.get("file_path", "?"), body))


def _edit_to_patch(call, result):
    args = call.arguments
    if not isinstance(args, dict) or args.get("replace_all"):
        return None
    hunks = (result.structured or {}).get("structuredPatch") or []
    if hunks:
        lines: list[str] = []
        for h in hunks:
            lines.append("@@")
            lines.extend(h.get("lines") or [])
        body = "\n".join(lines)
    else:
        old = [f"-{l}" for l in str(args.get("old_string", "")).splitlines()]
        new = [f"+{l}" for l in str(args.get("new_string", "")).splitlines()]
        body = "\n".join(old + new)
    return _retool(call, result, "apply_patch",
                   _patch("Update", args.get("file_path", "?"), body))


def _grep_to_rg(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("pattern"):
        return None
    # Grep's schema defaults output_mode to files_with_matches and a transcript
    # records only the args actually passed, so an omitted mode means -l. count
    # output (path:<n> = n matches) would read as a line number under rg -n, and
    # head_limit shows truncated output under a command implying the full
    # result: neither is expressible honestly, so both degrade to Tier 2.
    mode = args.get("output_mode") or "files_with_matches"
    if mode not in ("files_with_matches", "content") or args.get("head_limit"):
        return None
    flag = "-n" if mode == "content" else "-l"
    cmd = f"rg {flag}"
    if args.get("-i"):
        cmd += " -i"
    if args.get("glob"):
        cmd += f" -g {shlex.quote(args['glob'])}"
    cmd += f" {shlex.quote(args['pattern'])}"
    if args.get("path"):
        cmd += f" {_sh_quote(args['path'])}"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _glob_to_rg(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("pattern"):
        return None
    cmd = f"rg --files -g {shlex.quote(args['pattern'])}"
    if args.get("path"):
        cmd += f" {_sh_quote(args['path'])}"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _todos_to_plan(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not isinstance(args.get("todos"), list):
        return None
    plan = [{"step": t.get("content", ""), "status": t.get("status", "pending")}
            for t in args["todos"] if isinstance(t, dict)]
    return _retool(call, result, "update_plan", {"plan": plan})
```

Update the registry:

```python
_TO_CODEX = {
    "Bash": _bash_to_exec,
    "Read": _read_to_cat,
    "Write": _write_to_patch,
    "Edit": _edit_to_patch,
    "Grep": _grep_to_rg,
    "Glob": _glob_to_rg,
    "TodoWrite": _todos_to_plan,
}
```

Note: `shlex.quote("/a/b.py")` returns the path unquoted (safe chars), which is why the test expects `cat -n /a/b.py` but `'def main'` for the pattern with a space.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/toolmap.py tests/test_toolmap.py
git commit -m "feat: toolmap claude->codex Tier-1 mappings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: toolmap codex→claude — apply_patch→Write/Edit, update_plan→TodoWrite

**Files:**
- Modify: `src/tandem/toolmap.py`
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `_retool`, `_TO_CLAUDE`, `_passthrough` from Task 2.
- Produces: Tier-1 codex→claude mappings; `_parse_patch(patch: str) -> list[tuple[str, str, list[str]]]` (op, path, body-lines) helper.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toolmap.py`:

```python
ADD_PATCH = "*** Begin Patch\n*** Add File: /p/hello.txt\n+hi\n+there\n*** End Patch"
UPDATE_PATCH = ("*** Begin Patch\n*** Update File: /p/a.py\n@@\n ctx\n-x = 1\n+x = 2\n"
                "*** End Patch")
MULTI_PATCH = ("*** Begin Patch\n*** Add File: /p/a\n+1\n*** Add File: /p/b\n+2\n"
               "*** End Patch")
TWO_HUNK_PATCH = ("*** Begin Patch\n*** Update File: /p/a.py\n@@\n ctx\n-x = 1\n+x = 2\n"
                  "@@\n far\n-y = 1\n+y = 2\n*** End Patch")
MOVE_PATCH = ("*** Begin Patch\n*** Update File: /p/old.py\n*** Move to: /p/new.py\n"
              "@@\n ctx\n-x = 1\n+x = 2\n*** End Patch")


class TestCodexToClaudeTier1:
    def test_apply_patch_add_becomes_write(self):
        c, r = toolmap.map_pair(
            call("apply_patch", ADD_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "Write"
        assert c.arguments == {"file_path": "/p/hello.txt", "content": "hi\nthere"}
        assert r.structured == {"type": "create", "filePath": "/p/hello.txt",
                                "content": "hi\nthere"}

    def test_apply_patch_update_becomes_edit(self):
        c, r = toolmap.map_pair(
            call("apply_patch", UPDATE_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "Edit"
        assert c.arguments == {"file_path": "/p/a.py", "old_string": "ctx\nx = 1",
                               "new_string": "ctx\nx = 2"}
        assert r.structured == {"filePath": "/p/a.py", "oldString": "ctx\nx = 1",
                                "newString": "ctx\nx = 2"}

    def test_multi_file_patch_falls_back(self):
        c, _ = toolmap.map_pair(
            call("apply_patch", MULTI_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": MULTI_PATCH}

    def test_multi_hunk_update_falls_back(self):
        # two hunks touch non-adjacent regions; splicing them into one
        # old_string would assert text that appears nowhere in the file
        c, _ = toolmap.map_pair(
            call("apply_patch", TWO_HUNK_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": TWO_HUNK_PATCH}

    def test_move_to_update_falls_back(self):
        # an Edit record would swallow the rename and name a path that no
        # longer exists: honesty rule, Tier 2
        c, _ = toolmap.map_pair(
            call("apply_patch", MOVE_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": MOVE_PATCH}

    def test_update_plan_becomes_todowrite(self):
        c, r = toolmap.map_pair(
            call("update_plan",
                 '{"plan": [{"step": "fix bug", "status": "in_progress"}]}',
                 source="codex"),
            result("Plan updated", source="codex"), "claude",
        )
        assert c.tool == "TodoWrite"
        assert c.arguments == {"todos": [
            {"content": "fix bug", "status": "in_progress", "activeForm": "fix bug"},
        ]}
        assert r.output == "Plan updated"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: new class FAILS (apply_patch passes through with `{"input": ...}` but tool name stays `apply_patch` — the Write/Edit assertions fail)

- [ ] **Step 3: Implement**

Add to `src/tandem/toolmap.py` (codex→claude section):

```python
_FILE_OP_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def _parse_patch(patch: str) -> list[tuple[str, str, list[str]]]:
    """V4A patch text -> [(op, path, body-lines)]. Ignores Begin/End lines."""
    ops: list[tuple[str, str, list[str]]] = []
    for line in patch.splitlines():
        if line in ("*** Begin Patch", "*** End Patch"):
            continue
        m = _FILE_OP_RE.match(line)
        if m:
            ops.append((m.group(1), m.group(2), []))
        elif ops:
            ops[-1][2].append(line)
    return ops


def _patch_to_edit(call, result):
    patch = call.arguments if isinstance(call.arguments, str) else ""
    ops = _parse_patch(patch)
    if len(ops) != 1:
        return None
    op, path, body = ops[0]
    if op == "Add":
        content = "\n".join(l[1:] for l in body if l.startswith("+"))
        return _retool(
            call, result, "Write", {"file_path": path, "content": content},
            structured={"type": "create", "filePath": path, "content": content},
        )
    if op == "Update":
        # Honesty rule: separate hunks describe non-adjacent regions, so
        # splicing them yields an old_string that appears nowhere in the file;
        # a Move to: line would vanish, leaving the record asserting an
        # in-place edit of a path that no longer exists. Both are Tier 2.
        # (Zero @@ lines is a valid single-hunk patch and still maps.)
        if sum(1 for l in body if l.startswith("@@")) > 1 or any(
            l.startswith("*** Move to:") for l in body
        ):
            return None
        old = "\n".join(l[1:] for l in body if l[:1] in (" ", "-"))
        new = "\n".join(l[1:] for l in body if l[:1] in (" ", "+"))
        return _retool(
            call, result, "Edit",
            {"file_path": path, "old_string": old, "new_string": new},
            structured={"filePath": path, "oldString": old, "newString": new},
        )
    return None  # Delete etc.: Tier 2


def _plan_to_todos(call, result):
    args = call.arguments
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict) or not isinstance(args.get("plan"), list):
        return None
    todos = [{"content": s.get("step", ""), "status": s.get("status", "pending"),
              "activeForm": s.get("step", "")}
             for s in args["plan"] if isinstance(s, dict)]
    return _retool(call, result, "TodoWrite", {"todos": todos},
                   structured={"stdout": result.output})
```

Update the registry:

```python
_TO_CLAUDE = {
    "exec_command": _exec_to_bash,
    "apply_patch": _patch_to_edit,
    "update_plan": _plan_to_todos,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/toolmap.py tests/test_toolmap.py
git commit -m "feat: toolmap codex->claude Tier-1 mappings incl. V4A patch parsing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: codex renderer — function_call / custom_tool_call pairs

**Files:**
- Modify: `src/tandem/harness/codex.py` (`render_events`, currently lines 242–305)
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `ToolCall`/`ToolResult` events with **codex-vocabulary** content (from `toolmap`); the dict-vs-str arguments convention.
- Produces: `response_item` lines: `function_call {name, arguments: <json str>, call_id}` / `custom_tool_call {name, input, call_id}` and matching `function_call_output` / `custom_tool_call_output {call_id, output}`. No `event_msg` lines for tool activity.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_toolmap.py`:

```python
from tandem.events import SessionContext
from tandem.harness import get_adapter


def _ctx(direction="claude->codex"):
    return SessionContext(
        tandem_id="t1", cwd="/p", direction=direction,
        claude_session_id="11111111-1111-4111-8111-111111111111",
        codex_session_id="019faca1-0000-7000-8000-000000000001",
        claude_leaf_uuid="seed",
    )


class TestCodexToolRendering:
    def test_function_call_pair(self):
        ctx = _ctx()
        entries = get_adapter("codex").render_events([
            call("exec_command", {"cmd": "ls"}, call_id="c9"),
            result("a.py", call_id="c9"),
        ], ctx)
        assert [e["payload"]["type"] for e in entries] == [
            "function_call", "function_call_output"]
        assert entries[0]["payload"]["name"] == "exec_command"
        assert entries[0]["payload"]["arguments"] == '{"cmd": "ls"}'
        assert entries[0]["payload"]["call_id"] == "c9"
        assert entries[1]["payload"] == {
            "type": "function_call_output", "call_id": "c9", "output": "a.py"}
        assert all(e["type"] == "response_item" for e in entries)

    def test_custom_tool_call_pair(self):
        ctx = _ctx()
        entries = get_adapter("codex").render_events([
            call("apply_patch", "*** Begin Patch\n*** End Patch", call_id="c2"),
            result("Done!", call_id="c2"),
        ], ctx)
        assert [e["payload"]["type"] for e in entries] == [
            "custom_tool_call", "custom_tool_call_output"]
        assert entries[0]["payload"]["input"].startswith("*** Begin Patch")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: FAIL — `render_events` currently drops tool events (returns `[]`)

- [ ] **Step 3: Implement**

In `src/tandem/harness/codex.py` `render_events`, add branches to the `for ev in events:` loop (after the `assistant_message` branch), plus a batch-local set before the loop:

```python
        out: list[dict[str, Any]] = []
        custom_ids: set[str] = set()   # calls rendered as custom_tool_call
        for ev in events:
```

```python
            elif ev.kind == "tool_call":
                if isinstance(ev.arguments, str):
                    custom_ids.add(ev.call_id)
                    payload: dict[str, Any] = {
                        "type": "custom_tool_call", "name": ev.tool,
                        "input": ev.arguments, "call_id": ev.call_id,
                    }
                else:
                    payload = {
                        "type": "function_call", "name": ev.tool,
                        "arguments": json.dumps(ev.arguments, ensure_ascii=False),
                        "call_id": ev.call_id,
                    }
                out.append({"timestamp": ts, "type": "response_item", "payload": payload})
            elif ev.kind == "tool_result":
                ptype = ("custom_tool_call_output" if ev.call_id in custom_ids
                         else "function_call_output")
                out.append({
                    "timestamp": ts, "type": "response_item",
                    "payload": {"type": ptype, "call_id": ev.call_id,
                                "output": ev.output},
                })
```

Add `import json` to the module imports (it is not currently imported there). The converter always emits a call adjacent to its result in the same batch, so the batch-local `custom_ids` set is sufficient to pick the output type.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/harness/codex.py tests/test_toolmap.py
git commit -m "feat: codex renderer emits native function_call/custom_tool_call pairs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: claude renderer — tool_use/tool_result + message.id runs

**Files:**
- Modify: `src/tandem/events.py` (SessionContext)
- Modify: `src/tandem/harness/claude_code.py` (`render_events`, currently lines 226–254)
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `ToolCall`/`ToolResult` events with **claude-vocabulary** content; `ToolResult.structured` → `toolUseResult`, `.is_error` → `tool_result.is_error`.
- Produces: `assistant` entries with a `tool_use` block (`stop_reason: "tool_use"`) and `user` entries with a `tool_result` block + `toolUseResult` sibling; new `SessionContext.claude_run_msg_id: str | None` — consecutive assistant entries share one `message.id`, reset by any rendered user-side entry.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_toolmap.py`:

```python
from tandem.events import AssistantMessage, UserMessage


class TestClaudeToolRendering:
    def test_tool_pair_and_message_id_runs(self):
        ctx = _ctx("codex->claude")
        adapter = get_adapter("claude")
        entries = adapter.render_events([
            AssistantMessage(source="codex", text="Looking around."),
            call("Bash", {"command": "ls"}, source="codex", call_id="c1"),
            result("a.py", source="codex", call_id="c1",
                   structured={"stdout": "a.py", "exitCode": 0}),
            call("Bash", {"command": "cat a.py"}, source="codex", call_id="c2"),
            result("print(1)", source="codex", call_id="c2",
                   structured={"stdout": "print(1)", "exitCode": 0}),
        ], ctx)

        types = [e["type"] for e in entries]
        assert types == ["assistant", "assistant", "user", "assistant", "user"]

        text_msg, tool1, res1, tool2, res2 = entries
        # text and the first tool_use share one API message id; the
        # tool_result (a user entry) ends the run, so the next call gets a
        # fresh id
        assert tool1["message"]["id"] == text_msg["message"]["id"]
        assert tool2["message"]["id"] != tool1["message"]["id"]

        block = tool1["message"]["content"][0]
        assert block == {"type": "tool_use", "id": "c1", "name": "Bash",
                         "input": {"command": "ls"}}
        assert tool1["message"]["stop_reason"] == "tool_use"

        rblock = res1["message"]["content"][0]
        assert rblock == {"type": "tool_result", "tool_use_id": "c1",
                          "content": "a.py", "is_error": False}
        assert res1["toolUseResult"] == {"stdout": "a.py", "exitCode": 0}

        # uuid/parentUuid chain is intact across the mixed entries
        for prev, cur in zip(entries, entries[1:]):
            assert cur["parentUuid"] == prev["uuid"]
        assert ctx.claude_leaf_uuid == entries[-1]["uuid"]

    def test_is_error_flag_rides(self):
        ctx = _ctx("codex->claude")
        entries = get_adapter("claude").render_events([
            call("Bash", {"command": "false"}, source="codex", call_id="c3"),
            result("", source="codex", call_id="c3", is_error=True,
                   structured={"stdout": "", "exitCode": 1}),
        ], ctx)
        assert entries[1]["message"]["content"][0]["is_error"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: FAIL — tool events are dropped by the current renderer

- [ ] **Step 3: Implement**

In `src/tandem/events.py`, add to `SessionContext` (after `claude_leaf_uuid`):

```python
    # message.id shared by the current contiguous run of rendered assistant
    # entries; any rendered user-side entry resets it (a real transcript has
    # one id per API response, and regrouping by id must not strand a
    # tool_use away from its tool_result)
    claude_run_msg_id: str | None = None
```

In `src/tandem/harness/claude_code.py`, replace the body of `render_events` with:

```python
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.kind in ("user_message", "tool_result"):
                ctx.claude_run_msg_id = None
            if ev.kind == "user_message":
                entry = self._base_entry(ctx, "user")
                entry["message"] = {"role": "user", "content": ev.text}
            elif ev.kind == "assistant_message":
                entry = self._base_entry(ctx, "assistant")
                entry["message"] = self._assistant_message(
                    ctx, entry, [{"type": "text", "text": ev.text}], "end_turn"
                )
            elif ev.kind == "tool_call":
                entry = self._base_entry(ctx, "assistant")
                inp = ev.arguments if isinstance(ev.arguments, dict) else {"input": ev.arguments}
                entry["message"] = self._assistant_message(
                    ctx, entry,
                    [{"type": "tool_use", "id": ev.call_id, "name": ev.tool,
                      "input": inp}],
                    "tool_use",
                )
            elif ev.kind == "tool_result":
                entry = self._base_entry(ctx, "user")
                entry["message"] = {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": ev.call_id,
                                 "content": ev.output, "is_error": ev.is_error}],
                }
                entry["toolUseResult"] = ev.structured or {"stdout": ev.output}
            else:
                continue
            if ev.timestamp:
                entry["timestamp"] = ev.timestamp
            ctx.claude_leaf_uuid = entry["uuid"]
            out.append(entry)
        return out

    def _assistant_message(
        self, ctx: SessionContext, entry: dict[str, Any],
        content: list[dict[str, Any]], stop_reason: str,
    ) -> dict[str, Any]:
        if ctx.claude_run_msg_id is None:
            ctx.claude_run_msg_id = f"msg_tandem_{entry['uuid'][:8]}"
        return {
            "role": "assistant",
            "model": "<synced>",
            "id": ctx.claude_run_msg_id,
            "type": "message",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }
```

(This also switches plain assistant text to run-shared ids, per the spec.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: PASS. Also run `python3 -m pytest tests/ -q` — existing suites must still pass (they don't assert per-entry unique message ids).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/events.py src/tandem/harness/claude_code.py tests/test_toolmap.py
git commit -m "feat: claude renderer emits tool_use/tool_result with message.id runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: converter policy — pair-at-result emission + orphan fallback

**Files:**
- Modify: `src/tandem/converter.py` (`_apply_policy` ToolResult branch, lines 83–99; module docstring)
- Modify: `tests/test_converter.py` (golden expectations)
- Modify: `tests/test_sync.py:36` (one summary assertion)

**Interfaces:**
- Consumes: `toolmap.map_pair`, `summarize.summarize_orphan_result`, `other()` from `tandem.harness`.
- Produces: translated output for a completed pair is now `[mapped ToolCall, mapped ToolResult]` (untagged); orphan results stay prose commentary.

- [ ] **Step 1: Update the golden tests to expect native pairs**

Replace `TestClaudeToCodex.test_golden` and `TestCodexToClaude` golden assertions in `tests/test_converter.py`:

```python
class TestClaudeToCodex:
    def test_golden(self):
        entries, ctx = translate_file("claude-probe.jsonl", "claude->codex")

        assert all(set(e) == {"timestamp", "type", "payload"} for e in entries)

        user_items = [
            e for e in entries
            if e["type"] == "response_item" and e["payload"].get("role") == "user"
        ]
        assert len(user_items) == 1
        text = user_items[0]["payload"]["content"][0]["text"]
        assert text.startswith("[via claude-code] Create a file named hello.txt")

        assistant_items = [
            e for e in entries
            if e["type"] == "response_item" and e["payload"].get("role") == "assistant"
        ]
        texts = [a["payload"]["content"][0]["text"] for a in assistant_items]
        # 2 claude text messages, still attribution-tagged
        assert len(texts) == 2
        assert all(t.startswith("[via claude-code]") for t in texts)

        # the Write became a native custom_tool_call apply_patch pair
        customs = [e["payload"] for e in entries
                   if e["payload"].get("type") == "custom_tool_call"]
        assert len(customs) == 1
        assert customs[0]["name"] == "apply_patch"
        assert "*** Add File:" in customs[0]["input"]
        assert "+hi tandem" in customs[0]["input"]

        # the Bash became a native exec_command pair
        fcalls = [e["payload"] for e in entries
                  if e["payload"].get("type") == "function_call"]
        assert len(fcalls) == 1
        assert fcalls[0]["name"] == "exec_command"
        assert "cat hello.txt" in fcalls[0]["arguments"]
        outs = [e["payload"] for e in entries
                if e["payload"].get("type", "").endswith("_output")]
        assert len(outs) == 2
        assert any("hi tandem" in o["output"] for o in outs)

        # tool pairing consumed all pending calls
        assert ctx.pending_calls == {}
```

```python
class TestCodexToClaude:
    def test_golden(self):
        entries, ctx = translate_file("codex-probe.jsonl", "codex->claude")

        # chain integrity across mixed entry types
        assert entries[0]["parentUuid"] == "seed-leaf"
        for prev, cur in zip(entries, entries[1:]):
            assert cur["parentUuid"] == prev["uuid"]
        assert ctx.claude_leaf_uuid == entries[-1]["uuid"]
        assert all(e["sessionId"] == ctx.claude_session_id for e in entries)

        prompts = [e for e in entries if e["type"] == "user"
                   and isinstance(e["message"]["content"], str)]
        assert len(prompts) == 1
        assert prompts[0]["message"]["content"].startswith(
            "[via codex] Create a file named hello.txt"
        )

        texts = [
            e["message"]["content"][0]["text"] for e in entries
            if e["type"] == "assistant"
            and e["message"]["content"][0]["type"] == "text"
        ]
        # 3 codex messages (2 commentary + 1 final), still tagged; no summaries
        assert len(texts) == 3
        assert all(t.startswith("[via codex]") for t in texts)

        tool_uses = [
            e["message"]["content"][0] for e in entries
            if e["type"] == "assistant"
            and e["message"]["content"][0]["type"] == "tool_use"
        ]
        # apply_patch add hello.txt -> Write; 3 exec_command -> Bash
        # (count, not order: the probe's call order is not pinned here)
        assert sorted(t["name"] for t in tool_uses) == ["Bash", "Bash", "Bash", "Write"]
        write = next(t for t in tool_uses if t["name"] == "Write")
        assert write["input"]["file_path"].endswith("hello.txt")
        assert "hi tandem" in write["input"]["content"]
        assert any(t["name"] == "Bash" and t["input"]["command"] == "cat hello.txt"
                   for t in tool_uses)

        results = [e for e in entries if e["type"] == "user"
                   and isinstance(e["message"]["content"], list)]
        assert len(results) == 4
        cat = next(e for e in results
                   if "hi tandem" in e["message"]["content"][0]["content"])
        # exec header stripped from content, exit code in toolUseResult
        assert "Original token count" not in cat["message"]["content"][0]["content"]
        assert cat["toolUseResult"]["exitCode"] == 0
        assert ctx.pending_calls == {}
```

Delete the old `test_exec_output_header_stripped` (folded into `test_golden` above). Keep `test_error_localized_to_entry` unchanged.

In `tests/test_sync.py` line 36, replace:

```python
        assert any("ran `pytest -q`" in t and "3 failed" in t for t in texts)
```

with (add `read_jsonl` usage — it is already imported at the top):

```python
        rollout = read_jsonl(env.codex_shadow)
        fcalls = [e["payload"] for e in rollout
                  if e.get("type") == "response_item"
                  and e["payload"].get("type") == "function_call"]
        assert any('pytest -q' in f["arguments"] for f in fcalls)
        outs = [e["payload"] for e in rollout
                if e.get("type") == "response_item"
                and e["payload"].get("type") == "function_call_output"]
        assert any("3 failed" in o["output"] for o in outs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_converter.py tests/test_sync.py -q`
Expected: golden tests FAIL (converter still emits prose summaries)

- [ ] **Step 3: Implement**

In `src/tandem/converter.py`:
- Imports: add `from . import toolmap`, `from .harness import get_adapter, other`; drop `summarize_pair` from the summarize import (keep `summarize_orphan_result`).
- Replace the `ToolResult` branch of `_apply_policy`:

```python
            elif isinstance(ev, ToolCall):
                ctx.pending_calls[ev.call_id] = ev.model_dump(exclude_none=True)
            elif isinstance(ev, ToolResult):
                stored = ctx.pending_calls.pop(ev.call_id or "", None)
                if stored:
                    stored.pop("_structured", None)
                    call = ToolCall.model_validate(stored)
                    out.extend(toolmap.map_pair(call, ev, other(source_id)))
                else:
                    out.append(
                        AssistantMessage(
                            source=ev.source,
                            timestamp=ev.timestamp,
                            turn_index=ev.turn_index,
                            text=f"{tag} {summarize_orphan_result(ev)}",
                            phase="commentary",
                        )
                    )
```

- Update the class docstring: tool calls are now rendered as native pairs in the shadow vocabulary (mapped via `toolmap`), not action summaries. Update the `ToolCall` docstring in `src/tandem/events.py` the same way (it still says "Never replayed as a real tool call").

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/tandem/converter.py src/tandem/events.py tests/test_converter.py tests/test_sync.py
git commit -m "feat: converter emits native tool pairs via toolmap (pair-at-result)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: dangle flush — converter + SyncEngine + ops wiring

**Files:**
- Modify: `src/tandem/converter.py` (add `flush_dangling`)
- Modify: `src/tandem/sync.py` (`SyncEngine.flush_dangling`, `_prepare` recovery)
- Modify: `src/tandem/ops.py` (`drain_source` flag; `switch_session`, `run_oneoff`)
- Test: `tests/test_toolmap.py` (append)

**Interfaces:**
- Consumes: `toolmap.map_pair`, `toolmap.PLACEHOLDER_OUTPUT`, `TailLoop.ctx` / `TailLoop.cursor` attributes, `ctx_to_cursor`.
- Produces:
  - `ReferenceConverter.flush_dangling(ctx: SessionContext) -> list[NormalizedEvent]` — mapped call+placeholder pairs for every pending call; clears `ctx.pending_calls`.
  - `SyncEngine.flush_dangling(ctx, cursor) -> int` — renders+appends them exactly-once; returns entries appended.
  - `ops.drain_source(store, session, source, flush_dangling=False)` — flushes after the drain when the flag is set; `switch_session` and `run_oneoff` pass `True` for their outgoing-source drains.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toolmap.py`:

```python
from conftest import claude_assistant, claude_user, write_line
from tandem.util import read_jsonl


class TestDangleFlush:
    def _pendings(self, env):
        write_line(env.source_file, claude_user("do the thing"))
        write_line(env.source_file, claude_assistant(
            [{"type": "tool_use", "id": "dangling-1", "name": "Bash",
              "input": {"command": "sleep 999"}}]
        ))

    def test_flush_closes_pending_call(self, env_factory):
        env = env_factory()
        loop, engine = env.loop()
        self._pendings(env)
        loop.drain()
        assert loop.ctx.pending_calls  # the call is stashed, unpaired

        n = engine.flush_dangling(loop.ctx, loop.cursor)
        assert n == 2
        assert loop.ctx.pending_calls == {}

        rollout = read_jsonl(env.codex_shadow)
        pl = [e["payload"] for e in rollout if e.get("type") == "response_item"]
        fc = [p for p in pl if p.get("type") == "function_call"]
        out = [p for p in pl if p.get("type") == "function_call_output"]
        assert fc[-1]["name"] == "exec_command"
        assert fc[-1]["call_id"] == "dangling-1"
        assert out[-1] == {"type": "function_call_output",
                           "call_id": "dangling-1",
                           "output": "(tool result not recorded)"}

    def test_flush_noop_when_nothing_pending(self, env_factory):
        env = env_factory()
        loop, engine = env.loop()
        write_line(env.source_file, claude_user("hi"))
        loop.drain()
        before = env.codex_shadow.read_text()
        assert engine.flush_dangling(loop.ctx, loop.cursor) == 0
        assert env.codex_shadow.read_text() == before

    def test_drain_source_flag_flushes(self, env_factory, monkeypatch):
        from tandem import ops
        env = env_factory()
        self._pendings(env)
        # route drain_source at the stand-in transcript
        monkeypatch.setattr(
            ops, "source_transcript", lambda session, source: env.source_file
        )
        ops.drain_source(env.store, env.session, "claude", flush_dangling=True)
        rollout = read_jsonl(env.codex_shadow)
        outs = [e["payload"]["output"] for e in rollout
                if e.get("type") == "response_item"
                and e["payload"].get("type") == "function_call_output"]
        assert "(tool result not recorded)" in outs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_toolmap.py -q`
Expected: FAIL with `AttributeError: 'SyncEngine' object has no attribute 'flush_dangling'`

- [ ] **Step 3: Implement**

`src/tandem/converter.py`, on `ReferenceConverter`:

```python
    def flush_dangling(self, ctx: SessionContext) -> list[NormalizedEvent]:
        """Mapped call + placeholder-result pairs for every pending call.
        Both replay APIs reject a call without a result, so a drained source
        must never leave one behind. Clears ctx.pending_calls."""
        target_id = ctx.direction.split("->")[1]
        out: list[NormalizedEvent] = []
        for call_id, stored in list(ctx.pending_calls.items()):
            stored.pop("_structured", None)
            call = ToolCall.model_validate(stored)
            placeholder = ToolResult(
                source=call.source, turn_index=call.turn_index,
                call_id=call_id, output=toolmap.PLACEHOLDER_OUTPUT,
            )
            out.extend(toolmap.map_pair(call, placeholder, target_id))
        ctx.pending_calls.clear()
        return out
```

`src/tandem/sync.py`, on `SyncEngine` (module constant `_FLUSH_LINE = -1` near the top):

```python
    def flush_dangling(self, ctx: SessionContext, cursor: SyncCursor) -> int:
        """Close every pending tool call with a placeholder result. Called
        after a drain when the source is being handed off (role switch,
        one-off). Exactly-once via the same write-ahead intent as line
        appends, keyed on the sentinel line index _FLUSH_LINE."""
        if not self._prepared:
            self._prepare(ctx, cursor)
        fn = getattr(self.converter, "flush_dangling", None)
        if fn is None or not ctx.pending_calls:
            return 0
        events = fn(ctx)
        entries = self.target.render_events(events, ctx) if events else []
        if not entries:
            ctx_to_cursor(ctx, cursor)
            self.store.save_cursor(cursor)
            return 0
        pre_size = self.shadow_path.stat().st_size
        cursor.pending["intent"] = {"line": _FLUSH_LINE, "pre_size": pre_size}
        self.store.save_cursor(cursor)
        append_jsonl_fsync(self.shadow_path, entries)
        cursor.pending.pop("intent", None)
        ctx_to_cursor(ctx, cursor)
        self.store.save_cursor(cursor)
        return len(entries)
```

In `SyncEngine._prepare`, extend the intent recovery: a crashed *flush* whose append landed must not be replayed (the pending calls it closed were already written):

```python
        intent = cursor.pending.get("intent")
        if intent:
            try:
                grew = self.shadow_path.stat().st_size > int(intent["pre_size"])
            except OSError:
                grew = False
            if int(intent["line"]) == _FLUSH_LINE:
                if grew:
                    # the flush landed before the crash; its calls are closed
                    ctx.pending_calls.clear()
                cursor.pending.pop("intent", None)
                ctx_to_cursor(ctx, cursor)
                self.store.save_cursor(cursor)
            elif grew:
                self._skip_append_line = int(intent["line"])
```

(`ctx_to_cursor` is already imported in `sync.py` via `from .runner import ctx_to_cursor`.)

`src/tandem/ops.py` — change `drain_source` signature and tail:

```python
def drain_source(
    store: StateStore, session: PairedSession, source: str,
    *, flush_dangling: bool = False,
) -> int:
    """Translate any unsynced tail of `source`'s file into the other file.
    Pure local file I/O. Returns lines consumed. With flush_dangling=True,
    close any still-unpaired tool calls with placeholder results afterwards
    (required when the source is being handed off: both replay APIs reject
    a dangling call)."""
    transcript = source_transcript(session, source)
    if transcript is None:
        return 0
    engine = SyncEngine(store, session, source)
    loop = TailLoop(store, session, source, transcript, engine)
    total = 0
    while True:
        n = loop.drain()
        total += n
        if n == 0:
            break
    if loop.errors:
        raise SyncSetupError("; ".join(loop.errors))
    if flush_dangling:
        engine.flush_dangling(loop.ctx, loop.cursor)
    return total
```

In `switch_session`: `drain_source(store, session, old_active, flush_dangling=True)`.
In `run_oneoff`: both drains get `flush_dangling=True` (the initial `session.active` drain and the final `target` drain).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (existing switch/one-off tests in `test_ops.py` exercise the new flag with empty pending — no behavior change there)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/converter.py src/tandem/sync.py src/tandem/ops.py tests/test_toolmap.py
git commit -m "feat: dangle flush - close unpaired tool calls on source handoff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: shrink summarize.py to orphan-only

**Files:**
- Modify: `src/tandem/summarize.py`
- Test: existing suites (`tests/test_converter.py` orphan path is covered via converter tests; no test currently imports the deleted functions — verify with grep)

**Interfaces:**
- Consumes: nothing new.
- Produces: `summarize.py` exports only `summarize_orphan_result` (and its `_clip_lines` helper). `summarize_pair`, `_summarize_claude`, `_summarize_codex`, `_codex_exec_output`, `_clip_diff`, `_compact_args` and their constants (`DIFF_MAX_LINES`, `ARGS_MAX_CHARS`) are deleted — the exec-header parser now lives in `toolmap.parse_exec_output`.

- [ ] **Step 1: Confirm nothing else imports the dead code**

Run: `grep -rn "summarize_pair\|_codex_exec_output\|_summarize_claude\|_summarize_codex\|_clip_diff\|_compact_args\|DIFF_MAX_LINES\|ARGS_MAX_CHARS" src/ tests/`
Expected: no hits outside `src/tandem/summarize.py` itself. If `tests/` hits appear, delete those test cases in the same commit (they test retired behavior).

- [ ] **Step 2: Rewrite `src/tandem/summarize.py`**

```python
"""Orphan tool-result fallback.

Completed tool calls are translated natively (see toolmap.py / the
converter policy). The only tool activity still rendered as prose is a
result whose call was never seen (e.g. a restart lost the pairing): a bare
native tool_result without its call would break Claude resume, so it
becomes a one-line commentary instead.
"""

from __future__ import annotations

from .events import ToolResult

OUTPUT_HEAD_LINES = 25
OUTPUT_TAIL_LINES = 10
OUTPUT_MAX_CHARS = 4000


def _clip_lines(text: str, head: int = OUTPUT_HEAD_LINES, tail: int = OUTPUT_TAIL_LINES) -> str:
    text = text.rstrip("\n")
    if len(text) > OUTPUT_MAX_CHARS * 2:
        text = text[: OUTPUT_MAX_CHARS] + "\n…\n" + text[-OUTPUT_MAX_CHARS // 2 :]
    lines = text.splitlines()
    if len(lines) <= head + tail + 2:
        out = text
    else:
        omitted = len(lines) - head - tail
        out = "\n".join(lines[:head] + [f"… (+{omitted} lines omitted) …"] + lines[-tail:])
    if len(out) > OUTPUT_MAX_CHARS:
        out = out[:OUTPUT_MAX_CHARS] + "…"
    return out


def summarize_orphan_result(result: ToolResult) -> str:
    """Result whose call was never seen (e.g. restart lost the pairing)."""
    status = "error" if result.is_error else "ok"
    return f"tool result ({status}): {_clip_lines(result.output)}"
```

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add src/tandem/summarize.py
git commit -m "refactor: summarize.py shrinks to the orphan-result fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: live validation (manual, doctor-style)

No code — this validates the whole feature against the real CLIs, mirroring how M1–M5 were validated. Use a throwaway project dir.

**Files:**
- None (findings go in the final commit message / PR description; surprises get quarantined as spec amendments)

- [ ] **Step 1: claude→codex live check**

In a scratch project dir: run bare `tandem` (claude is the default active). In the claude session, run a short turn that uses at least one Bash, one Write, and one Edit, then exit claude to land at the tandem shell prompt and type `switch`. The shell flips roles and immediately resumes codex interactively — this exercises the Task-1 `model_provider` fix on the normal path. Confirm:
- the codex shadow rollout contains `function_call exec_command` / `custom_tool_call apply_patch` pairs (not prose), and
- the resumed codex answers a question about the synced work (e.g. "what file did I just create and what's in it?").

- [ ] **Step 2: codex→claude live check**

In a second scratch dir: `tandem --active codex`; run a codex turn with an `exec_command` and an `apply_patch`, exit to the tandem prompt, `switch`. Confirm the resumed claude session renders the history and answers "what did the last patch change?" correctly from the synced `tool_use`/`tool_result` entries.

- [ ] **Step 3: dangle check**

In the Step-1 pairing: start a claude turn with a long-running Bash (`sleep 120`), Ctrl-C claude mid-call, then `switch` at the tandem prompt. Confirm the codex shadow's last two response_items are the mapped `exec_command` call plus a `function_call_output` of `(tool result not recorded)`, and that codex still resumes cleanly on top.

- [ ] **Step 4: Record outcomes**

Append a dated "live validation" note to `docs/specs/2026-07-29-native-tool-call-translation-design.md` (Validation record section) stating CLI versions and the three outcomes; commit:

```bash
git add docs/specs/2026-07-29-native-tool-call-translation-design.md
git commit -m "docs: record live validation of native tool-call translation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- Spec coverage: converter policy → Task 7; pairing invariant/flush → Task 8; mapping tables → Tasks 2–4; codex renderer + model_provider rider → Tasks 5 and 1; claude renderer + message.id runs + SessionContext → Task 6; summarize shrink → Task 9; golden/live testing → Tasks 7 and 10. Spec's "`_codex_exec_output` stays in summarize.py" is deliberately amended: it moves to `toolmap.parse_exec_output` (the mapper is its only consumer).
- `pending_calls` keeps the full serialized ToolCall (spec requirement) — Task 7 leaves the stash line untouched.
- Attribution: tags only on `UserMessage`/`AssistantMessage` text paths, never added in Tasks 2–8.
