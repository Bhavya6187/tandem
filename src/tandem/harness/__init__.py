from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, HarnessAdapter] = {
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}


def get_adapter(harness_id: str) -> HarnessAdapter:
    return ADAPTERS[harness_id]
