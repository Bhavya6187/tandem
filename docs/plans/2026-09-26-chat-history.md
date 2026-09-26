# Chat history and Ctrl-R — Implementation Plan (parity PR 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prompts typed in the chat window survive it — Up/Down recalls them in every later window opened in the same directory, and Ctrl-R searches them.

**Architecture:** A `chat_history` table in the existing sqlite state store, keyed by working directory. The window seeds the composer's in-memory history from it at open and appends every `Submit` to it. The composer, still pure, gains a `search` mode entered by Ctrl-R that filters the history and hands the pick back to the draft; the search row replaces the composer row while it is active.

**Tech Stack:** Python 3.12, sqlite3, pytest, the window's pty test driver in `tests/test_chat_window.py`.

**Spec:** `docs/specs/2026-09-26-chat-parity-design.md`, section 1 ("History"). Sections 3 and 4 are later PRs and are out of scope here.

## Global Constraints

- No new dependency. The table joins `_SCHEMA` additively: nothing in `_schema_stale` changes and no existing database is moved aside.
- The composer stays pure: bytes in, actions out, no I/O. The window does every store call.
- Only `Submit` is recorded. `Answer` (approval keys, question answers) never reaches history, and the window never reads `composer.text` to record anything.
- Nothing already printed in the scroll region is redrawn; the search row lives in the composer rows.
- Commit messages follow the repo's imperative style and end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run the suite with `uv run pytest -q`; the baseline on `main` at `126dbd1` is 1457 passed.

## Review Focus

Inputs the spec implies but no test would otherwise exercise. Each is pinned to a test in the task that owns the code.

1. An approval request arrives while the user is mid-search (the turn is still running): the search must end, the original draft must not be lost into the answer, and the key that answers must not be read as query text. (Task 3: `test_an_approval_arriving_mid_search_ends_the_search_first`.)
2. A multi-line history entry shown as a search candidate must stay on one row. (Task 3: `test_a_multiline_candidate_is_shown_on_one_row`.)
3. Ctrl-R with an empty history: the row must still make sense and Enter must restore the draft. (Task 3: `test_search_with_no_history_at_all`.)
4. The store write fails (disk full, locked db): the prompt must still run and the window must say the prompt was not saved. (Task 4: `test_a_failed_history_write_is_a_note_not_a_lost_turn`.)
5. Up/Down after recalling a seeded multi-line entry must not inherit a stale column from an earlier vertical move. (Task 2: `test_recalling_a_multiline_entry_forgets_the_old_column_goal`.)

---

### Task 1: The `chat_history` table

**Files:**
- Modify: `src/tandem/state.py` (`_SCHEMA`, two new methods after `set_pin`)
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `StateStore.recent_prompts(cwd: str, limit: int) -> list[str]` (oldest first, newest last); `StateStore.add_prompt(cwd: str, text: str) -> None`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py`:

```python
def test_prompts_round_trip_oldest_first_with_a_limit(tmp_path):
    with make_store(tmp_path) as store:
        assert store.recent_prompts("/proj", 10) == []
        for text in ("one", "two", "three"):
            store.add_prompt("/proj", text)
        assert store.recent_prompts("/proj", 10) == ["one", "two", "three"]
        assert store.recent_prompts("/proj", 2) == ["two", "three"]      # the newest two


def test_prompts_skip_a_consecutive_duplicate_only(tmp_path):
    with make_store(tmp_path) as store:
        for text in ("a", "a", "b", "a"):
            store.add_prompt("/proj", text)
        assert store.recent_prompts("/proj", 10) == ["a", "b", "a"]


def test_prompts_are_capped_at_500_per_cwd(tmp_path):
    with make_store(tmp_path) as store:
        for i in range(503):
            store.add_prompt("/proj", f"p{i}")
        got = store.recent_prompts("/proj", 1000)
        assert len(got) == 500 and got[0] == "p3" and got[-1] == "p502"


