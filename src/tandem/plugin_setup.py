"""Install the tandem Claude Code plugin through claude's own CLI.

Detection reads claude's installed-plugin registry (read-only; claude
stays the sole writer of its own state). Install shells out to
`claude plugin …` — verified idempotent on claude 2.1.220: re-adding the
marketplace and re-installing the plugin both exit 0 with an "already"
notice. The one-time offer lives here too so both entry points (bare
`tandem` and `tandem plugin install`) share a single routine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import click

from . import paths

MARKETPLACE_REPO = "Bhavya6187/tandem"
PLUGIN_ID = "tandem@tandem"


def is_plugin_installed() -> bool:
    """True when claude's registry records a tandem@tandem install.

    Missing file or absent/empty entry is definitively False — a fresh
    claude install has no registry, and that user must get the offer.
    Everything ambiguous (unreadable, unparseable, unexpected shape)
    is True: the caller only decides whether to nag, and doubt must
    stay silent.
    """
    try:
        raw = paths.claude_installed_plugins_path().read_text()
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


MANUAL_COMMANDS = (
    "    claude plugin marketplace add Bhavya6187/tandem\n"
    "    claude plugin install tandem@tandem"
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Echo-and-run one claude command; None when it cannot run at all."""
    click.echo("  $ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
