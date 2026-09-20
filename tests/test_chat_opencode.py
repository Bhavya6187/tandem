import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.opencode import OpencodeRuntime, TurnState, mention_parts
from tandem.config import ChatConfig

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_opencode_server import SID, FakeOpencode  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "chat" / "opencode_events.jsonl"
FAKE_SERVE = Path(__file__).parent / "fakes" / "fake_opencode_serve.py"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events, self.approvals, self.questions = [], [], []
        self._approve, self._answer = approve, answer

    def emit(self, ev): self.events.append(ev)
    def approve(self, req): self.approvals.append(req); return self._approve
    def answer(self, req): self.questions.append(req); return self._answer
    def kinds(self): return [type(e).__name__ for e in self.events]


@pytest.fixture
def fake():
    def make(scenario="tool"):
        f = FakeOpencode(scenario); made.append(f); return f
    made = []
    yield make
    for f in made:
        f.stop()


SESSION = SimpleNamespace(cwd="/tmp/proj", tandem_id="tdm-opencode")


def test_tool_turn_streams_events(fake):
    f = fake("tool"); rec = Recorder()
    rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    out = rt.run_turn(SESSION, SID, "make x", "opencode/big-pickle", rec.emit, rec)
    assert out.status == "completed"
    assert f.posts == [{"model": {"providerID": "opencode", "modelID": "big-pickle"}, "parts": [{"type": "text", "text": "make x"}]}]
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ThinkingDelta("thinking…")
    assert rec.events[1] == ToolStarted("call_1", "bash", "touch x.txt")
    assert rec.events[2] == ToolOutput("call_1", "ok\n")
    assert rec.events[3] == ToolFinished("call_1", True, "touch x.txt")
    assert rec.events[4] == TextDelta("DONE")
    assert rec.events[5] == TurnFinished("completed", "120↑ 7↓")
    rt.close()


def test_no_model_pin_posts_without_model(fake):
    f = fake("tool"); rec = Recorder()
    OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "hi", "", rec.emit, rec)
    assert "model" not in f.posts[0]


def test_model_without_provider_fails_before_posting(fake):
    f = fake("tool"); rec = Recorder()
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "hi", "big-pickle", rec.emit, rec)
    assert out.status == "failed" and f.posts == []
    assert rec.kinds() == ["Failure", "TurnFinished"]


@pytest.mark.parametrize("choice,reply", [("allow", "once"), ("always", "always")])
def test_permission_allow_variants(fake, choice, reply):
    f = fake("permission"); rec = Recorder(choice)
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("permission", "bash: touch x.txt")]
    assert f.replies == [{"id": "per_1", "reply": reply}]
    assert TextDelta("DONE") in rec.events


def test_permission_deny(fake):
    f = fake("permission"); rec = Recorder("deny")
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert f.replies == [{"id": "per_1", "reply": "reject"}]
    assert ToolFinished("call_1", False, "The user rejected permission") in rec.events


def test_question_round_trip(fake):
    f = fake("question"); rec = Recorder(answer="blue")
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "ask", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert f.question_replies == [{"id": "que_1", "answers": [["blue"]]}]
    assert TextDelta("you chose blue") in rec.events


def test_interrupt_aborts(fake):
    f = fake("abort"); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url); holder = {}
    t = threading.Thread(target=lambda: holder.__setitem__("out", rt.run_turn(SESSION, SID, "go", "", rec.emit, rec))); t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt(); t.join(10)
    assert f.aborted.is_set() and holder["out"].status == "interrupted"


def test_session_error_fails_the_turn(fake):
    f = fake("error"); rec = Recorder()
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "go", "", rec.emit, rec)
    assert out.status == "failed" and "provider down" in out.error
    assert any(isinstance(e, Failure) for e in rec.events)


def test_malformed_event_costs_a_line_not_the_turn(fake):
    f = fake("malformed"); rec = Recorder()
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.kinds() == ["Failure", "TextDelta", "TurnFinished"]
    assert rec.events[0].message.startswith("opencode event tandem cannot handle: AttributeError")


