"""Behavioral audit fixtures independent of chat production rendering helpers."""
from plugins.recorder.rules import analyze


def event(elements=None, **detail):
    return {"id": detail.pop("event_id", "e1"), "kind": "render.checkpoint",
            "client_id": "tab-1", "session_id": "s1",
            "detail": {"complete": True, "active_session_id": "s1",
                       "elements": elements or [], **detail}}


def check(records, rule, **capture):
    return next(x for x in analyze(records, {"status": "completed", **capture})["checks"] if x["id"] == rule)


def user(id="u1", **extra):
    return {"id": id, "kind": "user", "session_id": "s1", "visible": True, **extra}


def group(id="g1", **extra):
    return {"id": id, "kind": "activity", "owner_id": "u1",
            "run_id": "r1", "segment_id": "segment1", **extra}


def test_guideline_fingerprint_and_unvisited_coverage_are_explicit():
    report = analyze([event([user(), group()])], {"status": "completed"})
    assert len(report["guideline_fingerprint"]) == 64
    assert report["result"] == "blocked"
    assert report["scope"] == "observed_transcript"
    assert check([], "journey.themes")["result"] == "not_run"


def test_activity_must_follow_its_user():
    assert check([event([user(), group()])], "transcript.order")["result"] == "pass"
    broken = check([event([group(), user()])], "transcript.order")
    assert broken["result"] == "fail"
    assert broken["evidence"] == ["e1"]


def test_interaction_chronology_is_independent_of_renderer():
    assert check([event([user("u2", interaction_seq=4), user("u1", interaction_seq=2)])], "transcript.order")["result"] == "fail"


def test_duplicate_activity_segment_fails_but_resumed_segment_is_allowed():
    assert check([event([group(), group("g2")])], "activity.unique")["result"] == "fail"
    assert check([event([group(), group("g2", segment_id="segment2")])], "activity.unique")["result"] == "pass"


def test_unknown_ownership_does_not_pass():
    assert check([event([{"id": "g", "kind": "activity"}])], "activity.unique")["result"] == "blocked"
    assert check([event([{"id": "u", "kind": "user", "visible": True}])], "session.ownership")["result"] == "blocked"


def test_stable_elements_cannot_reorder():
    events = [event([user("a"), user("b")]), event([user("b"), user("a")], event_id="e2")]
    assert check(events, "transcript.order")["result"] == "fail"


def test_partial_checkpoint_cannot_establish_unique_groups():
    assert check([event([group()], complete=False)], "activity.unique")["result"] == "blocked"


def test_counter_zero_with_tool_rows_fails():
    assert check([event([group(counters={"tools": 0}, rows={"tools": 2})])], "activity.counters")["result"] == "fail"
    assert check([event([group(counters={"tools": 2}, rows={"tools": 2})])], "activity.counters")["result"] == "pass"
    assert check([event([group(counters={"tools": 2})])], "activity.counters")["result"] == "blocked"


def test_finalized_content_change_fails_without_comparing_mutable_streaming_text():
    first = {"id": "p", "kind": "content", "finalized": True, "text_hash": "abc", "text_length": 120}
    second = {**first, "text_hash": "def", "text_length": 20}
    assert check([event([first]), event([second], event_id="e2")], "preview.durable")["result"] == "fail"
    assert check([event([{**first, "finalized": False}]), event([second])], "preview.durable")["result"] == "not_run"


def test_cross_document_or_session_order_is_not_compared():
    events = [event([user("a"), user("b")], document_id="d1"),
              event([user("b"), user("a")], document_id="d2")]
    assert check(events, "transcript.order")["result"] == "not_run"


def test_progress_requires_complete_send_observation():
    observed = {"observed_from_send": True, "progress_ms": 1, "first_content_ms": 4}
    assert check([event(progress_before_content=observed)], "progress.first")["result"] == "pass"
    assert check([event(progress_before_content=observed)], "progress.first", started_mid_run=True)["result"] == "blocked"
    assert check([event(progress_before_content={**observed, "progress_ms": 5})], "progress.first")["result"] == "fail"


