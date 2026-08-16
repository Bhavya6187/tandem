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
    return max(0, min(100, round(value)))


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
        if (pct is None or isinstance(secs, bool)
                or not isinstance(secs, (int, float)) or secs <= 0):
            continue
        out.append(Window(window_label(int(secs)), pct))
    return out


def format_windows(windows: list[Window]) -> str:
    # every glyph one cell wide (frame.StatusBar.line's width rule)
    return " ".join(f"{w.label} {w.used_percent}%" for w in windows)


# -- credentials -------------------------------------------------------------

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/"


class _SharedState:
    """Poller state that outlives one poller. A tandem session runs one
    `InteractiveRunner` per flip leg, each with its own poller; the account
    is the same throughout, so the endpoint's backoff, the min-gap clock and
    the last figures must carry across — or every flip is an immediate
    fetch pair that ignores an in-flight 429, and the new leg's bar goes
    blank until its first fetch returns."""

    def __init__(self) -> None:
        self.not_before: dict[str, float] = {}   # per-harness 429 backoff
        self.last_refresh: float = float("-inf")
        self.text: dict[str, str] = {}           # last published, per harness
        self.keychain_dead: bool = False         # one failure and we stop asking


_shared = _SharedState()


def _keychain_secret_uncached() -> str | None:
    """Claude Code keeps its OAuth blob in the login keychain on macOS
    (service `Claude Code-credentials`); elsewhere it is a file. One
    failure — the binary missing, a denied or timed-out (i.e. prompting)
    ACL — latches the read off for the process: a keychain dialog every
    60 s of an interactive session is not fail-quiet."""
    if sys.platform != "darwin" or _shared.keychain_dead:
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5, check=False,
            stdin=subprocess.DEVNULL,   # never touch the raw-mode tty
        )
    except (OSError, subprocess.SubprocessError):
        _shared.keychain_dead = True
        return None
    if proc.returncode != 0:
        _shared.keychain_dead = True
        return None
    return proc.stdout.strip()


_keychain_secret = _keychain_secret_uncached


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib re-sends Authorization across redirects, cross-host included;
    # these are fixed-URL calls, so any 3xx is simply a failed fetch
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _get_json(url: str, headers: dict[str, str], timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with _opener.open(req, timeout=timeout) as resp:
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
    """Owns the bar's rate-limit figures for every participant of a session
    leg: fetches each harness's account limits when the pump reports a bar
    (`ensure_started`), every `interval` seconds after, and on `poke()` (a
    turn just landed) — never more than once per `min_gap`, counted across
    legs. Publishes into `state["limits"]` — harness id → bar text ("" when
    unknown) — with a whole-dict replace, so the pump's read is a single
    GIL-atomic slot fetch, same as the usage text; the last figures of the
    previous leg are published at construction, so a fresh leg's bar never
    blanks. Credentials are re-read on every fetch: both CLIs rotate their
    tokens in place. `halt()` signals from any thread (the pump's drop path
    included) without joining; `stop()` also joins."""

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
        self._start_lock = threading.Lock()
        self._launched = False
        self.state["limits"] = {h: _shared.text.get(h, "") for h in self.fetchers}

    def refresh(self) -> None:
        now = time.monotonic()
        _shared.last_refresh = now
        out: dict[str, str] = {}
        for h, fetch in self.fetchers.items():
            if now < _shared.not_before.get(h, 0.0):
                out[h] = _shared.text.get(h, "")   # still told not to ask: keep what we had
                continue
            try:
                windows = fetch()
            except Throttled as exc:
                _shared.not_before[h] = now + exc.retry_after
                out[h] = _shared.text.get(h, "")
                continue
            except Exception:
                windows = None
            out[h] = format_windows(windows) if windows else ""
        _shared.text.update(out)
        self.state["limits"] = out

    def poke(self) -> None:
        if time.monotonic() - _shared.last_refresh >= self.min_gap:
            self._wake.set()

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._launched or self._halt.is_set():
                return
            self._launched = True
        self.start()

    def run(self) -> None:
        while not self._halt.is_set():
            if time.monotonic() - _shared.last_refresh >= self.min_gap:
                self.refresh()
            self._wake.wait(self.interval)
            self._wake.clear()

    def halt(self) -> None:
        self._halt.set()
        self._wake.set()

    def stop(self) -> None:
        self.halt()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1.0)