def test_sse_drop_mid_turn_leaves_the_next_turn_live(fake):
    f = fake("sse_drop"); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    first = Recorder()
    assert rt.run_turn(SESSION, SID, "one", "", first.emit, first).status == "completed"
    assert f.dropped.is_set()                   # the stream was cut mid-turn, not at EOF
    second = Recorder()
    assert rt.run_turn(SESSION, SID, "two", "", second.emit, second).status == "completed"
    assert TextDelta("DONE") in second.events   # the reader re-subscribed, the window still paints
    rt.close()


def test_events_for_other_sessions_are_ignored(fake):
    rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1")
    st = TurnState(session_id="ses_mine")
    rt.handle_event({"type": "message.part.delta", "properties": {"sessionID": "ses_other", "messageID": "m", "partID": "p", "field": "text", "delta": "x"}}, st, rec.emit, rec)
    assert rec.events == []


def test_reply_that_cannot_be_delivered_paints_a_failure():
    rec = Recorder("allow")     # nothing is listening on port 1: the reply POST cannot land
    rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1")
    st = TurnState(session_id="ses_mine")
    rt.handle_event({"type": "permission.asked",
                     "properties": {"id": "per_1", "sessionID": "ses_mine", "permission": "bash",
                                    "patterns": ["touch x.txt"]}}, st, rec.emit, rec)
    assert rec.approvals == [ApprovalRequest("permission", "bash: touch x.txt")]
    assert rec.kinds() == ["Failure"]
    assert rec.events[0].message.startswith("opencode reply failed: ")


def test_golden_events_drive_the_handler(fake):
    rec = Recorder("allow"); rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1")
    st = TurnState(session_id="ses_f88b30227ffeMqqajMYXp3Bmqs")
    rt._reply_permission = lambda pid, reply: rec.approvals.append(("reply", pid, reply))   # no server behind the golden lines
    for line in GOLDEN.read_text().splitlines():
        rt.handle_event(json.loads(line), st, rec.emit, rec)
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta"]
    assert rec.events[0] == ThinkingDelta("The")
    assert rec.events[1] == ToolStarted("call_bb6afd06e9414ec8bb76f7ca", "bash", "touch fixture-oc.txt")
    assert rec.events[3] == ToolFinished("call_bb6afd06e9414ec8bb76f7ca", True, "touch fixture-oc.txt")
    assert rec.approvals[0] == ApprovalRequest("permission", "bash: touch fixture-oc.txt")
    assert rec.approvals[1] == ("reply", "per_07751394f001ki7iX2q5s6tq9A", "once")


def test_ensure_server_spawns_and_polls_health(tmp_path):
    rt = OpencodeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE_SERVE)])
    base = rt.ensure_server(str(tmp_path))
    assert base.startswith("http://127.0.0.1:")
    assert rt.ensure_server(str(tmp_path)) == base       # idempotent while alive
    rec = Recorder()
    out = rt.run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    rt.close()
    assert rt._proc is None or rt._proc.poll() is not None


def test_a_server_that_never_becomes_healthy_is_killed_and_retried(tmp_path, monkeypatch):
    """Left alive with base_url set, every later ensure_server short-circuits
    on it: each opencode turn would cost another 10 s poll plus a failed POST
    for the life of the window. The spec's rule is one restart, then reported."""
    from tandem.chat.runtime import opencode as oc

    killed = []
    real_terminate = oc.terminate
    monkeypatch.setattr(oc, "terminate",
                        lambda proc, **kw: (killed.append(proc), real_terminate(proc, **kw))[1])
    monkeypatch.setenv("FAKE_OPENCODE_UNHEALTHY", "1")
    rt = OpencodeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE_SERVE)])
    with pytest.raises(RuntimeError, match="did not become healthy"):
        rt.ensure_server(str(tmp_path), health_timeout=1.0)
    assert rt._proc is None and rt.base_url is None
    assert killed and killed[0].poll() is not None          # the child is gone, not orphaned

    monkeypatch.delenv("FAKE_OPENCODE_UNHEALTHY")
    base = rt.ensure_server(str(tmp_path))                  # the next turn tries again
    assert base and rt._proc is not None and rt._proc.poll() is None
    rt.close()


