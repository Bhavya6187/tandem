"""Account rate limits for the bar: the `5h 4% 7d 4%` figures on each slot.

Both harnesses expose the numbers their own `/usage` (claude) and `/status`
(codex) commands show, through the same account endpoints those commands
call, authenticated with the credentials the CLIs already keep on disk.
Neither endpoint is documented, so this module is cosmetic by contract:
any failure — missing credentials, 401, timeout, unrecognized payload —
blanks the figure and never surfaces anywhere else. The one exception is
the endpoint throttling *us* (claude's answers 429 with a Retry-After of
a few minutes when asked too often): that keeps the last figure and backs
that harness off for as long as it said.

Windows are labeled by their length (18000 s → `5h`, 604800 s → `7d`),
never by position: codex plans differ in which of primary/secondary is
populated. Percentages are *used*, matching claude's `/usage`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from . import paths


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: int


def window_label(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def _percent(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


def parse_claude(payload) -> list[Window]:
    """`/api/oauth/usage`: `five_hour` / `seven_day` each carry `utilization`
    (a used percentage), independently absent."""
    if not isinstance(payload, dict):
        return []
    out: list[Window] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        win = payload.get(key)
        pct = _percent(win.get("utilization")) if isinstance(win, dict) else None
        if pct is not None:
            out.append(Window(label, pct))
    return out


def parse_codex(payload) -> list[Window]:
    """`wham/usage`: `rate_limit.primary_window` / `secondary_window` each
    carry `used_percent` and `limit_window_seconds`."""
    if not isinstance(payload, dict):
        return []
    rl = payload.get("rate_limit")
    if not isinstance(rl, dict):
        return []
    out: list[Window] = []
    for key in ("primary_window", "secondary_window"):
        win = rl.get(key)
        if not isinstance(win, dict):
            continue
        pct = _percent(win.get("used_percent"))
        secs = win.get("limit_window_seconds")
        if pct is None or isinstance(secs, bool) or not isinstance(secs, int) or secs <= 0:
            continue
        out.append(Window(window_label(secs), pct))
    return out


def format_windows(windows: list[Window]) -> str:
    # every glyph one cell wide (frame.StatusBar.line's width rule)
    return " ".join(f"{w.label} {w.used_percent}%" for w in windows)


# -- credentials -------------------------------------------------------------

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/"


def _keychain_secret() -> str | None:
    """Claude Code keeps its OAuth blob in the login keychain on macOS
    (service `Claude Code-credentials`); elsewhere it is a file."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def claude_token() -> str | None:
    raw = _keychain_secret()
    if raw is None:
        try:
            raw = (paths.claude_home() / ".credentials.json").read_text()
        except OSError:
            return None
    try:
        blob = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(blob, dict):
        return None
    oauth = blob.get("claudeAiOauth")
    tok = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return tok if isinstance(tok, str) and tok else None


