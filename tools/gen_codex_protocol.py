#!/usr/bin/env python3
# tools/gen_codex_protocol.py
"""Generate src/tandem/chat/runtime/codex_protocol.py from codex's app-server
JSON schema.

Default input is the schema the installed binary dumps
(`codex app-server generate-json-schema --out DIR`), so the models match
the codex on this machine exactly; `--schema-dir` points at a checkout's
`codex-rs/app-server-protocol/schema/json` instead (then pass
`--codex-version`). Only WANTED and what it references are emitted, via
datamodel-code-generator (dev-only dependency).

Schema facts the merge handles: definitions are namespaced under
`v2`/`v1` (`#/definitions/v2/Name`), which the generator would treat as
modules; two files have dotted stems; approval/user-input params are
standalone top-level schemas named by file.

Usage: uv run python tools/gen_codex_protocol.py [--schema-dir DIR --codex-version 0.153.4]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "tandem" / "chat" / "runtime" / "codex_protocol.py"

WANTED = [
    "InitializeParams", "InitializeResponse", "ClientInfo",
    "ThreadStartParams", "ThreadStartResponse", "ThreadResumeParams", "ThreadResumeResponse",
    "TurnStartParams", "TurnStartResponse", "TurnInterruptParams",
    "Thread", "Turn", "TurnStatus", "ThreadItem",
    "ItemStartedNotification", "ItemCompletedNotification", "AgentMessageDeltaNotification",
    "CommandExecutionOutputDeltaNotification", "FileChangeOutputDeltaNotification",
    "ReasoningSummaryTextDeltaNotification", "TurnStartedNotification", "TurnCompletedNotification",
    "ThreadTokenUsageUpdatedNotification", "AccountRateLimitsUpdatedNotification", "ErrorNotification",
    "CommandExecutionRequestApprovalParams", "CommandExecutionRequestApprovalResponse",
    "FileChangeRequestApprovalParams", "FileChangeRequestApprovalResponse",
    "PermissionsRequestApprovalParams", "PermissionsRequestApprovalResponse",
    "ToolRequestUserInputParams", "ToolRequestUserInputResponse",
]


def dump_schema_from_binary(tmp: Path) -> Path:
    subprocess.run(["codex", "app-server", "generate-json-schema", "--out", str(tmp)],
                   check=True, capture_output=True, text=True)
    hits = list(tmp.rglob("ClientRequest.json"))
    if not hits:
        sys.exit("codex app-server generate-json-schema produced no ClientRequest.json")
    return hits[0].parent


def codex_version() -> str:
    out = subprocess.run(["codex", "--version"], check=True, capture_output=True, text=True).stdout
    m = re.search(r"(\d+(?:\.\d+)+)", out)
    return m.group(1) if m else out.strip()


def normalize_refs(o) -> None:
    """`#/definitions/v2/Name` -> `#/definitions/Name`, in place."""
    if isinstance(o, dict):
        ref = o.get("$ref")
        if isinstance(ref, str):
            name = ref.split("#/definitions/")[-1].split("/")[-1]
            o["$ref"] = "#/definitions/" + (name[:-5] if name.endswith(".json") else name)
        for v in o.values():
            normalize_refs(v)
    elif isinstance(o, list):
        for v in o:
            normalize_refs(v)


def ref_targets(o, out: set[str]) -> None:
    """Collect every `#/definitions/Name` a normalized fragment points at."""
    if isinstance(o, dict):
        ref = o.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            out.add(ref[len("#/definitions/"):])
        for v in o.values():
            ref_targets(v, out)
    elif isinstance(o, list):
        for v in o:
            ref_targets(v, out)


def reachable(defs: dict, roots: list[str]) -> dict:
    """`defs` pruned to the roots plus what they reach through `$ref`.

    Without this every merged definition would be emitted, so the module
    would carry the whole app-server protocol instead of WANTED's closure.
    """
    seen: set[str] = set()
    dangling: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen or name in dangling:
            continue
        if name not in defs:
            dangling.add(name)
            continue
        seen.add(name)
        targets: set[str] = set()
        ref_targets(defs[name], targets)
        queue.extend(targets)
    if dangling:
        sys.exit(f"reachable definitions point at missing names: {sorted(dangling)}")
    return {k: v for k, v in defs.items() if k in seen}


def merge(schema_dir: Path) -> tuple[dict, str]:
    defs: dict = {}
    digest = hashlib.sha256()
    for f in sorted(schema_dir.rglob("*.json")):
        raw = f.read_bytes()
        digest.update(f.name.encode()); digest.update(raw)
        d = json.loads(raw)
        raw_defs = d.get("definitions", {})
        for ns in ("v2", "v1"):                     # v2 wins on a name clash
            for k, v in (raw_defs.get(ns) or {}).items():
                defs.setdefault(k, v)
        for k, v in raw_defs.items():
            if k not in ("v1", "v2"):
                defs.setdefault(k, v)
        top = {k: v for k, v in d.items() if k not in ("definitions", "$schema")}
        if f.stem.isidentifier() and any(k in top for k in ("properties", "oneOf", "enum", "type")):
            defs.setdefault(f.stem, top)
    normalize_refs(defs)
    missing = [w for w in WANTED if w not in defs]
    if missing:
        sys.exit(f"schema is missing wanted definitions: {missing}")
    defs = reachable(defs, WANTED)
    root = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CodexProtocolRoot", "type": "object",
        "properties": {w: {"$ref": f"#/definitions/{w}"} for w in WANTED},
        "definitions": defs,
    }
    return root, digest.hexdigest()[:12]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema-dir", type=Path)
    ap.add_argument("--codex-version")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="codex-schema-"))
    try:
        schema_dir = a.schema_dir or dump_schema_from_binary(tmp)
        version = a.codex_version or codex_version()
        root, sha = merge(schema_dir)
        merged = tmp / "merged.json"
        merged.write_text(json.dumps(root))
        gen = tmp / "generated.py"
        codegen = shutil.which("datamodel-codegen") or str(Path(sys.executable).parent / "datamodel-codegen")
        subprocess.run([
            codegen, "--input", str(merged), "--input-file-type", "jsonschema",
            "--output", str(gen), "--output-model-type", "pydantic_v2.BaseModel",
            "--target-python-version", "3.11", "--use-annotated",
            "--use-standard-collections", "--use-union-operator", "--disable-timestamp",
            "--allow-extra-fields", "--collapse-root-models", "--enum-field-as-literal", "all",
        ], check=True)
        header = (f"# generated by tools/gen_codex_protocol.py from codex {version} "
                  f"(schema sha256 {sha}) — do not edit\n# ruff: noqa\n")
        a.out.write_text(header + gen.read_text())
        print(f"wrote {a.out} ({len(a.out.read_text().splitlines())} lines) from codex {version}, schema {sha}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
