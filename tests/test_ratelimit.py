"""Account rate limits behind the bar's `5h 4% 7d 4%` figures.

Fixture shapes are the live payloads (probed 2026-08-16): claude's
`/api/oauth/usage` (what `/usage` calls) and codex's `wham/usage` (what
its `/status` calls, `codex-rs/backend-client`). Both are undocumented,
so every parser must go quiet on any shape it doesn't recognize.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tandem import ratelimit
from tandem.ratelimit import (
    RateLimitPoller,
    Window,
    format_windows,
    parse_claude,
    parse_codex,
)

CLAUDE_PAYLOAD = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-08-16T17:49:59+00:00"},
    "seven_day": {"utilization": 41.6, "resets_at": "2026-08-22T17:59:59+00:00"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False},
}

CODEX_PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 12,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 302642,
            "reset_at": 1787209799,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": None,
}


# -- parsing -----------------------------------------------------------------


def test_parse_claude_reads_both_windows_in_order():
    assert parse_claude(CLAUDE_PAYLOAD) == [Window("5h", 4), Window("7d", 41)]


def test_parse_claude_skips_absent_windows():
    assert parse_claude({"five_hour": {"utilization": 9.0}, "seven_day": None}) == [
        Window("5h", 9)
    ]


def test_parse_claude_rejects_junk():
    assert parse_claude({"five_hour": {"utilization": "lots"}}) == []
    assert parse_claude("nope") == []
    assert parse_claude({}) == []


def test_parse_codex_labels_windows_by_their_length():
    assert parse_codex(CODEX_PAYLOAD) == [Window("7d", 12)]


def test_parse_codex_reads_secondary_window_when_present():
    payload = json.loads(json.dumps(CODEX_PAYLOAD))
    payload["rate_limit"]["primary_window"] = {
        "used_percent": 63.4, "limit_window_seconds": 18000}
    payload["rate_limit"]["secondary_window"] = {
        "used_percent": 12, "limit_window_seconds": 604800}
    assert parse_codex(payload) == [Window("5h", 63), Window("7d", 12)]


def test_parse_codex_rejects_junk():
    assert parse_codex({"rate_limit": None}) == []
    assert parse_codex({"rate_limit": {"primary_window": {"used_percent": 3}}}) == []
    assert parse_codex([]) == []


@pytest.mark.parametrize("seconds,label", [
    (18000, "5h"), (604800, "7d"), (3600, "1h"), (86400, "1d"), (300, "5m"),
])
def test_window_label_from_seconds(seconds, label):
    assert ratelimit.window_label(seconds) == label


# -- formatting --------------------------------------------------------------


def test_format_windows_is_one_cell_per_glyph():
    text = format_windows([Window("5h", 4), Window("7d", 41)])
    assert text == "5h 4% 7d 41%"


def test_format_windows_empty_is_blank():
    assert format_windows([]) == ""


# -- credentials -------------------------------------------------------------


def test_claude_token_prefers_keychain(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ratelimit, "_keychain_secret",
                        lambda: json.dumps({"claudeAiOauth": {"accessToken": "kc-tok"}}))
    assert ratelimit.claude_token() == "kc-tok"


def test_claude_token_falls_back_to_credentials_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ratelimit, "_keychain_secret", lambda: None)
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}}))
    assert ratelimit.claude_token() == "file-tok"


def test_claude_token_absent_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ratelimit, "_keychain_secret", lambda: None)
    assert ratelimit.claude_token() is None
    (tmp_path / ".credentials.json").write_text("{not json")
    assert ratelimit.claude_token() is None


def _codex_home(tmp_path, tokens=None, config=None):
    home = tmp_path / "codex"
    home.mkdir()
    if tokens is not None:
        (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))
    if config is not None:
        (home / "config.toml").write_text(config)
    return home


def test_codex_credentials_default_url(monkeypatch, tmp_path):
    home = _codex_home(tmp_path, {"access_token": "t", "account_id": "acct"})
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert ratelimit.codex_credentials() == (
        "t", "acct", "https://chatgpt.com/backend-api/wham/usage")


def test_codex_credentials_honor_chatgpt_base_url(monkeypatch, tmp_path):
    home = _codex_home(tmp_path, {"access_token": "t", "account_id": "acct"},
                       'chatgpt_base_url = "https://example.test/backend-api/"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert ratelimit.codex_credentials()[2] == "https://example.test/backend-api/wham/usage"
    # a base without /backend-api takes codex's other path style
    (home / "config.toml").write_text('chatgpt_base_url = "https://example.test"\n')
    assert ratelimit.codex_credentials()[2] == "https://example.test/api/codex/usage"


def test_codex_credentials_absent_for_api_key_logins(monkeypatch, tmp_path):
    home = _codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert ratelimit.codex_credentials() is None
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}))
    assert ratelimit.codex_credentials() is None


# -- fetching ----------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body: bytes = b"{}"
    seen: list  # reset per `server` fixture
    extra_headers: dict = {}

    def do_GET(self):
        # header names are case-insensitive on the wire (urllib title-cases them)
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        for k, v in type(self).extra_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):  # keep pytest output pristine
        pass


@pytest.fixture
def server():
    _Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_claude_sends_bearer_and_beta_header(server):
    _Handler.status, _Handler.body = 200, json.dumps(CLAUDE_PAYLOAD).encode()
    assert ratelimit.fetch_claude("tok", url=server + "/usage") == [
        Window("5h", 4), Window("7d", 41)]
    path, headers = _Handler.seen[-1]
    assert path == "/usage"
    assert headers["authorization"] == "Bearer tok"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


def test_fetch_codex_sends_bearer_and_account_id(server):
    _Handler.status, _Handler.body = 200, json.dumps(CODEX_PAYLOAD).encode()
    assert ratelimit.fetch_codex("tok", "acct", url=server + "/wham/usage") == [
        Window("7d", 12)]
    _, headers = _Handler.seen[-1]
    assert headers["authorization"] == "Bearer tok"
    assert headers["chatgpt-account-id"] == "acct"


def test_fetch_non_200_is_none(server):
    _Handler.status, _Handler.body = 401, b'{"error":"expired"}'
    assert ratelimit.fetch_claude("tok", url=server + "/usage") is None
    assert ratelimit.fetch_codex("tok", "acct", url=server + "/wham/usage") is None


def test_fetch_unreachable_is_none():
    # nothing listens on this port; the connection is refused rather than hung
    assert ratelimit.fetch_claude("tok", url="http://127.0.0.1:9/usage", timeout=1) is None


# -- poller ------------------------------------------------------------------


def test_poller_refresh_publishes_text_per_harness():
    state = {}
    fetchers = {
        "claude": lambda: [Window("5h", 4), Window("7d", 41)],
        "codex": lambda: [Window("7d", 12)],
    }
    p = RateLimitPoller(["claude", "codex", "opencode"], state, fetchers=fetchers)
    p.refresh()
    assert state["limits"] == {"claude": "5h 4% 7d 41%", "codex": "7d 12%"}


def test_poller_blanks_a_harness_whose_fetch_fails():
    state = {}
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return [Window("7d", 12)]
        raise RuntimeError("boom")

    p = RateLimitPoller(["codex"], state, fetchers={"codex": flaky})
    p.refresh()
    assert state["limits"] == {"codex": "7d 12%"}
    p.refresh()
    assert state["limits"] == {"codex": ""}
    # None (no credentials / non-200) blanks the same way
    p = RateLimitPoller(["codex"], state, fetchers={"codex": lambda: None})
    p.refresh()
    assert state["limits"] == {"codex": ""}


def test_poller_thread_fetches_on_start_and_on_poke():
    state = {}
    fetched = threading.Semaphore(0)
    value = {"pct": 4}

    def fetch():
        fetched.release()
        return [Window("5h", value["pct"])]

    p = RateLimitPoller(["claude"], state, fetchers={"claude": fetch},
                        interval=3600, min_gap=0)
    p.start()
    try:
        assert fetched.acquire(timeout=5)
        assert state["limits"]["claude"] == "5h 4%"
        value["pct"] = 9
        p.poke()
        assert fetched.acquire(timeout=5)
        assert state["limits"]["claude"] == "5h 9%"
    finally:
        p.stop()
    assert not p.is_alive()


def test_poller_poke_inside_min_gap_is_ignored():
    state = {}
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [Window("5h", 1)]

    p = RateLimitPoller(["claude"], state, fetchers={"claude": fetch},
                        interval=3600, min_gap=3600)
    p.start()
    try:
        deadline = 5
        while calls["n"] < 1 and deadline > 0:
            threading.Event().wait(0.05)
            deadline -= 0.05
        assert calls["n"] == 1
        p.poke()
        threading.Event().wait(0.3)
        assert calls["n"] == 1
    finally:
        p.stop()


def test_poller_default_fetchers_cover_claude_and_codex_only():
    p = RateLimitPoller(["claude", "codex", "opencode"], {})
    assert set(p.fetchers) == {"claude", "codex"}


# -- throttling by the endpoint itself ---------------------------------------


def test_fetch_429_raises_throttled_with_retry_after(server):
    _Handler.status, _Handler.body = 429, b'{"error":{"type":"rate_limit_error"}}'
    _Handler.extra_headers = {"Retry-After": "213"}
    try:
        with pytest.raises(ratelimit.Throttled) as exc:
            ratelimit.fetch_claude("tok", url=server + "/usage")
        assert exc.value.retry_after == 213
        with pytest.raises(ratelimit.Throttled):
            ratelimit.fetch_codex("tok", "acct", url=server + "/wham/usage")
    finally:
        _Handler.extra_headers = {}


def test_fetch_429_without_retry_after_backs_off_by_default(server):
    _Handler.status, _Handler.body = 429, b"{}"
    with pytest.raises(ratelimit.Throttled) as exc:
        ratelimit.fetch_claude("tok", url=server + "/usage")
    assert exc.value.retry_after == ratelimit.DEFAULT_RETRY_AFTER


def test_poller_keeps_the_last_figure_and_backs_off_when_throttled():
    state = {}
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return [Window("5h", 4)]
        raise ratelimit.Throttled(3600)

    p = RateLimitPoller(["claude"], state, fetchers={"claude": fetch})
    p.refresh()
    p.refresh()
    assert state["limits"] == {"claude": "5h 4%"}   # kept, not blanked
    p.refresh()
    assert calls["n"] == 2                          # backing off: no third call


def test_poller_throttled_with_nothing_known_stays_blank():
    def fetch():
        raise ratelimit.Throttled(3600)

    state = {}
    p = RateLimitPoller(["claude"], state, fetchers={"claude": fetch})
    p.refresh()
    assert state["limits"] == {"claude": ""}
