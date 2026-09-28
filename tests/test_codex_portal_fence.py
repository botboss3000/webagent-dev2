import json

from app.api.chat import _is_codex_portal_agent


def test_only_codex_portal_agents_hit_persistence_fence():
    portal = {"metadata": json.dumps({"engine": "codex", "codex_code": {"context_mode": "codex_portal"}})}
    legacy = {"engine": "codex", "metadata": json.dumps({"codex_code": {"context_mode": "native_codex"}})}
    wrapper = {"engine": "codex", "metadata": {"codex_code": {"context_mode": "webagent_wrapper"}}}
    assert _is_codex_portal_agent(portal)
    assert not _is_codex_portal_agent(legacy)
    assert not _is_codex_portal_agent(wrapper)
    assert not _is_codex_portal_agent({"engine": "default", "metadata": portal["metadata"]})
