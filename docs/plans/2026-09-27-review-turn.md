# Review Turn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `tandem --review`, the reviewer's verdict becomes a real turn on its shared session (synced into the executor's transcript), and a spoken verdict starts one follow-up turn on the executor. One round, no fork.

**Architecture:** A third delivery mode, `navigator_deliver = "turn"`. In that mode the `Navigator` no longer runs its own thread: after the gate it hands the turn's facts to `Dispatcher.start_round`, which queues a *review* item ahead of everything typed. The dispatcher runs that item through its ordinary turn pipeline (prepare → runtime → adopt id → sync) on the reviewer's shared session, read-only with the verdict schema and deny-all answers, collects the JSON reply instead of painting it, then asks the navigator to settle the verdict. A spoken verdict queues a *follow-up* item on the executor whose prompt starts with `[tandem navigator]`, which the gate already never reviews. Both runtimes gain a per-turn `review=` schema parameter; `TurnStarted` gains `kind`/`peer` so the renderer can name the two turns.

**Tech Stack:** Python 3.11+, stdlib only, click for the CLI, pytest with the fake CLIs under `tests/fakes/`.

**Spec:** `docs/specs/2026-09-26-review-turn-design.md`

## Global Constraints

- No new dependencies.
- `navigator_deliver` accepts exactly `"bar" | "prompt" | "turn"`; an unknown value falls back to `"bar"`, never raises.
- The review turn runs read-only and never asks: codex `approvalPolicy: "never"`, `sandbox: "read-only"`; claude `--permission-mode default --allowedTools Read Grep Glob --disallowedTools Edit Write MultiEdit NotebookEdit Agent Task --max-turns 4`. Answers are `DenyAll`.
- Neither round turn moves the default harness: the review is an aside, and the follow-up runs on the executor without touching `set_active`, so a `/codex` typed during the review survives the round.
- The follow-up prompt's first line starts with `[tandem navigator]`; the gate skips it as `skip:tandem-prompt`. Nothing else enforces one round.
- An interrupted review is logged as an error verdict with `error == "interrupted"` and does **not** count toward the three strikes.
- The navigator must never take the window down: every dispatcher entry into it swallows.
- The existing fork-based path for `bar`/`prompt` is untouched: every existing test in `tests/test_chat_navigator.py`, `tests/test_chat_reviewers.py` and `tests/test_chat_dispatch.py` keeps passing unchanged (only `FakeRuntime` gains a keyword).
- Work happens in a worktree `/Users/bhavya/git/tandem-review-turn` on the existing branch `review-turn` (it already holds the spec commit `2fbd52c`). Run tests with `uv run pytest -q` from that directory. Commit after every task.

## Review Focus

- A user prompt typed while the executor's turn is running: it must run *after* the review and the follow-up, never between them (Task 7, `test_a_round_runs_review_then_followup_ahead_of_what_was_typed`).
- A review whose reply is not JSON (the model ignored the schema): an error verdict, a strike, no follow-up, window still usable (Task 7, `test_an_unparsable_review_reply_is_an_error_and_no_followup`).
- Ctrl-C during the review turn: no follow-up, no strike, the next gated turn is still reviewed (Task 6 `test_an_interrupted_review_is_logged_but_not_a_strike`; Task 7 `test_an_interrupted_review_ends_the_round_without_a_strike`).
- The window closing while a review or follow-up is queued: the queued item never runs (Task 7, `test_start_round_after_close_queues_nothing`).
- A round on a session whose executor is codex and reviewer is claude (`--on codex --review`): the mirror works and the default stays codex (Task 7, `test_the_mirror_round_reviews_on_claude_and_follows_up_on_codex`).
- A bare `/codex` typed while codex reviews: the follow-up starts after that route with a fresh spoken snapshot, and must still not move the default back (Task 7, `test_a_route_typed_during_the_review_outlives_the_followup`).

---

### Task 1: Config accepts `navigator_deliver = "turn"`

