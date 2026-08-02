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
    except OSError:
        return True
    try:
        plugins = json.loads(raw)["plugins"]
        if not isinstance(plugins, dict):
            return True
        return bool(plugins.get(PLUGIN_ID))
    except Exception:
        return True
