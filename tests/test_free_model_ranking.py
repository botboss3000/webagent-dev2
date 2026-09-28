from app.free_model_ranking import (
    TOOL_PROBE_NAME,
    TOOL_PROBE_VALUE,
    free_candidates,
    redact_error,
    score,
    status_since,
    validate_tool_probe,
)


def _tool_payload(name=TOOL_PROBE_NAME, arguments=None):
    if arguments is None:
        arguments = '{"action":"echo","value":"%s"}' % TOOL_PROBE_VALUE
    return {"choices": [{"message": {"tool_calls": [{"type": "function", "function": {
        "name": name, "arguments": arguments,
    }}]}}]}


def test_candidate_filter_requires_zero_prices_and_excludes_native_router():
    models = [
        {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "vendor/free", "name": "Free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "vendor/paid", "pricing": {"prompt": "0.1", "completion": "0"}},
    ]
    assert [row["id"] for row in free_candidates(models)] == ["vendor/free"]


def test_free_candidates_annotate_detected_capabilities():
    """A free candidate carries the same capability set the model table shows, so the
    Free Model Ranking table can render the full flag row per model."""
    models = [
        {"id": "vendor/vision", "name": "Seer", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "vendor/plain", "name": "Plain", "pricing": {"prompt": "0", "completion": "0"}},
    ]
    rows = {row["id"]: row for row in free_candidates(models)}
    assert rows["vendor/vision"]["capabilities"] == {
        "text": True, "vision": True, "image_out": False, "voice": False,
        "video": False, "known": True,
    }
    # No modality info at all: text is assumed, media capabilities stay off.
    assert rows["vendor/plain"]["capabilities"] == {
        "text": True, "vision": False, "image_out": False, "voice": False,
        "video": False, "known": False,
    }


def test_valid_webagent_tool_call_is_agentic_and_compatible():
    result = validate_tool_probe(_tool_payload())
    assert result == {"agentic_ok": True, "compatibility_ok": True,
                      "reason": "valid WebAgent-style tool call"}


def test_prose_only_response_is_not_agentic():
    result = validate_tool_probe({"choices": [{"message": {"content": "I would call it"}}]})
    assert result["agentic_ok"] is False
    assert result["compatibility_ok"] is False


def test_malformed_arguments_are_agentic_but_incompatible():
    result = validate_tool_probe(_tool_payload(arguments="{not json"))
    assert result["agentic_ok"] is True
    assert result["compatibility_ok"] is False
    assert "JSON" in result["reason"]


def test_wrong_name_or_required_argument_is_incompatible():
    wrong_name = validate_tool_probe(_tool_payload(name="some_real_tool"))
    missing_value = validate_tool_probe(_tool_payload(arguments='{"action":"echo"}'))
    assert wrong_name["compatibility_ok"] is False
    assert missing_value["compatibility_ok"] is False


def test_score_returns_approved_components_and_perfect_model_is_eligible():
    result = score([{"ok": True, "latency_ms": 1, "tokens_per_second": 20,
                     "agentic_ok": True, "compatibility_ok": True}], rating=5)
    assert result["eligible"] is True
    assert set(result["score_components"]) == {
        "reliability", "latency", "throughput", "agentic", "compatibility", "manual"
    }
    assert result["score_components"]["reliability"] == 30
    assert result["score_components"]["throughput"] == 10
    assert result["score_components"]["agentic"] == 20
    assert result["score_components"]["compatibility"] == 10
    assert result["score_components"]["manual"] == 10
    assert 99 < result["score"] <= 100


def test_manual_rating_cannot_rescue_unhealthy_or_incompatible_model():
    unhealthy = score([{"ok": False}], rating=5)
    incompatible = score([{"ok": True, "latency_ms": 1, "tokens_per_second": 20,
                           "agentic_ok": True, "compatibility_ok": False}], rating=5)
    assert unhealthy["eligible"] is False
    assert incompatible["eligible"] is False


def test_redaction_hides_bearer_token_and_bounds_error():
    value = redact_error("Bearer secret-token " + "x" * 400, limit=60)
    assert "Bearer secret-token" not in value
    assert "Bearer [redacted]" in value
    assert len(value) == 60


def test_status_since_keeps_an_unchanged_verdict_and_restamps_a_transition():
    # The health age counts from the probe that FIRST reported the current status, so
    # a re-probe returning the same verdict must carry the original base forward...
    assert status_since("ok", "ok", 1000.0, now=5000.0) == 1000.0
    assert status_since("failed", "failed", 1000.0, now=5000.0) == 1000.0
    # ...while a real transition, or a first observation, starts the counter now.
    assert status_since("ok", "failed", 1000.0, now=5000.0) == 5000.0
    assert status_since("pending", "ok", 1000.0, now=5000.0) == 5000.0
    assert status_since(None, "pending", None, now=5000.0) == 5000.0


def test_status_since_never_claims_an_age_it_does_not_have():
    # Rows persisted before the stamp existed, or with an unusable value, are dated
    # as a fresh observation instead of inventing an age.
    assert status_since("ok", "ok", None, now=5000.0) == 5000.0
    assert status_since("ok", "ok", "not-a-timestamp", now=5000.0) == 5000.0
    assert status_since("ok", "ok", 0, now=5000.0) == 5000.0
