"""Tool mapping layer + related shadow-file invariants."""

import json


class TestShadowRolloutMeta:
    def test_model_provider_written(self, env_factory):
        env = env_factory()
        meta = json.loads(env.codex_shadow.read_text().splitlines()[0])
        assert meta["type"] == "session_meta"
        # codex >= 0.145 interactive thread/resume rejects rollouts without
        # this ("Model provider `` not found", -32600)
        assert meta["payload"]["model_provider"] == "openai"