**Files:**
- Modify: `src/tandem/config.py:203` (`_NAVIGATOR_DELIVERY`), `:228` (the field comment)
- Modify: `docs/configuration.md:270-285` (the `[chat]` block and the navigator paragraph)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ChatConfig.navigator_deliver` may be `"turn"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`, next to `test_navigator_keys_read_and_validate`:

```python
def test_navigator_deliver_turn_is_accepted(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[chat]\nnavigator = "codex"\nnavigator_deliver = "turn"\n')
    assert load_chat_config().navigator_deliver == "turn"
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -q tests/test_config.py::test_navigator_deliver_turn_is_accepted`
Expected: FAIL, `assert 'bar' == 'turn'`.

- [ ] **Step 3: Accept the value and document it**

In `src/tandem/config.py`:

```python
_NAVIGATOR_DELIVERY = ("bar", "prompt", "turn")
```

and the field comment:

```python
    navigator_deliver: str = "bar"      # "bar": you see it; "prompt": it also rides your next prompt;
                                        # "turn": the review is a turn both models share + one follow-up
```

In `docs/configuration.md`, replace the two `navigator_deliver` comment lines with:

```toml
# navigator_deliver = "bar"    # "bar": the note is shown to you and rides only prompts you route to
#                              # the navigator; "prompt": it also rides your next prompt to any harness;
#                              # "turn": the review runs as a turn on the navigator's shared session,
#                              # synced into the other transcript, and a concern starts one follow-up
#                              # turn on the harness it reviewed (what `tandem --review` uses)
```

and append this paragraph after the navigator paragraph:

```markdown
With `navigator_deliver = "turn"` (what `tandem --review` selects for one
launch) the review is not a private aside: it runs as a real turn on the
navigator's own session, so its prompt, the files it read and its verdict
land in both transcripts, and when it flags something the reviewed harness
takes one more turn, with the verdict as its prompt, before you type again.
One round per reviewed turn; the follow-up is not reviewed. It costs one
navigator turn per reviewed turn, plus one executor turn when the navigator
speaks, and the window is busy for the length of the review.
```

- [ ] **Step 4: Run the config tests**

Run: `uv run pytest -q tests/test_config.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py docs/configuration.md tests/test_config.py
git commit -m "Config: navigator_deliver accepts \"turn\""
```

---

### Task 2: `--review` selects `turn` delivery

**Files:**
- Modify: `src/tandem/cli.py:180-190` (`_review_config`)
- Test: `tests/test_cli.py` (the `# -- --review` section, line 1055 on)

**Interfaces:**
- Produces: `--review` → `cfg.navigator_deliver == "turn"`; `--no-review` still only clears `navigator`.

- [ ] **Step 1: Write the failing tests**

Append to the `--review` section of `tests/test_cli.py`:

```python
def test_review_flag_selects_turn_delivery(homes, ok_versions, chat_cfgs):
    result = click.testing.CliRunner().invoke(cli.main, ["--review"])
    assert result.exit_code == 0, result.output
    assert [(c.navigator, c.navigator_deliver) for c in chat_cfgs] == [("codex", "turn")]


def test_review_flag_selects_turn_delivery_over_a_configured_mode(homes, ok_versions, chat_cfgs):
    _config('[chat]\nnavigator = "codex"\nnavigator_deliver = "prompt"\n')
    result = click.testing.CliRunner().invoke(cli.main, ["--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator_deliver for c in chat_cfgs] == ["turn"]


def test_no_review_flag_leaves_the_delivery_mode_alone(homes, ok_versions, chat_cfgs):
    _config('[chat]\nnavigator = "codex"\nnavigator_deliver = "prompt"\n')
    result = click.testing.CliRunner().invoke(cli.main, ["--no-review"])
    assert result.exit_code == 0, result.output
    assert [(c.navigator, c.navigator_deliver) for c in chat_cfgs] == [("", "prompt")]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_cli.py -k "turn_delivery or leaves_the_delivery"`
Expected: the first two FAIL on `navigator_deliver == "bar"` / `"prompt"`; the third passes already (keep it as the guard).

- [ ] **Step 3: Set the mode in `_review_config`**

```python
    if review is True:
        return replace(cfg, navigator=_reviewer(executing, participants, cfg),
                       navigator_deliver="turn", navigator_invalid="")
```

Update the `_review_option` help string to:

```python
    help="Review mode: the harness not taking the first prompt reviews each of the other's turns "
         "as a shared turn, and a concern starts one follow-up turn (the chat navigator, "
         "deliver = turn) [default: the [chat] navigator config keys].")
```

- [ ] **Step 4: Run the CLI tests**

Run: `uv run pytest -q tests/test_cli.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_cli.py
git commit -m "--review selects turn delivery"
```

---

### Task 3: `TurnStarted.kind`/`peer` and the two new headers

**Files:**
- Modify: `src/tandem/chat/events.py:82-87` (`TurnStarted`)
- Modify: `src/tandem/chat/render.py:253-270` (`Screen.turn_started`)
- Test: `tests/test_chat_render.py`

**Interfaces:**
- Produces: `TurnStarted(harness, model, prompt, carried="", kind="", peer="")` where `kind ∈ {"", "review", "followup"}` and `peer` is the round's other harness. Renderer: review → `"{harness} reviewing {peer}'s turn"`, no prompt echoed; followup → `"{peer} → {harness}[ · model]"` + the dim `+ navigator note:` line, no prompt echoed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_render.py` after `test_turn_and_tool_rows`:

```python
def test_a_review_turn_header_names_both_harnesses_and_echoes_no_prompt(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("codex", "gpt-5.5", "[tandem navigator] You are reviewing…",
                               kind="review", peer="claude"))
    s.tool_started(ToolStarted("c1", "Read", "s.py"))
    s.tool_finished(ToolFinished("c1", True, ""))
    s.turn_finished(TurnFinished("completed", ""))
    t = out.text()
    assert "codex reviewing claude's turn\r\n" in t
    assert "You are reviewing" not in t and "you →" not in t
    assert "  ▸ Read s.py\r\n" in t


def test_a_followup_turn_header_is_peer_to_harness_plus_the_note(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("claude", "opus", "[tandem navigator] codex reviewed your previous turn…",
                               carried="bad loop", kind="followup", peer="codex"))
    t = out.text()
    assert "codex → claude · opus\r\n" in t
    assert "  + navigator note: bad loop\r\n" in t
    assert "[tandem navigator]" not in t and "you →" not in t
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_chat_render.py -k "review_turn_header or followup_turn_header"`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'kind'`.

- [ ] **Step 3: Extend the event and the painter**

`src/tandem/chat/events.py`:

```python
@dataclass(frozen=True)
class TurnStarted:
    harness: str
    model: str
    prompt: str
    carried: str = ""   # a navigator note's summary when one rode this prompt; "" otherwise
    kind: str = ""      # "" for a prompt; "review" | "followup": the two turns of a review round
    peer: str = ""      # the round's other harness: whose turn a review reads, who asked for a follow-up
```

`src/tandem/chat/render.py`, `turn_started` — replace from `label = ...` to the end of the method:

```python
        self.line()
        if ev.kind == "review":
            # the prompt is tandem's own text and the diff; nothing to echo
            self.line(self._bold(f"{ev.harness} reviewing {ev.peer}'s turn"))
            return
        who = f"{ev.peer} → {ev.harness}" if ev.kind == "followup" else f"you → {ev.harness}"
        label = who + (f" · {ev.model}" if ev.model else "")
        prompt = _safe(ev.prompt)
        if ev.kind == "followup":
            self.line(self._bold(label))       # the note it carries was painted as the verdict row
        elif "\n" in prompt:
            self.line(self._bold(label))
            self.print(prompt + "\n")
        else:
            self.line(self._bold(label) + "  " + prompt)
        if ev.carried:
            self.line(self._dim(f"  + navigator note: {_safe(ev.carried)}"))
```

(Keep the `self.line()` that precedes the label where it was; the block above starts with it.)

- [ ] **Step 4: Run the renderer tests**

Run: `uv run pytest -q tests/test_chat_render.py tests/test_chat_window.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/events.py src/tandem/chat/render.py tests/test_chat_render.py
git commit -m "Chat: TurnStarted.kind/peer and the review / follow-up headers"
```

---

### Task 4: Claude runtime `review=` parameter

**Files:**
- Modify: `src/tandem/chat/runtime/claude.py:116-150` (`__init__`, `argv`), `:267-290` (`run_turn`)
- Modify: `src/tandem/chat/reviewers.py:20-24, 121-126` (use the shared constant)
- Test: `tests/test_chat_claude.py`

**Interfaces:**
- Produces: `ClaudeRuntime.argv(native_id, fresh, model, cfg=None, review=None)` and `ClaudeRuntime.run_turn(session, native_id, prompt, model, emit, answers, command="", review=None)`. `review` is the JSON schema dict; when given, the turn is read-only, mode `ask`, and replies through `--json-schema` (`TurnOutcome.structured`). Exposes `REVIEW_ARGS: list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_claude.py`:

```python
def test_argv_for_a_review_turn_is_read_only_with_the_schema_and_no_fork():
    argv = ClaudeRuntime(ChatConfig(skip_permissions=True)).argv(
        "sid-1", fresh=False, model="", review={"type": "object"})
    assert "--fork-session" not in argv and "--resume" in argv
    assert argv.count("--permission-mode") == 1
    assert argv[argv.index("--permission-mode") + 1] == "default"
    i = argv.index("--allowedTools"); assert argv[i + 1:i + 4] == ["Read", "Grep", "Glob"]
    j = argv.index("--disallowedTools"); assert argv[j + 1:j + 7] == ["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task"]
    assert argv[argv.index("--max-turns") + 1] == "4"
    assert argv[argv.index("--json-schema") + 1] == '{"type": "object"}'


def test_argv_without_review_is_unchanged():
    rt = ClaudeRuntime(ChatConfig())
    assert rt.argv("sid-1", fresh=False, model="") == rt.argv("sid-1", fresh=False, model="", review=None)
    assert "--json-schema" not in rt.argv("sid-1", fresh=False, model="")


def test_run_turn_hands_review_to_argv(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "review")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "sid-1", "review", "", rec.emit, rec, review={"type": "object"})
    argv = json.loads((env.tmp / "argv.json").read_text())
    assert "--json-schema" in argv and "--fork-session" not in argv and "--allowedTools" in argv
    assert out.status == "completed" and out.structured is not None
```

(`FAKE_CLAUDE_SCENARIO=review` is the fake's scenario that answers with a JSON delta and a `structured_output` result; see the docstring at the top of `tests/fakes/fake_claude.py`.)

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_chat_claude.py -k "review_turn_is_read_only or without_review or hands_review"`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'review'`.

- [ ] **Step 3: Implement**

In `src/tandem/chat/runtime/claude.py`, above `class ClaudeRuntime`:

```python
# a review turn: read-only by allowlist, by denylist and by permission mode,
# and bounded — the diff it would reach for with git is already in its prompt
REVIEW_ARGS = ["--permission-mode", "default",
               "--allowedTools", "Read", "Grep", "Glob",
               "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task",
               "--max-turns", "4"]
