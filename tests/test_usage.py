"""Usage meters: per-harness token accounting behind the status bar stats.

Fixture shapes mirror docs/formats.md and live transcripts: claude repeats
one message's usage across its per-content-block entries (output still
growing), codex precomputes both totals in token_count events, opencode
carries a tokens block per assistant row.

"Input" throughout means the whole prompt side as sent over the wire —
fresh input plus cache reads and cache writes. Codex's input_tokens
already includes cached_input_tokens (live rollouts: total = input +
output exactly); claude and opencode report cache fields separately, so
their meters fold them in.
"""

import json

from tandem.harness import get_adapter
from tandem.harness.base import UsageSnapshot
from tandem.runner import UsageFeed


def _claude_entry(mid, inp=0, cr=0, cc=0, out=0, sidechain=False):
    e = {
        "type": "assistant",
        "message": {
            "id": mid,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
                "output_tokens": out,
            },
        },
    }
    if sidechain:
        e["isSidechain"] = True
    return e


def _token_count(total=None, last=None, window=None, inp=None, out=None):
    info = {}
    if total is not None or inp is not None:
        usage = {}
        if total is not None:
            usage["total_tokens"] = total
        if inp is not None:
            usage["input_tokens"] = inp
        if out is not None:
            usage["output_tokens"] = out
        info["total_token_usage"] = usage
    if last is not None:
        info["last_token_usage"] = {"total_tokens": last}
    if window is not None:
        info["model_context_window"] = window
    return {"type": "event_msg", "payload": {"type": "token_count", "info": info}}


def _oc_turn(assistants):
    return {
        "user": {"message": {"id": "u1", "role": "user"}, "parts": []},
        "assistants": [{"message": m, "parts": []} for m in assistants],
    }


# -- claude ------------------------------------------------------------------


def test_claude_meter_ctx_tracks_latest_usage():
    m = get_adapter("claude").make_usage_meter()
    m.feed(_claude_entry("m1", inp=2, cr=50_000, cc=1_000, out=300))
    m.feed(_claude_entry("m2", inp=2, cr=60_000, cc=2_000, out=500))
    assert m.snapshot().ctx_tokens == 2 + 60_000 + 2_000


def test_claude_meter_splits_input_and_output():
    m = get_adapter("claude").make_usage_meter()
    m.feed(_claude_entry("m1", inp=2, cr=50_000, cc=1_000, out=300))
    m.feed(_claude_entry("m2", inp=2, cr=60_000, cc=2_000, out=500))
    snap = m.snapshot()
    assert snap.input_tokens == (2 + 50_000 + 1_000) + (2 + 60_000 + 2_000)
    assert snap.output_tokens == 300 + 500


def test_claude_meter_dedups_entries_sharing_a_message_id():
    m = get_adapter("claude").make_usage_meter()
    m.feed(_claude_entry("m1", inp=2, cr=1_000, out=100))
    m.feed(_claude_entry("m1", inp=2, cr=1_000, out=250))  # later block, same msg
    snap = m.snapshot()
    assert snap.input_tokens == 2 + 1_000
    assert snap.output_tokens == 250


def test_claude_meter_counts_sidechains_in_totals_but_not_ctx():
    # Parallel Task subagents interleave sidechain entries with the main
    # chain; they are real spend but a different context window.
    m = get_adapter("claude").make_usage_meter()
    m.feed(_claude_entry("main1", inp=2, cr=1_000, out=100))
    m.feed(_claude_entry("side1", inp=500, out=50, sidechain=True))
    m.feed(_claude_entry("main1", inp=2, cr=1_000, out=200))
    snap = m.snapshot()
    assert snap.input_tokens == (2 + 1_000) + 500
    assert snap.output_tokens == 200 + 50
    assert snap.ctx_tokens == 2 + 1_000


def test_claude_meter_ignores_entries_without_usage():
    m = get_adapter("claude").make_usage_meter()
    m.feed({"type": "user", "message": {"role": "user", "content": "hi"}})
    m.feed({"type": "assistant", "message": {"id": "x"}})
    m.feed({"type": "system", "subtype": "hook"})
    assert m.snapshot().bar_text() == ""


# -- codex -------------------------------------------------------------------


def test_codex_meter_reads_percent_and_split():
    m = get_adapter("codex").make_usage_meter()
    m.feed(_token_count(total=75_118, last=75_118, window=258_400,
                        inp=75_043, out=75))
    snap = m.snapshot()
    assert snap.ctx_percent == 29
    assert snap.input_tokens == 75_043
    assert snap.output_tokens == 75


def test_codex_meter_split_is_cumulative_not_summed():
    # total_token_usage is already cumulative — later events replace,
    # never add
    m = get_adapter("codex").make_usage_meter()
    m.feed(_token_count(total=10_000, last=10_000, inp=9_900, out=100))
    m.feed(_token_count(total=25_000, last=15_000, inp=24_600, out=400))
    snap = m.snapshot()
    assert snap.input_tokens == 24_600
    assert snap.output_tokens == 400


def test_codex_meter_without_window_falls_back_to_tokens():
    m = get_adapter("codex").make_usage_meter()
    m.feed(_token_count(total=75_118, last=75_118, inp=75_043, out=75))
    snap = m.snapshot()
    assert snap.ctx_percent is None
    assert snap.ctx_tokens == 75_118


