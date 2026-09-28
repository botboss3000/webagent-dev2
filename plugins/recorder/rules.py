"""Mechanical transcript audit, independent of the production renderer.

Called by the recorder report service; observations come from the optional
frontend capture module. See plugins/recorder/README.md for the integration map.
To add a rule: extend RULES with a stable ID and guideline expectation, implement
its evidence predicate below, and add a behavioral fixture to
tests/test_recorder_rules.py. Bump RULE_SET_VERSION when semantics change.
Never import render sorting/projection logic here: it cannot prove itself.
Absent fields are unknown, never false; timestamps are compared only within a
client/document. Results cover observed transcript evidence, not journey acceptance.
"""
from collections import Counter
from hashlib import sha256
from pathlib import Path

RULE_SET_VERSION = "1.4.0"
GUIDELINE = (Path(__file__).resolve().parents[2]
             / "plugins/abilities/Administrator/diagnostics/skills/journey_testing.skill.md")
RULES = {
    "capture.integrity": ("Capture completeness", "Required evidence has no recorded gaps or truncation."),
    "transcript.order": ("Transcript order", "Users precede their owned activity and closer; stable elements retain chronological order."),
    "activity.unique": ("One activity group per segment", "An uninterrupted segment has one activity group."),
    "run.sequence": ("Anchored turn sequence", "A settled successful run renders its user message, one activity group, then its final closer."),
    "anchor.current_turn": ("Current user anchor", "The active user turn has exactly one visible turn anchor until the user scrolls downward."),
    "closer.unique": ("Terminal outcome", "A completed run has exactly one closer or an explicit failure."),
    "progress.first": ("Progress before content", "Meaningful visible progress precedes tool activity or response content."),
    "activity.counters": ("Semantic activity counters", "Each displayed counter equals its corresponding semantic row count."),
    "preview.durable": ("Finalized preview content", "Finalized preview content retains its complete text."),
    "controls.stop": ("Stop lifecycle", "Stop stays visible during an active run and clears on terminal paths."),
    "compaction.notice": ("Compaction notice", "A successful compaction notice remains visible for two seconds."),
    "session.ownership": ("Session ownership", "Visible transcript content belongs to the displayed session."),
    "cache.revision": ("Cache/live revision merge", "An older revision cannot replace newer transcript state."),
    "scroll.stability": ("Scroll anchoring", "Visible anchors remain stable without user scroll or intentional navigation."),
    "journey.themes": ("Both themes", "Required checkpoints pass in both themes."),
    "journey.keyboard": ("Keyboard and accessibility", "Pointer, keyboard, focus and accessible states are verified."),
    "journey.recovery": ("Recovery", "Reload/reconnect preserve durable content and clear transient state."),
}

def _detail(record):
    detail = record.get("detail", {})
    return detail if isinstance(detail, dict) else {}

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def _checkpoints(records):
    """RECORDER-HOOK: join byte-bounded checkpoint parts before rule evaluation.

    Raw parts retain their own archive IDs. A missing/duplicate part is a real
    evidence gap; it must never be evaluated as a complete transcript snapshot.
    """
    groups = {}
    output = []
    for record in records:
        if record.get("kind") != "checkpoint.part":
            output.append(record)
            continue
        d = _detail(record)
        key = (record.get("client_id"), d.get("checkpoint_id"))
        if key not in groups:
            groups[key] = (len(output), [])
            output.append(record)
        groups[key][1].append(record)
    for position, parts in groups.values():
        first = parts[0]
        d = _detail(first)
        count = d.get("parts")
        indexes = [_detail(p).get("part") for p in parts]
        valid = (isinstance(count, int) and 0 < count <= 2000 and len(parts) == count
                 and all(isinstance(i, int) for i in indexes)
                 and sorted(indexes) == list(range(count)))
        if not valid:
            output[position] = {**first, "kind": "capture.gap", "detail": {"reason": "Incomplete checkpoint parts"}}
            continue
        ordered = sorted(parts, key=lambda p: p["detail"]["part"])
        elements = [e for p in ordered for e in p["detail"].get("elements", [])]
        output[position] = {**first, "kind": "checkpoint", "detail": {
            **d.get("metadata", {}), "elements": elements,
            "part_evidence": [p.get("id") for p in ordered]}}
    return output


