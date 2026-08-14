from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter

ADAPTERS: dict[str, HarnessAdapter] = {
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
    "opencode": OpencodeAdapter(),
}


def get_adapter(harness_id: str) -> HarnessAdapter:
    return ADAPTERS[harness_id]
