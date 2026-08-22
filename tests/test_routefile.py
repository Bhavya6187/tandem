"""routefile: the two files that carry a routed turn between the
UserPromptSubmit hook and the tandem frame.

The hook is a separate short-lived process, so both directions travel
through disk. Every read here degrades to None — a hook that raises kills
a user's turn, and a frame that raises kills the session — so the tests
below pin the failure modes (missing, corrupt) as hard as the happy path,
plus the pending -> claimed -> gone lifecycle the frame walks. The request
id is what makes each hand-off safe without locking, so the tests that
matter most are the ones where two requests exist at once."""

import pytest

from tandem import routefile
from tandem.routefile import RouteRequest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    return tmp_path


def req(prompt="fix it", target="codex") -> RouteRequest:
    return RouteRequest(target=target, model="gpt-5.3-codex", prompt=prompt,
                        source="claude", reason=f"→ {target}")


def test_every_request_gets_its_own_id():
    a, b = req(), req()
    assert a.id and a.id != b.id


def test_pending_round_trip_carries_the_id():
    r = req()
    routefile.write_route("abc123", r)
    assert routefile.read_pending("abc123") == r
    assert routefile.read_claimed("abc123") is None


def test_read_missing_is_none():
    assert routefile.read_pending("abc123") is None
    assert routefile.read_claimed("abc123") is None


def test_read_corrupt_is_none(home):
    p = home / "tmp" / "abc123-route.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert routefile.read_pending("abc123") is None


def test_read_without_an_id_is_none(home):
    # the id is the whole protocol; a request that cannot be identified
    # cannot be claimed or released, so it is not a request
    p = home / "tmp" / "abc123-route.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"target": "codex", "model": "", "prompt": "hi",'
                 ' "source": "claude", "reason": "x"}')
    assert routefile.read_pending("abc123") is None


def test_claim_moves_the_request_to_the_claimed_slot():
    r = req()
    routefile.write_route("abc123", r)
    assert routefile.claim("abc123", r.id) is True
    assert routefile.read_pending("abc123") is None
    assert routefile.read_claimed("abc123") == r


def test_claim_of_another_id_touches_nothing():
    # the frame validated one request and a second prompt overwrote the
    # slot before it got there: claiming would arm a flip for a prompt
    # nobody looked at
    r = req()
    routefile.write_route("abc123", r)
    assert routefile.claim("abc123", "deadbeef1234") is False
    assert routefile.read_pending("abc123") == r
    assert routefile.read_claimed("abc123") is None


def test_claim_with_nothing_pending_is_false():
    assert routefile.claim("abc123", "deadbeef1234") is False


def test_release_deletes_the_claimed_request():
    r = req()
    routefile.write_route("abc123", r)
    routefile.claim("abc123", r.id)
    routefile.release("abc123", r.id)
    assert routefile.read_claimed("abc123") is None


def test_release_deletes_a_pending_request_too():
    # the frame drops a request it cannot route (a target that left the
    # session) without ever claiming it
    r = req(target="gemini")
    routefile.write_route("abc123", r)
    routefile.release("abc123", r.id)
    assert routefile.read_pending("abc123") is None


def test_release_never_deletes_someone_elses_request():
    # a second routed prompt lands while the first is still being typed in:
    # an unconditional unlink would destroy a prompt that never ran
    first = req("do it")
    routefile.write_route("abc123", first)
    routefile.claim("abc123", first.id)
    later = req("and then this one")
    routefile.write_route("abc123", later)
    routefile.release("abc123", first.id)
    assert routefile.read_claimed("abc123") is None
    assert routefile.read_pending("abc123") == later


def test_release_of_an_unknown_id_is_quiet():
    routefile.release("nope", "deadbeef1234")   # must not raise


def test_sweep_returns_both_leftovers_and_deletes_them(home):
    stranded = req("never picked up")
    routefile.write_route("abc123", stranded)
    routefile.claim("abc123", stranded.id)
    fresh = req("never dispatched")
    routefile.write_route("abc123", fresh)
    assert routefile.sweep("abc123") == (fresh, stranded)
    assert routefile.read_pending("abc123") is None
    assert routefile.read_claimed("abc123") is None
    assert list((home / "tmp").iterdir()) == []


def test_sweep_has_no_age_limit(home):
    # a leftover from a run that crashed days ago is still a typed prompt;
    # the sweep is what turns it into a note instead of a silent delete
    import os
    import time

    r = req()
    routefile.write_route("abc123", r)
    p = home / "tmp" / "abc123-route.json"
    old = time.time() - 86400 * 7
    os.utime(p, (old, old))
    assert routefile.sweep("abc123") == (r, None)
    assert not p.exists()


def test_sweep_of_nothing_is_quiet():
    assert routefile.sweep("abc123") == (None, None)


def test_sweep_deletes_an_unparseable_file(home):
    # nothing to quote in a note, and leaving it would strand it forever
    p = home / "tmp" / "abc123-route.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert routefile.sweep("abc123") == (None, None)
    assert not p.exists()


def test_frame_state_round_trip():
    routefile.write_frame_state("abc123", {"tab": "mixed", "focus": "codex"})
    assert routefile.read_frame_state("abc123") == {
        "tab": "mixed", "focus": "codex"}
    assert routefile.read_frame_state("other") is None


def test_write_failure_is_quiet(home):
    # a file squatting on the tmp dir makes mkdir fail; a hook must never
    # crash a user's turn over route bookkeeping
    (home / "tmp").write_text("squatter")
    routefile.write_route("abc123", req())   # must not raise
    assert routefile.read_pending("abc123") is None


def test_unserializable_frame_state_is_quiet(home):
    # the snapshot comes from the mixer thread; a bad value must degrade to
    # "no frame state" (which the hook reads as "not the mixed tab")
    routefile.write_frame_state("abc123", {"tab": object()})  # must not raise
    assert routefile.read_frame_state("abc123") is None
    # serialization happens before the write, so a bad snapshot leaves
    # nothing on disk at all — not even a scratch file
    assert not (home / "tmp").exists()


def test_write_leaves_no_scratch_file(home):
    # nothing sweeps $TANDEM_HOME/tmp, so every write must clean up after
    # itself — a stray scratch file would live there forever
    routefile.write_route("abc123", req())
    routefile.write_frame_state("abc123", {"tab": "mixed"})
    assert sorted(p.name for p in (home / "tmp").iterdir()) == [
        "abc123-frame.json", "abc123-route.json"]
