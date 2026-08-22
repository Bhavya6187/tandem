"""Install the tandem plugin through each harness's own CLI.

Detection reads the harness's own registry (read-only; each CLI stays the
sole writer of its own state). Install shells out to `claude plugin …` —
verified idempotent on claude 2.1.220: re-adding the marketplace and
re-installing the plugin both exit 0 with an "already" notice — and then
mirrors the same two steps onto codex, which since 0.145 loads
claude-format plugin trees. Codex spells the second step `add`
(`codex plugin add <plugin>@<marketplace>`, verified on codex-cli
0.147.0), not `install`; its marketplace step matches claude's. The one-time offer lives here too so both
entry points (bare `tandem` and `tandem plugin install`) share a single
routine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import click

from . import paths

MARKETPLACE_REPO = "Bhavya6187/tandem"
PLUGIN_NAME = "tandem"
# <plugin>@<marketplace>; both halves happen to be "tandem"
PLUGIN_ID = f"{PLUGIN_NAME}@tandem"


def is_plugin_installed() -> bool:
    """True when claude's registry records a tandem@tandem install.

    Missing file or absent/empty entry is definitively False — a fresh
    claude install has no registry, and that user must get the offer.
    Everything ambiguous (unreadable, unparseable, unexpected shape)
    is True: the caller only decides whether to nag, and doubt must
    stay silent.
    """
    try:
        raw = paths.claude_installed_plugins_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    # ValueError covers the UnicodeDecodeError read_text() raises when the
    # file is not UTF-8: it is not an OSError, so without it an undecodable
    # registry would crash the caller instead of reading as ambiguous.
    except (OSError, ValueError):
        return True
    try:
        plugins = json.loads(raw)["plugins"]
        if not isinstance(plugins, dict):
            return True
        return bool(plugins.get(PLUGIN_ID))
    except Exception:
        return True


def is_plugin_installed_codex() -> bool:
    """True when codex's own config records a tandem@tandem plugin.

    Codex keeps installed plugins in its `config.toml` `[plugins]` table,
    keyed the same `<plugin>@<marketplace>` way claude keys its registry.
    The ambiguity rule mirrors is_plugin_installed(): a missing file or an
    absent entry is definitively False (a codex that has never installed a
    plugin has no table, and that user must get the nudge), while anything
    unreadable or unparseable is True — the caller only decides whether to
    warn, and doubt must stay silent.
    """
    try:
        with open(paths.codex_home() / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return False
    # ValueError covers tomllib's TOMLDecodeError; OSError covers a
    # config.toml that is a directory or is unreadable.
    except (OSError, ValueError):
        return True
    plugins = cfg.get("plugins")
    if plugins is None:
        return False           # no plugin has ever been installed here
    if not isinstance(plugins, dict):
        return True            # a shape tandem does not understand: doubt
    return any(k.startswith(PLUGIN_NAME + "@") for k in plugins)


def hook_available(harness_id: str) -> bool:
    """Can @-routing intercept a prompt typed into this harness right now?

    The static half of the question ("does this CLI have a prompt hook at
    all") lives on the adapter as `prompt_hook_capable`; this is the
    dynamic half — is tandem's plugin actually registered there. Both must
    hold, or an `@codex …` prefix is just literal prompt text.
    """
    if harness_id == "claude":
        return is_plugin_installed()
    if harness_id == "codex":
        return is_plugin_installed_codex()
    return False


MANUAL_COMMANDS = (
    "    claude plugin marketplace add Bhavya6187/tandem\n"
    "    claude plugin install tandem@tandem"
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Echo-and-run one CLI command; None when it cannot run at all."""
    click.echo("  $ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None


def install_plugin() -> bool:
    """Marketplace add + plugin install through claude's CLI.

    The add step is advisory — claude 2.1.220 exits 0 when the
    marketplace is already declared, and if the add genuinely failed the
    install step fails right after and reports. Only the install step
    decides the return value.
    """
    if shutil.which("claude") is None:
        click.secho("error: claude not found on PATH.", fg="red", err=True)
        click.echo("Once it is installed, run:\n" + MANUAL_COMMANDS, err=True)
        return False
    add = _run(["claude", "plugin", "marketplace", "add", MARKETPLACE_REPO])
    if add is not None and add.returncode != 0:
        detail = (add.stderr or add.stdout).strip()
        if detail:
            click.secho(f"  marketplace add failed: {detail}",
                        fg="yellow", err=True)
    ins = _run(["claude", "plugin", "install", PLUGIN_ID])
    if ins is None or ins.returncode != 0:
        detail = "" if ins is None else (ins.stderr or ins.stdout).strip()
        if detail:
            click.secho(f"  {detail}", fg="red", err=True)
        click.secho("Plugin install failed. Manual commands:",
                    fg="red", err=True)
        click.echo(MANUAL_COMMANDS, err=True)
        return False
    click.echo(
        "Plugin installed. It takes effect in new Claude sessions "
        "(running sessions are unaffected)."
    )
    install_plugin_codex()
    return True


def install_plugin_codex() -> bool:
    """Register the same plugin tree with codex, best-effort.

    Codex >= 0.145 loads claude-format plugins; `.codex-plugin/plugin.json`
    is the metadata it reads. Its two commands are
    `codex plugin marketplace add <source>` and `codex plugin add
    <plugin>@<marketplace>` — the second is spelled `add`, where claude
    says `install` (codex-cli 0.147.0). Only @-routing *from* codex in the mixed tab
    needs this — routing *to* codex, subagents and sync all work without
    it — so every failure here is a yellow note naming what the user loses,
    never an error, and a machine with no codex at all says nothing.
    """
    if shutil.which("codex") is None:
        return False
    add = _run(["codex", "plugin", "marketplace", "add", MARKETPLACE_REPO])
    if add is not None and add.returncode != 0:
        # advisory, exactly like the claude add: if it genuinely mattered
        # the install right below fails and reports
        detail = (add.stderr or add.stdout).strip()
        if detail:
            click.secho(f"  codex marketplace add failed: {detail}",
                        fg="yellow", err=True)
    ins = _run(["codex", "plugin", "add", PLUGIN_ID])
    if ins is None or ins.returncode != 0:
        detail = "" if ins is None else (ins.stderr or ins.stdout).strip()
        if detail:
            click.secho(f"  {detail}", fg="yellow", err=True)
        click.secho(
            "  codex plugin add did not succeed — @-routing from codex "
            "in the mixed tab will be unavailable (routing to codex still "
            "works). Retry later with: tandem plugin install",
            fg="yellow", err=True)
        return False
    click.echo(
        "Codex plugin installed. It takes effect in new codex sessions."
    )
    return True


LATER_HINT = "You can install it later with: tandem plugin install"


def _offer_stamp() -> Path:
    return paths.tandem_home() / "plugin-offer"


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):    # closed/replaced stdin
        return False


def offer_install() -> None:
    """One-time [Y/n] offer before the interactive shell.

    Every gate resolves to silence; only a *shown* offer stamps, and it
    stamps whatever the answer or install outcome — the hint names the
    retry path, so re-asking would just be nagging.
    """
    if not _stdin_is_tty():
        return
    if shutil.which("claude") is None:
        return
    stamp = _offer_stamp()
    try:
        if stamp.exists():
            return
    except OSError:
        return
    if is_plugin_installed():
        return
    try:
        accepted = click.confirm(
            "Install the tandem Claude Code plugin for codex-model "
            "subagents?",
            default=True,
        )
    except (click.Abort, EOFError):     # ctrl-C / ctrl-D = not now
        click.echo()                    # newline after the aborted prompt
        accepted = False
    if accepted:
        install_plugin()
    else:
        click.echo(LATER_HINT)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass                            # best-effort, like warned/ stamps