def test_stop_clears_at_terminal_and_is_visible_while_active():
    assert check([event(run_state="running", stop_visible=False)], "controls.stop")["result"] == "fail"
    assert check([event(run_state="completed", stop_visible=True)], "controls.stop")["result"] == "fail"
    assert check([event(run_state="running", stop_visible=True)], "controls.stop")["result"] == "pass"


def test_terminal_closer_requires_settled_complete_checkpoint():
    terminal = [{"run_id": "r1", "settled": True, "explicit_failure": False, "outcome_region_complete": True}]
    closer = {"id": "c1", "kind": "closer", "run_id": "r1"}
    assert check([event([closer], terminal_runs=terminal)], "closer.unique")["result"] == "pass"
    assert check([event([], terminal_runs=terminal)], "closer.unique")["result"] == "fail"
    assert check([event([closer], terminal_runs=terminal, complete=False)], "closer.unique")["result"] == "blocked"
    assert check([event([closer, {**closer, "id": "c2"}], terminal_runs=terminal)], "closer.unique")["result"] == "fail"


def test_stale_cache_replace_fails_but_discard_passes_and_merge_is_uncertain():
    def decision(name):
        return [{"id": "cache1", "kind": "cache.decision",
                 "detail": {"decision": name, "incoming_revision": 4, "current_revision": 8}}]
    assert check(decision("replace"), "cache.revision")["result"] == "fail"
    assert check(decision("discard"), "cache.revision")["result"] == "pass"
    assert check(decision("merge"), "cache.revision")["result"] == "blocked"


def test_capture_gaps_block_positive_results_but_retain_observed_failures():
    assert check([event([group()])], "activity.unique", dropped_events=1)["result"] == "blocked"
    assert check([event([group(), group("g2")])], "activity.unique", dropped_events=1)["result"] == "fail"
    assert analyze([], {"status": "recording"})["provisional"] is True


def test_scroll_displacement_is_suspected_not_automatic_product_failure():
    report = analyze([event(scroll={"anchor_delta_px": 70, "user_scrolled": False})], {"status": "completed"})
    assert report["findings"][0]["severity"] == "suspected"
    assert report["result"] == "blocked"


def test_compaction_timing_requires_measured_success_notice():
    data = [{"id": "n1", "kind": "compaction.notice",
             "detail": {"outcome": "success", "visible_duration_ms": 100}}]
    assert check(data, "compaction.notice")["result"] == "fail"
    data[0]["detail"]["visible_duration_ms"] = 2000
    assert check(data, "compaction.notice")["result"] == "pass"


def test_malformed_details_are_not_assumed_to_be_valid_capture():
    report = analyze([{"id": "bad", "kind": "render.checkpoint", "detail": "truncated"}], {"status": "completed"})
    assert report["result"] == "blocked"
    assert not report["findings"]


def test_same_checkpoint_progress_and_content_cannot_establish_paint_order():
    progress = {"observed_from_send": True, "progress_ms": 100, "first_content_ms": 100}
    assert check([event(progress_before_content=progress)], "progress.first")["result"] == "blocked"


def test_unmounted_terminal_run_does_not_imply_missing_closer_failure():
    terminals = [{"run_id": "old-run", "settled": True}]
    assert check([event([user()], terminal_runs=terminals)], "closer.unique")["result"] == "blocked"

def test_new_witness_coverage_and_next_actions():
    dark = event([user()], viewport={"theme": "dark"})
    light = event([user()], viewport={"theme": "light"}, event_id="e2")
    assert check([dark, light], "journey.themes")["result"] == "pass"
    keyboard = {"id": "k", "kind": "journey.keyboard", "detail": {"method": "keyboard", "accessible_name": True, "focus_visible": True, "focus_indicator": True}}
    pointer = {"id": "p", "kind": "journey.keyboard", "detail": {"method": "pointer", "accessible_name": True, "focus_visible": True, "focus_indicator": True}}
    assert check([keyboard, pointer], "journey.keyboard")["result"] == "pass"
    assert "next_action" in check([keyboard], "journey.keyboard")

