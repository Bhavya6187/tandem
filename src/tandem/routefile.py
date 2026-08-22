"""Route-request and frame-state files: the two channels between the
UserPromptSubmit hook (a separate short-lived process inside the harness)
and the tandem frame that owns the session.

- frame state (`<id>-frame.json`): frame → hook. Which tab is active and
  which harness has focus; rewritten by the runner's mixer thread on every
  tab change. The hook treats a missing/corrupt file as "not the mixed tab"
  and stays silent — the safe default.
- route request: hook → frame. Two filenames, and the *location* is the
  state, so nothing here is ever mutated in place:
  `<id>-route.json` is pending (the hook wrote it, nobody has taken it) and
  `<id>-route.claimed.json` is claimed (the frame took it by rename, and is
  on the hook for delivering it).

Lifecycle: the hook writes pending durably BEFORE blocking the turn — the
stash is what makes the prompt unlosable; the frame `claim`s it at pickup,
which is one atomic rename, so its own next tick cannot re-arm on it; the
injector `release`s it once the prompt has landed. Whatever is still on
disk at the next frame start is a routed prompt that never ran: `sweep`
deletes both files and hands back what they held, at any age, so the frame
can quote them. There is no TTL — an old leftover is still a typed prompt,
and deleting it quietly would be the one way to lose one.

Every request carries an `id`, and that is what makes each hand-off safe
without a lock: the hook proves ITS stash landed by id, the frame claims
one specific id, and delivery releases only the id it delivered. The single
pending slot means a second routed prompt written before pickup overwrites
the first — but the losing hook's verify then fails the id check and allows
its native turn, so the prompt runs where it was typed instead of vanishing.

Best-effort like pinstash: writes go through `util.write_file_atomic`
(fsync + rename in the destination dir) so a concurrent read never sees a
torn entry, and every failure degrades to "no route" rather than raising
into a hook or the frame."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field

from . import paths, util

_FIELDS = ("target", "model", "prompt", "source", "reason", "id")


@dataclass(frozen=True)
class RouteRequest:
    target: str
    model: str
    prompt: str
    source: str
    reason: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


def _pending_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-route.json"


def _claimed_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-route.claimed.json"


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


def _read_req(path) -> RouteRequest | None:
    obj = _read_json(path)
    if obj is None:
        return None
    try:
        return RouteRequest(**{k: str(obj[k]) for k in _FIELDS})
    except (KeyError, TypeError):
        return None


def _unlink(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def write_route(tandem_id: str, req: RouteRequest) -> None:
    _write_json(_pending_path(tandem_id), asdict(req))


def read_pending(tandem_id: str) -> RouteRequest | None:
    return _read_req(_pending_path(tandem_id))


def read_claimed(tandem_id: str) -> RouteRequest | None:
    return _read_req(_claimed_path(tandem_id))


def claim(tandem_id: str, req_id: str) -> bool:
    """Take the pending request, if it is still the one with this id.

    The rename is the claim: after it the frame owns the request and a
    later tick reads an empty pending slot. Checking the id first is what
    keeps a second prompt — written between validation and here — from
    being armed as a flip nobody looked at."""
    req = _read_req(_pending_path(tandem_id))
    if req is None or req.id != req_id:
        return False
    try:
        os.replace(_pending_path(tandem_id), _claimed_path(tandem_id))
    except OSError:
        return False
    return True


def release(tandem_id: str, req_id: str) -> None:
    """Delete this request's file wherever it sits: the claimed slot once
    the prompt has landed, the pending slot when the frame drops one it
    cannot route. Never anyone else's — a second prompt written while this
    one was in flight carries a different id and is left alone."""
    for path in (_claimed_path(tandem_id), _pending_path(tandem_id)):
        req = _read_req(path)
        if req is not None and req.id == req_id:
            _unlink(path)
            return


def sweep(tandem_id: str) -> tuple[RouteRequest | None, RouteRequest | None]:
    """Startup recovery: clear both slots and return `(pending, claimed)` —
    a prompt that was never picked up and one that was never delivered.

    Whatever the caller does with them, the files go: they belong to a run
    that is over, and replaying one into a fresh session would type a stale
    prompt into a harness the user has moved on from. Unparseable files are
    deleted too — there is nothing to quote, and nothing else would ever
    remove them."""
    found = []
    for path in (_pending_path(tandem_id), _claimed_path(tandem_id)):
        found.append(_read_req(path))
        _unlink(path)
    return found[0], found[1]


def write_frame_state(tandem_id: str, snapshot: dict) -> None:
    _write_json(_frame_path(tandem_id), snapshot)


def read_frame_state(tandem_id: str) -> dict | None:
    return _read_json(_frame_path(tandem_id))