def test_codex_meter_survives_null_info():
    # rate-limit-only token_count events carry info: null
    m = get_adapter("codex").make_usage_meter()
    m.feed({"type": "event_msg",
            "payload": {"type": "token_count", "info": None}})
    assert m.snapshot().bar_text() == ""


def test_codex_meter_ignores_other_entries():
    m = get_adapter("codex").make_usage_meter()
    m.feed({"type": "response_item", "payload": {"type": "message"}})
    m.feed({"type": "event_msg", "payload": {"type": "agent_message"}})
    assert m.snapshot().bar_text() == ""


# -- opencode ----------------------------------------------------------------


def test_opencode_meter_splits_turns_and_tracks_last_context():
    # reasoning tokens are separate in opencode's block but a subset of
    # output in codex's — folding them into output makes ↓ mean the same
    # thing on both slots
    m = get_adapter("opencode").make_usage_meter()
    a1 = {"id": "a1", "tokens": {"input": 100, "output": 50, "reasoning": 10,
                                 "cache": {"read": 400, "write": 20}}}
    a2 = {"id": "a2", "tokens": {"input": 200, "output": 80, "reasoning": 0,
                                 "cache": {"read": 900, "write": 0}}}
    m.feed(_oc_turn([a1]))
    m.feed(_oc_turn([a2]))
    snap = m.snapshot()
    assert snap.input_tokens == (100 + 400 + 20) + (200 + 900)
    assert snap.output_tokens == (50 + 10) + 80
    assert snap.ctx_tokens == 200 + 900


def test_opencode_meter_skips_assistants_without_tokens():
    m = get_adapter("opencode").make_usage_meter()
    m.feed(_oc_turn([{"id": "a1"}]))
    assert m.snapshot().bar_text() == ""


def test_opencode_meter_ignores_all_zero_sentinel_rows():
    # tandem-seeded shadow rows carry a tokens block of all zeros; they must
    # not blank a real context reading
    m = get_adapter("opencode").make_usage_meter()
    real = {"id": "a1", "tokens": {"input": 100, "output": 50, "reasoning": 0,
                                   "cache": {"read": 400, "write": 0}}}
    sentinel = {"id": "a2", "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                       "cache": {"read": 0, "write": 0}}}
    m.feed(_oc_turn([real]))
    m.feed(_oc_turn([sentinel]))
    snap = m.snapshot()
    assert snap.ctx_tokens == 100 + 400
    assert snap.input_tokens == 100 + 400
    assert snap.output_tokens == 50


# -- snapshot formatting -------------------------------------------------------


def test_bar_text_formats_tokens_and_split():
    snap = UsageSnapshot(ctx_tokens=142_512,
                         input_tokens=7_600_000, output_tokens=312_000)
    assert snap.bar_text() == "142k ctx · 7.6M↑ 312k↓"


def test_bar_text_prefers_percent_when_known():
    snap = UsageSnapshot(ctx_tokens=75_118, ctx_percent=29,
                         input_tokens=75_043, output_tokens=75)
    assert snap.bar_text() == "29% ctx · 75k↑ 75↓"


def test_bar_text_small_counts_stay_exact():
    snap = UsageSnapshot(ctx_tokens=950, input_tokens=900, output_tokens=50)
    assert snap.bar_text() == "950 ctx · 900↑ 50↓"


def test_bar_text_shows_split_when_only_one_side_moved():
    # first turn of a session: prompt side counted, no output yet — the
    # pair still renders as a unit
    snap = UsageSnapshot(ctx_tokens=950, input_tokens=950)
    assert snap.bar_text() == "950 ctx · 950↑ 0↓"


def test_bar_text_empty_without_data():
    assert UsageSnapshot().bar_text() == ""


# -- the runner's usage feed ---------------------------------------------------


class _Session:
    tandem_id = "t1"


def _write_jsonl(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_usage_feed_tails_the_transcript_into_state(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [_claude_entry("m1", inp=2, cr=140_000, cc=2_512, out=300)])
    state = {"text": ""}
    feed = UsageFeed(get_adapter("claude"), _Session(), p, state)
    feed.poll()
    assert state["text"] == "142k ctx · 142k↑ 300↓"
    _write_jsonl(p, [_claude_entry("m2", inp=2, cr=150_000, cc=0, out=1_000)])
    feed.poll()
    assert state["text"].startswith("150k ctx")


def test_usage_feed_failure_disables_it_without_raising(tmp_path):
    # a shrunk transcript raises TranscriptTruncated inside poll: the feed
    # must swallow it and go quiet — stats are cosmetic, sync is not
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [_claude_entry("m1", inp=2, cr=1_000, out=10)])
    state = {"text": ""}
    feed = UsageFeed(get_adapter("claude"), _Session(), p, state)
    feed.poll()
    before = state["text"]
    p.write_text("")
    _write_jsonl(p, [_claude_entry("m2", inp=1, cr=1, out=1)])
    feed.poll()
    assert state["text"] == before
    _write_jsonl(p, [_claude_entry("m3", inp=9, cr=9, out=9)])
    feed.poll()   # stays disabled, still no raise
    assert state["text"] == before