def test_recovery_fingerprint_and_scroll_witnesses():
    before = {"id": "before", "kind": "journey.recovery", "detail": {"phase": "before", "boundary_id": "b", "complete": True, "settled": True, "elements": [{"id": "m", "text_hash": "h", "text_length": 2}]}}
    after = {"id": "after", "kind": "journey.recovery", "detail": {"phase": "after", "boundary_id": "b", "complete": True, "settled": True, "run_active": False, "stop_visible": False, "transient_count": 0, "elements": [{"id": "m", "text_hash": "h", "text_length": 2}]}}
    assert check([before, after], "journey.recovery")["result"] == "pass"
    assert check([event(scroll={"comparable": True, "anchor_delta_px": 4, "user_scrolled": False, "intentional_navigation": False})], "scroll.stability")["result"] == "pass"

def test_pending_progress_and_monotonic_cache_evidence():
    pending = event(progress_before_content={"observed_from_send": True, "progress_ms": 2, "first_content_ms": None})
    complete = event(progress_before_content={"observed_from_send": True, "progress_ms": 2, "first_content_ms": 5}, event_id="e2")
    assert check([pending, complete], "progress.first")["result"] == "pass"
    rows = [{"id": "stream", "kind": "trace", "label": "DEBUG-TAG:cache.stream.monotonic", "detail": {"event": "cache.stream.monotonic", "incoming_length": 4, "current_length": 8, "decision": "apply"}}]
    assert check(rows, "cache.revision")["result"] == "fail"


def test_stream_discard_does_not_fail_length_nonregression():
    def trace(decision):
        return {"kind": "trace", "detail": {"event": "cache.stream.monotonic",
                "decision": decision, "incoming_length": 4, "current_length": 8}}
    assert check([trace("discard")], "cache.revision")["result"] == "pass"
    assert check([trace("apply")], "cache.revision")["result"] == "fail"
    assert check([trace(None)], "cache.revision")["result"] == "blocked"


def test_unexercised_theme_is_coverage_gap_not_rendering_failure():
    dark = event([user()], viewport={"theme": "dark", "visibility": "visible"})
    assert check([dark], "journey.themes")["result"] == "blocked"
    assert check([event([], viewport={"theme": "light"})], "journey.themes")["result"] == "not_run"


def test_optional_screenshot_gap_does_not_hide_independent_event_loss():
    rows = [event([group()])]
    assert check(rows, "activity.unique", gaps=["screenshots-not-enabled"])["result"] == "pass"
    assert check(rows, "activity.unique", gaps=["screenshots-not-enabled"], dropped_events=1)["result"] == "blocked"
    rows.append({"kind": "capture.gap", "detail": {"reason": "outbox-not-durable"}})
    assert check(rows, "activity.unique", gaps=["screenshots-not-enabled"])["result"] == "blocked"


def test_scroll_navigation_is_not_failure_and_unexplained_jump_needs_review():
    stable = event(scroll={"comparable": True, "anchor_delta_px": 0, "user_scrolled": False})
    navigation = event(scroll={"comparable": True, "anchor_delta_px": 100,
                               "user_scrolled": False, "intentional_navigation": True})
    assert check([stable, navigation], "scroll.stability")["result"] == "pass"
    jump = event(scroll={"comparable": True, "anchor_delta_px": 100, "user_scrolled": False})
    result = analyze([jump], {"status": "completed"})
    assert next(x for x in result["checks"] if x["id"] == "scroll.stability")["result"] == "blocked"
    assert all(f["severity"] == "suspected" for f in result["findings"])