```

`argv`:

```python
    def argv(self, native_id: str, fresh: bool, model: str, cfg=None,
             review: dict | None = None) -> list[str]:
        cfg = cfg if cfg is not None else self.cfg
        if review is not None:
            cfg = cfg.with_mode("ask")     # a review never bypasses, plans or accepts edits
        argv = [*self.binary, "-p", "--session-id" if fresh else "--resume", native_id,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-prompt-tool", "stdio",
                "--setting-sources", ",".join(cfg.claude_setting_sources)]
        flag = CLAUDE_MODES.get(cfg.effective_mode)
        if flag:
            # the prompt tool stays: AskUserQuestion still has to reach the window
            argv += ["--permission-mode", flag]
        if model:
            argv += ["--model", model]
        argv += self.extra_args
        if review is not None:
            argv += [*REVIEW_ARGS, "--json-schema", json.dumps(review)]
        return argv
```

`run_turn`: add `review: dict | None = None` after `command: str = ""` and change the `Popen` argv to `self.argv(native_id, fresh, model, cfg, review=review)`.

In `src/tandem/chat/reviewers.py` delete `_CLAUDE_REVIEW_TOOLS` and `_CLAUDE_DENIED_TOOLS`, import `REVIEW_ARGS` from `.runtime.claude`, and build the fork reviewer's flags as:

```python
        extra = ["--fork-session", "--json-schema", json.dumps(schema), *REVIEW_ARGS]
```

(Same flags in the same order as before, so `tests/test_chat_reviewers.py` is unchanged.)

- [ ] **Step 4: Run the runtime and reviewer tests**

Run: `uv run pytest -q tests/test_chat_claude.py tests/test_chat_reviewers.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/runtime/claude.py src/tandem/chat/reviewers.py tests/test_chat_claude.py
git commit -m "Claude runtime: per-turn review schema and read-only recipe"
```

---

### Task 5: Codex runtime `review=` parameter

**Files:**
- Modify: `src/tandem/chat/runtime/codex.py:517-560` (`run_turn`, the overrides and `turn/start`)
- Test: `tests/test_chat_codex.py`

**Interfaces:**
- Produces: `CodexRuntime.run_turn(session, native_id, prompt, model, emit, answers, command="", review=None)`. When `review` is given: `approvalPolicy: "never"`, `sandbox: "read-only"` regardless of mode or `codex_*` keys; `turn/start.outputSchema = review`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat_codex.py` after `test_an_output_schema_rides_turn_start`:

```python
def test_a_review_turn_is_read_only_never_approves_and_carries_the_schema(env):
    rt = CodexRuntime(ChatConfig(skip_permissions=True, codex_sandbox="workspace-write"),
                      binary=[sys.executable, str(FAKE)])
    rec = Recorder("allow")
    rt.run_turn(env.session, "thread-1", "review", "gpt-5.5", rec.emit, rec, review={"type": "object"})
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd,
                                           "approvalPolicy": "never", "sandbox": "read-only"}
    assert env.params("turn/start")["outputSchema"] == {"type": "object"}
    assert env.params("turn/start")["model"] == "gpt-5.5"
    # the same runtime, next turn, is the window's again
    (env.tmp / "params.jsonl").unlink()
    rt.run_turn(env.session, "thread-1", "go", "", rec.emit, rec)
    assert env.params("thread/resume")["sandbox"] == "workspace-write"
    assert "outputSchema" not in env.params("turn/start")
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -q tests/test_chat_codex.py::test_a_review_turn_is_read_only_never_approves_and_carries_the_schema`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'review'`.

- [ ] **Step 3: Implement**

In `run_turn`: add `review: dict | None = None` after `command: str = ""`. After the `if cfg.codex_sandbox:` line add:

```python
            if review is not None:
                # a review never inherits the window's mode or its codex_* keys
                overrides = {"approvalPolicy": "never", "sandbox": "read-only"}
```

and in the `turn/start` params:

```python
                turn = cp.TurnStartParams(threadId=thread_id, input=[{"type": "text", "text": prompt}],
                                          model=model or None,
                                          outputSchema=review if review is not None else self.output_schema)
```

- [ ] **Step 4: Run the codex runtime tests**

Run: `uv run pytest -q tests/test_chat_codex.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/runtime/codex.py tests/test_chat_codex.py
git commit -m "Codex runtime: per-turn review schema and read-only overrides"
```

---

### Task 6: Navigator `turn` mode: dispatch hook, `settle_round`, follow-up prompt

**Files:**
- Modify: `src/tandem/chat/navigator.py` (`Note`, `Navigator.__init__`, `turn_ended`, new `turn_mode`, `settle_round`; `DenyAll` and `Collector` move here)
- Modify: `src/tandem/chat/reviewers.py:26-51` (delete the two classes, import them)
- Test: `tests/test_chat_navigator.py`, `tests/test_chat_reviewers.py` (unchanged, must still pass)

**Interfaces:**
- Produces:
  - `Navigator(harness, cfg, reviewer, post, log, *, headroom=…, clock=…, diff=…, dispatch: Callable[[TurnFacts], None] | None = None)`; attribute `dispatch` may be set after construction.
  - `Navigator.turn_mode -> bool` (`cfg.navigator_deliver == "turn"`).
  - `Navigator.settle_round(facts: TurnFacts, verdict: Verdict) -> Note | None`.
  - `Note.followup_prompt() -> str`.
  - `navigator.DenyAll`, `navigator.Collector` (re-exported from `reviewers`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_navigator.py` (after the `make_nav`/`finished` helpers; `Evidence`, `Verdict`, `NavigatorLog`, `ReviewFinished`, `ChatConfig`, `facts_with`, `g`, `SESSION` are already there):

