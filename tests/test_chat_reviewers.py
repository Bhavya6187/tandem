"""The real reviewers over the fake CLIs: a codex review forks the shadow
and deletes the fork; a claude review forks at spawn and deletes what it
minted. Both run read-only and deny every approval."""

import json
import sys
import threading
from pathlib import Path

import pytest

from tandem import ops, paths
from tandem.chat.navigator import ReviewError
from tandem.chat.reviewers import ClaudeReviewer, CodexReviewer, Collector, DenyAll, make_reviewer
from tandem.config import ChatConfig
from tandem.harness import get_adapter

FAKE_CODEX = Path(__file__).parent / "fakes" / "fake_codex_appserver.py"
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def test_deny_all_and_collector():
    from tandem.chat.events import ApprovalRequest, Failure, QuestionRequest, TextDelta
    d = DenyAll()
    assert d.approve(ApprovalRequest("command", "rm -rf")) == "deny"
    assert d.answer(QuestionRequest("which?", ("a",))) == ""
    c = Collector()
    c(TextDelta("{\"verdict\""))
    c(TextDelta(": \"clean\"}"))
    c(Failure("hm"))
    assert c.text == '{"verdict": "clean"}' and c.failures == ["hm"]


@pytest.fixture
def codex_env(env_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_PARAMS_OUT", str(tmp_path / "params.jsonl"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    env = env_factory()

    def params(method):
        for line in (tmp_path / "params.jsonl").read_text().splitlines():
            m = json.loads(line)
            if m["method"] == method:
                return m["params"]
        return None

    env.params = params
    env.tmp = tmp_path
    return env


def test_codex_review_runs_on_a_deleted_fork_read_only_with_the_schema(codex_env, monkeypatch):
    env = codex_env
    seen = {}
    real_fork = ops.fork_shadow

    def spy(store, session):
        fid, fpath = real_fork(store, session)
        seen["id"], seen["path"], seen["existed"] = fid, fpath, fpath.exists()
        return fid, fpath

    monkeypatch.setattr(ops, "fork_shadow", spy)
    r = CodexReviewer(ChatConfig(navigator="codex"), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    lock = threading.Lock()
    out = r.review(env.session, "gpt-x", "review please", {"type": "object"}, lock)
    assert out.text == "DONE" and out.structured is None
    assert seen["existed"] and not seen["path"].exists()             # forked, then deleted
    assert seen["id"] != env.session.native_id("codex")
    resume = env.params("thread/resume")
    assert resume["threadId"] == seen["id"]
    assert resume["approvalPolicy"] == "never" and resume["sandbox"] == "read-only"
    turn = env.params("turn/start")
    assert turn["outputSchema"] == {"type": "object"} and turn["model"] == "gpt-x"
    assert not lock.locked()                                         # released after the copy
    assert json.loads((env.tmp / "reply.json").read_text()) != {}    # the fake asked; we denied


def test_codex_review_denies_approvals(codex_env):
    env = codex_env
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    r.review(env.session, "", "p", {}, threading.Lock())
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "decline"}


def test_codex_review_failure_is_a_review_error_and_still_deletes_the_fork(codex_env, monkeypatch):
    env = codex_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "crash")
    forks = []
    real_fork = ops.fork_shadow
    monkeypatch.setattr(ops, "fork_shadow", lambda s, sess: forks.append(real_fork(s, sess)) or forks[-1])
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, threading.Lock())
    assert forks and not forks[0][1].exists()


def test_codex_review_without_a_shadow_is_a_review_error(env_factory, monkeypatch):
    env = env_factory(active="codex", seed_active=False)       # codex has no id yet
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    monkeypatch.setattr(ops, "_create_codex_shadow_late", lambda *a, **k: (_ for _ in ()).throw(ops.SyncSetupError("no")))
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, threading.Lock())


def test_make_reviewer_picks_by_harness(env_factory):
    env = env_factory()
    assert isinstance(make_reviewer("codex", ChatConfig(), env.store), CodexReviewer)
    assert isinstance(make_reviewer("claude", ChatConfig(), env.store), ClaudeReviewer)
    with pytest.raises(ValueError):
        make_reviewer("opencode", ChatConfig(), env.store)
