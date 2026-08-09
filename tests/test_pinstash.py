"""pinstash: out-of-band model-pin recovery for relay-degraded briefs.

The haiku relay drops a `tandem-model:` first line from the brief it echoes
into `tandem sub` (live transcripts, 2026-08-08 audit: 5 of 6 pinned
dispatches before the prompt fix; still ~2 of 7 after). The hook sees the
pristine parent prompt, so it stashes the pin keyed by a hash of the brief
body; `tandem sub` recovers the pin when its stdin arrives headerless.
Everything here is best-effort: a stash problem must never break a dispatch,
and a miss degrades to today's behavior (config default)."""

import os
import time

import pytest

from tandem import paths, pinstash


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))


def _backdate_all(seconds: float) -> None:
    past = time.time() - seconds
    for p in (paths.tandem_home() / "model-pins").iterdir():
        os.utime(p, (past, past))


class TestStashLookup:
    def test_roundtrip_returns_requested_name(self):
        pinstash.stash("do the thing", "sol")
        assert pinstash.lookup("do the thing") == "sol"

    def test_unknown_body_is_empty(self):
        assert pinstash.lookup("never stashed") == ""

    def test_different_body_does_not_match(self):
        pinstash.stash("task one", "sol")
        assert pinstash.lookup("task two") == ""

    def test_lookup_is_idempotent_for_sandbox_reruns(self):
        # the relay's one sanctioned retry re-sends the SAME brief with
        # --sandbox workspace-write; the pin must survive the first read
        pinstash.stash("task", "sol")
        assert pinstash.lookup("task") == "sol"
        assert pinstash.lookup("task") == "sol"

    def test_key_tolerates_edge_whitespace(self):
        # the hook hashes the body split off the parent prompt; sub hashes
        # its stripped stdin — both sides normalize edge whitespace
        pinstash.stash("task\n", "sol")
        assert pinstash.lookup("\ntask") == "sol"

    def test_parallel_pins_keep_distinct_models(self):
        pinstash.stash("review the docs", "sol")
        pinstash.stash("fix the tests", "terra")
        assert pinstash.lookup("review the docs") == "sol"
        assert pinstash.lookup("fix the tests") == "terra"


class TestExpiry:
    def test_expired_pin_is_ignored(self):
        pinstash.stash("task", "sol")
        _backdate_all(pinstash.PIN_TTL + 60)
        assert pinstash.lookup("task") == ""

    def test_stash_prunes_expired_entries(self):
        pinstash.stash("old", "sol")
        _backdate_all(pinstash.PIN_TTL + 60)
        pinstash.stash("new", "terra")
        assert pinstash.lookup("new") == "terra"
        # the expired entry is gone from disk, not merely ignored
        assert len(list((paths.tandem_home() / "model-pins").iterdir())) == 1


class TestRobustness:
    def test_stash_failure_is_silent(self):
        # a file squatting on the pins dir makes mkdir fail; the hook must
        # never crash a dispatch over stash bookkeeping
        home = paths.tandem_home()
        home.mkdir(parents=True)
        (home / "model-pins").write_text("squatter")
        pinstash.stash("task", "sol")
        assert pinstash.lookup("task") == ""

    def test_corrupt_entry_is_empty_not_loud(self):
        # a mangled entry must degrade to "no pin" — recovering garbage
        # would abort the dispatch with an unknown-model error, which is
        # worse than the silent default the stash exists to improve on
        pinstash.stash("task", "sol")
        [p] = (paths.tandem_home() / "model-pins").iterdir()
        p.write_bytes(b"\xff\xfegarbage\xff")
        assert pinstash.lookup("task") == ""