```python
# -- turn mode: the round runs on the dispatcher --------------------------------


def turn_cfg(**kw):
    return ChatConfig(navigator="codex", navigator_deliver="turn", **kw)


def test_turn_mode_hands_a_gated_turn_to_the_dispatcher_and_runs_no_review(tmp_path):
    handed = []
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path, cfg=turn_cfg(), dispatch=handed.append)
    facts = facts_with(paths=("a.py",))
    nav.turn_ended(facts, SESSION)
    nav.join(1)
    assert nav.turn_mode and handed == [facts]
    assert reviewer.calls == [] and posted == []
    assert NavigatorLog.read(log.path) == []          # the round logs when it settles
    assert nav.mark() == "" and nav.pending() is None and nav.take("claude") is None


def test_turn_mode_still_gates(tmp_path):
    handed = []
    nav, _, _, log = make_nav([], tmp_path, cfg=turn_cfg(), dispatch=handed.append)
    nav.turn_ended(facts_with(), SESSION)             # quiet: no paths, no failure, no claim
    assert handed == [] and NavigatorLog.read(log.path)[-1]["gate"] == "skip:quiet"


def test_turn_mode_without_a_dispatcher_logs_and_skips(tmp_path):
    nav, _, _, log = make_nav([], tmp_path, cfg=turn_cfg())
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:no-dispatcher"


def test_settle_round_logs_posts_and_returns_the_note_when_spoken(tmp_path):
    nav, _, posted, log = make_nav([], tmp_path, cfg=turn_cfg(), dispatch=lambda f: None)
    facts = facts_with(paths=("a.py",))
    v = Verdict("speak", severity="block", note="bad loop", evidence=(Evidence("s.py", 12, "w"),),
                navigator="codex", elapsed=3.0)
    note = nav.settle_round(facts, v)
    assert note is not None and note.turn_harness == "claude" and note.verdict.note == "bad loop"
    assert [type(e).__name__ for e in posted] == ["ReviewFinished"] and posted[0].verdict.spoken
    rec = NavigatorLog.read(log.path)[-1]
    assert rec["gate"] == "review" and rec["verdict"] == "speak" and rec["ts"] == note.ref
    assert nav.pending() is None                      # nothing waits for a prompt in turn mode
    assert nav.dismiss("good") is True                 # the last spoken ref is remembered
    last = NavigatorLog.read(log.path)[-1]
    assert last["kind"] == "feedback" and last["ref"] == note.ref and last["value"] == "good"
    assert nav.settle_round(facts, Verdict("clean", navigator="codex")) is None
    assert finished(posted)[-1].verdict.verdict == "clean"


def test_settle_round_dedupes_and_counts_strikes_like_the_worker(tmp_path):
    nav, _, posted, log = make_nav([], tmp_path, cfg=turn_cfg(), dispatch=lambda f: None)
    facts = facts_with(paths=("a.py",))
    ev = (Evidence("s.py", 12, "w"),)
    assert nav.settle_round(facts, Verdict("speak", severity="warn", note="x", evidence=ev)) is not None
    assert nav.settle_round(facts, Verdict("speak", severity="warn", note="x again", evidence=ev)) is None
    assert finished(posted)[-1].verdict.verdict == "dup"
    for _ in range(3):
        nav.settle_round(facts, Verdict("error", error="boom"))
    assert finished(posted)[-1].verdict.verdict == "off"
    handed = []
    nav.dispatch = handed.append
    nav.turn_ended(facts, SESSION)
    assert handed == [] and NavigatorLog.read(log.path)[-1]["gate"] == "skip:disabled"


def test_an_interrupted_review_is_logged_but_not_a_strike(tmp_path):
    nav, _, posted, log = make_nav([], tmp_path, cfg=turn_cfg(), dispatch=lambda f: None)
    facts = facts_with(paths=("a.py",))
    for _ in range(3):
        assert nav.settle_round(facts, Verdict("error", error="interrupted")) is None
    assert [r["error"] for r in NavigatorLog.read(log.path)] == ["interrupted"] * 3
    assert finished(posted)[-1].verdict.verdict == "error"       # never "off"
    handed = []
    nav.dispatch = handed.append
    nav.turn_ended(facts, SESSION)
    assert handed == [facts]


def test_a_post_that_raises_in_settle_round_still_returns_the_note(tmp_path):
    def boom(ev):
        raise RuntimeError("window gone")
    nav, _, _, log = make_nav([], tmp_path, cfg=turn_cfg(), dispatch=lambda f: None)
    nav.post = boom
    note = nav.settle_round(facts_with(paths=("a.py",)),
                            Verdict("speak", severity="block", note="n", evidence=(Evidence("s.py", 1),)))
    assert note is not None and NavigatorLog.read(log.path)[-1]["verdict"] == "speak"


def test_followup_prompt_starts_with_the_tandem_marker_and_is_never_reviewed():
    note = Note("r", "codex", "claude", Verdict("speak", severity="block", note="bad loop",
                                                 evidence=(Evidence("s.py", 12, "w"), Evidence("t.py", 3))))
    p = note.followup_prompt()
    assert p.splitlines() == [
        "[tandem navigator] codex reviewed your previous turn and flagged (block): bad loop",
        "s.py:12 — w",
        "t.py:3",
        "Act on this in this turn: fix what you agree with, and say plainly what you disagree with and why.",
    ]
    assert g(facts_with(prompt=p, paths=("s.py",))) == "skip:tandem-prompt"


def test_deny_all_and_collector_live_in_navigator_and_reviewers_reexport_them():
    from tandem.chat import navigator, reviewers
    assert reviewers.DenyAll is navigator.DenyAll and reviewers.Collector is navigator.Collector
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_chat_navigator.py -k "turn_mode or settle_round or interrupted_review or followup_prompt or reexport"`
Expected: FAIL (`unexpected keyword argument 'dispatch'`, `AttributeError: turn_mode`, `followup_prompt`, `reviewers.DenyAll is not navigator.DenyAll`).

- [ ] **Step 3: Implement**

In `src/tandem/chat/navigator.py`, add to the imports from `.events`: `ApprovalRequest, Failure, QuestionRequest` (keep the rest). After `class ReviewResult` and before `class Reviewer(Protocol)`, paste the two classes moved verbatim from `reviewers.py`:

```python
class DenyAll:
    """A review's Answers: nobody is at the keyboard for a review."""

    def approve(self, req: ApprovalRequest) -> str:
        return "deny"

    def answer(self, req: QuestionRequest) -> str:
        return ""


class Collector:
    """A review's emit sink: the final text and any failures, nothing painted."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.failures: list[str] = []

    def __call__(self, ev: LiveEvent) -> None:
        if isinstance(ev, TextDelta):
            self._parts.append(ev.text)
        elif isinstance(ev, Failure):
            self.failures.append(ev.message)

    @property
    def text(self) -> str:
        return "".join(self._parts)
```

In `reviewers.py` delete those two classes and change the navigator import to
`from .navigator import Collector, DenyAll, ReviewError, ReviewResult`
(the names stay importable from `reviewers`, which `tests/test_chat_reviewers.py` relies on).

`Note` gains:

