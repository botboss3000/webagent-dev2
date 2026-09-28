"""Promoted evidence establishes only the run history actually retained."""
from plugins.recorder.rules import analyze


def fixture(start=True, client="tab", available=3000):
    coverage = {"requested_ms": 5000, "available_ms": available,
                "trigger_elapsed_ms": 3000, "limitations": ["warm-up"]}
    records = [{"id": "trigger", "kind": "meta", "label": "prebuffer-trigger",
                "client_id": "tab", "elapsed_ms": 3000, "detail": {"prebuffer": coverage}}]
    if start:
        records.append({"id": "send", "kind": "trace", "label": "DEBUG-TAG:run.started",
                        "client_id": client, "elapsed_ms": 1000,
                        "detail": {"run_id": "run", "session_id": "session"}})
    records.append({"id": "frame", "kind": "checkpoint", "client_id": "tab", "elapsed_ms": 3500,
                    "detail": {"active_session_id": "session", "complete": True, "elements": [],
                               "run_state": "running", "stop_visible": True,
                               "progress_before_content": {"run_id": "run", "observed_from_send": True,
                                                           "progress_ms": 1100, "first_content_ms": 1500}}})
    return records


def result(records, **capture):
    report = analyze(records, {"status": "completed", "started_midrun": True, **capture})
    return report, {c["id"]: c["result"] for c in report["checks"]}


def test_retained_send_lifts_midrun_even_with_short_warmup():
    report, checks = result(fixture())
    assert checks["progress.first"] == "pass"
    assert checks["controls.stop"] == "pass"
    assert report["prebuffer"]["browser"]["available_ms"] == 3000
    assert report["prebuffer"]["server"] is None


def test_evicted_or_other_document_send_never_proves_history():
    for records in (fixture(start=False), fixture(client="other"), fixture(available=1000)):
        _, checks = result(records)
        assert checks["progress.first"] == "blocked"
        assert checks["controls.stop"] == "blocked"


def test_actual_gaps_still_block_positive_assertions():
    _, checks = result(fixture(), gaps=["dropped-events"])
    assert checks["progress.first"] == "blocked"


def test_scope_metadata_survives_missing_trigger_event_and_reports_workers():
    records = fixture()
    browser = records.pop(0)["detail"]["prebuffer"]
    server = {"workers": [{"worker_id": "a", "available_ms": 2300}], "missing_workers": ["b"]}
    report, _ = result(records, prebuffer_browser=browser, prebuffer={"requested_ms": 5000, "server": server})
    assert report["prebuffer"] == {"requested_ms": 5000, "browser": browser, "server": server}


def test_legacy_reports_do_not_invent_prebuffer_coverage():
    report, checks = result(fixture()[1:])
    assert "prebuffer" not in report
    assert checks["progress.first"] == "blocked"


def test_retained_send_does_not_hide_observed_failure():
    records = fixture()
    records[-1]["detail"]["progress_before_content"]["progress_ms"] = 1600
    _, checks = result(records)
    assert checks["progress.first"] == "fail"


def test_final_promotion_coverage_supersedes_click_estimate_after_eviction():
    records = fixture()
    click = {"requested_ms": 5000, "available_ms": 5000, "trigger_elapsed_ms": 5000}
    final = {"requested_ms": 5000, "available_ms": 700, "trigger_elapsed_ms": 3000,
             "limitations": ["memory-pressure"]}
    records.append({"id": "promoted", "kind": "meta", "label": "DEBUG-TAG:prebuffer-trigger",
                    "client_id": "tab", "elapsed_ms": 4000, "detail": {"prebuffer": final}})
    report, checks = result(records, prebuffer_browser=click)
    assert report["prebuffer"]["browser"] == final
    assert checks["progress.first"] == "blocked"
