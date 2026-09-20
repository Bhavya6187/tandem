import uuid

from tandem import compat, paths
from tandem.constants import ATTRIBUTION
from tandem.util import uuid7


def test_claude_munge_matches_observed():
    # Observed on claude 2.1.220: /private/tmp/claude-501/... project dir.
    assert (
        paths.claude_munge_cwd("/private/tmp/claude-501/probe")
        == "-private-tmp-claude-501-probe"
    )
    assert paths.claude_munge_cwd("/Users/x/git/a.b_c") == "-Users-x-git-a-b-c"


def test_rollout_session_id_extraction(tmp_path):
    sid = "019faca1-ad54-7092-bed0-f0b2cc71e164"
    p = tmp_path / f"rollout-2026-07-28T23-48-24-{sid}.jsonl"
    assert paths.codex_rollout_session_id(p) == sid
    assert paths.codex_rollout_session_id(tmp_path / "other.jsonl") is None


def test_find_codex_rollout(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sid = "019faca1-ad54-7092-bed0-f0b2cc71e164"
    day = tmp_path / "sessions" / "2026" / "07" / "28"
    day.mkdir(parents=True)
    f = day / f"rollout-2026-07-28T23-48-24-{sid}.jsonl"
    f.write_text("{}\n")
    assert paths.find_codex_rollout(sid) == f
    assert paths.find_codex_rollout("0" * 8 + "-dead-beef-dead-beefdeadbeef") is None


def test_version_parse_and_ranges():
    assert compat.parse_version("2.1.220 (Claude Code)") == (2, 1, 220)
    assert compat.parse_version("codex-cli 0.145.0") == (0, 145, 0)
    assert compat.parse_version("garbage") is None
    assert compat.version_supported("claude", "2.1.220 (Claude Code)")
    assert not compat.version_supported("claude", "3.0.0")
    assert compat.version_supported("codex", "codex-cli 0.145.0")
    assert not compat.version_supported("codex", "codex-cli 0.160.0")


def test_uuid7_is_valid_and_ordered():
    a, b = uuid7(), uuid7()
    ua, ub = uuid.UUID(a), uuid.UUID(b)
    assert ua.version == 7
    assert a != b
    assert ua.bytes[:6] <= ub.bytes[:6]  # time-ordered prefix


def test_opencode_compat_floor_only():
    assert compat.version_supported("opencode", "1.18.15")
    assert compat.version_supported("opencode", "9.9.9")      # no ceiling
    assert not compat.version_supported("opencode", "1.17.0")  # below floor


def test_codex_verified_range_keeps_a_future_version_guard():
    assert compat.version_supported("claude", "2.1.220")
    assert compat.version_supported("codex", "0.150.0")
    assert compat.version_supported("codex", "0.153.4")
    assert compat.version_supported("codex", "0.159.0")
    assert not compat.version_supported("codex", "0.160.0")


def test_opencode_attribution_tag():
    assert ATTRIBUTION["opencode"] == "[via opencode]"


class TestClaudeLocateTranscript:
    """claude's EnterWorktree renames the session transcript into the
    worktree's project dir (observed: claude 2.1.277), so the file for an id
    is not always under the session cwd's slug."""

    def test_finds_the_transcript_under_another_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        moved = tmp_path / "projects" / "-proj--claude-worktrees-wt" / "sid-1.jsonl"
        moved.parent.mkdir(parents=True)
        moved.write_text("{}\n")
        assert paths.claude_locate_transcript("sid-1") == moved

    def test_none_when_no_project_holds_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert paths.claude_locate_transcript("sid-1") is None
        (tmp_path / "projects" / "-proj").mkdir(parents=True)
        assert paths.claude_locate_transcript("sid-1") is None

    def test_most_recently_written_copy_wins(self, tmp_path, monkeypatch):
        import os

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        old = tmp_path / "projects" / "-a" / "sid-1.jsonl"
        new = tmp_path / "projects" / "-b" / "sid-1.jsonl"
        for f in (old, new):
            f.parent.mkdir(parents=True)
            f.write_text("{}\n")
        os.utime(old, (1_000, 1_000))
        os.utime(new, (2_000, 2_000))
        assert paths.claude_locate_transcript("sid-1") == new

    def test_adapter_resolves_a_relocated_transcript(self, tmp_path, monkeypatch):
        from tandem.harness import get_adapter

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        adapter = get_adapter("claude")
        assert adapter.transcript_path("/proj", "sid-1") is None
        moved = tmp_path / "projects" / "-proj--claude-worktrees-wt" / "sid-1.jsonl"
        moved.parent.mkdir(parents=True)
        moved.write_text("{}\n")
        assert adapter.transcript_path("/proj", "sid-1") == moved
        # the session cwd's own copy still wins once it exists
        home = paths.claude_transcript_path("/proj", "sid-1")
        home.parent.mkdir(parents=True)
        home.write_text("{}\n")
        assert adapter.transcript_path("/proj", "sid-1") == home
