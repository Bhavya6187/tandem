"""Route-request and frame-state files: the two channels between the
UserPromptSubmit hook (a separate short-lived process inside the harness)
and the tandem frame that owns the session.

- frame state (`<id>-frame.json`): frame → hook. Which tab is active and
  which harness has focus; rewritten by the runner's mixer thread on every
  tab change. The hook treats a missing/corrupt file as "not the mixed tab"
  and stays silent — the safe default.
- route request: hook → frame. Every request gets its own immutable file;
  the *location* is the state, so nothing is mutated in place:
  `<session>-route.<request>.json` is pending and
  `<session>-route.<request>.claimed.json` is claimed by exact-path rename.

Lifecycle: the hook writes pending durably BEFORE blocking the turn — the
stash is what makes the prompt unlosable; the frame `claim`s it at pickup,
which is one atomic rename, so its own next tick cannot re-arm on it; the
injector `release`s it once the prompt has landed. Whatever is still on
disk at the next frame start is a routed prompt that never ran: `sweep`
deletes every request file and hands back what it held, at any age, so the frame
can quote them. There is no TTL — an old leftover is still a typed prompt,
and deleting it quietly would be the one way to lose one.

Every request carries an `id`, and that is what makes each hand-off safe
without a lock: the hook proves ITS stash landed by id, the frame renames
that exact request path, and delivery unlinks only that id. Separate paths
mean concurrent hooks cannot overwrite one another.

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
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{12}\Z")


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


def _pending_path(tandem_id: str, req_id: str | None = None):
    """One pending request path; no id names the pre-v3 legacy slot."""
    name = (f"{tandem_id}-route.{req_id}.json" if req_id else
            f"{tandem_id}-route.json")
    return paths.tandem_home() / "tmp" / name


def _claimed_path(tandem_id: str, req_id: str | None = None):
    """One claimed request path; no id names the pre-v3 legacy slot."""
    name = (f"{tandem_id}-route.{req_id}.claimed.json" if req_id else
            f"{tandem_id}-route.claimed.json")
    return paths.tandem_home() / "tmp" / name


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


def _read_quotable(path) -> RouteRequest | None:
    """A request good enough to quote in a note, even when it is not good
    enough to route. Only the sweep uses it, and the sweep is the last
    thing that will ever see these files: a shape this version cannot read
    — a request written by a tandem from before the ids, say — must still
    reach the user as text instead of being deleted in silence. The
    synthetic id is never used for anything; nothing claims or releases a
    request the sweep has already taken off disk. No prompt really is
    nothing to say."""
    req = _read_req(path)
    if req is not None:
        return req
    obj = _read_json(path)
    if obj is None or not isinstance(obj.get("prompt"), str):
        return None
    return RouteRequest(target=str(obj.get("target", "")), model="",
                        prompt=obj["prompt"], source="", reason="")


def _unlink(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _request_paths(tandem_id: str, claimed: bool) -> list:
    """Current per-request paths in deterministic order, plus a legacy
    shared slot when upgrading from the previous protocol."""
    root = paths.tandem_home() / "tmp"
    prefix = f"{tandem_id}-route."
    suffix = ".claimed.json" if claimed else ".json"
    try:
        found = [p for p in root.iterdir()
                 if p.name.startswith(prefix) and p.name.endswith(suffix)
                 and (claimed or not p.name.endswith(".claimed.json"))]
    except OSError:
        found = []
    legacy = _claimed_path(tandem_id) if claimed else _pending_path(tandem_id)
    try:
        if legacy.exists():
            found.append(legacy)
    except OSError:
        pass
    return sorted(found, key=lambda p: p.name)


def write_route(tandem_id: str, req: RouteRequest) -> None:
    if not _valid_request_id(req.id):
        return
    _write_json(_pending_path(tandem_id, req.id), asdict(req))


def read_pending(tandem_id: str, req_id: str | None = None) -> RouteRequest | None:
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
    """Take the pending request, if it is still the one with this id.

    The exact-path rename is the claim: after it the frame owns this request
    and concurrent request ids remain independently pending."""
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
    """Delete only this id's claimed/pending files. A concurrent request has
    a different path, so no read-before-unlink comparison is needed."""
    if not _valid_request_id(req_id):
        return
    _unlink(_claimed_path(tandem_id, req_id))
    _unlink(_pending_path(tandem_id, req_id))


def sweep(tandem_id: str, preserve_id: str | None = None
          ) -> tuple[list[RouteRequest], list[RouteRequest]]:
    """Clear leftovers and return pending/claimed lists.

    `preserve_id` belongs to the injecting run and remains in place; every
    other request, including legacy shared-slot files, is surfaced and removed.

    Whatever the caller does with them, the files go: they belong to a run
    that is over, and replaying one into a fresh session would type a stale
    prompt into a harness the user has moved on from. What comes back is
    read loosely on purpose (`_read_quotable`) — a file too old or too
    broken to route is still a prompt somebody typed, and this is its last
    chance to be quoted. Only a file with no prompt in it goes without a
    word; there is nothing to say about one."""
    found: list[list[RouteRequest]] = []
    for claimed in (False, True):
        group = []
        for path in _request_paths(tandem_id, claimed):
            req = _read_quotable(path)
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
