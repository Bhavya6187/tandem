"""Small shared helpers: atomic file IO and timestamp/id formats."""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_now_ms() -> str:
    """Both harnesses write UTC ISO-8601 with millisecond precision + 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (time.time_ns() // 1_000_000 % 1000)
    )


def uuid7() -> str:
    """RFC 9562 UUIDv7 (time-ordered). Codex session ids are v7; stdlib has no
    generator until 3.14, so we build one from unix-ms + random bits."""
    ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return str(uuid.UUID(int=value))


def json_line(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def append_jsonl_fsync(path: Path, entries: list[dict]) -> None:
    """Append whole JSONL lines and fsync before returning, so a crash can
    never leave the shadow file with a torn entry that was already recorded
    as synced."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(json_line(e) for e in entries)
    with open(path, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_file_atomic(path: Path, data: str | bytes) -> None:
    """write-temp + rename in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    tmp = path.with_name(f".{path.name}.tandem-tmp-{secrets.token_hex(4)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def git_branch(cwd: str) -> str:
    head = Path(cwd) / ".git" / "HEAD"
    try:
        text = head.read_text().strip()
    except OSError:
        return ""
    if text.startswith("ref: refs/heads/"):
        return text.removeprefix("ref: refs/heads/")
    return "HEAD"
