"""Route-request and frame-state files: the two channels between the
UserPromptSubmit hook (a separate short-lived process inside the harness)
and the tandem frame that owns the session.

- frame state (`<id>-frame.json`): frame → hook. Which tab is active and
  which harness has focus; rewritten by the runner on every tab change. The
  hook treats a missing/corrupt file as "not the mixed tab" and stays
  silent — the safe default.
- route request: hook → frame. One immutable file per request, and the
  *location* is the state, so nothing is ever mutated in place:
  `<session>-route.<request>.json` is pending (the hook wrote it, nobody has
  taken it) and `<session>-route.<request>.claimed.json` is claimed (the
  frame took it by rename and is on the hook for delivering it).

Lifecycle: the hook writes pending durably BEFORE blocking the turn — the
stash is what makes the prompt unlosable; the frame `claim`s it at pickup,
which is one atomic rename of that exact path, so its own next tick cannot
re-arm on it; the injector `release`s it once the prompt has landed.
Whatever is still on disk at the next frame start is a routed prompt that
never ran: `sweep` deletes every request file and hands back what it held,
at any age, so the frame can quote it. There is no TTL — an old leftover is
still a typed prompt, and deleting it quietly would be the one way to lose
one.

Every request carries an `id`, and the id is in the filename: the hook
proves ITS stash landed by id, the frame renames that one path, delivery
unlinks that one path. Two prompts in flight never share a file, so there
is nothing to lock and no read-then-rename window to race.

Best-effort like pinstash: writes go through `util.write_file_atomic`
(fsync + rename in the destination dir) so a concurrent read never sees a
torn entry, and every failure degrades to "no route" rather than raising
into a hook or the frame."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field

from . import paths, util

_FIELDS = ("target", "model", "prompt", "source", "reason", "id")
# The id is a filename component, so it is validated everywhere it is
# taken from outside this process: a request file's content, a hook's
# stash, a caller's claim/release.
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{12}")


def _valid_request_id(req_id: str) -> bool:
    return bool(_REQUEST_ID_RE.fullmatch(req_id))


@dataclass(frozen=True)
class RouteRequest:
    target: str
    model: str
    prompt: str
    source: str
    reason: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


def _pending_path(tandem_id: str, req_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-route.{req_id}.json"


def _claimed_path(tandem_id: str, req_id: str):
    return (paths.tandem_home() / "tmp"
            / f"{tandem_id}-route.{req_id}.claimed.json")


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
        req = RouteRequest(**{k: str(obj[k]) for k in _FIELDS})
    except (KeyError, TypeError):
        return None
    return req if _valid_request_id(req.id) else None


def _unlink(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _request_paths(tandem_id: str, claimed: bool) -> list:
    """This session's request files of one kind, in name order."""
    pattern = re.compile(
        re.escape(tandem_id) + r"-route\.[0-9a-f]{12}"
        + (r"\.claimed" if claimed else "") + r"\.json")
    try:
        return sorted(p for p in (paths.tandem_home() / "tmp").iterdir()
                      if pattern.fullmatch(p.name))
    except OSError:
        return []


def write_route(tandem_id: str, req: RouteRequest) -> None:
    if not _valid_request_id(req.id):
        return
    _write_json(_pending_path(tandem_id, req.id), asdict(req))


def read_pending(tandem_id: str, req_id: str | None = None) -> RouteRequest | None:
    """One request by id, or the first pending one when no id is given."""
    if req_id is not None:
        if not _valid_request_id(req_id):
            return None
        return _read_req(_pending_path(tandem_id, req_id))
    return next((req for path in _request_paths(tandem_id, False)
                 if (req := _read_req(path)) is not None), None)


def read_claimed(tandem_id: str, req_id: str | None = None) -> RouteRequest | None:
    if req_id is not None:
        if not _valid_request_id(req_id):
            return None
        return _read_req(_claimed_path(tandem_id, req_id))
    return next((req for path in _request_paths(tandem_id, True)
                 if (req := _read_req(path)) is not None), None)


def claim(tandem_id: str, req_id: str) -> bool:
    """Take one pending request. The rename of its exact path is the claim:
    after it the frame owns this request, and any other request in flight
    is untouched."""
    if not _valid_request_id(req_id):
        return False
    source = _pending_path(tandem_id, req_id)
    if _read_req(source) is None:
        return False
    try:
        os.replace(source, _claimed_path(tandem_id, req_id))
    except OSError:
        return False
    return True


def release(tandem_id: str, req_id: str) -> None:
    """Delete this one request wherever it sits: the claimed path once the
    prompt has landed, the pending path when the frame drops one it cannot
    route."""
    if not _valid_request_id(req_id):
        return
    _unlink(_claimed_path(tandem_id, req_id))
    _unlink(_pending_path(tandem_id, req_id))


def sweep(tandem_id: str, preserve_id: str | None = None
          ) -> tuple[list[RouteRequest], list[RouteRequest]]:
    """Clear this session's leftovers and return `(pending, claimed)`: the
    prompts that were never picked up and the ones that were never
    delivered.

    `preserve_id` is the request the calling run is about to deliver; it
    stays. Everything else goes, whatever the caller does with it: it
    belongs to a run that is over, and replaying it into a fresh session
    would type a stale prompt into a harness the user has moved on from. A
    file that will not parse is deleted without a word — there is nothing
    to quote — but every write here is atomic, so that is a file something
    else damaged."""
    found: list[list[RouteRequest]] = []
    for claimed in (False, True):
        group = []
        for path in _request_paths(tandem_id, claimed):
            req = _read_req(path)
            if req is not None and req.id == preserve_id:
                continue
            if req is not None:
                group.append(req)
            _unlink(path)
        found.append(group)
    return found[0], found[1]


def write_frame_state(tandem_id: str, snapshot: dict) -> None:
    _write_json(_frame_path(tandem_id), snapshot)


def read_frame_state(tandem_id: str) -> dict | None:
    return _read_json(_frame_path(tandem_id))
