"""routefile: the two files that carry a routed turn between the
UserPromptSubmit hook and the tandem frame.

The hook is a separate short-lived process, so both directions travel
through disk. Every read here degrades to None — a hook that raises kills
a user's turn, and a frame that raises kills the session — so the tests
below pin the failure modes (missing, corrupt, stale) as hard as the happy
path, plus the pending -> dispatched -> gone lifecycle the injector walks."""

import os
import time

import pytest

from tandem import routefile
from tandem.routefile import RouteRequest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    return tmp_path


REQ = RouteRequest(target="codex", model="gpt-5.3-codex",
                   prompt="fix it", source="claude", reason="→ codex")


def test_route_round_trip():
    routefile.write_route("abc123", REQ)
    got = routefile.read_route("abc123")
    assert got == REQ and got.state == "pending"


def test_read_missing_is_none():
    assert routefile.read_route("abc123") is None


def test_read_corrupt_is_none(home):
    p = home / "tmp" / "abc123-route.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert routefile.read_route("abc123") is None


def test_read_stale_is_none(home):
    routefile.write_route("abc123", REQ)
    p = home / "tmp" / "abc123-route.json"
    old = time.time() - routefile.ROUTE_TTL - 5
    os.utime(p, (old, old))
    assert routefile.read_route("abc123") is None


def test_mark_dispatched_then_clear():
    routefile.write_route("abc123", REQ)
    routefile.mark_dispatched("abc123", REQ)
    got = routefile.read_route("abc123")
    assert got is not None and got.state == "dispatched"
    routefile.clear_route("abc123")
    assert routefile.read_route("abc123") is None


def test_clear_missing_is_quiet():
    routefile.clear_route("nope")   # must not raise


def test_frame_state_round_trip():
    routefile.write_frame_state("abc123", {"tab": "mixed", "focus": "codex"})
    assert routefile.read_frame_state("abc123") == {
        "tab": "mixed", "focus": "codex"}
    assert routefile.read_frame_state("other") is None


def test_write_failure_is_quiet(home):
    # a file squatting on the tmp dir makes mkdir fail; a hook must never
    # crash a user's turn over route bookkeeping
    (home / "tmp").write_text("squatter")
    routefile.write_route("abc123", REQ)   # must not raise
    assert routefile.read_route("abc123") is None


def test_unserializable_frame_state_is_quiet():
    # the snapshot comes from the mixer thread; a bad value must degrade to
    # "no frame state" (which the hook reads as "not the mixed tab")
    routefile.write_frame_state("abc123", {"tab": object()})  # must not raise
    assert routefile.read_frame_state("abc123") is None