def _prebuffer(records, capture):
    """RECORDER-HOOK: report retained coverage, never infer five seconds from intent.

    Final promotion metadata supersedes the click-time scope estimate: pressure
    during the start handshake may evict more history. Scope metadata is only a
    fallback until that event arrives. Old captures have no prebuffer section.
    """
    stored = capture.get("prebuffer")
    stored = stored if isinstance(stored, dict) else {}
    browser = next((_detail(r).get("prebuffer") for r in reversed(records)
                    if str(r.get("label", "")).removeprefix("DEBUG-TAG:") == "prebuffer-trigger"
                    and isinstance(_detail(r).get("prebuffer"), dict)), None)
    if browser is None:
        browser = capture.get("prebuffer_browser")
        browser = browser if isinstance(browser, dict) else None
    if not stored and not browser:
        return None
    return {"requested_ms": (browser or {}).get("requested_ms", stored.get("requested_ms")),
            "browser": browser, "server": stored.get("server")}


def _retained_run_start(records, record, run_id, coverage):
    """Require the actual same-document send in the retained window, not a flag.

    A short warm-up window may still contain this entire run. A send witness
    whose original run.started was evicted cannot establish historical coverage.
    """
    browser = (coverage or {}).get("browser") or {}
    trigger = browser.get("trigger_elapsed_ms")
    available = browser.get("available_ms")
    if not run_id or not _number(trigger) or not _number(available) or available <= 0:
        return False
    session = _detail(record).get("active_session_id") or record.get("session_id")
    for start in records:
        d = _detail(start)
        topic = d.get("event") or str(start.get("label", "")).removeprefix("DEBUG-TAG:")
        at = start.get("elapsed_ms")
        if (topic == "run.started" and d.get("run_id") == run_id
                and start.get("client_id") == record.get("client_id")
                and (d.get("session_id") or start.get("session_id")) == session
                and _number(at) and max(0, trigger - available) <= at <= (record.get("elapsed_ms") if _number(record.get("elapsed_ms")) else trigger)):
            return True
    return False


