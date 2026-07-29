"""Instruction/memory file sync: CLAUDE.md <-> AGENTS.md, plus opt-in MCP
server copying.

v1 mapping: shared content lives in a marker-delimited common block; text
outside the markers is tool-specific and never touched.

    <!-- tandem:shared:begin -->
    ...shared instructions...
    <!-- tandem:shared:end -->

Rules (documented in README):
- A file without markers contributes its ENTIRE content as the shared block
  but is itself never modified (we don't rewrite user files to add markers).
- The counterpart file is created/refreshed with the shared block wrapped in
  markers; anything it keeps outside the markers survives.
- When both files carry markers, the newer mtime wins the shared block.
- When both exist and neither has markers we refuse to guess and report it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .util import write_file_atomic

BEGIN = "<!-- tandem:shared:begin -->"
END = "<!-- tandem:shared:end -->"

_BLOCK_RE = re.compile(
    re.escape(BEGIN) + r"\n?(.*?)\n?" + re.escape(END), re.S
)

FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}


@dataclass
class MemorySyncReport:
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _shared_block(text: str) -> str | None:
    m = _BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


def _replace_block(text: str, shared: str) -> str:
    new_block = f"{BEGIN}\n{shared}\n{END}"
    if _BLOCK_RE.search(text):
        # replacement via callable: content must be literal, never treated
        # as backreference syntax
        return _BLOCK_RE.sub(lambda _m: new_block, text, count=1)
    prefix = text.rstrip() + "\n\n" if text.strip() else ""
    return prefix + new_block + "\n"


def sync_memory_files(cwd: str) -> MemorySyncReport:
    """Reflect shared instructions between CLAUDE.md and AGENTS.md in cwd."""
    report = MemorySyncReport()
    root = Path(cwd)
    paths_ = {k: root / v for k, v in FILES.items()}
    texts = {}
    for k, p in paths_.items():
        try:
            texts[k] = p.read_text() if p.exists() else None
        except OSError as exc:
            report.warnings.append(f"cannot read {p.name}: {exc}")
            return report

    existing = [k for k, t in texts.items() if t is not None]
    if not existing:
        return report

    if len(existing) == 1:
        src = existing[0]
        dst = "codex" if src == "claude" else "claude"
        shared = _shared_block(texts[src])
        if shared is None:
            shared = texts[src].strip()
        if not shared:
            return report
        write_file_atomic(paths_[dst], _replace_block("", shared))
        report.actions.append(
            f"created {FILES[dst]} from {FILES[src]} (shared block)"
        )
        return report

    blocks = {k: _shared_block(texts[k]) for k in ("claude", "codex")}
    if blocks["claude"] is None and blocks["codex"] is None:
        report.warnings.append(
            f"both {FILES['claude']} and {FILES['codex']} exist without tandem "
            f"markers; not merging. Wrap the shared part of one of them in "
            f"'{BEGIN}' / '{END}' to enable sync."
        )
        return report

    # Effective shared content per side: the marker block, or (for a
    # marker-less file) its whole content.
    effective = {
        k: blocks[k] if blocks[k] is not None else texts[k].strip()
        for k in ("claude", "codex")
    }
    if effective["claude"] == effective["codex"]:
        return report

    newer = max(("claude", "codex"), key=lambda k: paths_[k].stat().st_mtime)
    older = "codex" if newer == "claude" else "claude"
    if blocks[older] is None:
        # never rewrite a user file that has no markers
        report.warnings.append(
            f"{FILES[older]} has no tandem markers; not updating it from "
            f"{FILES[newer]}."
        )
        return report
    write_file_atomic(
        paths_[older], _replace_block(texts[older], effective[newer])
    )
    report.actions.append(
        f"updated shared block in {FILES[older]} from {FILES[newer]}"
    )
    return report


# -- MCP config copy (opt-in via `tandem sync-mcp`) --------------------------


def claude_mcp_servers() -> dict[str, dict]:
    """Global stdio MCP servers from ~/.claude.json."""
    cfg = Path.home() / ".claude.json"
    try:
        data = json.loads(cfg.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") or {}
    return {
        name: s
        for name, s in servers.items()
        if isinstance(s, dict) and s.get("command")
    }


def codex_mcp_servers() -> dict[str, dict]:
    """[mcp_servers.*] from ~/.codex/config.toml."""
    import tomllib

    cfg = paths.codex_home() / "config.toml"
    try:
        with open(cfg, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcp_servers") or {}
    return {
        name: s
        for name, s in servers.items()
        if isinstance(s, dict) and s.get("command")
    }


def _toml_str(value: str) -> str:
    return json.dumps(value)  # JSON string escaping is valid TOML


def copy_mcp(report: MemorySyncReport | None = None) -> MemorySyncReport:
    """Bidirectional additive copy of stdio MCP servers: each side gains the
    servers it is missing; existing definitions are never modified."""
    report = report or MemorySyncReport()
    claude_servers = claude_mcp_servers()
    codex_servers = codex_mcp_servers()

    # claude -> codex: append TOML sections for missing servers
    missing_in_codex = {
        n: s for n, s in claude_servers.items() if n not in codex_servers
    }
    if missing_in_codex:
        cfg = paths.codex_home() / "config.toml"
        text = cfg.read_text() if cfg.exists() else ""
        chunks = [text.rstrip() + "\n" if text.strip() else ""]
        for name, s in missing_in_codex.items():
            chunks.append(f"\n# added by tandem sync-mcp\n[mcp_servers.{name}]")
            chunks.append(f"command = {_toml_str(s['command'])}")
            args = s.get("args") or []
            chunks.append(
                "args = [" + ", ".join(_toml_str(a) for a in args) + "]"
            )
            env = s.get("env") or {}
            if env:
                chunks.append(f"\n[mcp_servers.{name}.env]")
                for k, v in env.items():
                    chunks.append(f"{k} = {_toml_str(str(v))}")
            report.actions.append(f"added MCP server {name!r} to codex config.toml")
        write_file_atomic(cfg, "\n".join(chunks) + "\n")

    # codex -> claude: merge into ~/.claude.json mcpServers
    missing_in_claude = {
        n: s for n, s in codex_servers.items() if n not in claude_servers
    }
    if missing_in_claude:
        cfg = Path.home() / ".claude.json"
        try:
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
        except json.JSONDecodeError:
            report.warnings.append("~/.claude.json is not valid JSON; skipping")
            return report
        servers = data.setdefault("mcpServers", {})
        for name, s in missing_in_claude.items():
            entry: dict = {"command": s["command"], "args": list(s.get("args") or [])}
            if s.get("env"):
                entry["env"] = {k: str(v) for k, v in s["env"].items()}
            servers[name] = entry
            report.actions.append(f"added MCP server {name!r} to ~/.claude.json")
        write_file_atomic(cfg, json.dumps(data, indent=2) + "\n")

    if not report.actions:
        report.actions.append("MCP configs already in sync (nothing copied)")
    return report
