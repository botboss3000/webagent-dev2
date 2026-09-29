from app.agent.gate_registry import instances_for_agent, prompt_instructions_for_agent
from app.chat.visibility import classify, leading_marker_pending


def test_leading_marker_classification_and_safe_fallback():
    decision = classify('<webagent:visibility main> Important update', active=True)
    assert decision.content == 'Important update'
    assert decision.main is True
    assert decision.marker_seen is True

    internal = classify('<webagent:visibility internal> Routine work', active=True)
    assert internal.content == 'Routine work'
    assert internal.main is False

    malformed = classify('<webagent:visibility unknown> Keep working', active=True)
    assert malformed.content == 'Keep working'
    assert malformed.main is False

    absent = classify('Routine work', active=True)
    assert absent.content == 'Routine work'
    assert absent.main is False


def test_only_possible_leading_markers_are_buffered():
    assert leading_marker_pending('<webagent:visibility m', active=True)
    assert not leading_marker_pending('ordinary response', active=True)
    assert not leading_marker_pending('<webagent:visibility main> body', active=True)


def test_gate_defaults_on_and_custom_guidance_is_additive():
    agent = {'metadata': {'gates': {'gates': []}}}
    row = next(item for item in instances_for_agent(agent) if item['type'] == 'message_visibility')
    assert row['enabled'] is True

    agent['metadata']['gates']['gates'] = [{
        'type': 'message_visibility', 'enabled': True,
        'message': 'Promote material risk discoveries.',
    }]
    prompt = prompt_instructions_for_agent(agent)['require_message_visibility']
    assert 'Ordinary substantive non-final assistant text stays inside the current activity group automatically.' in prompt
    assert 'publish_main_message' in prompt
    assert 'Promote material risk discoveries.' in prompt


def test_explicit_disable_removes_prompt_injection():
    agent = {'metadata': {'gates': {'gates': [{
        'type': 'message_visibility', 'enabled': False,
    }]}}}
    assert 'require_message_visibility' not in prompt_instructions_for_agent(agent)