def codex_credentials() -> tuple[str, str, str] | None:
    """(access token, account id, usage URL) from `$CODEX_HOME/auth.json`
    and the optional `chatgpt_base_url` in config.toml — mirroring
    codex-rs/backend-client: `/wham/usage` under a `/backend-api` base,
    `/api/codex/usage` otherwise. None for API-key logins."""
    home = paths.codex_home()
    try:
        blob = json.loads((home / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    tokens = blob.get("tokens") if isinstance(blob, dict) else None
    if not isinstance(tokens, dict):
        return None
    tok, acct = tokens.get("access_token"), tokens.get("account_id")
    if not (isinstance(tok, str) and tok and isinstance(acct, str) and acct):
        return None
    base = CODEX_DEFAULT_BASE_URL
    try:
        with open(home / "config.toml", "rb") as f:
            cfg_base = tomllib.load(f).get("chatgpt_base_url")
        if isinstance(cfg_base, str) and cfg_base:
            base = cfg_base
    except (OSError, ValueError):
        pass
    base = base.rstrip("/")
    path = "/wham/usage" if "/backend-api" in base else "/api/codex/usage"
    return tok, acct, base + path


# -- fetching ----------------------------------------------------------------

# a 429 with no Retry-After (live claude 429s carry one, ~200 s): long enough
# that a poller never becomes the thing keeping the endpoint throttled
DEFAULT_RETRY_AFTER = 300.0


class Throttled(Exception):
    """The usage endpoint itself said 429: the figure is not unknown, we
    were just told not to ask. Carries the seconds to wait."""

    def __init__(self, retry_after: float):
        super().__init__(f"retry after {retry_after:.0f}s")
        self.retry_after = retry_after


def _get_json(url: str, headers: dict[str, str], timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise Throttled(_retry_after(exc.headers.get("Retry-After"))) from None
        return None   # 401 (token rolled), 5xx: figure unknown for now
    except Exception:
        # URLError (refused, DNS), timeout, bad JSON
        return None


def _retry_after(value) -> float:
    # delta-seconds only; the HTTP-date form is legal but not worth parsing
    # for a cosmetic figure — the default backoff covers it
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    return secs if secs > 0 else DEFAULT_RETRY_AFTER


def fetch_claude(token: str, *, url: str = CLAUDE_USAGE_URL,
                 timeout: float = 5.0) -> list[Window] | None:
    payload = _get_json(url, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    }, timeout)
    return None if payload is None else parse_claude(payload)


def fetch_codex(token: str, account_id: str, *, url: str,
                timeout: float = 5.0) -> list[Window] | None:
    payload = _get_json(url, {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account_id,
        "User-Agent": "codex-cli",
    }, timeout)
    return None if payload is None else parse_codex(payload)


# -- poller ------------------------------------------------------------------


def _claude_fetcher() -> list[Window] | None:
    tok = claude_token()
    return None if tok is None else fetch_claude(tok)


def _codex_fetcher() -> list[Window] | None:
    creds = codex_credentials()
    return None if creds is None else fetch_codex(creds[0], creds[1], url=creds[2])


DEFAULT_FETCHERS: dict[str, Callable[[], list[Window] | None]] = {
    "claude": _claude_fetcher,
    "codex": _codex_fetcher,
}


class RateLimitPoller(threading.Thread):
    """Owns the bar's rate-limit figures for every participant of a session:
    fetches each harness's account limits on start, every `interval`
    seconds after, and on `poke()` (a turn just landed) no more than once
    per `min_gap`. Publishes into `state["limits"]` — harness id → bar text
    ("" when unknown) — with a whole-dict replace, so the pump's read is
    a single GIL-atomic slot fetch, same as the usage text. Credentials
    are re-read on every fetch: both CLIs rotate their tokens in place."""

    def __init__(self, harnesses: list[str], state: dict, *,
                 fetchers: dict[str, Callable[[], list[Window] | None]] | None = None,
                 interval: float = 60.0, min_gap: float = 30.0):
        super().__init__(name="tandem-ratelimit", daemon=True)
        pool = DEFAULT_FETCHERS if fetchers is None else fetchers
        self.fetchers = {h: pool[h] for h in harnesses if h in pool}
        self.state = state
        self.interval = interval
        self.min_gap = min_gap
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._last = float("-inf")
        self._not_before: dict[str, float] = {}   # per-harness 429 backoff

    def refresh(self) -> None:
        now = time.monotonic()
        self._last = now
        prev = self.state.get("limits") or {}
        out: dict[str, str] = {}
        for h, fetch in self.fetchers.items():
            if now < self._not_before.get(h, 0.0):
                out[h] = prev.get(h, "")   # still told not to ask: keep what we had
                continue
            try:
                windows = fetch()
            except Throttled as exc:
                self._not_before[h] = now + exc.retry_after
                out[h] = prev.get(h, "")
                continue
            except Exception:
                windows = None
            out[h] = format_windows(windows) if windows else ""
        self.state["limits"] = out

    def poke(self) -> None:
        if time.monotonic() - self._last >= self.min_gap:
            self._wake.set()

    def run(self) -> None:
        while not self._halt.is_set():
            self.refresh()
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1.0)
