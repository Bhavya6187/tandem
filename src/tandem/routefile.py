"""Route-request and frame-state files: the two channels between the
UserPromptSubmit hook (a separate short-lived process inside the harness)
and the tandem frame that owns the session.

- frame state (`<id>-frame.json`): frame → hook. Which tab is active and
  which harness has focus; rewritten by the runner's mixer thread on every
  tab change. The hook treats a missing/corrupt file as "not the mixed tab"
  and stays silent — the safe default.
- route request (`<id>-route.json`): hook → frame. One pending routed turn.
  Lifecycle: the hook writes `pending` (durably, BEFORE blocking the turn —
  the stash is what makes the prompt unlosable); the frame flips it to
  `dispatched` at pickup so its own next run cannot re-arm on it; the
  injector deletes it once the prompt has landed in the target. A
  `dispatched` file still on disk at the next frame start is a routed prompt
  that never landed; the frame surfaces it there — but only while it is
  within ROUTE_TTL, since that is the only window any reader here can see.
  An older leftover is invisible to `read_route` and is cleared silently at
  frame start rather than replayed into a stale session.

Best-effort like pinstash: writes go through `util.write_file_atomic`
(fsync + rename in the destination dir) so a concurrent read never sees a
torn entry, and every failure degrades to "no route" rather than raising
into a hook or the frame."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from . import paths, util

ROUTE_TTL = 600


@dataclass(frozen=True)
class RouteRequest:
    target: str
    model: str
    prompt: str
    source: str
    reason: str
    state: str = "pending"


def _route_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-route.json"


def _frame_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-frame.json"


def _write_json(path, obj: dict) -> None:
    try:
        # serialize first, so a bad snapshot leaves no file and no scratch
        # file behind; write_file_atomic then does mkdir + fsync + rename
        # under a random scratch name, which is what keeps the hook and the
        # frame's mixer thread from clobbering each other mid-write
        util.write_file_atomic(path, json.dumps(obj))
    # TypeError/ValueError: an unserializable snapshot must not raise into
    # the mixer thread that writes it — a missing file already means "not
    # the mixed tab", which is the safe default on the reading side too
    except (OSError, TypeError, ValueError):
        pass


def _read_json(path) -> dict | None:
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_route(tandem_id: str, req: RouteRequest) -> None:
    _write_json(_route_path(tandem_id), asdict(req))


def read_route(tandem_id: str) -> RouteRequest | None:
    p = _route_path(tandem_id)
    try:
        if time.time() - p.stat().st_mtime > ROUTE_TTL:
            return None
    except OSError:
        return None
    obj = _read_json(p)
    if obj is None:
        return None
    try:
        return RouteRequest(**{k: str(obj[k]) for k in
                               ("target", "model", "prompt", "source",
                                "reason")},
                            state=str(obj.get("state", "pending")))
    except (KeyError, TypeError):
        return None


def mark_dispatched(tandem_id: str, req: RouteRequest) -> None:
    write_route(tandem_id, RouteRequest(
        req.target, req.model, req.prompt, req.source, req.reason,
        state="dispatched"))


def clear_route(tandem_id: str) -> None:
    try:
        _route_path(tandem_id).unlink(missing_ok=True)
    except OSError:
        pass


def write_frame_state(tandem_id: str, snapshot: dict) -> None:
    _write_json(_frame_path(tandem_id), snapshot)


def read_frame_state(tandem_id: str) -> dict | None:
    return _read_json(_frame_path(tandem_id))
