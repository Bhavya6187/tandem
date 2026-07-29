import uuid

from tandem import compat, paths
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
    assert not compat.version_supported("codex", "codex-cli 0.155.0")


def test_uuid7_is_valid_and_ordered():
    a, b = uuid7(), uuid7()
    ua, ub = uuid.UUID(a), uuid.UUID(b)
    assert ua.version == 7
    assert a != b
    assert ua.bytes[:6] <= ub.bytes[:6]  # time-ordered prefix