```python
    def followup_prompt(self) -> str:
        """The whole prompt of the follow-up turn in `turn` mode. Starts with
        the `[tandem` marker, so the gate never reviews it: one round."""
        lines = [f"[tandem navigator] {self.navigator} reviewed your previous turn and flagged "
                 f"({self.verdict.severity or 'note'}): {self.verdict.note}"]
        lines += [f"{e.file}:{e.line}" + (f" — {e.why}" if e.why else "") for e in self.verdict.evidence]
        lines.append("Act on this in this turn: fix what you agree with, and say plainly what you "
                     "disagree with and why.")
        return "\n".join(lines)
```

`Navigator.__init__` gains the keyword `dispatch: Callable[[TurnFacts], None] | None = None` and stores `self.dispatch = dispatch`. Update the class docstring's first sentence to: `"""One review in flight, one pending slot (newest wins), one pending note — in bar and prompt mode. In turn mode the review is a dispatcher turn: `turn_ended` hands the facts to `dispatch` and `settle_round` takes the verdict back."""`

Add, right after `__init__`:

```python
    @property
    def turn_mode(self) -> bool:
        """`deliver = "turn"`: the review is a dispatcher turn on the shared
        session and a spoken verdict starts one follow-up turn (a round).
        Nothing here forks, runs a thread, or holds a note in that mode."""
        return self.cfg.navigator_deliver == "turn"
```

In `turn_ended`, after the `if reason:` block and before `with self._lock:`:

```python
            if self.turn_mode:
                if self.dispatch is None:
                    self.log.review(facts, "skip:no-dispatcher", None)
                else:
                    self.dispatch(facts)
                return
```

Add after `give_back`:

```python
    def settle_round(self, facts: TurnFacts, verdict: Verdict) -> Note | None:
        """Turn mode: the dispatcher ran the review as a turn and parsed the
        reply. Count, dedupe and log it as the worker would, paint the
        verdict row, and hand back the note the follow-up turn carries —
        None when there is nothing to act on. A review the user interrupted
        is logged but is not the reviewer's failure: no strike."""
        if not (verdict.verdict == "error" and verdict.error == "interrupted"):
            verdict = self._settle(verdict)
        ref = self.log.review(facts, "", verdict)
        if verdict.spoken:
            with self._lock:
                self._last_spoken_ref = ref
        try:
            self.post(ReviewFinished(self.harness, verdict))
        except Exception:
            pass                                   # the window is gone; the log has it
        return Note(ref, self.harness, facts.harness, verdict) if verdict.spoken else None
```

- [ ] **Step 4: Run the navigator and reviewer tests**

Run: `uv run pytest -q tests/test_chat_navigator.py tests/test_chat_reviewers.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/navigator.py src/tandem/chat/reviewers.py tests/test_chat_navigator.py
git commit -m "Navigator: turn mode hands the round to the dispatcher"
```

---

### Task 7: Dispatcher runs the round

**Files:**
- Modify: `src/tandem/chat/dispatch.py` (`Pending`, imports, new `start_round`, `_run`, two helpers)
- Test: `tests/test_chat_dispatch.py`

**Interfaces:**
- Consumes: `Navigator.settle_round`, `Note.followup_prompt`, `Note.summary`, `navigator.DenyAll`, `navigator.SCHEMA`, `navigator.build_prompt`, `navigator.compute_diff`, `navigator.parse_verdict` (Task 6); `run_turn(..., review=SCHEMA)` (Tasks 4, 5); `TurnStarted(kind=, peer=)` (Task 3).
- Produces: `Dispatcher.start_round(facts: TurnFacts) -> None`; `Pending(harness, model, prompt, command="", kind="", peer="", carried="", facts=None)`.

- [ ] **Step 1: Extend `FakeRuntime` and add the round tests**

In `tests/test_chat_dispatch.py`, change `FakeRuntime`:

```python
    def __init__(self, harness, env, *, block=None, fresh_id=None, half_turn=False, fail_models=False,
                 review_reply='{"verdict": "clean"}'):
        ...existing assignments...
        self.review_reply = review_reply
        self.last_answers = None

    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        call = (native_id, prompt, model)
        if command:
            call += (command,)
        if review is not None:
            call += (review,)
        self.calls.append(call)
        self.last_answers = answers
        if self.block is not None:
            self.block.wait(5)
        reply = self.review_reply if review is not None else f"{self.harness} did {prompt}"
        emit(TextDelta(reply if review is not None else f"{self.harness} says hi"))
        if self.harness == "claude":
            write_line(self.env.claude_shadow, claude_user(prompt, uuid=f"u-{len(self.calls)}"))
            if not self.half_turn:
                write_line(
                    self.env.claude_shadow,
                    claude_assistant([{"type": "text", "text": reply}], uuid=f"a-{len(self.calls)}"),
                )
        elif self.harness == "codex" and native_id:
            entries = codex_turn(prompt, reply)
            for obj in entries[:2] if self.half_turn else entries:
                write_line(self.env.codex_shadow, obj)
        ...rest unchanged...
```

(Existing assertions on `calls` tuples and on `"claude did hello there"` keep holding: a non-review call still records three items and writes `f"{harness} did {prompt}"`.)

Then append this section at the end of the file:

```python
# -- turn mode: the review round ---------------------------------------------------

from types import SimpleNamespace

from tandem.chat.events import ReviewFinished
from tandem.chat.navigator import SCHEMA, DenyAll, TurnFacts
from tandem.config import ChatConfig

SPEAK = json.dumps({"verdict": "speak", "severity": "block", "note": "bad loop",
                    "evidence": [{"file": "s.py", "line": 12, "why": "w"}]})


def facts_for(harness="claude", prompt="fix it"):
    return TurnFacts(harness, prompt, False, False, "completed", ("s.py",), 0, 0, "", 0.0, 1.0)


class RoundNavigator:
    """A turn-mode navigator with no gate of its own: the first `rounds`
    completed non-tandem turns go to the dispatcher's queue (the real gate
    would skip the quiet ones), and a settled verdict comes back as the
    note to act on. Records everything; posts nothing."""

    def __init__(self, harness="codex", rounds=1):
        self.harness = harness
        self.cfg = ChatConfig(navigator=harness, navigator_deliver="turn", navigator_model="rev-model")
        self.shadow_lock = threading.Lock()
        self.dispatch = None
        self.rounds = rounds
        self.ended, self.settled, self.rides, self.closed = [], [], [], 0
        self.log = SimpleNamespace(ridden=lambda ref, to: self.rides.append((ref, to)))

    def take(self, harness):
        return None

    def give_back(self, note):
        pass

    def turn_ended(self, facts, session):
        self.ended.append(facts)
        if facts.status == "completed" and not facts.prompt.startswith("[tandem") and self.rounds > 0:
            self.rounds -= 1
            self.dispatch(facts)

    def settle_round(self, facts, verdict):
        self.settled.append(verdict)
        return Note("ref-9", self.harness, facts.harness, verdict) if verdict.spoken else None

    def close(self):
        self.closed += 1


def round_setup(env, nav, runtimes):
    """A dispatcher wired the way run_chat wires it, whose emit pumps on
    Idle as the window does. Returns (dispatcher, events)."""
    events, holder = [], {}

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            holder["d"].pump()

    d = holder["d"] = Dispatcher(env.store, env.session, runtimes, emit, Answers(), navigator=nav)
    nav.dispatch = d.start_round
    return d, events


def starts(events):
    return [(e.harness, e.kind, e.peer, e.carried) for e in events if isinstance(e, TurnStarted)]


def test_a_round_runs_review_then_followup_ahead_of_what_was_typed(env_factory):
    env = env_factory(active="claude")
    env.store.set_pin(env.session.tandem_id, "claude", "opus")
    nav = RoundNavigator()
    gate = threading.Event()
    rts = {"claude": FakeRuntime("claude", env, block=gate, review_reply=SPEAK),
           "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        assert d.submit("fix it") == ""
        assert d.submit("next") == "queued → claude"
        gate.set()
        wait_idle(events, 4)
        # order: the prompt, its review, the follow-up, then what was typed
        assert starts(events) == [("claude", "", "", ""), ("codex", "review", "claude", ""),
                                  ("claude", "followup", "codex", "bad loop"), ("claude", "", "", "")]
        review = rts["codex"].calls[0]
        assert review[1].startswith("[tandem navigator] You are reviewing") and "s.py" in review[1]
        assert review[2] == "rev-model" and review[3] == SCHEMA
        assert isinstance(rts["codex"].last_answers, DenyAll)
        assert isinstance(rts["claude"].last_answers, Answers)
        prompts = [c[1] for c in rts["claude"].calls]
        assert prompts[0] == "fix it" and prompts[2] == "next"
        assert prompts[1].startswith("[tandem navigator] codex reviewed your previous turn and flagged (block): bad loop")
        assert prompts[1].endswith("disagree with and why.") and "s.py:12 — w" in prompts[1]
        assert rts["claude"].calls[1][2] == "opus"            # the executor's pin
        assert nav.rides == [("ref-9", "claude")]
        assert [v.verdict for v in nav.settled] == ["speak"]
        # the review's JSON was collected, not painted
        assert not any(isinstance(e, TextDelta) and "verdict" in e.text for e in events)
        # the follow-up was handed to the gate (and would be skipped there), the review was not
        assert [f.prompt[:18] for f in nav.ended] == ["fix it", "[tandem navigator]", "next"]
        assert nav.ended[1].carried_note is True
        # the review never became the default
        assert env.store.get_session(env.session.tandem_id).active == "claude"
        # both transcripts hold the round
        codex = json.dumps(list(read_jsonl(env.codex_shadow)))
        assert "[tandem navigator] You are reviewing" in codex and "bad loop" in codex
        claude = "\n".join(claude_texts(env.claude_shadow))
        assert "bad loop" in claude and "[tandem navigator] codex reviewed your previous turn" in claude
    finally:
        d.close()


def test_a_clean_review_ends_the_round_without_a_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it"); d.submit("next")
        wait_idle(events, 3)
        assert starts(events) == [("claude", "", "", ""), ("codex", "review", "claude", ""), ("claude", "", "", "")]
        assert [v.verdict for v in nav.settled] == ["clean"] and nav.rides == []
        assert [c[1] for c in rts["claude"].calls] == ["fix it", "next"]
    finally:
        d.close()


def test_a_failed_review_is_an_error_verdict_and_no_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env, half_turn=True)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert [(v.verdict, v.error) for v in nav.settled] == [("error", "boom")]
        assert nav.rides == [] and len(rts["claude"].calls) == 1
        assert isinstance(events[-1], Idle)
    finally:
        d.close()


class InterruptedReview(FakeRuntime):
    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        if review is None:
            return super().run_turn(session, native_id, prompt, model, emit, answers, command=command)
        self.calls.append((native_id, prompt, model, review))
        emit(TurnFinished("interrupted", ""))
        return TurnOutcome("interrupted")


def test_an_interrupted_review_ends_the_round_without_a_strike(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": InterruptedReview("codex", env)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert [(v.verdict, v.error) for v in nav.settled] == [("error", "interrupted")]
        assert nav.rides == [] and len(rts["claude"].calls) == 1
    finally:
        d.close()


def test_an_unparsable_review_reply_is_an_error_and_no_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env, review_reply="no json here")}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert nav.settled[0].verdict == "error" and "unparsable" in nav.settled[0].error
        assert nav.rides == [] and len(rts["claude"].calls) == 1
    finally:
        d.close()


def test_a_review_whose_sync_fails_is_settled_as_an_error(env_factory, monkeypatch):
    env = env_factory()
    nav = RoundNavigator()
    real = dispatch.ops.sync_after_turn

    def flaky(store, session, target, **kw):
        if target == "codex":
            raise dispatch.SyncSetupError("codex boom")
        return real(store, session, target, **kw)

    monkeypatch.setattr(dispatch.ops, "sync_after_turn", flaky)
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert nav.settled[0].verdict == "error" and nav.settled[0].error == "sync: codex boom"
        assert nav.rides == [] and any(isinstance(e, Failure) and e.message.startswith("sync:") for e in events)
        assert isinstance(events[-1], Idle)
    finally:
        d.close()


def test_start_round_after_close_queues_nothing(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, rts, lambda ev: None, Answers(), navigator=nav)
    d.close()
    d.start_round(facts_for())
    assert not d.queue


def test_start_round_puts_the_review_ahead_of_the_queue(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, rts, lambda ev: None, Answers(), navigator=nav)
    try:
        d.queue.append(dispatch.Pending("claude", "", "typed"))
        d.start_round(facts_for())
        first = d.queue[0]
        assert first.kind == "review" and first.harness == "codex" and first.peer == "claude"
        assert first.model == "rev-model" and first.facts.prompt == "fix it"
        assert d.queue[1].prompt == "typed"
    finally:
        d.close()


def test_a_route_typed_during_the_review_outlives_the_followup(env_factory):
    """`/codex` sent while codex reviews claude moves the default at once.
    The follow-up that runs next was queued by tandem, not typed, so it
    must not move the default back — even though it starts after that
    route and so carries a fresh spoken snapshot."""
    env = env_factory(active="claude")
    nav = RoundNavigator()
    gate = threading.Event()
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK),
           "codex": FakeRuntime("codex", env, block=gate, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 1)                            # claude ran; the review is now blocked in codex
        deadline = time.monotonic() + 5
        while not rts["codex"].calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rts["codex"].calls, "the review turn never started"
        assert d.submit("/codex").startswith("default → codex")
        gate.set()
        wait_idle(events, 3)
        assert starts(events)[1:] == [("codex", "review", "claude", ""), ("claude", "followup", "codex", "bad loop")]
        assert env.store.get_session(env.session.tandem_id).active == "codex"
    finally:
        d.close()


def test_the_mirror_round_reviews_on_claude_and_follows_up_on_codex(env_factory):
    env = env_factory(active="codex")
    nav = RoundNavigator(harness="claude")
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 3)
        assert starts(events) == [("codex", "", "", ""), ("claude", "review", "codex", ""),
                                  ("codex", "followup", "claude", "bad loop")]
        assert rts["claude"].calls[0][3] == SCHEMA and isinstance(rts["claude"].last_answers, DenyAll)
        assert rts["codex"].calls[1][1].startswith("[tandem navigator] claude reviewed your previous turn")
        assert env.store.get_session(env.session.tandem_id).active == "codex"
    finally:
        d.close()
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_chat_dispatch.py -k "round or mirror"`
Expected: FAIL with `AttributeError: 'Dispatcher' object has no attribute 'start_round'`. Then run the whole file: `uv run pytest -q tests/test_chat_dispatch.py` — every pre-existing test must still pass with the extended `FakeRuntime` before you go on.

- [ ] **Step 3: Implement**

`src/tandem/chat/dispatch.py` imports:

```python
import time
...
from .events import (Answers, Failure, Idle, LiveEvent, Notice, TextDelta, TurnFinished, TurnOutcome,
                     TurnStarted, Verdict)
from .navigator import SCHEMA, DenyAll, FactsCollector, build_prompt, compute_diff, parse_verdict
```

(Keep whatever else the existing `.events` import lists.)

`Pending`:

```python
@dataclass
class Pending:
    harness: str
    model: str
    prompt: str
    command: str = ""      # "" for a prompt; "compact" | "models" for a window command
    kind: str = ""         # "" for a prompt; "review" | "followup": the two turns of a review round
    peer: str = ""         # the round's other harness (TurnStarted.peer)
    carried: str = ""      # a follow-up: the note summary the header shows
    facts: object = None   # a review: the TurnFacts of the turn it reviews
```

Module-level helper, after `_close_note`:

```python
def _mute_text(forward: Callable[[LiveEvent], None], sink: list[str]) -> Callable[[LiveEvent], None]:
    """A review turn's emit: its reply is the JSON verdict, collected for
    the parser and never painted; every other event goes through."""
    def emit(ev: LiveEvent) -> None:
        if isinstance(ev, TextDelta):
            sink.append(ev.text)
        else:
            forward(ev)
    return emit
```

`start_round`, after `pump`:

```python
    def start_round(self, facts) -> None:
        """The navigator's turn mode: queue a review of the turn `facts`
        describe ahead of anything typed. Called from the worker that ran
        that turn, before it declares itself idle, so nothing typed can
        start between the turn and its review."""
        nav = self.navigator
        with self._lock:
            if self._closed or nav is None:
                return
            self.queue.appendleft(Pending(nav.harness, nav.cfg.navigator_model, "",
                                          kind="review", peer=facts.harness, facts=facts))
```

`_run` — the full method replaces the existing one:

```python
    def _run(self, item: Pending, spoken: int) -> None:
        harness = item.harness
        if item.command == "models":
            self._list_models(item)
            return
        ran = False
        nav = self.navigator
        review = item.kind == "review"
        started = time.monotonic()
        # a note the navigator left rides this prompt as a trailer — taken
        # now, not at submit, so a note that lands while a prompt is queued
        # still reaches it. A command turn (a compact) carries none, and a
        # round's own turns carry none: the follow-up IS the note.
        note = nav.take(harness) if nav is not None and not item.command and not item.kind else None
        prompt = item.prompt + note.trailer() if note is not None else item.prompt
        if review:
            # the diff is read here, on the worker, after the reviewed turn synced
            prompt = build_prompt(item.facts, compute_diff(self.session.cwd, item.facts.paths,
                                                           item.facts.commands))
        carried = note.summary if note is not None else item.carried
        self.emit(TurnStarted(harness, item.model, prompt if review else item.prompt,
                              carried=carried, kind=item.kind, peer=item.peer))
        facts = None
        emit = self.emit
        text: list[str] = []
        lock = nav.shadow_lock if nav is not None else contextlib.nullcontext()
        outcome = None
        synced = False
        settled = False
        err = ""
        try:
            if self._first_turn is not None:
                self._first_turn()
                self._first_turn = None
            # read after the seeding: a turn that gets this far has its
            # shadows, so its review is not skipped as a first turn
            first = self._first_turn is not None
            if nav is not None and not item.command and not review:
                facts = FactsCollector(harness, item.prompt, note is not None or item.kind == "followup",
                                       first, self.emit)
                emit = facts.emit
            elif review:
                emit = _mute_text(self.emit, text)
            problems = self._validate(harness)
            if problems:
                err = f"{harness} transcript: " + "; ".join(problems)
                self.emit(Failure(err))
                self.emit(TurnFinished("failed", ""))
                self._give_back(note)
                return
            session = self.session
            if session.native_id(harness):
                # prepare_turn also seeds any participant whose harness has
                # never run (a fresh session's active claude has no file yet),
                # and hands back the session that knows the ids it minted.
                # Under the navigator's lock: a claude review fork reads the
                # shadow this drains into.
                with lock:
                    session = self.session = ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below. Nothing needs seeding there
            # either: an active codex with no id is the only harness a fresh
            # pairing leaves fileless, and every other side already has one.
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), prompt, item.model, emit,
                DenyAll() if review else self.answers, command=item.command,
                **({"review": SCHEMA} if review else {}))
            ran = True      # from here on the runtime has emitted its own TurnFinished
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target becomes the default — its file holds the turn, partial
            # or not — unless the user named another harness since this turn
            # started: a bare `/codex` typed while claude worked is the later word.
            # A round's turns never take it: the review is an aside, and the
            # follow-up was queued by tandem, not typed — a `/codex` sent while
            # the review ran predates it and must outlive it
            if self._spoken == spoken and not item.kind:
                self._set_default(harness)
            else:
                self._reload_session()          # keep the ids this turn minted alongside that word
            # A turn that did not complete can have recorded the prompt and no
            # answer (a model call that 401s does exactly that). Synced
            # outward as-is it leaves every other session ending on a user
            # message, which opencode's dry-resume check rejects — wedging
            # every later turn there. Close it in the shadows as we sync.
            quarantine_pre = self._failed_turns(harness)
            with lock:
                ops.sync_after_turn(self.store, self.session, harness,
                                    close_note=_close_note(harness, outcome))
            synced = True       # only a turn the shadows received is worth reviewing
            self._report_quarantine(harness, quarantine_pre)
            self.store.touch_used(self.session.tandem_id)
            if self._add_meters is not None:
                self._add_meters(self.session)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
            if review:
                self._settle_review(item, outcome, "".join(text), started)
                settled = True
        except SyncSetupError as exc:
            err = f"sync: {exc}"
            self.emit(Failure(err))
            self._finish_unrun(ran)
            if not ran:
                self._give_back(note)
        except Exception as exc:                       # a runtime bug must not kill the window
            err = f"{harness}: {type(exc).__name__}: {exc}"
            self.emit(Failure(err))
            self._finish_unrun(ran)
            if not ran:
                self._give_back(note)
        finally:
            # the navigator hears the turn before the dispatcher frees itself:
            # in turn mode it queues the review from inside turn_ended, and a
            # prompt submitted in between must land behind it
            if facts is not None and outcome is not None and synced:
                try:
                    nav.turn_ended(facts.finish(outcome.status), self.session)
                except Exception:
                    pass                               # the navigator must never take the window down
            if review and not settled:
                self._fail_review(item, err or "review ended without a verdict", started)
            # free before the announcement: a window that pumps straight out
            # of this Idle — even synchronously, on this thread — must find
            # the dispatcher idle, or the queued turn stalls until the next
            # submit and then runs out of order
            with self._lock:
                self._current = None
                self._running = False
            self.emit(Idle())
```

The two helpers, after `_give_back`:

```python
    def _settle_review(self, item: Pending, outcome: TurnOutcome, text: str, started: float) -> None:
        """A review turn ended and synced: parse the reply, let the navigator
        settle and paint it, and queue the follow-up when it spoke."""
        nav = self.navigator
        base = dict(navigator=nav.harness, model=item.model, elapsed=time.monotonic() - started)
        if outcome.status == "interrupted":
            verdict = Verdict("error", error="interrupted", **base)
        elif outcome.status != "completed":
            verdict = Verdict("error", error=(outcome.error or f"{item.harness} review {outcome.status}")[:200],
                              **base)
        else:
            verdict = parse_verdict(outcome.structured, text, **base)
        note = nav.settle_round(item.facts, verdict)
        if note is None:
            return
        executor = item.facts.harness
        followup = Pending(executor, self.pin(executor), note.followup_prompt(),
                           kind="followup", peer=nav.harness, carried=note.summary)
        with self._lock:
            if self._closed:
                return
            self.queue.appendleft(followup)
        nav.log.ridden(note.ref, executor)

    def _fail_review(self, item: Pending, error: str, started: float) -> None:
        """A review turn that never reached the parser: the round ends on an
        error verdict the navigator logs and counts."""
        nav = self.navigator
        try:
            nav.settle_round(item.facts, Verdict("error", error=error[:200], navigator=nav.harness,
                                                 model=item.model, elapsed=time.monotonic() - started))
        except Exception:
            pass                                       # the navigator must never take the window down
```

Check `test_the_shadow_lock_is_held_across_prepare_and_sync` and `test_facts_reach_the_navigator_after_sync` still pass: the lock usage is unchanged and `turn_ended` still precedes `Idle`.

- [ ] **Step 4: Run the dispatcher tests, then the whole suite**

Run: `uv run pytest -q tests/test_chat_dispatch.py`
Expected: all pass.
Run: `uv run pytest -q`
Expected: all pass (the window tests' `StubDispatcher` has no `start_round`; Task 8 wires it only through `run_chat`).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/dispatch.py tests/test_chat_dispatch.py
git commit -m "Dispatcher: the review round (review turn, follow-up turn)"
```

---

### Task 8: Window wiring and `/status`

**Files:**
- Modify: `src/tandem/chat/window.py:556-558` (`run_chat`, after the `Dispatcher(...)` line)
- Test: `tests/test_chat_window.py`

**Interfaces:**
- Consumes: `Navigator.dispatch` attribute (Task 6), `Dispatcher.start_round` (Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_window.py`, after `test_status_names_the_navigator`:

```python
def test_status_names_turn_delivery(env_factory):
    env = env_factory()
    w, d, out, _ = make_nav_window(env, cfg=ChatConfig(navigator="codex", navigator_deliver="turn"))
    w.handle_input(b"/status\r")
    assert "navigator codex · turn" in out.text()


def test_run_chat_wires_the_navigator_to_the_dispatcher(env_factory, monkeypatch):
    """A Navigator built by run_chat can hand a round to the dispatcher: its
    dispatch hook is the dispatcher's start_round."""
    from tandem.chat import window as window_mod
    built = []
    real = window_mod.Navigator

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k); built.append(self)

    class StubReviewer:
        def __init__(self, harness): self.harness = harness
        def review(self, *a, **k): return None
        def close(self): pass

    monkeypatch.setattr(window_mod, "Navigator", Spy)
    monkeypatch.setattr(window_mod, "make_reviewer", lambda h, cfg, store, **k: StubReviewer(h))
    env = env_factory()
    hermetic_frame()

    def launch(**kwargs):
        return run_chat(env.session, env.store, ChatConfig(navigator="codex", navigator_deliver="turn"), **kwargs)

    code, text = drive_chat(env, launch=launch, ping=False)
    assert code == 0 and len(built) == 1
    assert built[0].turn_mode and built[0].dispatch is not None
    assert built[0].dispatch.__name__ == "start_round"
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_chat_window.py -k "turn_delivery or wires_the_navigator"`
Expected: the first passes already (the status line prints whatever the mode is); the second FAILS on `dispatch is not None`.

- [ ] **Step 3: Wire it**

In `run_chat`, right after `dispatcher = Dispatcher(...)`:

```python
    if navigator is not None:
        navigator.dispatch = dispatcher.start_round   # turn mode: the round runs on the dispatcher
```

- [ ] **Step 4: Run the window tests and the suite**

Run: `uv run pytest -q tests/test_chat_window.py && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/window.py tests/test_chat_window.py
git commit -m "Chat window: hand the navigator the dispatcher's start_round"
```

---

### Task 9: Live gate on the real CLIs

**Files:**
- Create (throwaway, outside the repo): `/tmp/review-turn-gate/gate.py`
- Read: `docs/specs/2026-09-26-review-turn-design.md` § Testing

No unit test; this task's deliverable is the evidence pasted into the PR body. It needs the real `claude` and `codex` binaries, a logged-in account for each, and `tmux`.

- [ ] **Step 1: A scratch repo with a bug the executor will write**

```bash
rm -rf /tmp/review-turn-gate && mkdir -p /tmp/review-turn-gate/repo && cd /tmp/review-turn-gate/repo
git init -q && printf 'def total(xs):\n    return sum(xs)\n' > s.py && git add s.py && git commit -qm init
export TANDEM_HOME=/tmp/review-turn-gate/home
```

- [ ] **Step 2: Run the round under `tandem --review` (executor claude, reviewer codex)**

Drive the window in tmux from `/tmp/review-turn-gate/repo`:

```bash
tmux new-session -d -s rt -x 140 -y 40 -c /tmp/review-turn-gate/repo "TANDEM_HOME=$TANDEM_HOME tandem --review"
sleep 8
tmux send-keys -t rt 'In s.py add a function last(xs) that returns the last element as xs[len(xs)] and say done.' Enter
```

Wait for the round (poll `tmux capture-pane -p -t rt` every 5 s, up to 4 min) until the pane shows, in order:

1. `you → claude  In s.py add …`
2. `codex reviewing claude's turn` followed by tool rows
3. either `codex reviewed · no concerns` or `codex ⚑ block …` with the off-by-one at `s.py:<line>`
4. when flagged: `codex → claude` and a claude turn that changes `xs[len(xs)]` to `xs[-1]`

Then `/status` must print `navigator codex · turn`. Save the pane: `tmux capture-pane -p -S -200 -t rt > /tmp/review-turn-gate/pane-claude.txt`. Quit with `/quit`.

- [ ] **Step 3: Check both transcripts hold the three turns**

```bash
cd /tmp/review-turn-gate && TANDEM_HOME=$TANDEM_HOME tandem sessions -n 1
TANDEM_HOME=$TANDEM_HOME tandem navigator log
```

`tandem navigator log` must show one `review` line with the verdict and, when spoken, a `ridden → claude` line. Then, with the session's claude transcript path and codex rollout path from `tandem sessions` (or `tandem doctor`), confirm:

```bash
grep -c "tandem navigator\] You are reviewing" <claude transcript>     # ≥ 1: the review prompt synced in
grep -c '"verdict"' <claude transcript>                               # ≥ 1: the JSON verdict synced in
grep -c "tandem navigator\] codex reviewed your previous turn" <claude transcript>   # 1 when flagged
grep -c "tandem navigator\] You are reviewing" <codex rollout>        # ≥ 1: the review ran on the shared session
ls ~/.codex/sessions/**/rollout-*.jsonl | wc -l                        # unchanged before/after: no fork left behind
```

- [ ] **Step 4: The mirror: `tandem --on codex --review`**

Repeat Steps 2 and 3 with `tandem --on codex --review`, expecting `claude reviewing codex's turn`, `claude → codex`, `/status` → `navigator claude · turn`, and no new file under `~/.claude/projects/<slug>/` beyond the session's own transcript (no `--fork-session` file).

- [ ] **Step 5: Ctrl-C during the review**

Start a third window with `tandem --review`, send a prompt that edits a file, and when the pane shows `codex reviewing claude's turn`, send `C-c`. Expect the review row to end `interrupted`, no `codex → claude` turn, the composer usable, and `tandem navigator log` to show the review with `error: interrupted`. Send another editing prompt and confirm a review runs again (no strike was counted).

- [ ] **Step 6: Record**

Paste the three pane captures and the grep counts into the PR body under "Live gate". Delete `/tmp/review-turn-gate` afterwards.

---

### Task 10: Open the PR

**Files:** none in the repo (there is no changelog file; the release-notes line lives in the PR body, as for PRs #83 and #85).

- [ ] **Step 1: Run the whole suite one last time**

Run: `uv run pytest -q`
Expected: all pass. Note the count for the PR body.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin review-turn
gh pr create --title "Review mode: the review as a shared turn, then one follow-up" --body "$(cat <<'EOF'
## What

Under `tandem --review` the reviewer's verdict is no longer a private aside. The review runs as a real turn on the reviewer's shared session, so its prompt, the files it read and its JSON verdict land in both transcripts through the normal sync. When it flags something, the reviewed harness takes one follow-up turn with the verdict as its prompt. One round per reviewed turn; the follow-up is never reviewed.

New `[chat] navigator_deliver = "turn"`; `--review` selects it for one launch. `bar` and `prompt` (the private-fork path) are unchanged.

Spec: `docs/specs/2026-09-26-review-turn-design.md`. Plan: `docs/plans/2026-09-27-review-turn.md`.

## How

- `Navigator.turn_ended` in turn mode hands the facts to `Dispatcher.start_round`, which queues a review item ahead of anything typed, before the worker frees itself.
- The review item runs through the ordinary turn pipeline on the reviewer's session: read-only, approvals denied, verdict schema per turn (`run_turn(..., review=SCHEMA)` on both runtimes), reply collected instead of painted, never the default harness.
- `Navigator.settle_round` counts, dedupes, logs and paints the verdict; a spoken one queues the follow-up. Its prompt starts with `[tandem navigator]`, which the gate already skips.
- `TurnStarted.kind`/`peer` give the window its two new headers: `codex reviewing claude's turn` and `codex → claude`.

## Live gate

<paste the three pane captures and the grep counts from plan Task 9>

## Tests

`uv run pytest -q`: <count> passed.

## Release notes

- `tandem --review` now runs the reviewer's verdict as a real turn on its shared session (synced into the other transcript) and, when it flags something, gives the reviewed harness one follow-up turn. New `[chat] navigator_deliver = "turn"`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