def test_recovery_missing_rows_and_unknown_terminal_fields_do_not_pass():
    def boundary(phase, rows, **extra):
        return {"kind": "journey.recovery", "detail": {
            "phase": phase, "boundary_id": "b", "session_id": "s", "complete": True,
            "settled": True, "elements": rows, **extra}}
    one = {"id": "one", "text_hash": "abc", "text_length": 3}
    two = {**one, "id": "two"}
    before = boundary("before", [one, two])
    after = boundary("after", [one], run_active=False, stop_visible=False, transient_count=0)
    assert check([before, after], "journey.recovery")["result"] == "blocked"
    assert check([before, boundary("after", [one, two])], "journey.recovery")["result"] == "blocked"
    after = boundary("after", [one, two], run_active=False, stop_visible=True, transient_count=0)
    assert check([before, after], "journey.recovery")["result"] == "fail"


def test_pending_second_run_cannot_borrow_first_runs_progress_pass():
    first = event(progress_before_content={"run_id": "r1", "observed_from_send": True,
                                           "progress_ms": 1, "first_content_ms": 4})
    second = event(progress_before_content={"run_id": "r2", "observed_from_send": True,
                                            "progress_ms": 5, "first_content_ms": None})
    assert check([first, second], "progress.first")["result"] == "blocked"


def test_pointerdown_does_not_require_keyboard_focus_ring():
    pointer = {"kind": "journey.keyboard", "detail": {"method": "pointer", "accessible_name": True,
                                                     "focus_visible": False, "focus_indicator": False}}
    keyboard = {"kind": "journey.keyboard", "detail": {"method": "keyboard", "accessible_name": True,
                                                      "focus_visible": True, "focus_indicator": True}}
    assert check([pointer, keyboard], "journey.keyboard")["result"] == "pass"
    keyboard["detail"]["focus_indicator"] = False
    assert check([pointer, keyboard], "journey.keyboard")["result"] == "blocked"


def test_preview_is_compared_to_durable_detail_and_clamping_is_measured():
    preview = {"id": "preview:p", "kind": "preview", "finalized": True,
               "text_hash": "abc", "text_length": 30,
               "source_text_hash": "abc", "source_text_length": 30, "text_clamped": False}
    assert check([event([preview])], "preview.durable")["result"] == "pass"
    assert check([event([{**preview, "text_length": 12}])], "preview.durable")["result"] == "fail"
    assert check([event([{**preview, "text_clamped": True}])], "preview.durable")["result"] == "fail"


def test_manifest_refresh_context_does_not_poison_a_measured_merge():
    request = {"kind": "trace", "label": "DEBUG-TAG:cache.manifest",
               "detail": {"decision": "refresh", "current_revision": 4, "incoming_revision": 5}}
    applied = {"kind": "cache.decision", "detail": {"decision": "apply",
               "current_revision": 4, "incoming_revision": 5}}
    assert check([request], "cache.revision")["result"] == "blocked"
    assert check([request, applied], "cache.revision")["result"] == "pass"


def test_fragmented_checkpoint_preserves_order_and_requires_every_part():
    from plugins.recorder.rules import _checkpoints
    metadata = {"complete": True, "active_session_id": "s1"}
    parts = [
        {"id": "part-a", "kind": "checkpoint.part", "client_id": "tab",
         "detail": {"checkpoint_id": "frame", "part": 0, "parts": 2,
                    "metadata": metadata, "elements": [user()]}},
        {"id": "part-b", "kind": "checkpoint.part", "client_id": "tab",
         "detail": {"checkpoint_id": "frame", "part": 1, "parts": 2,
                    "metadata": metadata, "elements": [group()]}},
    ]
    later = event([group(), user()], event_id="later")
    rebuilt = _checkpoints(parts + [later])
    assert [r["id"] for r in rebuilt] == ["part-a", "later"]
    assert check(parts, "transcript.order")["result"] == "pass"
    assert check(parts + [later], "transcript.order")["result"] == "fail"
    assert check(parts[:1], "capture.integrity")["result"] == "blocked"
    assert check(parts[:1], "transcript.order")["result"] == "not_run"