def analyze(records: list[dict], capture: dict) -> dict:
    """Audit captured facts; unsupported or incomplete evidence cannot establish pass."""
    records = _checkpoints(records)
    prebuffer = _prebuffer(records, capture)
    run_starts = [r for r in records if (_detail(r).get("event") or
                  str(r.get("label", "")).removeprefix("DEBUG-TAG:")) == "run.started"]
    checks = {
        key: dict(id=key, title=title, expected=expected, actual="No applicable evidence.",
                  result="not_run", evidence=[], limitations=[],
                  guideline="plugins/abilities/Administrator/diagnostics/skills/"
                            "journey_testing.skill.md#chat-ui-journey-testing-and-requirements-learning")
        for key, (title, expected) in RULES.items()
    }
    suspects = []
    def observe(key, good, record, actual, limitation=None):
        check = checks[key]
        result = "blocked" if good is None else ("pass" if good else "fail")
        rank = {"not_run": 0, "pass": 1, "blocked": 2, "fail": 3}
        if rank[result] >= rank[check["result"]]:
            check.update(result=result, actual=actual)
        evidence_id = record.get("id")
        if evidence_id is not None and evidence_id not in check["evidence"]:
            check["evidence"].append(evidence_id)
        if limitation and limitation not in check["limitations"]:
            check["limitations"].append(limitation)

    # RECORDER-HOOK: optional screenshot permission is a visual channel gap;
    # dropped structured events remain a mechanical gap even when both occur.
    optional_gap = lambda value: value == "screenshots-not-enabled"
    gaps = any(capture.get(k) for k in ("gaps", "dropped_events", "truncated", "capture_errors"))
    mechanical_gaps = any(capture.get(k) for k in ("dropped_events", "truncated", "capture_errors")) or any(
        not optional_gap(g) for g in capture.get("gaps", []))
    active = capture.get("status", "recording") in ("starting", "recording", "active", "degraded", "finalizing")
    midrun = bool(capture.get("started_midrun") or capture.get("started_mid_run"))
    frames = []
    theme_values = {}
    keyboard_observations = []
    recovery_boundaries = {}
    cache_context = None
    progress_witnesses = {}
    for record in records:
        detail = _detail(record)
        kind = str(record.get("kind", ""))
        label = str(record.get("label", "")).removeprefix("DEBUG-TAG:")
        if kind in ("capture.gap", "capture.error") or detail.get("dropped_events") or detail.get("truncated"):
            gaps = True
            if not optional_gap(detail.get("reason")) or detail.get("dropped_events") or detail.get("truncated"):
                mechanical_gaps = True
        if kind == "journey.recovery":
            boundary = str(detail.get("boundary_id") or "")
            phase = detail.get("phase")
            if boundary and phase in ("before", "after"):
                recovery_boundaries.setdefault(boundary, {})[phase] = (detail, record)
        if kind == "journey.keyboard":
            method = detail.get("method")
            # Pointerdown precedes browser focus; pointer does not require a
            # focus ring. Custom focus styling needs review when not recognized.
            fields = ("accessible_name",) if method == "pointer" else (
                "accessible_name", "focus_visible", "focus_indicator")
            ok = True if method in ("keyboard", "pointer") and all(
                detail.get(k) is True for k in fields) and detail.get("disabled") is not True else None
            keyboard_observations.append((method, ok, record))
        if kind in ("render.checkpoint", "checkpoint") or label in ("render.checkpoint", "checkpoint", "render-checkpoint"):
            viewport = detail.get("viewport") if isinstance(detail.get("viewport"), dict) else {}
            theme = viewport.get("theme")
            elements = detail.get("elements")
            if detail.get("complete") is True and isinstance(elements, list) and any(
                    isinstance(e, dict) and e.get("visible") is True for e in elements
            ) and viewport.get("visibility") != "hidden" and theme in ("light", "dark"):
                theme_values[theme] = record
            frames.append((record, detail))
        topic = str(detail.get("event") or label or kind)
        if kind.startswith("cache") or label.startswith("cache") or topic.startswith("cache"):
            incoming, current = detail.get("incoming_revision"), detail.get("current_revision")
            decision = detail.get("decision")
            if topic == "cache.stream.monotonic":
                incoming_length, current_length = detail.get("incoming_length"), detail.get("current_length")
                if _number(incoming_length) and _number(current_length) and decision in ("apply", "replace", "discard", "reject"):
                    bad = decision in ("apply", "replace") and incoming_length < current_length
                    observe("cache.revision", not bad, record,
                            f"{decision}: stream lengths incoming {incoming_length}, current {current_length}.",
                            "Stream length non-regression is measured; transcript authority revisions need separate evidence.")
                else:
                    observe("cache.revision", None, record, "Stream decision lacks lengths or an applied/discarded outcome.")
            elif topic == "cache.manifest" and decision in ("reuse", "refresh"):
                if _number(incoming) and _number(current):
                    # Refresh requests fetch authoritative content; they do not
                    # assert that the returned revision has been applied.
                    if decision == "reuse":
                        observe("cache.revision", incoming == current, record,
                                f"Manifest reuse: incoming revision {incoming}, current {current}.")
                    else:
                        cache_context = record
            elif decision in ("apply", "replace", "discard", "reject", "merge"):
                if _number(incoming) and _number(current):
                    if decision == "merge" and incoming < current and detail.get("replaces_newer") is None:
                        observe("cache.revision", None, record, "Older data merged; replacement semantics unavailable.")
                    else:
                        bad = incoming < current and (decision in ("apply", "replace") or detail.get("replaces_newer") is True)
                        observe("cache.revision", not bad, record, f"{decision}: incoming revision {incoming}, current {current}.")
                # Cache reads, absent-cache discards and reconcile requests have
                # no comparable revision and are context, not merge assertions.
        if kind == "compaction.notice" and detail.get("outcome") == "success":
            duration = detail.get("visible_duration_ms")
            if _number(duration):
                observe("compaction.notice", 1900 <= duration <= 2300, record,
                        f"Notice measured visible for {duration} ms.",
                        "Duration tolerance is 100 ms early / 300 ms late; content formatting and durable detail require separate evidence.")
            else:
                observe("compaction.notice", None, record, "Notice duration was not measured.")
    if cache_context and checks["cache.revision"]["result"] == "not_run":
        observe("cache.revision", None, cache_context, "Manifest refresh was requested; no subsequent merge decision was observed.")
    if keyboard_observations:
        methods = {method for method, _ok, _record in keyboard_observations}
        all_ok = all(ok is True for _method, ok, _record in keyboard_observations)
        any_unknown = any(ok is None for _method, ok, _record in keyboard_observations)
        if {"keyboard", "pointer"}.issubset(methods):
            observe("journey.keyboard", None if any_unknown else all_ok, keyboard_observations[-1][2], "Keyboard and pointer witnesses are complete." if not any_unknown else "Focus or accessible-state witness is incomplete.", "Sampled control names and focus cues do not establish full keyboard or accessibility acceptance.")
        else:
            observe("journey.keyboard", None, keyboard_observations[-1][2], "Only one interaction method was observed.", "Both keyboard and pointer witnesses are required.")
    if theme_values:
        for theme, record in theme_values.items():
            observe("journey.themes", True if set(theme_values) == {"light", "dark"} else None,
                    record, "Observed visible transcript themes: " + str(sorted(theme_values)),
                    "Sampled structure in both themes does not establish pixel correctness or every journey checkpoint.")
    for boundary, phases in recovery_boundaries.items():
        before = phases.get("before")
        after = phases.get("after")
        if not before or not after:
            observe("journey.recovery", None, (after or before)[1], "Recovery boundary lacks before/after evidence.", "A reload or reconnect must emit both sides of the boundary.")
            continue
        before_detail, before_record = before
        after_detail, after_record = after
        if not before_detail.get("complete") or not after_detail.get("complete") or not after_detail.get("settled"):
            observe("journey.recovery", None, after_record, "Recovery evidence is incomplete or not settled.")
            continue
        before_rows = {x.get("id"): (x.get("text_hash"), x.get("text_length")) for x in before_detail.get("elements", []) if isinstance(x, dict) and x.get("id")}
        after_rows = {x.get("id"): (x.get("text_hash"), x.get("text_length")) for x in after_detail.get("elements", []) if isinstance(x, dict) and x.get("id")}
        common = set(before_rows) & set(after_rows)
        valid_fingerprints = all(isinstance(v[0], str) and _number(v[1])
                                 for v in list(before_rows.values()) + list(after_rows.values()))
        valid_state = all(isinstance(after_detail.get(k), bool) for k in ("run_active", "stop_visible")) and _number(after_detail.get("transient_count"))
        if not valid_fingerprints or not valid_state or before_detail.get("session_id") != after_detail.get("session_id"):
            observe("journey.recovery", None, after_record, "Recovery ownership, fingerprints or control state is incomplete.")
        elif not common:
            observe("journey.recovery", None, after_record, "Recovery has no comparable durable elements.", "Virtualization can hide rows; capture a settled checkpoint containing shared IDs.")
        elif any(before_rows[key] != after_rows[key] for key in common):
            observe("journey.recovery", False, after_record, "Durable element fingerprints changed across recovery.")
        elif set(before_rows) - set(after_rows):
            observe("journey.recovery", None, after_record, "Some durable rows were not restored in the observed window.",
                    "Virtualization can hide rows; reveal the missing durable IDs before concluding content loss.")
        elif (not after_detail.get("run_active")) and (after_detail.get("stop_visible") or int(after_detail.get("transient_count", 0))):
            observe("journey.recovery", False, after_record, "Recovery retained transient state after an idle boundary.")
        else:
            observe("journey.recovery", True, after_record, "Durable elements and idle controls survived recovery.")
    observe("capture.integrity", None if gaps or active or not records else True, {},
            "Evidence has gaps." if gaps else ("Capture is still active." if active else ("No events captured." if not records else "No reported capture gaps.")),
            "A lack of reported gaps does not prove all external log sources were available.")
    previous = {}
    finalized = {}
    for record, detail in frames:
        raw = detail.get("elements")
        if not isinstance(raw, list):
            continue
        elements = [x for x in raw if isinstance(x, dict)]
        complete = detail.get("complete") is True
        visible = [x for x in elements if x.get("visible") is True]
        scope = (record.get("client_id"), detail.get("document_id") or record.get("document_id"),
                 detail.get("active_session_id") or record.get("session_id"))
        ids = [x["id"] for x in elements if x.get("id")]
        if ids and len(ids) != len(set(ids)):
            observe("transcript.order", False, record, "Duplicate semantic element IDs.")
        chronological = [x.get("interaction_seq") for x in elements if x.get("kind") in ("user", "closer")]
        if len(chronological) > 1 and all(_number(n) for n in chronological):
            observe("transcript.order", chronological == sorted(chronological), record,
                    f"Independent interaction sequence in displayed order: {chronological}.")
        groups = [x for x in elements if x.get("kind") in ("activity", "activity_group")]
        keyed_groups = [(x.get("run_id"), x.get("segment_id")) for x in groups if x.get("run_id") and x.get("segment_id") is not None]
        if groups:
            if not complete:
                observe("activity.unique", None, record, "Partial checkpoint cannot establish unique groups.")
            if len(keyed_groups) != len(groups):
                observe("activity.unique", None, record, "Activity groups lack run/segment ownership.")
            else:
                observe("activity.unique", max(Counter(keyed_groups).values(), default=0) <= 1, record,
                        "Activity-group ownership counts: " + str(dict(Counter(keyed_groups))))
        positions = {x.get("id"): n for n, x in enumerate(elements) if x.get("id")}
        transcript_positions = {x.get("id"): x.get("transcript_index") for x in elements
                                if x.get("id") and _number(x.get("transcript_index"))}
        if detail.get("run_state") in ("active", "running"):
            anchors = [x for x in elements if x.get("kind") == "user" and x.get("turn_anchor") is True]
            anchor_state = detail.get("user_turn_anchor")
            if not isinstance(anchor_state, dict):
                observe("anchor.current_turn", None, record,
                        "Chat anchor lifecycle state was not recorded.")
            elif anchor_state.get("active") is True:
                observe("anchor.current_turn", len(anchors) == 1, record,
                        f"Active lifecycle state has {len(anchors)} anchored user bubbles.")
            elif anchor_state.get("released_by_user") is True:
                observe("anchor.current_turn", not anchors, record,
                        f"Reader-released lifecycle state has {len(anchors)} anchored user bubbles.")
            else:
                observe("anchor.current_turn", None, record,
                        "Active run has neither an anchor nor a recorded reader release.")
        for x in elements:
            owner = x.get("owner_id")
            if owner and owner in positions and x.get("id") in positions:
                observe("transcript.order", positions[owner] < positions[x["id"]], record,
                        f"Owner {owner} must precede {x['id']}.")
            counters, rows = x.get("counters"), x.get("rows")
            if isinstance(counters, dict) and counters:
                if isinstance(rows, dict) and all(k in rows and _number(v) and _number(rows[k]) for k, v in counters.items()):
                    observe("activity.counters", all(v == rows[k] for k, v in counters.items()), record,
                            f"Displayed {counters}; semantic rows {rows}.")
                else:
                    observe("activity.counters", None, record, "Semantic row counts are incomplete.")
            if x.get("kind") == "preview" and x.get("finalized") is True:
                source_hash, source_length = x.get("source_text_hash"), x.get("source_text_length")
                if source_hash and _number(source_length) and x.get("text_hash") and _number(x.get("text_length")):
                    observe("preview.durable", source_hash == x["text_hash"] and source_length == x["text_length"]
                            and x.get("text_clamped") is not True, record,
                            "Finalized preview compared with its durable detail DOM and text-clamping style.",
                            "DOM equality does not establish that the server initially delivered every intended character.")
                else:
                    observe("preview.durable", None, record, "Finalized preview lacks comparable durable detail content.")
            key = scope + (x.get("id"),)
            if x.get("id") and x.get("finalized") is True and x.get("text_hash") and _number(x.get("text_length")):
                fingerprint = (x["text_hash"], x["text_length"])
                if key in finalized:
                    observe("preview.durable", finalized[key] == fingerprint, record,
                            f"Finalized element {x['id']} content {'retained' if finalized[key] == fingerprint else 'changed'}.",
                            "Fingerprints detect changes but cannot prove the initial text was complete.")
                finalized[key] = fingerprint
        session_id = detail.get("active_session_id")
        if visible and session_id:
            owned = [x for x in visible if x.get("session_id")]
            if len(owned) != len(visible):
                observe("session.ownership", None, record, "Some visible elements lack session ownership.")
            if owned:
                observe("session.ownership", all(x["session_id"] == session_id for x in owned), record,
                        f"Visible content compared against session {session_id}.",
                        "Transcript-only evidence does not establish header/footer shell atomicity.")
        old = previous.get(scope)
        if old and complete and old.get("complete") is True:
            common = set(ids) & set(old["ids"])
            if len(common) > 1:
                observe("transcript.order", [x for x in old["ids"] if x in common] == [x for x in ids if x in common],
                        record, "Relative order compared across complete render checkpoints.")
        previous[scope] = {"ids": ids, "complete": complete}
        state, stop = detail.get("run_state"), detail.get("stop_visible")
        if state in ("idle", "active", "running", "completed", "failed", "cancelled", "terminal"):
            if isinstance(stop, bool):
                observe("controls.stop", stop == (state in ("active", "running")), record,
                        f"Run state {state}; Stop visible {stop}.",
                        "Sampled checkpoints cannot exclude flicker between samples.")
            else:
                observe("controls.stop", None, record, "Run state is known but Stop visibility is missing.")
        terminals = detail.get("terminal_runs", [])
        for terminal in terminals if isinstance(terminals, list) else []:
            if not isinstance(terminal, dict) or not terminal.get("run_id"):
                continue
            closers = [x for x in elements if x.get("kind") == "closer" and x.get("run_id") == terminal["run_id"]]
            groups_for_run = [x for x in elements if x.get("kind") in ("activity", "activity_group")
                              and x.get("run_id") == terminal["run_id"]]
            if complete and terminal.get("settled") is True and (closers or terminal.get("outcome_region_complete") is True or terminal.get("explicit_failure") is True):
                observe("closer.unique", len(closers) == 1 or (not closers and terminal.get("explicit_failure") is True), record,
                        f"Terminal run {terminal['run_id']}: {len(closers)} closers, explicit failure {terminal.get('explicit_failure') is True}.")
                if not closers and terminal.get("explicit_failure") is True:
                    observe("run.sequence", True, record,
                            f"Terminal run {terminal['run_id']} ended in an explicit failure without a final closer.")
                else:
                    user_id = "user:" + str(terminal["run_id"])
                    sequence_ids = [user_id] + [x.get("id") for x in groups_for_run] + [x.get("id") for x in closers]
                    sequence_indexes = [transcript_positions.get(item) for item in sequence_ids]
                    if len(groups_for_run) != 1:
                        observe("run.sequence", False, record,
                                f"Run {terminal['run_id']} rendered {len(groups_for_run)} activity groups; expected one.")
                    elif len(closers) != 1:
                        observe("run.sequence", False, record,
                                f"Run {terminal['run_id']} rendered {len(closers)} closers; expected one.")
                    elif not all(_number(index) for index in sequence_indexes):
                        observe("run.sequence", None, record,
                                "Settled turn lacks direct transcript positions for user, activity group, or closer.")
                    else:
                        user_index, group_index, closer_index = sequence_indexes
                        observe("run.sequence", user_index < group_index < closer_index and closer_index == group_index + 1,
                                record, f"Transcript positions user/activity/closer: {sequence_indexes}.")
            else:
                observe("closer.unique", None, record, "Terminal outcome region is not proven loaded, render has not settled, or checkpoint is partial.")
                observe("run.sequence", None, record, "Terminal turn is not proven settled in a complete checkpoint.")
        progress = detail.get("progress_before_content")
        if isinstance(progress, dict):
            key = scope + (progress.get("run_id"),)
            prior = progress_witnesses.get(key)
            if not prior or _number(progress.get("first_content_ms")) or not _number(prior[0].get("first_content_ms")):
                progress_witnesses[key] = (progress, record)
        anchor = detail.get("scroll")
        if isinstance(anchor, dict):
            delta = anchor.get("anchor_delta_px")
            intentional = anchor.get("user_scrolled") is True or anchor.get("intentional_navigation") is True
            if anchor.get("comparable") is True and _number(delta) and not intentional and anchor.get("user_scrolled") is False:
                observe("scroll.stability", True if abs(delta) <= 8 else None, record,
                        f"Comparable anchor delta: {delta} px.",
                        "Sampled anchors cannot rule out motion between checkpoints.")
            if _number(delta) and abs(delta) > 8 and anchor.get("user_scrolled") is False and not intentional and anchor.get("comparable") is not False:
                observe("scroll.stability", None, record, "Anchor moved without recorded user scroll; visual review required.")
                suspects.append(dict(rule_id="scroll.stability", severity="suspected", title="Possible scroll jump",
                                     evidence=[record["id"]] if record.get("id") is not None else []))
    for progress, record in progress_witnesses.values():
        before, content = progress.get("progress_ms"), progress.get("first_content_ms")
        missing_start = midrun and not _retained_run_start(run_starts, record, progress.get("run_id"), prebuffer)
        if missing_start or not progress.get("observed_from_send") or not (_number(before) and _number(content)):
            observe("progress.first", None, record, "Send-to-first-content observation is incomplete.")
        else:
            observe("progress.first", None if before == content else before < content, record,
                    f"Progress at {before} ms; first tool/content at {content} ms.",
                    "Equal sampled timestamps cannot establish first-paint order." if before == content else None)
    # RECORDER-HOOK: only lift the old mid-run limitation when every observed
    # active run has its actual send in the promoted evidence. Unknown runs stay
    # blocked; an unrelated run.started must not certify the whole recording.
    running_frames = [(r, d) for r, d in frames if d.get("run_state") in ("active", "running")]
    history_retained = bool(running_frames) and all(
        _retained_run_start(run_starts, r, (d.get("progress_before_content") or {}).get("run_id"), prebuffer)
        for r, d in running_frames)
    for key in ("transcript.order", "progress.first", "preview.durable", "controls.stop"):
        if midrun and not history_retained and checks[key]["result"] == "pass":
            checks[key].update(result="blocked")
            checks[key]["limitations"].append("Recording started mid-run; historical continuity is unproven.")
    if mechanical_gaps:
        for key, check in checks.items():
            if check["result"] == "pass":
                check.update(result="blocked")
                check["limitations"].append("Capture gaps prevent a complete positive assertion.")
    for check in checks.values():
        if check["result"] == "not_run":
            check["limitations"].append("Required trajectory or structured evidence was not exercised.")
        check["evidence"] = check["evidence"][:100]
    next_actions = {
        "capture.integrity": "Start a new recording and resolve the listed capture gaps; screenshots are optional for mechanical checks.",
        "transcript.order": "Record a chat message and its activity through completion with the transcript visible.",
        "activity.unique": "Record a run with its activity group visible, including any interruption and resume.",
        "run.sequence": "Record one full successful turn with its user message, activity group, and final answer visible together.",
        "anchor.current_turn": "Start recording before send and leave the newest user message visible while the run is active.",
        "closer.unique": "Keep recording until the final answer or explicit error appears and the run settles.",
        "progress.first": "Turn recording on before sending, then keep chat visible through the first response or tool.",
        "activity.counters": "Record an activity group containing tools or messages and open its disclosure.",
        "preview.durable": "Keep the finalized answer visible for at least two checkpoints, then revisit it.",
        "controls.stop": "Keep recording from send through completion or cancellation with Stop visible.",
        "compaction.notice": "Perform a successful compaction while its transient notice is in view.",
        "session.ownership": "Record a session load or switch with the target transcript visible.",
        "journey.themes": "View the chat transcript in both light and dark themes while recording.",
        "journey.keyboard": "Use chat controls with both the keyboard and pointer; keep focused controls visible.",
        "journey.recovery": "Keep recording across reload or reconnect, then wait for the same durable messages to settle.",
        "cache.revision": "Record live streaming or a cached session revisit through its completed merge.",
        "scroll.stability": "Keep a transcript anchor visible during streaming without scrolling or navigating.",
    }
    for check in checks.values():
        if check["result"] in ("not_run", "blocked"):
            check["next_action"] = next_actions.get(check["id"], "Exercise the required trajectory and emit the structured witness for this check.")
    findings = [dict(rule_id=x["id"], title=x["title"], severity="failure", actual=x["actual"], evidence=x["evidence"])
                for x in checks.values() if x["result"] == "fail"] + suspects
    results = [x["result"] for x in checks.values()]
    result = "fail" if "fail" in results else "blocked" if "blocked" in results or "not_run" in results else "pass"
    try:
        fingerprint = sha256(GUIDELINE.read_bytes()).hexdigest()
    except OSError:
        fingerprint = None
        if result != "fail":
            result = "blocked"
    return dict(rule_set_version=RULE_SET_VERSION, guideline_fingerprint=fingerprint,
                scope="observed_transcript", result=result, provisional=active,
                checks=list(checks.values()), findings=findings,
                **({"prebuffer": prebuffer} if prebuffer is not None else {}),
                limitations=["Automated evidence does not establish full journey acceptance.",
                             "Unexercised themes, accessibility and recovery require visible journey assertions."])