def test_factory_builds_one_runtime_per_participant():
    from tandem.chat.runtime.factory import make_runtimes
    session = SimpleNamespace(participants=["claude", "codex", "opencode"], cwd="/p")
    rts = make_runtimes(session, ChatConfig())
    assert sorted(rts) == ["claude", "codex", "opencode"]
    assert all(rts[h].harness == h for h in rts)


def test_server_is_told_which_session_it_belongs_to(tmp_path, monkeypatch):
    """The server outlives the turn and runs every tool call of the window's
    one session, so the id goes in at spawn."""
    from tandem.chat.runtime import opencode as mod

    seen = []
    real = mod.subprocess.Popen
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **kw: (seen.append(kw["env"]), real(*a, **kw))[1])
    rt = OpencodeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE_SERVE)])
    try:
        rt.ensure_server(str(tmp_path), tandem_id="tdm-opencode")
    finally:
        rt.close()
    assert seen[0]["TANDEM_SESSION_ID"] == "tdm-opencode"


# -- @path mentions become file parts, as opencode's own TUI sends them ---------

@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "my notes.txt").write_text("")
    return tmp_path


def test_a_mention_of_an_existing_file_becomes_a_file_part(proj):
    prompt = "explain @src/app.py please"
    assert mention_parts(prompt, str(proj)) == [{
        "type": "file", "mime": "text/plain", "filename": "src/app.py",
        "url": (proj / "src" / "app.py").resolve().as_uri(),
        "source": {"type": "file", "path": "src/app.py",
                   "text": {"value": "@src/app.py", "start": 8, "end": 19}}}]


def test_a_quoted_mention_carries_a_path_with_a_space(proj):
    [part] = mention_parts('read @"my notes.txt" now', str(proj))
    assert part["filename"] == "my notes.txt"
    assert part["url"].endswith("/my%20notes.txt")
    assert part["source"]["text"] == {"value": '@"my notes.txt"', "start": 5, "end": 20}


def test_a_directory_mention_is_a_directory_part(proj):
    [part] = mention_parts("what is in @src/", str(proj))
    assert part["mime"] == "application/x-directory" and part["filename"] == "src/"


def test_a_mention_of_nothing_on_disk_stays_text(proj):
    assert mention_parts("ping @alice about @src/missing.py", str(proj)) == []


def test_an_at_sign_inside_a_word_is_not_a_mention(proj):
    (proj / "example.com").write_text("")
    assert mention_parts("mail me@example.com", str(proj)) == []


def test_sentence_punctuation_after_a_mention_is_not_part_of_the_path(proj):
    [part] = mention_parts("did you read @src/app.py?", str(proj))
    assert part["filename"] == "src/app.py"
    assert part["source"]["text"] == {"value": "@src/app.py", "start": 13, "end": 24}


def test_a_path_outside_the_session_directory_stays_text(proj, tmp_path_factory):
    """opencode asks before it reads outside the project; a part would skip the asking."""
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("")
    assert mention_parts(f"read @{outside} and @../{outside.parent.name}/secret.txt", str(proj)) == []


def test_a_file_mentioned_twice_is_attached_once(proj):
    assert len(mention_parts("@src/app.py vs @src/app.py", str(proj))) == 1


def test_the_turn_posts_the_prompt_verbatim_with_its_file_parts(fake, proj):
    f = fake("tool"); rec = Recorder()
    session = SimpleNamespace(cwd=str(proj), tandem_id="tdm-opencode")
    rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    rt.run_turn(session, SID, "explain @src/app.py", "", rec.emit, rec)
    parts = f.posts[0]["parts"]
    assert parts[0] == {"type": "text", "text": "explain @src/app.py"}
    assert [p["filename"] for p in parts[1:]] == ["src/app.py"]
    rt.close()