def test_prompts_are_per_cwd(tmp_path):
    with make_store(tmp_path) as store:
        store.add_prompt("/a", "from a")
        store.add_prompt("/b", "from b")
        assert store.recent_prompts("/a", 10) == ["from a"]
        assert store.recent_prompts("/b", 10) == ["from b"]


def test_chat_history_table_added_to_existing_db(tmp_path):
    """An older state.db without chat_history is extended in place, not moved aside."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (tandem_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,"
        " active TEXT NOT NULL, participants TEXT NOT NULL,"
        " native_session_ids TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,"
        " last_sync_at TEXT, last_used_at TEXT);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES"
        " ('abc', '/p', 'claude', '[\"claude\"]', '{}', 'now', NULL, NULL)"
    )
    conn.commit()
    conn.close()
    with StateStore(db_path=db) as store:
        assert store.get_session("abc") is not None          # not moved aside
        store.add_prompt("/p", "hello")
        assert store.recent_prompts("/p", 5) == ["hello"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_state.py -q -k "prompts or chat_history"`
Expected: 5 FAIL — `AttributeError: 'StateStore' object has no attribute 'add_prompt'`

- [ ] **Step 3: Add the table and the two methods**

In `src/tandem/state.py`, extend `_SCHEMA` after the `chat_pins` table:

```sql
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY,
    cwd TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_history_cwd ON chat_history (cwd, id);
```

Add after `set_pin`:

```python
    # -- chat history ---------------------------------------------------------

    _HISTORY_CAP = 500

    def recent_prompts(self, cwd: str, limit: int) -> list[str]:
        """The newest `limit` prompts typed in chat windows opened in `cwd`,
        oldest first — the order the composer's Up walks backwards through."""
        rows = self._conn.execute(
            "SELECT text FROM chat_history WHERE cwd = ? ORDER BY id DESC LIMIT ?",
            (cwd, max(0, limit)),
        ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def add_prompt(self, cwd: str, text: str) -> None:
        """Record a prompt for `cwd`. A repeat of the newest one is skipped,
        matching the composer's own consecutive-duplicate rule, so a seeded
        list steps like a live one; rows beyond the cap go, oldest first."""
        with self._tx():
            last = self._conn.execute(
                "SELECT text FROM chat_history WHERE cwd = ? ORDER BY id DESC LIMIT 1", (cwd,)
            ).fetchone()
            if last is not None and last["text"] == text:
                return
            self._conn.execute(
                "INSERT INTO chat_history (cwd, text, ts) VALUES (?, ?, ?)", (cwd, text, _now()))
            self._conn.execute(
                "DELETE FROM chat_history WHERE cwd = ? AND id NOT IN"
                " (SELECT id FROM chat_history WHERE cwd = ? ORDER BY id DESC LIMIT ?)",
                (cwd, cwd, self._HISTORY_CAP))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_state.py -q`
Expected: all passed (the older schema tests included)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/state.py tests/test_state.py
git commit -m "State store: a per-directory chat prompt history

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Seeding the composer, and the two reset rules

**Files:**
- Modify: `src/tandem/chat/composer.py` (`__init__`, `begin_approval`, `begin_question`, `end_answer`, `_set`)
- Test: `tests/test_chat_composer.py`

**Interfaces:**
- Produces: `Composer(history_limit=200, paths=None, commands=None, history: list[str] | None = None)`; a private `_reset_history_cursor()` used by Task 3.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_composer.py`:

```python
# -- seeded history and the reset rules ---------------------------------------


def test_a_seed_is_the_initial_history():
    c = Composer(history=["older", "newer"])
    feed(c, b"\x1b[A"); assert c.text == "newer"
    feed(c, b"\x1b[A"); assert c.text == "older"
    feed(c, "\r")                                    # submitting a recalled entry
    assert c.history == ["older", "newer", "older"]


def test_the_seed_is_copied_not_shared():
    seed = ["one"]
    c = Composer(history=seed)
    feed(c, "two\r")
    assert seed == ["one"]


def test_answering_a_question_resets_the_history_cursor():
    c = Composer(history=["one", "two", "three"])
    feed(c, b"\x1b[A"); feed(c, b"\x1b[A")           # at "two"
    c.begin_question(QuestionRequest("Which?", ("a", "b")))
    feed(c, "1")
    c.end_answer()
    feed(c, b"\x1b[A")
    assert c.text == "three"                         # fresh from the newest, not resumed at "one"


def test_an_approval_resets_the_history_cursor_and_the_draft():
    c = Composer(history=["one", "two"])
    feed(c, "dra"); feed(c, b"\x1b[A")               # draft parked, at "two"
    c.begin_approval(ApprovalRequest("command", "ls"))
    c.end_answer()
    feed(c, b"\x1b[A")
    assert c.text == "two"
    feed(c, b"\x1b[B")
    assert c.text == ""                              # the parked draft was dropped with the mode


def test_recalling_a_multiline_entry_forgets_the_old_column_goal():
    """The goal only bites when its cursor index equals the current cursor,
    so the recalled entry is 19 chars long and the vertical move leaves the
    goal at index 19, column 19."""
    c = Composer(history=["abcdefghijkl\ncdefgh"])          # 19 chars
    feed(c, "a" * 25 + "\n" + "z" * 19)                     # cursor on row 2, column 19
    feed(c, b"\x1b[A")                                       # up: cursor index 19, goal (19, 19)
    feed(c, b"\x01"); feed(c, b"\x1b[A")                    # line start, then Up recalls the entry
    assert c.text == "abcdefghijkl\ncdefgh" and c.cur == 19
    feed(c, b"\x1b[A")                                       # up inside the recalled entry, from column 6
    assert c.cur == 6                                        # column 6 — a stale goal of 19 would give 12


def test_a_step_through_history_keeps_the_cursor_and_the_draft():
    """_set must not reset _hidx/_draft: repeated Up walks older, Down returns the draft."""
    c = Composer(history=["one", "two", "three"])
    feed(c, "dra")
    feed(c, b"\x1b[A"); feed(c, b"\x1b[A"); feed(c, b"\x1b[A")
    assert c.text == "one"
    feed(c, b"\x1b[B"); feed(c, b"\x1b[B"); feed(c, b"\x1b[B")
    assert c.text == "dra"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_composer.py -q -k "seed or resets_the_history or forgets_the_old_column or keeps_the_cursor_and_the_draft"`
Expected: FAIL on the seed tests with `TypeError: Composer.__init__() got an unexpected keyword argument 'history'`; `test_a_step_through_history_keeps_the_cursor_and_the_draft` may already pass (it pins today's behavior against the change in Step 3) — that is fine.

- [ ] **Step 3: Implement**

In `src/tandem/chat/composer.py`:

```python
    def __init__(self, history_limit: int = 200,
                 paths: Callable[[], list[str]] | None = None,
                 commands: Callable[[], list[Command]] | None = None,
                 history: list[str] | None = None):
        self.buf: list[str] = []
        self.cur = 0
        self.history: list[str] = list(history or [])      # oldest first; the seed is copied
```

Replace the three mode methods:

```python
    def begin_approval(self, req: ApprovalRequest) -> None:
        self.mode, self.pending = "approval", req
        self._reset_history_cursor()

    def begin_question(self, req: QuestionRequest) -> None:
        self.mode, self.pending = "question", req
        self.buf, self.cur = [], 0
        self._reset_history_cursor()

    def end_answer(self) -> None:
        self.mode, self.pending = "prompt", None
        self.buf, self.cur = [], 0
        self._reset_history_cursor()

    def _reset_history_cursor(self) -> None:
        """A mode change ends any walk through history: the next Up starts
        from the newest entry, and no parked draft comes back. Kept out of
        `_set`, which every recall calls."""
        self._hidx, self._draft, self._goal = None, "", None
```

In `_set`, add the goal reset as the first line of the body:

```python
    def _set(self, text: str) -> None:
        self._goal = None                       # a new draft has no column to aim for
        self.buf, self.cur = list(text), len(text)
```

- [ ] **Step 4: Run the whole composer file**

Run: `uv run pytest tests/test_chat_composer.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/composer.py tests/test_chat_composer.py
git commit -m "Composer: seed history at construction; reset the cursor on mode changes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Ctrl-R search mode

**Files:**
- Modify: `src/tandem/chat/composer.py` (docstring, `__init__`, `feed`, `_typed`, `_backspace`, `_enter`, `rows`, new search helpers)
- Test: `tests/test_chat_composer.py`

**Interfaces:**
- Produces: `Composer.mode == "search"` while searching; `Composer.rows()` returns one search row in that mode.
- Consumes: `_reset_history_cursor()` from Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_composer.py`:

```python
# -- Ctrl-R search -------------------------------------------------------------

H = ["git status", "run the tests", "fix the Tests in ci", "deploy"]


def searching(history=H):
    c = Composer(history=list(history))
    return c


def test_ctrl_r_enters_search_showing_the_newest_entry():
    c = searching()
    feed(c, "dra")
    assert feed(c, b"\x12") == [] and c.mode == "search"
    rows, r, col = c.rows(60, 8)
    assert rows == ["(search) '': deploy"] and (r, col) == (0, len("(search) '"))


def test_typing_narrows_to_the_newest_match_case_insensitively():
    c = searching()
    feed(c, b"\x12"); feed(c, "test")
    rows, _, col = c.rows(60, 8)
    assert rows == ["(search) 'test': fix the Tests in ci"]
    assert col == len("(search) 'test")


def test_ctrl_r_again_steps_older_and_wraps_with_a_marker():
    c = searching()
    feed(c, b"\x12"); feed(c, "test")
    feed(c, b"\x12")
    assert c.rows(60, 8)[0] == ["(search) 'test': run the tests"]
    feed(c, b"\x12")
    assert c.rows(60, 8)[0] == ["(search, wrapped) 'test': fix the Tests in ci"]


def test_backspace_widens_the_query():
    c = searching()
    feed(c, b"\x12"); feed(c, "testx")
    assert c.rows(60, 8)[0] == ["(search) 'testx': (no match)"]
    feed(c, b"\x7f")
    assert c.rows(60, 8)[0] == ["(search) 'test': fix the Tests in ci"]


def test_enter_accepts_into_the_draft_without_submitting():
    c = searching()
    feed(c, b"\x12"); feed(c, "sta")
    assert feed(c, "\r") == []
    assert c.mode == "prompt" and c.text == "git status" and c.cur == len(c.text)
    assert feed(c, "\r") == [Submit("git status")]


def test_tab_accepts_like_enter():
    c = searching()
    feed(c, b"\x12"); feed(c, "dep"); feed(c, "\t")
    assert c.mode == "prompt" and c.text == "deploy"


def test_esc_cancels_and_restores_the_draft():
    c = searching()
    feed(c, "my draft"); feed(c, b"\x12"); feed(c, "dep")
    assert feed(c, b"\x1b") == []                    # not an Interrupt
    assert c.mode == "prompt" and c.text == "my draft" and c.cur == 8


def test_no_match_then_enter_restores_the_draft():
    c = searching()
    feed(c, "my draft"); feed(c, b"\x12"); feed(c, "zzz")
    assert c.rows(60, 8)[0] == ["(search) 'zzz': (no match)"]
    feed(c, "\r")
    assert c.text == "my draft"


def test_an_arrow_leaves_search_keeping_the_candidate_then_moves():
    c = searching()
    feed(c, b"\x12"); feed(c, "dep")
    feed(c, b"\x1b[D")                               # left
    assert c.mode == "prompt" and c.text == "deploy" and c.cur == len("deploy") - 1


def test_search_starts_a_fresh_history_walk_afterwards():
    c = searching()
    feed(c, b"\x1b[A"); feed(c, b"\x1b[A")           # at "fix the Tests in ci"
    feed(c, b"\x12"); feed(c, b"\x1b")               # in and out of search
    feed(c, b"\x1b[A")
    assert c.text == "deploy"                        # newest, not resumed


def test_ctrl_r_is_ignored_in_approval_and_question_mode():
    c = searching()
    c.begin_approval(ApprovalRequest("command", "ls"))
    assert feed(c, b"\x12") == [] and c.mode == "approval"
    c.end_answer()
    c.begin_question(QuestionRequest("Which?", ()))
    assert feed(c, b"\x12") == [] and c.mode == "question" and c.text == ""


def test_ctrl_r_with_a_picker_open_closes_it_first():
    c = Composer(history=["look at @README.md"], paths=lambda: ["README.md", "docs/"])
    feed(c, "see @RE")
    assert c.candidates == ["README.md"]
    feed(c, b"\x12")
    assert c.mode == "search" and c.candidates == []
    feed(c, b"\x1b")
    assert c.text == "see @RE" and c.candidates == []          # the dismissal holds


def test_a_pasted_ctrl_r_is_literal_text():
    c = searching()
    feed(c, b"\x1b[200~a\x12b\x1b[201~")
    assert c.mode == "prompt" and c.text == "a\x12b"


def test_a_paste_while_searching_extends_the_query():
    """Bracketed paste bypasses `_typed`; while searching it must still feed
    the query, or Enter would replace the pasted text with the unfiltered
    newest entry."""
    c = searching()
    feed(c, b"\x12")
    feed(c, b"\x1b[200~sta\x1b[201~")
    assert c.mode == "search" and c.rows(60, 8)[0] == ["(search) 'sta': git status"]
    feed(c, "\r")
    assert c.mode == "prompt" and c.text == "git status"


def test_a_pasted_newline_in_a_search_query_is_literal():
    c = Composer(history=["two\nlines", "one line"])
    feed(c, b"\x12")
    feed(c, b"\x1b[200~o\r\nl\x1b[201~")          # the terminal pastes CRLF; the draft holds LF
    assert c.rows(60, 8)[0] == ["(search) 'o⏎l': two⏎lines"]


def test_a_multiline_candidate_is_shown_on_one_row():
    c = Composer(history=["first line\nsecond line"])
    feed(c, b"\x12")
    rows, _, _ = c.rows(60, 8)
    assert rows == ["(search) '': first line⏎second line"]
    feed(c, "\r")
    assert c.text == "first line\nsecond line"


def test_search_with_no_history_at_all():
    c = Composer()
    feed(c, "keep me"); feed(c, b"\x12")
    assert c.rows(60, 8)[0] == ["(search) '': (no match)"]
    feed(c, "\r")
    assert c.mode == "prompt" and c.text == "keep me"


def test_the_search_row_is_clipped_to_the_width():
    c = Composer(history=["x" * 100])
    feed(c, b"\x12")
    rows, _, col = c.rows(30, 8)
    assert len(rows[0]) == 30 and col == len("(search) '")


def test_an_approval_arriving_mid_search_ends_the_search_first():
    c = searching()
    feed(c, "my draft"); feed(c, b"\x12"); feed(c, "dep")
    c.begin_approval(ApprovalRequest("command", "ls"))
    assert c.mode == "approval"
    assert feed(c, "y") == [Answer("allow")]          # the key answers; it is not query text
    c.end_answer()
    assert c.text == "" and c.mode == "prompt"       # the answer mode's usual clean slate
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_composer.py -q -k "search or ctrl_r or accepts_into or widens or restores_the_draft or candidate_is_shown or leaves_search"`
Expected: FAIL — `assert c.mode == "search"` fails with `'prompt'` (0x12 is currently an ignored control byte)

- [ ] **Step 3: Implement search mode**

In `src/tandem/chat/composer.py`:

Docstring — replace "Three modes." at the top with "Four modes." and add after the `question` sentence:

```
`search` is Ctrl-R over the history: the row reads `(search) 'query':
candidate`, typing narrows to the newest entry containing the query,
Ctrl-R again steps older and wraps, Enter or Tab puts the candidate in the
draft, Esc restores the draft that was there, any arrow keeps the
candidate and then moves.
```

`__init__` — add after `self._draft = ""`:

```python
        # Ctrl-R state while mode == "search": the query typed so far, the
        # history index of the candidate (None = no match), the draft to
        # restore on Esc, and whether the last step wrapped to the newest
        self._search: dict | None = None
```

`begin_approval`, `begin_question` — first line of each body:

```python
        self._leave_search(keep=False)
```

(before the `self.mode, self.pending = ...` assignment; `end_answer` needs nothing — search is never active in an answer mode.)

In `feed`, inside the byte loop, the Esc branch becomes:

```python
                if name == "paste_start":
                    self._paste = True
                elif self.mode == "search":
                    if name == "esc":
                        self._leave_search(keep=False)
                    elif name:
                        self._leave_search(keep=True)
                        self._key(name)
                elif name == "esc":
                    if self.candidates:
                        self._dismissed = self._locate()[:2]
                    else:
                        actions.append(Cancel() if self.mode != "prompt" else Interrupt())
                elif name:
                    self._key(name)
                continue
```

Still in the loop, add the Ctrl-R byte before the `0x0D` branch and route the search-mode control keys:

```python
            i += 1
            if b == 0x03:
                actions.append(CtrlC())
            elif b == 0x0C:
                actions.append(Repaint())
            elif b == 0x12:
                self._ctrl_r()
            elif self.mode == "search" and b in (0x0D, 0x09):
                self._leave_search(keep=True)          # Enter / Tab: the candidate is the draft
            elif self.mode == "search" and b in (0x7F, 0x08):
                self._search["query"] = self._search["query"][:-1]
                self._find(restart=True)
            elif self.mode == "search" and b < 0x20:
                self._leave_search(keep=True)          # any other control key: keep, then apply
                continue                               # (the key itself is dropped: Ctrl-U on a
                                                       # candidate would kill what was just found)
            elif b == 0x0D:
                self._enter(actions)
```

(Everything from `elif b == 0x0D:` on is unchanged. The printable-run branch at the end of the loop calls `_typed`, which grows the query when searching — see below.)

In `_typed`, add at the top, before the approval branch:

```python
        if self.mode == "search":
            self._search["query"] += text
            self._find(restart=True)
            return
```

In `_pasted`, the paste path that bypasses `_typed`, route the normalized
text the same way — replace its last line:

```python
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.mode == "search":
            self._search["query"] += text          # a pasted query narrows like a typed one
            self._find(restart=True)
            return
        self._insert(text)
```

(the `_paste_cr` bookkeeping above it is unchanged.) The search row shows a
newline in the query as `⏎`, so `rows` renders `_printable(s["query"].replace("\n", "⏎"))`
in the label rather than the raw query.

Add the helpers after `_history_step`:

```python
    # -- Ctrl-R search -----------------------------------------------------------

    def _ctrl_r(self) -> None:
        if self.mode == "search":
            self._find(restart=False)                # step to the next older match
            return
        if self.mode != "prompt":
            return                                   # an answer row is not a place to search
        if self.candidates:
            self._dismissed = self._locate()[:2]     # the picker closes before the search opens
        self._search = {"query": "", "pos": None, "saved": self.text, "wrapped": False}
        self._reset_history_cursor()
        self.mode = "search"
        self._find(restart=True)

    def _find(self, *, restart: bool) -> None:
        """Newest history entry containing the query, case-insensitively.
        `restart` searches from the newest; otherwise from just above the
        current candidate, wrapping to the newest once (and saying so)."""
        s = self._search
        q = s["query"].lower()
        n = len(self.history)
        start = n - 1 if restart or s["pos"] is None else s["pos"] - 1
        s["wrapped"] = False
        for i in range(start, -1, -1):
            if q in self.history[i].lower():
                s["pos"] = i
                return
        if not restart:
            for i in range(n - 1, start, -1):
                if q in self.history[i].lower():
                    s["pos"], s["wrapped"] = i, True
                    return
        s["pos"] = None

    def _leave_search(self, *, keep: bool) -> None:
        """Back to prompt mode with the candidate as the draft (`keep`), or
        with the draft that was there before Ctrl-R. A search with no match
        has nothing to keep and restores the draft either way."""
        s = self._search
        if s is None:
            return
        self._search = None
        self.mode = "prompt"
        text = self.history[s["pos"]] if keep and s["pos"] is not None else s["saved"]
        self._set(text)
        self._reset_history_cursor()
```

In `rows`, add before the `prompt = ...` line:

```python
        if self.mode == "search":
            s = self._search
            shown_q = _printable(s["query"].replace("\n", "⏎"))
            label = ("(search, wrapped) '" if s["wrapped"] else "(search) '") + shown_q + "': "
            cand = self.history[s["pos"]] if s["pos"] is not None else "(no match)"
            row = label + _printable(cand.replace("\n", "⏎"))
            return [_clip_cells(row, cols)], 0, min(len(label) - 3, cols - 1)
```

and add the module helper next to `_printable`:

```python
def _clip_cells(text: str, cells: int) -> str:
    """The longest prefix of `text` that fits in `cells` terminal cells."""
    used = 0
    for i, ch in enumerate(text):
        used += _width(ch)
        if used > cells:
            return text[:i]
    return text
```

Note on `_leave_search` and `_set`: `_set` calls `_locate()`, which returns `None` while `self.mode == "search"`; that is why the mode is set back to `"prompt"` before `_set` runs, so a restored `@` draft records its dismissal correctly.

- [ ] **Step 4: Run the whole composer file**

Run: `uv run pytest tests/test_chat_composer.py -q`
Expected: all passed — including the pre-existing `test_control_keys` (a lone Esc in prompt mode is still an Interrupt) and every picker test

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/composer.py tests/test_chat_composer.py
git commit -m "Composer: Ctrl-R searches the history

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The window records and seeds; docs; live check; PR

**Files:**
- Modify: `src/tandem/chat/window.py` (`Window.__init__`, `handle_input`, `run_chat`), `docs/configuration.md` (the `[chat]` section, after the picker paragraph), `README.md:39-41`
- Test: `tests/test_chat_window.py`

**Interfaces:**
- Consumes: `store.add_prompt`, `store.recent_prompts` (Task 1); `Composer(history=...)` (Task 2).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chat_window.py`:

```python
def test_every_submit_is_recorded_before_anything_runs(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    assert w.handle_input(b"hello there\r") is True
    assert w.handle_input(b"/status\r") is True
    assert w.handle_input(b"/quit\r") is False
    assert env.store.recent_prompts(env.session.cwd, 10) == ["hello there", "/status", "/quit"]


def test_answers_are_never_recorded(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    w.handle_event(ApprovalRequest("command", "ls"))
    w.handle_input(b"y")
    w.handle_event(QuestionRequest("Which?", ("a", "b")))
    w.handle_input(b"2")
    w.handle_event(QuestionRequest("Name?", ()))
    w.handle_input(b"free text\r")
    assert env.store.recent_prompts(env.session.cwd, 10) == []


def test_a_failed_history_write_is_a_note_not_a_lost_turn(env_factory, monkeypatch):
    env = env_factory(); w, d, out, _ = make_window(env)

    def boom(cwd, text):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.store, "add_prompt", boom)
    assert w.handle_input(b"still runs\r") is True
    assert d.submitted == ["still runs"]
    assert "history not saved" in out.text() and "disk full" in out.text()


def test_the_window_opens_with_the_directorys_history(env_factory):
    """Seeded at open: Up recalls a prompt typed in an earlier window here."""
    env = env_factory()
    hermetic_frame()
    env.store.add_prompt(env.session.cwd, "older prompt")
    code, text = drive_chat(env, keys=b"\x1b[A\r", expect=b"echo:older prompt")
    assert code == 0 and "echo:older prompt" in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_window.py -q -k "recorded or failed_history or directorys_history"`
Expected: 4 FAIL — `recent_prompts` returns `[]` where prompts are expected, the failing-write test finds no note, and the pty test times out without the echo

- [ ] **Step 3: Record and seed**

In `src/tandem/chat/window.py`, `handle_input`:

```python
    def handle_input(self, data: bytes) -> bool:
        for action in self.composer.feed(data):
            if isinstance(action, Submit):
                self._record(action.text)
                command = window_command(action.text)
```

Add the method next to `status_line`:

```python
    def _record(self, text: str) -> None:
        """Every prompt the user submits, window commands and routes
        included, goes to the directory's history before anything runs on
        it. The store is a courtesy here: a write that fails is a note."""
        try:
            self.store.add_prompt(self.session.cwd, text)
        except Exception as exc:
            self.screen.note(f"history not saved: {type(exc).__name__}: {exc}")
```

In `run_chat`, replace the composer construction:

```python
    try:
        seed = store.recent_prompts(session.cwd, 200)
    except Exception:                                   # a courtesy, never a blocker
        seed = []
    composer = Composer(paths=lambda: list_paths(session.cwd), commands=lambda: win.catalog(),
                        history=seed)
```

- [ ] **Step 4: Run the window tests, then the whole suite**

Run: `uv run pytest tests/test_chat_window.py -q`
Expected: all passed

Run: `uv run pytest -q`
Expected: all passed; count ≥ 1457 + the new tests

- [ ] **Step 5: Document**

`docs/configuration.md`, after the paragraph that ends "…as its TUI would." (the `/` picker paragraph), add:

```
Prompts you submit are kept per directory, across windows, in tandem's
own state store (the newest 500; approval keys and question answers are
never recorded). Up and Down step through them from the newest, as they
do within a window; Ctrl-R searches them: type to narrow to the newest
entry containing the text, Ctrl-R again steps to an older one (wrapping
round), Enter or Tab puts the match in the composer to edit or send, Esc
brings back what you were typing.
```

`README.md`, after "Type `/` to see the commands and routes; `/help` prints them.", add: "Up recalls earlier prompts from this directory, and Ctrl-R searches them."

- [ ] **Step 6: Live check**

From a scratch directory with `tandem` (any harness): type `alpha one`, wait for the reply, `/quit`; run `tandem` again in the same directory and press Up — the composer shows `alpha one`; Ctrl-R, type `alp` — the row reads `(search) 'alp': alpha one`; Enter, then Enter sends it. From a different directory, `tandem` and Up shows nothing from the first. Record the outcome in the PR body.

- [ ] **Step 7: Commit, push, open the PR**

```bash
git add src/tandem/chat/window.py tests/test_chat_window.py docs/configuration.md README.md
git commit -m "Chat: prompts survive the window, Up recalls them and Ctrl-R searches them

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin chat-history
gh pr create --base main --title "Chat: persistent prompt history and Ctrl-R search" --body-file /tmp/pr-body.md
```

The PR body: what changed, the spec path, the live-check outcome, the review outcome, and the closing line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

- [ ] **Step 8: Codex review of the PR**

Stage the branch diff and the changed sources under a `/private/tmp` path that does not contain the word "git" (the worktree Bash guard rejects `tandem sub` briefs naming git, and the repo path itself does), then dispatch two `tandem:gpt` reviews in one message — composer (search mode) and state+window (recording, seeding, failure path) — each naming its staged files and capped at six minutes. Verify each finding against the code before acting; fix real ones in this PR with a failing test first; record dismissed ones in the PR body.
