"""Run-rendering contract regression tests.

These pin the transcript/rendering contract for a single agent run, as observed
in real Chrome:

  user message → immediate progress element → exactly one activity group (with
  cumulative counters, one latest-message preview of only complete lines, the
  backend progress note) → a separate terminal closer after the group.

On interruption the last complete working-agent message is the fallback closer.
Stale DB/WS progress after Stop must never resurrect a Working/Stopping shell or
re-engage Stop, and Stop/Continue are mutually exclusive.

These are source-assertion tests in the same idiom as test_chat_render_stability
and test_control_page: they pin the *presence* of the mechanism so a regression
that deletes it fails loudly. Runtime equivalence is covered by the JS syntax
check and by the live-playwright journey where available.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function(source: str, name: str, next_name: str) -> str:
    """Slice the body of `name` through the start of the next function."""
    start = source.index("function " + name + "(")
    end = source.index("function " + next_name + "(", start)
    return source[start:end]


def test_current_user_anchor_releases_only_on_manual_downward_scroll():
    source = _source("ui/chat/js/chat-ui.js")
    assert "let _lastObservedScrollTop = 0;" in source
    assert "const movedDown = scrollTop > _lastObservedScrollTop + 1;" in source
    assert "let _transcriptScrollIntentAt = 0;" in source
    assert "messagesEl.addEventListener('wheel'" in source
    assert "messagesEl.addEventListener('touchmove'" in source
    assert "const transcriptIntent = performance.now() - _transcriptScrollIntentAt < _TRANSCRIPT_SCROLL_INTENT_MS;" in source
    # Any deliberate gesture that actually moves the transcript releases the
    # anchor — up as well as down (see the release comment in chat-ui.js).
    assert "if (!programmatic && transcriptIntent && (movedDown || movedUp) && _hasActiveTurnAnchor())" in source
    assert "_releaseTurnAnchor({ userScrolled: true });" in source
    release = _function(source, "_releaseTurnAnchor", "_prevUserTarget")
    assert "classList.remove('turn-anchor')" in release


def test_current_user_anchor_adds_uncapped_tail_clearance_before_landing():
    source = _source("ui/chat/js/chat-ui.js")
    assert "const clearance = target - maxScrollTop;" in source
    assert "_turnAnchorPadPx = Math.max(_turnAnchorPadPx, currentPad + clearance);" in source
    assert "messagesInner.style.paddingBottom = _turnAnchorPadPx + 'px';" in source
    assert "requestAnimationFrame(land);" in source
    assert "_turnAnchorPadPx = 0;" in source


def test_current_user_anchor_relands_after_keyboard_or_composer_layout_changes():
    source = _source("ui/chat/js/chat-ui.js")
    assert "function _reanchorActiveTurnAfterLayout()" in source
    assert "if (_hasActiveTurnAnchor()) app.anchorUserTurn(_activeTurnAnchor);" in source
    assert "window.visualViewport.addEventListener('resize'" in source
    assert "_reanchorActiveTurnAfterLayout();" in source
    assert "Math.max(totalH + 8, _USER_STEP_PAD)" in source


def _run_owner_reorder(nodes):
    """Deterministic model of _groupClosersByOwner.

    nodes: list of {"kind": "group"|"closer"|..., "key": <exact run key>}.
    Every closer sharing a group's exact key is moved to be that group's immediate
    next sibling; duplicate/foreign closers are removed. A closer with no matching
    group (orphan) is LEFT as-is — never absorbed into a foreign run's group.
    This mirrors the real reorder's exact key (data-run-key), not owner-only.
    """
    runs = {}
    for node in nodes:
        key = node.get("key")
        if not key:
            continue
        runs.setdefault(key, {"group": None, "closers": []})
        if node["kind"] == "group":
            if runs[key]["group"] is None:
                runs[key]["group"] = node
        elif node["kind"] == "closer":
            runs[key]["closers"].append(node)
    keep = {}
    stray = []
    for key, r in runs.items():
        if r["group"] is not None and r["closers"]:
            # newest text wins (last), all others for this run are stray
            for c in r["closers"][:-1]:
                stray.append(c)
            keep[key] = r["closers"][-1]
    out = []
    removed = set(stray)
    for node in nodes:
        if any(node is s for s in stray):
            continue
        out.append(node)
    # Reposition each kept closer to immediately after its group (higher-key
    # groups first so earlier splices don't shift an already-fixed group).
    order = sorted(keep.keys(), key=lambda k: -next((i for i, n in enumerate(out) if n is runs[k]["group"]), -1))
    for key in order:
        if key not in keep:
            continue
        g = runs[key]["group"]
        c = keep[key]
        gIdx = next((i for i, n in enumerate(out) if n is g), -1)
        cIdx = next((i for i, n in enumerate(out) if n is c), -1)
        if gIdx == -1 or cIdx == -1:
            continue
        if cIdx == gIdx + 1:
            continue
        out.pop(cIdx)
        gIdx = next((i for i, n in enumerate(out) if n is g), -1)
        out.insert(gIdx + 1, c)
    return out


# ── 0. One canonical module instance (#8) ────────────────────────────────────
def test_chat_stream_imports_are_canonical_and_versioned():
    importers = {
        "ui/chat/js/chat-reconcile.js": "from './chat-stream.js?v=260'",
        "ui/chat/js/session-load.js": "from './chat-stream.js?v=260'",
        "ui/chat/js/chat-ui.js": "from './chat-stream.js?v=260'",
        "ui/chat/js/chat-virtual-scroll.js": "from './chat-stream.js?v=260'",
        "ui/chat-widget/js/chat-widget.js": "from '../../chat/js/chat-stream.js?v=260'",
    }
    for path, spec in importers.items():
        assert spec in _source(path), f"{path} does not use the canonical specifier"


def test_chat_stream_has_no_unversioned_imports():
    # Match only a real `from '...chat-stream.js'` specifier (comments may
    # mention the file freely, and "importer s" never starts an import).
    offenders = []
    for p in (ROOT / "ui").rglob("*.js"):
        if ".bak" in str(p) or ".orig" in str(p):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"from\s+['\"][^'\"]*chat-stream\.js(?!\?v=\d+)", txt):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"unversioned chat-stream.js imports: {offenders}"


# ── 1. One durable user bubble per id (#4) ───────────────────────────────────
def test_ensure_user_bubble_sweeps_same_id_duplicates():
    actions = _source("ui/chat/js/chat-bubble-actions.js")
    start = actions.index("function ensureUserBubble(")
    end = actions.index("\n}\n", start) + 2
    body = actions[start:end]
    assert "sameId" in body
    assert "sameId[i].remove()" in body
    assert "canonical = sameId[0] || null;" in body
    assert "const candidate = users.length ? users[users.length - 1] : null;" in body
    assert "candidateText === cont.trim()" in body


# ── 2. Exactly one activity group per run (#3) ───────────────────────────────
def test_activity_group_is_singleton_via_coalesce():
    stream = _source("ui/chat/js/chat-stream.js")
    body = _function(stream, "_coalesceActivityGroups", "_coalesceLiveActivityGroups")
    assert "extra.remove()" in body
    assert "app._activeToolGroupBubble = primary" in body
    resolver = _function(stream, "_activityGroupForTurn", "positionActivityGroupAfterOwner")
    assert "_coalesceActivityGroups(groups)" in resolver


def test_provisional_owner_is_rekeyed_to_durable_id():
    send = _source("ui/chat/js/chat-send.js")
    stream = _source("ui/chat/js/chat-stream.js")
    assert "app.rekeyActivityGroupOwner(provisionalTurnId, turnId)" in send
    rekey = _function(stream, "rekeyActivityGroupOwner", "_coalesceActivityGroups")
    assert "group.dataset.activityTurnId = String(durableTurnId)" in rekey
    assert "positionActivityGroupAfterOwner(durableTurnId, group)" in rekey


def test_durable_activity_group_uses_exact_phase_identity():
    stream = _source("ui/chat/js/chat-stream.js")
    attach = _function(stream, "attachActivityEntries", "attachProgressToActivity")
    durable = _function(stream, "applyDurableActivitySummary", "attachActivityEntries")
    assert "const registeredLiveGroup = !persistedSegment && owner ? _liveActivityGroupForTurn(owner) : null" in attach
    assert "if (!bubble && persistedSegment) bubble = _persistedActivityGroupForOwner(owner);" not in attach
    assert "if (!bubble && !persistedSegment)" in attach
    assert "activityGroupId: groupId" in durable
    assert "activity_group_id=${encodeURIComponent(groupId)}" in durable
    assert "id: event.id || groupId" in durable
    assert "_rememberLiveActivityGroup(owner, bubble)" in attach
    assert "_clearLiveActivityGroupsForSession(owner)" in stream

# ── 3. Run-render order: user → group → separate closer (#1) ─────────────────
def test_preview_stands_down_when_a_separate_closer_renders():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = _function(stream, "_hasCloserAfter", "_latestActivityRow")
    assert "b === groupBubble" in closer
    assert "b.classList.contains('tool-only')" in closer
    assert "b.classList.contains('streaming')" in closer
    assert "return true" in closer
    # The preview sync must stand the preview down once a closer renders.
    assert "!_hasCloserAfter(container)" in _function(
        stream, "_syncToolCallPreviews", "_setPreviewVisible"
    )


# ── 4. Preview shows only complete lines, never partial fragments (#2) ───────
def test_partial_stream_fragments_never_publish_to_preview():
    stream = _source("ui/chat/js/chat-stream.js")
    appender = stream[stream.index("function appendStreamToActiveBubble("):]
    assert '"The") is not a message' in appender
    assert "is not a message and must never" in appender
    assert "replace the last complete preview" in appender
    reconcile = _source("ui/chat/js/chat-reconcile.js")
    assert "seedStreamingBubble(key, text, msg.created_at)" in reconcile
    assert "_setTurnBufferMonotonic(key, text)" in reconcile
    assert "_appendMessagesToTranscript" in _source("ui/chat/js/session-load.js")


# ── 5. Stop/Continue are mutually exclusive; a stopped run never revives (#5) ─
def test_stop_continue_mutual_exclusion_and_no_revival():
    ui = _source("ui/chat/js/chat-ui.js")
    activity = _source("ui/shared/js/chat-activity.js")
    ws = _source("ui/shared/js/agentWs.js")
    stream = _source("ui/chat/js/chat-stream.js")
    assert "app._runControlsActive || app.isProcessing" in ui
    assert "continueBtn.hidden = runActive || !!hasInput" in ui
    assert "app.beginRunControls(_noteTurnId)" in activity
    assert "app.endRunControls(event.turn_id)" in activity
    assert "app.endRunControls()" in ws
    assert "if (!app._stopPending) app.isProcessing = true" in stream
    assert "app._stopPending = false; // never carry a stale stop into a new turn" in activity
    assert "app.beginRunControls" in activity
    assert "app.endRunControls" in activity


# ── 6. Interruption produces a terminal closer / clears the live run (#1,#6) ─
def test_interruption_publishes_terminal_row_and_clears_live_run():
    stream = _source("ui/chat/js/chat-stream.js")
    interrupted = _function(stream, "markAgentInterrupted", "_renderTerminalCloser")
    assert "app._stopPending = false" in interrupted
    assert "kind: 'system'" in interrupted
    assert "'stopped-'" in interrupted
    assert "if (isDebugMode())" in interrupted
    assert "app.isProcessing = false" in interrupted
    cold_load = _source("ui/chat/js/session-load.js")
    assert "terminal status rows are diagnostics" in cold_load
    assert "if (isDebugMode())" in cold_load
    assert "attachActivityEntries(entries" in interrupted
    # Authoritative terminal: must end active controls + render the closer.
    assert "_renderTerminalCloser(" in interrupted
    assert "app.endRunControls(ownerTurnId)" in interrupted
    assert "app._runEndedFor[String(ownerTurnId)] = true" in interrupted


# ── 9. Authoritative errored terminal: single closer + control end (#1,#5,#6) ─
def test_errored_terminal_ends_controls_and_renders_single_closer():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = _function(stream, "_renderTerminalCloser", "notifyExecutionMode")
    # Exactly one separate agent bubble AFTER the group (the closer).
    assert "addChatBubble(" in closer
    assert "'terminal-closer'" in closer
    assert "group.insertAdjacentElement('afterend', bubble)" in closer
    # Idempotent: update an existing closer instead of creating a second one.
    assert ".chat-bubble.agent.terminal-closer" in closer
    assert "const created = !bubble" in closer


def test_backend_completion_requires_a_nonblank_terminal_response():
    chat = _source("app/api/chat.py")
    # Both a fresh web run and its watchdog-resume builder reject a stream that
    # simply ends without a user-visible closer. The existing empty-response
    # cause is resumable, so this cannot strand the task as falsely complete.
    assert chat.count("_saw_final_response = False") == 2
    assert chat.count('final_status == "complete" and not _saw_final_response') == 2
    assert chat.count('"stop_cause": "empty_response"') >= 2


def test_terminal_closer_uses_last_complete_message_fallback():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = _function(stream, "_renderTerminalCloser", "notifyExecutionMode")
    # A partial token fragment / punctuation draft is never the closer content.
    assert "placeholder" in closer
    assert "const content = (text && !placeholder.test(text)) ? text : fallback" in closer
    assert "const fallback = (statusLabel || 'Stopped').trim()" in closer
    # On terminal error/interrupt the closer falls back to the LAST COMPLETE
    # working message already rendered in the run's activity group (never a
    # fragment, never a bare label when a real message exists).
    assert "_lastCompleteMessageInRun(group)" in closer


def test_terminal_closer_last_complete_message_helper_is_run_scoped():
    stream = _source("ui/chat/js/chat-stream.js")
    body = _function(stream, "_lastCompleteMessageInRun", "notifyExecutionMode")
    assert "function _lastCompleteMessageInRun(group)" in body
    assert "group.querySelectorAll(" in body
    assert ".ca-progress-row" in body
    assert ".ca-response-row" in body
    # Strictly scoped to the group so another run's message can never leak in.
    assert "!group.isConnected" in body


def test_ws_error_routes_to_terminal_marker_and_ends_controls():
    ws = _source("ui/shared/js/agentWs.js")
    start = ws.index("case 'error':")
    end = ws.index("case 'interrupted':", start)
    body = ws[start:end]
    # A terminal WS error is a semantic boundary: it must render the fallback
    # closer (markAgentInterrupted) and end active controls so Stop can never
    # coexist with the errored run — the same treatment as 'interrupted'.
    assert "app.markAgentInterrupted(" in body
    assert "'Error'" in body
    assert "else if (app.isDebug)" in body


def test_stale_terminal_owner_cannot_reengage_stop():
    ui = _source("ui/chat/js/chat-ui.js")
    state = _source("ui/shared/js/state.js")
    assert "_runEndedFor" in state
    start = ui.index("app.beginRunControls = (ownerTurnId) =>")
    end = ui.index("app.endRunControls = (ownerTurnId) =>", start)
    body = ui[start:end]
    assert "app._runEndedFor[owner]" in body
    assert "return" in body


def test_reconcile_error_branch_routes_to_terminal_marker():
    reconcile = _source("ui/chat/js/chat-reconcile.js")
    start = reconcile.index("msg.status === 'error'")
    end = reconcile.index("} else {", start)
    body = reconcile[start:end]
    assert "markAgentInterrupted(" in body
    assert "'Error'" in body
    assert "_dispatchTurnCompleted(sid)" in body
    # The dead finalize/error-mark block is gone; the branch is clean.
    assert "finalizeAgentResponse(text, key)" not in body
    assert "b.classList.add('error')" not in body


# ── 7. Preview height watermark: long→short never shifts the group down (#7) ─
def test_preview_height_watermark_preserves_group_bottom():
    stream = _source("ui/chat/js/chat-stream.js")
    assert "_ACTIVITY_PREVIEW_TAIL_RESERVE" in stream
    assert "function _captureActivityPreviewTailWatermark(" in stream
    assert "function _restoreActivityPreviewTailWatermark(" in stream
    assert "_ACTIVITY_PREVIEW_TAIL_RESERVE, reserve + 'px'" in stream


# ── 8. Cumulative counters live alongside the live note in the group ─────────
def test_cumulative_counters_and_progress_note_are_group_scoped():
    activity = _source("ui/shared/js/chat-activity.js")
    stream = _source("ui/chat/js/chat-stream.js")
    assert "cumulativeIn" in activity
    assert "cumulativeOut" in activity
    assert "_TOKEN_CACHE_KEY" in activity
    assert "function _pinMirrorNoteToBottom(" in stream
    assert "live-activity-note" in stream


# ── 9. Compaction coalesces into one run group (#1) ───────────────────────────
def test_compaction_coalesces_into_run_group():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function notifyCompactionCompleted(")
    end = stream.index("// Expose on app immediately for session-load.js", start)
    body = stream[start:end]
    assert "const manual = event.trigger === 'manual'" in body
    # Resolve the run still open at the transcript tail BEFORE attaching.
    assert "_coalesceLiveActivityGroups()" in body
    assert "const liveGroup" in body
    # Manual compaction folds into the run's own group, not a fresh shell.
    assert "target = liveGroup" in body
    assert "resolvedOwner = liveGroup.dataset.activityTurnId || ''" in body
    # A fresh shell is still the fallback when no run is open.
    assert "addChatBubble(" in body
    assert "activity-' + interactionId" in body


# ── 10. A closed prior-run group never receives new-run activity (#2) ────────
def test_closed_prior_run_group_cannot_receive_new_activity():
    stream = _source("ui/chat/js/chat-stream.js")
    assert "function _activityGroupIsLiveTail(" in stream
    start = stream.index("function _activityGroupForTurn(")
    end = stream.index("function positionActivityGroupAfterOwner(", start)
    body = stream[start:end]
    assert "_activityGroupIsLiveTail(active)" in body
    assert ("_activityGroupIsLiveTail(active)) return active;"
            in body)
    # The guard only returns the active group when it is still the current run's
    # live tail — i.e. positioned AFTER the last user row.
    tail_start = stream.index("function _activityGroupIsLiveTail(")
    tail_end = stream.index("function _activityGroupForTurn(", tail_start)
    tail = stream[tail_start:tail_end]
    assert "const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user')" in tail
    assert "const lastUser = users[users.length - 1]" in tail
    assert "while (node)" in tail


# ── 11. Normal completion ends controls and renders a separate closer (#3) ────
def test_completion_ends_controls_and_renders_separate_closer():
    stream = _source("ui/chat/js/chat-stream.js")
    assert "function _renderCompletionCloser(" in stream
    # Fold path: the final answer must not remain only as the in-group preview.
    fav_start = stream.index("function finalizeAgentResponse(")
    fav_end = stream.index("function _renderCompletionCloser(", fav_start)
    fav = stream[fav_start:fav_end]
    assert "app.endRunControls(ownerTurnId || turnId)" in fav
    assert "_renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true)" in fav
    # The closer helper is a real separate bubble positioned after the group.
    closer = stream[stream.index("function _renderCompletionCloser("):
                    stream.index("function ensureStreamingBubbleForActiveTurn(")]
    assert "terminal-closer" in closer
    assert "group.insertAdjacentElement('afterend', bubble)" in closer
    assert "data-msg-id" in closer


# ── 12. Reconcile response rows dedupe by durable message id (#4) ─────────────
def test_reconcile_response_rows_dedupe_by_durable_message_id():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function _appendCallsToToolContainer(")
    end = stream.index("function _comparableOutput(", start)
    body = stream[start:end]
    assert "knownDurableMsgIds" in body
    assert "Array.from(panel.querySelectorAll('[data-msg-id]'))" in body
    assert "knownDurableMsgIds.has(String(entry.id))" in body


# ── 13. Provisional live shell sits after its user and is adopted (#1) ───────
def test_provisional_live_shell_positions_after_user_and_is_adopted():
    stream = _source("ui/chat/js/chat-stream.js")
    pstart = stream.index("function positionActivityGroupAfterOwner(")
    pend = stream.index("function rekeyActivityGroupOwner(", pstart)
    pos = stream[pstart:pend]
    assert "_LIVE_ACTIVITY_CLS" in pos
    assert "lastUser.insertAdjacentElement('afterend', group)" in pos
    # A single run never renders two groups: the durable creation path adopts
    # the provisional live-activity mirror instead of making a second shell.
    astart = stream.index("function attachActivityEntries(")
    aend = stream.index("function attachProgressToActivity(", astart)
    attach = stream[astart:aend]
    assert "_mirrorBubble(String(app.currentSessionId || ''))" in attach
    assert "bubble = liveMirror" in attach
    assert "bubble.dataset.activityTurnId = String(owner)" in attach


# ── 14. A partial response never replaces the last complete preview (#2) ─────
def test_partial_response_never_replaces_complete_preview():
    stream = _source("ui/chat/js/chat-stream.js")
    sstart = stream.index("function _setPreviewVisible(")
    send = stream.index("let _activityPreviewPopover = null;", sstart)
    body = stream[sstart:send]
    assert "entry._finalized !== true" in body
    assert "source.classList.contains('ca-progress-row')" in body
    assert "entry._finalized !== true || entry.previewExcluded === true" in body
    # The live folded completion marks its response row finalized so the
    # preview swaps to the complete answer on completion.
    assert "{ kind: 'response', id: turnId, content: text, createdAt, _finalized: true }" in stream


# ── 15. Ack adopts/removes the pending duplicate synchronously (#1) ──────────
def test_durable_ack_removes_user_twin_before_activity_rekey():
    send = _source("ui/chat/js/chat-send.js")
    start = send.index("function _markBubbleSaved(")
    body = send[start:start + 2500]
    sweep = body.index("Array.from(app.chatMessages?.querySelectorAll(selector)")
    rekey = body.index("app.rekeyActivityGroupOwner(")
    assert "bubble.setAttribute('data-msg-id', String(turnId))" in body
    assert "if (other !== bubble) other.remove();" in body
    assert sweep < rekey, "durable user twins must be removed before group rekey"


def test_reconcile_adopts_only_latest_pending_provisional_user():
    actions = _source("ui/chat/js/chat-bubble-actions.js")
    start = actions.index("function ensureUserBubble(")
    end = actions.index("// Expose on app immediately", start)
    body = actions[start:end]
    assert "const candidate = users.length ? users[users.length - 1] : null;" in body
    assert "candidate.hasAttribute('data-pending-id')" in body
    assert "provisionalId.startsWith('msg_')" in body
    assert "candidateText === cont.trim()" in body
    assert "if (b !== canonical) b.remove();" in body


def test_more_menu_model_row_shows_alias_then_real_model():
    """The ⋮ menu's Model row names a model by its DISPLAY NAME (the roster
    alias) and falls back to the real model name only when the roster row has
    none. The provider is always attributed to the WebAgent Catalog — the real
    upstream provider is never presented on a bubble."""
    actions = _source("ui/chat/js/chat-bubble-actions.js")

    # Alias wins; the real model name (provider path dropped) is the fallback.
    assert "function _publicModelLabel(model, alias)" in actions
    assert "if (name && name !== raw) return name;" in actions
    assert "return _shortModelName(name || raw);" in actions

    # The alias travels in message metadata as display_name; the server stores
    # the real model id there when the row has no alias, so only a differing
    # value counts as a display name. A closer records its own name; a legacy
    # closer row that stored only the raw id inherits the parent's name, but ONLY
    # when the parent ran on the same model, and never its model or effort.
    assert "if (!displayName && alias && alias !== String(meta.model).trim()) displayName = alias;" in actions
    assert "if (!displayName && cached && cached.parent_id) {" in actions
    assert "String(pm.model) === String(model)" in actions
    assert "return { model, effort, displayName };" in actions

    # The Model row routes through the alias-aware label…
    assert "const name = _publicModelLabel(model, displayName);" in actions
    # …and attributes the provider to the catalog, never the real one.
    assert "row.title = (pos ? pos + ' · ' : '') + 'WebAgent Catalog · ' + name" in actions


def test_live_turn_and_closer_bubbles_carry_the_users_display_name():
    """A live turn stamps the bubble with the alias reported on the model event —
    never the raw id alone — and the Output Closer persists its own display name,
    so the closer bubble is labelled exactly like the agent's replies."""
    stream = _source("ui/chat/js/chat-stream.js")
    assert "app._activeTurnDisplayName" in stream
    assert (
        "_setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort,"
        " app._activeTurnDisplayName)" in stream
    )
    # No live stamp may drop the alias — a bare three-argument stamp leaves the
    # bubble with only the raw id until (and unless) metadata arrives.
    assert "_setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort);" not in stream

    closer = _source("app/agent/output_closer.py")
    assert "async def _closer_display_name(user_id: str, agent_rec: Any, model_str: str) -> str:" in closer
    assert '"display_name": closer_display_name,' in closer
    # Presentation only: the closer resolves the Standard roster row for its name,
    # while the request itself still runs on the raw model id.
    assert 'resolve_agent_role_config(user_id, agent_rec, "standard")' in closer

    # The chat records the name the event reported before stamping.
    activity = _source("ui/shared/js/chat-activity.js")
    assert "app._activeTurnDisplayName = event.display_name || '';" in activity


# ── 16. Durable preview finalization is monotonic (#2) ───────────────────────
def test_unfinalized_response_is_invisible_even_without_prior_preview():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function _latestActivityRow(")
    body = stream[start:start + 1800]
    assert "row.classList.contains('ca-response-row')" in body
    assert "entry._finalized !== true" in body
    preview = stream[stream.index("function _setPreviewVisible("):stream.index("let _activityPreviewPopover", stream.index("function _setPreviewVisible("))]
    assert "entry._finalized !== true || entry.previewExcluded === true" in preview


def test_same_durable_id_replaces_partial_row_with_finalized_content():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function _appendCallsToToolContainer(")
    end = stream.index("function _comparableOutput(", start)
    body = stream[start:end]
    assert "panel.querySelector(`[data-msg-id=\"${durableKey}\"]`)" in body
    assert "const finalityChanged = prior._finalized !== entry._finalized;" in body
    assert "if (!contentChanged && !finalityChanged) continue;" in body
    assert "existing.replaceWith(replacement);" in body


def test_preview_only_moves_forward_by_stable_order_and_content():
    stream = _source("ui/chat/js/chat-stream.js")
    mono = stream[stream.index("function _activityRowMonoKey("):stream.index("function _setPreviewVisible(")]
    assert "entry.interactionSeq ?? entry.sessionSeq ?? entry.session_seq" in mono
    preview = stream[stream.index("function _setPreviewVisible("):stream.index("let _activityPreviewPopover", stream.index("function _setPreviewVisible("))]
    assert "prevEl.__monoKey > newMonoKey" in preview
    assert "prevEl.__sourceRow === source && prevEl.__sourceText === sourceText" in preview


# ── 17. Response + summary + reload converge to one closer (#3) ──────────────
def test_summary_paths_use_canonical_terminal_closer_not_summary_bubble():
    ws = _source("ui/shared/js/agentWs.js")
    load = _source("ui/chat/js/session-load.js")
    reconcile = _source("ui/chat/js/chat-reconcile.js")
    virtual = _source("ui/chat/js/chat-virtual-scroll.js")
    assert "app._renderCompletionCloser(" in ws
    assert "app.renderSummary(msg.content || '', msg.id" in load
    assert "app.renderSummary(cont, mid || undefined" in reconcile
    assert "app.renderSummary(msg.content || '', msg.id || msgId" in virtual
    closer = _function(_source("ui/chat/js/chat-stream.js"), "_renderCompletionCloser", "notifyExecutionMode")
    assert "node.classList?.contains('summary-bubble')" in closer
    assert "node.remove();" in closer


def test_completed_reload_only_uses_finalized_response_for_closer():
    load = _source("ui/chat/js/session-load.js")
    start = load.index("function _settleCompletedRunGroup(")
    end = load.index("function _emitToolGroupBubble(", start)
    body = load[start:end]
    assert "e._finalized === true" in body
    assert "app._renderCompletionCloser(" in body
    assert "app.endRunControls(owner || '')" in body


# ── 18. Streamed multi-line progress commits only at agent_step_end ─────────
def test_progress_preview_waits_for_complete_finalized_multiline_payload():
    stream = _source("ui/chat/js/chat-stream.js")
    latest = stream[stream.index("function _latestActivityRow("):stream.index("// Scroll a row", stream.index("function _latestActivityRow("))]
    assert "entry._finalized !== true || entry.previewExcluded === true" in latest
    attach = stream[stream.index("function attachProgressToActivity("):stream.index("// ── Live-activity mirror", stream.index("function attachProgressToActivity("))]
    assert "agent_step_end is the commit boundary" in attach
    assert "kind: 'progress', id: asstId, content: text, createdAt," in attach
    assert "_finalized: true" in attach
    preview = stream[stream.index("function _setPreviewVisible("):stream.index("let _activityPreviewPopover", stream.index("function _setPreviewVisible("))]
    assert "source.classList.contains('ca-progress-row')" in preview
    assert "entry._finalized !== true || entry.previewExcluded === true" in preview


# ── 19. Terminal closer payload is never a preview source ───────────────────
def test_terminal_closer_excludes_source_response_from_preview_and_count():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = stream[stream.index("function _renderCompletionCloser("):stream.index("function ensureStreamingBubbleForActiveTurn(", stream.index("function _renderCompletionCloser("))]
    assert "entry.previewExcluded = true;" in closer
    assert "const panel = detail && detail.querySelector('.bubble-tool-calls-panel');" in closer
    assert "_updateToolCallsHead(head, panel);" in closer
    latest = stream[stream.index("function _latestActivityRow("):stream.index("// Scroll a row", stream.index("function _latestActivityRow("))]
    assert "entry.previewExcluded === true" in latest
    head = stream[stream.index("function _updateToolCallsHead("):stream.index("// ── Collapsed preview", stream.index("function _updateToolCallsHead("))]
    assert "row.__entry?.previewExcluded !== true" in head

# ── 20. agent_step_end commits accumulated stream content before finality ────
def test_step_end_consumes_authoritative_accumulator_before_finalized_preview():
    stream = _source("ui/chat/js/chat-stream.js")
    resolver = stream[stream.index("function consumeFinalizedStepContent("):stream.index("function finalizeAgentStep(", stream.index("function consumeFinalizedStepContent("))]
    assert "_resolveStepContent(content, asstId)" in resolver
    assert "_wsTurnBuffers.delete(asstId)" in resolver
    # The step id is the durable asst_id shared by stream AND agent_step_end; the
    # run turn must never be the accumulator key (it is constant across the run).
    assert "keyed by the durable step asst_id" in resolver
    finalizer = stream[stream.index("function finalizeAgentStep("):stream.index("function markAgentInterrupted(", stream.index("function finalizeAgentStep("))]
    assert "agent_step_end is a PENDING-finalization signal" in finalizer
    assert "_pendingStepFinalize.set(key," in finalizer
    assert "kind: 'progress', id: asstId, content: text" in stream


# ── 21. response wins over summary and removes every terminal candidate ──────
def test_canonical_response_replaces_prior_terminal_candidates_and_locks_summary():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = stream[stream.index("function _renderCompletionCloser("):stream.index("function ensureStreamingBubbleForActiveTurn(", stream.index("function _renderCompletionCloser("))]
    assert "group.dataset.terminalCanonicalId && !canonicalFinal" in closer
    assert "let node = group.nextElementSibling;" in closer
    assert "Replace ALL prior terminal candidates" in closer
    assert "node.classList?.contains('terminal-closer')" in closer
    assert "node.classList?.contains('agent')" in closer
    assert "if (canonicalFinal) group.dataset.terminalCanonicalId" in closer
    response = stream[stream.index("function finalizeAgentResponse("):stream.index("function _renderCompletionCloser(", stream.index("function finalizeAgentResponse("))]
    assert response.count("_renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true)") == 2


# ── 22. Sequential step ends consume their own stream buffer segments ───────
def test_sequential_step_end_consumes_buffer_before_later_response_streams():
    stream = _source("ui/chat/js/chat-stream.js")
    consume = stream[stream.index("function consumeFinalizedStepContent("):stream.index("function finalizeAgentStep(", stream.index("function consumeFinalizedStepContent("))]
    assert "_resolveStepContent(content, asstId)" in consume
    assert "if (asstId) _wsTurnBuffers.delete(asstId);" in consume
    assert "_dropPendingStreamRender(asstId);" in consume
    # Each step is keyed by its own durable asst_id, so sequential steps in one
    # run never share a segment.
    assert "Per-step consume: the accumulator is keyed by the durable step asst_id" in consume
    finalizer = stream[stream.index("function finalizeAgentStep("):stream.index("function markAgentInterrupted(", stream.index("function finalizeAgentStep("))]
    assert "_pendingStepFinalize.set(key," in finalizer
    assert "_armStepFlush(asstId);" in finalizer
    assert "_flushPreviousPendingStep(ownerTurnId, asstId);" in finalizer
    publish = stream[stream.index("function _publishFinalizedStep("):stream.index("function finalizeAgentStep(", stream.index("function _publishFinalizedStep("))]
    # A completed progress step is not a terminal answer, even if an upstream
    # event labels it main-like. Only finalizeAgentResponse handles main/final.
    assert "Progress is never a standalone answer" in publish
    assert "progressKind === 'main'" not in publish
    assert "attachProgressToActivity(" in publish


# ── 23. Canonical final can upgrade an earlier ordinary candidate ───────────
def test_authoritative_final_upgrade_replaces_prior_candidate_not_lock_it():
    stream = _source("ui/chat/js/chat-stream.js")
    closer = stream[stream.index("function _renderCompletionCloser("):stream.index("function ensureStreamingBubbleForActiveTurn(", stream.index("function _renderCompletionCloser("))]
    assert "group.dataset.terminalCanonicalId && !canonicalFinal" in closer
    assert "if (node !== bubble && terminalCandidate) node.remove();" in closer
    assert "if (canonicalFinal) group.dataset.terminalCanonicalId" in closer
    response = stream[stream.index("function finalizeAgentResponse("):stream.index("function _renderCompletionCloser(", stream.index("function finalizeAgentResponse("))]
    assert response.count("_renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true)") == 2


# ── 24. Stream and step-end may use different assistant/item IDs ───────────
def test_run_scoped_segment_aliases_mismatched_stream_and_step_end_ids():
    stream = _source("ui/chat/js/chat-stream.js")
    # stream and agent_step_end share ONE durable asst_id per step; the run turn
    # must not be the accumulator key. Pin the per-step keying + debounce map.
    segment = stream[stream.index("const _pendingStepFinalize"):stream.index("const _pendingStreamRenders", stream.index("const _pendingStepFinalize"))]
    assert "stepId (asst_id) → { content" in segment
    assert "function _cancelStepFlush(stepId)" in segment
    assert "function _armStepFlush(stepId)" in segment
    assert "function _resolveStepContent(content, stepId)" in segment
    # _flushPendingStep lives after _pendingStreamRenders; check the whole file.
    assert "function _flushPendingStep(stepId, reason)" in stream
    append = stream[stream.index("function appendStreamToActiveBubble("):stream.index("function finalizeAgentResponse(", stream.index("function appendStreamToActiveBubble("))]
    assert "_armStepFlush(turnId);" in append
    assert "_flushPreviousPendingStep(ownerTurnId, turnId);" in append
    step = stream[stream.index("function finalizeAgentStep("):stream.index("function markAgentInterrupted(", stream.index("function finalizeAgentStep("))]
    assert "_pendingStepFinalize.set(key," in step


# ── 25. Back-to-back progress and final consume separate segments ──────────
def test_progress_then_final_consume_separate_segments_and_final_wins_terminal():
    stream = _source("ui/chat/js/chat-stream.js")
    response = stream[stream.index("function finalizeAgentResponse("):stream.index("function _renderCompletionCloser(", stream.index("function finalizeAgentResponse("))]
    # A final response first flushes the preceding pending progress step, THEN
    # starts a fresh final segment — progress and final must be separate.
    assert "_flushPreviousPendingStep(ownerTurnId, turnId);" in response
    assert "content = consumeFinalizedStepContent(content, turnId, ownerTurnId);" in response
    assert "The final response owns a fresh post-step segment" in response
    assert "_renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true)" in response
    closer = stream[stream.index("function _renderCompletionCloser("):stream.index("function ensureStreamingBubbleForActiveTurn(", stream.index("function _renderCompletionCloser("))]
    assert "Replace ALL prior terminal candidates" in closer
    assert "terminalCanonicalId" in closer


# ── 26. step_end-before-remaining-deltas: pending, then quiet-period flush ────
def test_step_end_before_remaining_deltas_keeps_pending_until_debounce():
    stream = _source("ui/chat/js/chat-stream.js")
    finalizer = stream[stream.index("function finalizeAgentStep("):stream.index("function markAgentInterrupted(", stream.index("function finalizeAgentStep("))]
    # agent_step_end must register the step as PENDING, never publish inline.
    assert "agent_step_end is a PENDING-finalization signal" in finalizer
    assert "_pendingStepFinalize.set(key," in finalizer
    assert "_armStepFlush(asstId);" in finalizer
    assert "content = consumeFinalizedStepContent(content, asstId, ownerTurnId);" not in finalizer
    # A delta for a pending step re-arms the quiet period so the full ordered
    # stream accumulates before publish.
    append = stream[stream.index("function appendStreamToActiveBubble("):stream.index("function finalizeAgentResponse(", stream.index("function appendStreamToActiveBubble("))]
    assert "_armStepFlush(turnId);" in append
    # The flush resolves the authoritative (longest) content before publishing.
    flush = stream[stream.index("function _flushPendingStep("):stream.index("function _publishFinalizedStep(", stream.index("function _flushPendingStep("))]
    assert "_resolveStepContent(pending.content || '', key)" in flush
    assert "_publishFinalizedStep(content, key, pending.createdAt, pending.ownerTurnId," in flush


# ── 27. Boundary flush at semantic boundary (tool / final / completion) ──────
def test_pending_step_flushes_at_semantic_boundary():
    stream = _source("ui/chat/js/chat-stream.js")
    assert "function _flushPreviousPendingStep(ownerTurnId, currentStepId)" in stream
    flush = stream[stream.index("function _flushPreviousPendingStep("):stream.index("function _flushPendingStep(", stream.index("function _flushPreviousPendingStep("))]
    assert "_flushPendingStep(key, 'boundary')" in flush
    assert "pending.ownerTurnId !== ownerTurnId" in flush
    # Both a new step_end and a final response are boundary triggers.
    assert "_flushPreviousPendingStep(ownerTurnId, asstId);" in stream[stream.index("function finalizeAgentStep("):stream.index("function markAgentInterrupted(", stream.index("function finalizeAgentStep("))]
    assert "_flushPreviousPendingStep(ownerTurnId, turnId);" in stream[stream.index("function finalizeAgentResponse("):stream.index("function _renderCompletionCloser(", stream.index("function finalizeAgentResponse("))]


# ── 28. Interrupted pending flush is cancelled, never published ─────────────
def test_interrupted_pending_flush_is_cancelled():
    stream = _source("ui/chat/js/chat-stream.js")
    interrupted = stream[stream.index("function markAgentInterrupted("):stream.index("function _renderTerminalCloser(", stream.index("function markAgentInterrupted("))]
    assert "_cancelAllPendingStepFlushes();" in interrupted
    # The late-timer guard also refuses to publish into a switched session.
    flush = stream[stream.index("function _flushPendingStep("):stream.index("function _publishFinalizedStep(", stream.index("function _flushPendingStep("))]
    assert "pending.sessionId !== app.currentSessionId" in flush
    assert "_cancelStepFlush(key);" in flush


# ── 29. Session switch cancels pending step flushes via the choke point ─────
def test_session_switch_cancels_pending_step_flushes():
    load = _source("ui/chat/js/session-load.js")
    start = load.index("isSwitch && app._lastLoadedSessionId && typeof app.chatActivitySessionChanged")
    end = load.index("app._sessionLoadInFlight = true;", start)
    body = load[start:end]
    assert "app.cancelAllPendingStepFlushes" in body
    stream = _source("ui/chat/js/chat-stream.js")
    assert "app.cancelAllPendingStepFlushes = _cancelAllPendingStepFlushes;" in stream
    assert "function _cancelAllPendingStepFlushes()" in stream
    core = _source("ui/chat/js/session-core.js")
    init = _source("ui/chat/js/session-init.js")
    assert "app.loadChatComponents?.(null)" in core
    assert init.count("app.loadChatComponents?.(null)") >= 2


# ── 30. Durable row is full but lagging reconcile snapshot must not truncate ─
# The durable API row carries the COMPLETE three-line payload. During streaming
# the reconcile poll reads the throttled (~0.6s) durable snapshot which still
# lags — e.g. "RED" — while the live WS buffer already holds the full text. A
# raw `_wsTurnBuffers.set(key, text)` would clobber the live buffer and the
# subsequent finalize would publish only "RED". The monotonic writer must keep
# the longer live buffer and never shorten it.
def test_reconcile_snapshot_never_truncates_live_multiline_buffer():
    stream = _source("ui/chat/js/chat-stream.js")
    monotonic = stream[stream.index("function _setTurnBufferMonotonic("):stream.index("function consumeFinalizedStepContent(")]
    assert "incoming.length > current.length" in monotonic
    assert "_wsTurnBuffers.set(key, incoming);" in monotonic
    # Both streaming writes in seedStreamingBubble route through the monotonic
    # writer, so a lagging snapshot can never overwrite the live stream.
    seed = stream[stream.index("function seedStreamingBubble("):stream.index("// \"N tool calls\" label")]
    assert seed.count("_setTurnBufferMonotonic(turnId, content || '')") >= 1
    # Reconcile uses the monotonic writer (not a raw buffer clobber) on the
    # streaming branch.
    reconcile = _source("ui/chat/js/chat-reconcile.js")
    assert "_setTurnBufferMonotonic(key, text);" in reconcile
    assert "_wsTurnBuffers.set(key, text)" not in reconcile


# ── 31. Reconcile finalize resolves to the authoritative payload, never a
#        mismatched fragment ────────────────────────────────────────────────
# The step_end payload (eventText) is authoritative for the durable asstId.
# The WS buffer may be an ordered delta accumulation OR a reconcile snapshot
# that lags the live stream. A fragment that is merely longer but is NOT a
# prefix-superset must never override the complete payload (that is how the
# v580 "OR, The, My, Let" fragmentation arose). The resolver trusts the buffer
# only when it begins with the authoritative text; otherwise it returns the
# authoritative eventText.
def test_reconcile_finalize_prefers_authoritative_payload_over_fragment():
    stream = _source("ui/chat/js/chat-stream.js")
    resolve = stream[stream.index("function _resolveStepContent("):stream.index("function _setTurnBufferMonotonic(")]
    assert "buffered.indexOf(eventText) === 0" in resolve
    assert "eventText.indexOf(buffered) === 0" in resolve
    assert "return eventText;" in resolve


# ── 32. Real event sequence: WS delta chunks + lagging reconcile snapshot never
#        fragment one durable step into multiple rows ─────────────────────────
# The server persists the step_end snapshot (authoritative) on a ~0.6s throttle,
# so the reconcile `streaming` poll may read a partial "OR" while the live WS has
# already accumulated the full multiline payload. Both writers share ONE durable
# asst_id key; the resolver trusts the authoritative payload over a mismatched
# fragment, and _appendCallsToToolContainer replaces the row by that same
# asst_id (entry.id) rather than appending a second fragment row.
def test_ws_delta_vs_reconcile_snapshot_keeps_single_row_per_asst_id():
    reconcile = _source("ui/chat/js/chat-reconcile.js")
    # The reconcile streaming branch must re-key the snapshot to the durable
    # asst_id and use the monotonic writer (never a raw buffer clobber).
    assert "seedStreamingBubble(key, text, msg.created_at);" in reconcile
    assert "_setTurnBufferMonotonic(key, text);" in reconcile
    # The reconcile must never SET a raw shorter snapshot over the live buffer.
    assert "_wsTurnBuffers.set(key, text)" not in reconcile
    # The board rows coalesce by the durable asst_id (entry.id), not by run.
    stream = _source("ui/chat/js/chat-stream.js")
    append = stream[stream.index("function _appendCallsToToolContainer("):stream.index("function _comparableOutput(")]
    assert '`[data-activity-entry-id="${key}"]`' in append
    assert "existing.replaceWith(replacement);" in append


# ── 33. One closer per run, scoped to its activity group, replace-in-place ────
# A late explicit final must replace the same run's fallback closer in place and
# never merge with / move another run's closer. The closer is found first as the
# activity group's own following sibling (run-scoped), falling back to a
# data-msg-id query only when there is no adjacent closer.
def test_terminal_closer_is_run_scoped_and_replaced_in_place():
    stream = _source("ui/chat/js/chat-stream.js")
    completion = stream[stream.index("function _renderCompletionCloser("):stream.index("function _renderTerminalCloser(")]
    assert "group.nextElementSibling" in completion
    assert "terminal-closer" in completion
    assert "bubble.setAttribute('data-msg-id', String(turnId))" in completion
    # The close-of-a-run replacement re-stamps the closer so a later event for
    # the same run finds it by its own id, never a prior run's.
    assert "group.dataset.terminalCanonicalId" in completion
    # Both closer functions must share the run-scoped lookup before any global
    # data-msg-id query, so consecutive runs never see one shared closer.
    terminal = stream[stream.index("function _renderTerminalCloser("):stream.index("function _hasCloserAfter(")]
    assert "terminal-closer" in terminal
    assert "group.nextElementSibling" in terminal
    assert "group.isConnected" in terminal
    assert "querySelector" not in terminal or "data-msg-id" in terminal


# ── 34. Completion clears the live "Sending… · Ns" progress note ─────────────
# When a run completes normally (finalizeAgentResponse → closer/end controls),
# the live-activity note and its transcript mirror must be retired, so a
# "Sending… · 32.0s" note can never outlive a Completed session. The mirror
# retirement is invoked on every completion path (fold, non-fold, interrupt).
def test_completion_retires_live_activity_note():
    stream = _source("ui/chat/js/chat-stream.js")
    body = stream[stream.index("function finalizeAgentResponse("):stream.index("function finalizeAgentStep(")]
    # The fold completion path retires the mirror note.
    assert "mirrorActivityEnd" in body
    # The interrupt path (markAgentInterrupted) also retires it for completeness.
    interrupt = stream[stream.index("function markAgentInterrupted("):stream.index("function _renderTerminalCloser(")]
    assert "mirrorActivityEnd" in interrupt


# ── 35. Consecutive completed runs: one closer anchored per run's group ───────
# Two runs back-to-back must each keep EXACTLY ONE closer, and that closer must be
# anchored (data-activity-turn-id / data-owner-run) to ITS OWN activity group so
# the canonical transcript reorder groups them run-by-run. The closer is never a
# shared/global node, so a second run can never inherit or move the first run's.
def test_consecutive_completed_runs_keep_one_closer_per_run():
    stream = _source("ui/chat/js/chat-stream.js")
    completion = _function(stream, "_renderCompletionCloser", "_renderTerminalCloser")
    terminal = _function(stream, "_renderTerminalCloser", "_hasCloserAfter")
    for closer in (completion, terminal):
        # The closer claims the run's owner anchor, so the reorder can pair it.
        assert "data-activity-turn-id" in closer
        assert "data-owner-run" in closer
        # The anchor is taken from the run's group, never a global id.
        assert "group.dataset.activityTurnId" in closer


# ── 36. Progress-only group still labels "1 message" (blank-header fix) ───────
# A run group with only progress rows (no _finalized response, no tools) must not
# produce a blank header. The message counter counts distinct message/progress
# rows regardless of _finalized, so it reads "1 message" instead of "".
def test_progress_only_group_labels_message_count():
    stream = _source("ui/chat/js/chat-stream.js")
    body = _function(stream, "_updateToolCallsHead", "_syncToolCallPreviews")
    assert "ca-progress-row" in body
    assert "ca-response-row" in body
    assert "row.__entry?.previewExcluded !== true" in body
    # The counter does NOT require _finalized — a progress-only group still counts.
    assert "row.__entry?._finalized === true" not in body.replace("previewExcluded", "")


def test_persisted_activity_without_semantic_counters_has_an_update_label():
    stream = _source("ui/chat/js/chat-stream.js")
    body = _function(stream, "_updateToolCallsHead", "_syncToolCallPreviews")
    assert "summaryCounts.updates" in body
    assert "activity update" in body
    assert "if (!parts.length && updates)" in body


# ── 37. DOM/visual order after reload: group immediately before its closest ───
# _reorderTranscriptCanonical (chat-bubble.js) is the single ordering authority.
# It must force each activity group to sit IMMEDIATELY before its one closer,
# anchored by the shared owner-run key, so session_seq sorting can never place a
# reloaded closer above/away from its group.
def test_reload_order_forces_group_immediately_before_closer():
    bubble = _source("ui/chat/js/chat-bubble.js")
    assert "_groupClosersByOwner(ordered)" in bubble
    start = bubble.index("function _groupClosersByOwner(")
    body = bubble[start:]
    # The reorder is keyed by the run owner (dataset.ownerRun / dataset.activityTurnId).
    assert "dataset.ownerRun" in body or "dataset.activityTurnId" in body
    assert "terminal-closer" in body
    # It repositions the closer to the group's immediate next sibling.
    assert "gIdx + 1" in body or "immediately" in body
    # It removes stray/duplicate closers for the same run (exactly one closer).
    assert "stray" in body


# ── 38. Late final replacement on reload: non-folded answer becomes the closer ─
# When the main/final answer is NOT folded (fold_main_messages=false), the phased
# builder tags it _agentCloser and the append path promotes the emitted bubble to
# a terminal-closer anchored to its run, so a reloaded completed run renders
# exactly one closer after its group instead of an ordinary detached .agent bubble.
def test_reload_nonfolded_answer_promotes_to_run_closer():
    load = _source("ui/chat/js/session-load.js")
    main_start = load.index("if (phase === 'main' || phase === 'final')")
    builder = load[main_start:load.index("// Progress is activity, never a standalone answer.", main_start)]
    assert "msg._agentCloser = true" in builder
    assert "msg._closerOwner" in builder
    append = load[load.index("function _appendMessagesToTranscript("):load.index("app._appendMessagesToTranscript")]
    assert "_promoteRunCloserBubble" in append
    stream = _source("ui/chat/js/chat-stream.js")
    promote = _function(stream, "_promoteRunCloserBubble", "_lastCompleteMessageInRun")
    assert "'terminal-closer'" in promote
    assert "data-owner-run" in promote
    assert "app.endRunControls(owner)" in promote
    assert "group.dataset.terminalCanonicalId" in promote
    # EXACT per-run key gating: promotion is STRICTLY keyed on a runKey established
    # before render. A bubble with no exact matching group is NOT promoted, and
    # there is no nearest/current-group fallback across a user boundary.
    assert "runKey" in promote
    assert "info.runKey" in promote
    assert "if (!runKey) return bubble" in promote
    assert 'data-run-key' in promote
    assert "if (!group) return bubble" in promote


# ── 39. Exact per-run key: three consecutive runs never cross-promote ──────────
# Regression for the scroll/reload corruption where the older FIRST NORTH final
# was merged into the newest ONE COPPER closer. Each run gets ONE group + ONE
# closer keyed by an EXACT run key (owner turn id + leading durable seq). An
# orphan final with no matching key is left as ordinary prose (its own run keeps
# its group and closer); the key is never derived from a mutable/current fallback.
def test_exact_runkey_prevents_cross_run_closer_promotion():
    load = _source("ui/chat/js/session-load.js")
    builder = load[load.index("const _runKey = (ownerTurnId, leadingSeq) =>"):
        load.index("// Folding mode")]
    assert "const o = (ownerTurnId == null ? '' : String(ownerTurnId)).trim();" in builder
    assert "const s = (leadingSeq == null ? '' : String(leadingSeq)).trim();" in builder
    assert "return o ? (o + '::' + s) : '';" in builder
    main_start = load.index("if (phase === 'main' || phase === 'final')")
    main = load[main_start:load.index("// Progress is activity, never a standalone answer.", main_start)]
    assert "msg._closerKey = _runKey(owner, currentRunSeq)" in main
    assert "msg._closerOwner = owner" in main
    # The key must never come from an ambiguous current-turn-only fallback.
    assert "currentRunSeq" in main
    ensure = load[load.index("const ensureActivity = msg => {") + 1:
        load.index("  for (const msg of messages) {", load.index("const ensureActivity = msg => {"))]
    assert "runKey: _runKey(owner, currentRunSeq)" in ensure
    append = load[load.index("function _appendMessagesToTranscript("):load.index("app._appendMessagesToTranscript")]
    assert "runKey: msg._closerKey || ''" in append
    # The live/builder group is stamped with the exact run key. The function must
    # accept an explicit fifth runKey param and resolve it as runKey || anchor.runKey
    # (NEW: the previous code read a dead `opts.runKey`, so the live group never got
    # data-run-key — that was the settle corruption). A last-resort derived key uses
    # the same owner+leading-seq formula.
    stream = _source("ui/chat/js/chat-stream.js")
    attach = stream[stream.index("function attachActivityEntries("):
        stream.index("function attachProgressToActivity(")]
    assert "function attachActivityEntries(entries, targetBubble, anchor, ownerTurnId, runKey)" in attach
    assert "opts.runKey" not in attach
    assert "runKey || (anchor && anchor.runKey)" in attach or "anchor && anchor.runKey" in attach
    assert "bubble.dataset.runKey" in attach
    closer = _function(stream, "_renderCompletionCloser", "ensureStreamingBubbleForActiveTurn")
    assert "data-run-key" in closer
    terminal = _function(stream, "_renderTerminalCloser", "_lastCompleteMessageInRun")
    assert "data-run-key" in terminal
    # The canonical reorder keys the run by the exact run key, never owner-only.
    bubble = _source("ui/chat/js/chat-bubble.js")
    reorder = _function(bubble, "_groupClosersByOwner", "_setBubbleSessionSeq")
    assert "el.dataset.runKey" in reorder
    assert "NEVER match by nearest/current group" in reorder or "orphan final" in reorder


# ── 40. Deterministic DOM/order: three runs keep each final in its OWN closer ──
# Reproduces _groupClosersByOwner against an ordered node list (no live DOM needs
# to be loaded) to prove the fix deterministically: three consecutive runs — two
# with the SAME container text (duplicate final) and an older run with a DISTINCT
# final — must each keep exactly ONE activity group immediately followed by its
# OWN closer. Re-invoking the reorder (the scroll/reload path) must be idempotent:
# it never relocates a closer into a different run's group.
def test_reorder_keeps_each_run_final_in_its_own_closer():
    # Node model: {kind, key, nextKey} is enough to drive _groupClosersByOwner's
    # owner-run clustering + "group immediately before its one closer" guarantee.
    # key = the exact per-run key the builder now stamps (owner turn id + seq).
    R1, R2, R3 = "user-a::10", "user-b::20", "user-c::30"

    def closer_in_group(ordered):
        # After reorder, for every group with a closer sharing its exact key, the
        # closer must be the group's immediate next sibling.
        keys = {}
        for i, node in enumerate(ordered):
            if node["kind"] == "group":
                keys.setdefault(node["key"], i)
        for i, node in enumerate(ordered):
            if node["kind"] != "closer":
                continue
            gi = keys.get(node["key"], -1)
            if gi == -1:
                continue  # no group for this closer -> must NOT be consumed by another
            assert ordered[gi]["kind"] == "group"
            assert gi + 1 == i, f"closer for {node['key']} detached (at {i}, group at {gi})"

    def group_has_closer(ordered):
        seen_groups = set()
        seen_closers = set()
        for node in ordered:
            if node["kind"] == "group":
                seen_groups.add(node["key"])
            elif node["kind"] == "closer":
                seen_closers.add(node["key"])
        return seen_groups, seen_closers

    # Three runs: older distinct final (R1) + two repeats of ONE COPPER (R2,R3).
    # The older orphan/duplicate scenario is what the bug corrupted.
    ordered = [
        {"kind": "group", "key": R1},
        {"kind": "closer", "key": R1, "text": "FIRST NORTH"},
        {"kind": "group", "key": R2},
        {"kind": "closer", "key": R2, "text": "ONE COPPER"},
        {"kind": "group", "key": R3},
        {"kind": "closer", "key": R3, "text": "ONE COPPER"},
    ]
    # Re-invoke the reorder twice (init + scroll/reload) — must be idempotent.
    for _ in range(2):
        reordered = _run_owner_reorder(ordered)
        closer_in_group(reordered)
        g, c = group_has_closer(reordered)
        assert g == {R1, R2, R3}
        assert c == {R1, R2, R3}  # exactly one closer per run; none stolen
        # Duplicate text never collapses two runs: each closer keeps its key.
        assert [n["text"] for n in reordered if n["kind"] == "closer"] == [
            "FIRST NORTH", "ONE COPPER", "ONE COPPER",
        ]
        ordered = reordered

    # The cross-run corruption scenario: an orphan final from R1 (its group not yet
    # materialised) must NOT be folded into R3's closer. A closer with a key whose
    # group is absent stays detached (never promoted into a foreign run's group).
    orphan_list = [
        {"kind": "group", "key": R2},
        {"kind": "closer", "key": R2, "text": "ONE COPPER"},
        {"kind": "group", "key": R3},
        {"kind": "closer", "key": R3, "text": "ONE COPPER"},
        {"kind": "closer", "key": R1, "text": "FIRST NORTH"},
    ]
    reordered = _run_owner_reorder(orphan_list)
    closer_in_group(reordered)
    for i, node in enumerate(reordered):
        if node["kind"] == "closer" and node["key"] == R1:
            # The orphan final stays where it is (own group absent) — never merged
            # into R2/R3's closer.
            assert node["text"] == "FIRST NORTH"
            # It must not sit immediately after R3's group (that would be a steal).
            if i > 0 and reordered[i - 1]["kind"] == "group":
                assert reordered[i - 1]["key"] != R3

    # An unkeyed historical closer is equally orphaned. Later activity groups do
    # not establish ownership, so canonical ordering must leave it in place.
    unkeyed_orphan_list = [
        {"kind": "closer", "key": "", "text": "UNKEYED FINAL"},
        {"kind": "group", "key": R2},
        {"kind": "group", "key": R3},
    ]
    assert _run_owner_reorder(unkeyed_orphan_list) == unkeyed_orphan_list


def test_live_settle_keeps_group_preview_before_closer_in_both_phases():
    """Deterministic model of the live run A settle path (two phases).

    Phase 1 — "Closer writing active": the live activity group holds the full
    preview (RUN A LIME / RUN A NAVY) and a fallback closer has already been
    rendered immediately after the group. DOM order: user -> group+preview ->
    closer, Stop visible.
    Phase 2 — "settled reorder": _reorderTranscriptCanonical runs (triggered by
    the settle/reconcile). The closer must NOT jump above the group, the preview
    row must remain in the group, and there is exactly one closer. DOM order stays
    user -> group+preview -> closer, Stop -> Continue.

    The exact per-run key (owner turn id + leading durable seq) is carried on BOTH
    the group and the closer. Without it the reorder falls to an owner/nearest
    fallback and can move the closer above its group on settlement.
    """
    run_key = "userTurnA::42"
    # Each node is a full ordered list (the transcript DOM); the group carries a
    # `preview` row and the closer is its immediate next sibling.
    user_row = {"kind": "user", "key": "userTurnA"}

    def phase_html():
        return {
            "group": {"kind": "group", "key": run_key, "preview": "RUN A LIME / RUN A NAVY"},
            "closer": {"kind": "closer", "key": run_key, "text": "RUN A LIME / RUN A NAVY"},
        }

    def assert_phase(order, phase_label):
        kinds = [n["kind"] for n in order]
        # Exactly one activity group and exactly one closer.
        assert kinds.count("group") == 1, f"{phase_label}: expected one group"
        assert kinds.count("closer") == 1, f"{phase_label}: expected one closer"
        # The group's preview row is intact and belongs to this run.
        g = next(n for n in order if n["kind"] == "group")
        assert g["key"] == run_key
        assert "RUN A LIME / RUN A NAVY" in g["preview"], (
            f"{phase_label}: preview must remain in the group")
        # Order: user -> group -> closer (closer immediately after its group).
        assert kinds == ["user", "group", "closer"], (
            f"{phase_label}: expected user->group->closer, got {kinds}")
        # The closer immediately follows ITS group (same exact key).
        ci = next(i for i, n in enumerate(order) if n["kind"] == "closer")
        assert order[ci - 1]["kind"] == "group", (
            f"{phase_label}: closer must be immediately after its group")
        assert order[ci - 1]["key"] == run_key, (
            f"{phase_label}: closer must follow ITS OWN group (exact key)")

    # Phase 1: closer writing active — fallback closer already rendered.
    phase1 = [user_row, phase_html()["group"], phase_html()["closer"]]
    assert_phase(phase1, "phase1 (closer writing)")

    # Phase 2: settled reorder — run the deterministic reorder model on the
    # same DOM. This reproduces the settle/reconcile invocation.
    phase2 = _run_owner_reorder(
        [user_row, phase_html()["group"], phase_html()["closer"]],
    )
    assert_phase(phase2, "phase2 (settled reorder)")

    # A settle reorder must never split/duplicate: re-running it is idempotent.
    phase2_again = _run_owner_reorder(phase2)
    assert [n["key"] for n in phase2_again] == [n["key"] for n in phase2]
    assert [n["kind"] for n in phase2_again] == [n["kind"] for n in phase2]


def test_empty_model_completion_after_sending_keeps_user_turn_and_renders_fallback_closer():
    """Deterministic regression for an EMPTY/no-output model completion after the
    user turn is submitted.

    A completed run whose assistant answer is empty must still be an authoritative
    terminal boundary (the "absolute closer guarantee"):
      * the submitted USER turn is preserved through reconciliation (never removed),
      * the run's activity group gets a STABLE per-run key even when there are no
        durable assistant rows,
      * EXACTLY ONE terminal fallback closer is rendered, using the last non-empty
        working-agent/progress message, or a deterministic failure label when none
        exists,
      * DOM order is user -> group (key) -> closer (same key) and Stop -> Continue.

    This models both the LIVE empty-completion path (finalizeAgentResponse with no
    content) and the reconcile path (phased builder emitting a fallback closer).
    """
    # User turn must survive: it is a separate durable row, never folded.
    user_row = {"kind": "user", "key": "userTurnEmpty"}
    # The run's activity group gets its exact per-run key even with no assistant
    # final row (derived from the owner turn + leading durable seq).
    run_key = "userTurnEmpty::90"

    def group_with_last_message(last_msg):
        return {
            "kind": "group",
            "key": run_key,
            # The last non-empty working/progress message inside the group.
            "preview": last_msg,
        }

    def fallback_closer(label):
        return {
            "kind": "closer",
            "key": run_key,
            # The closer content is the fallback label (derived from the last
            # complete working message, or a deterministic failure label).
            "text": label,
        }

    def last_complete_message(group):
        # Mirrors _lastCompleteMessageInRun: newest non-empty, non-placeholder
        # progress/response row, else ''.
        import re
        placeholder = re.compile(r"^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$")
        text = (group.get("preview") or "").strip()
        return text if text and not placeholder.match(text) else ""

    def emit_fallback_closer(group):
        # Mirrors the live _renderTerminalCloser / builder fallback logic.
        msg = last_complete_message(group)
        return fallback_closer(msg if msg else "No response")

    def assert_settled(order, phase_label):
        kinds = [n["kind"] for n in order]
        # The original user turn is preserved (never removed).
        assert kinds.count("user") == 1, (
            f"{phase_label}: the submitted user turn must be preserved")
        # Exactly one group, and it carries the stable per-run key.
        assert kinds.count("group") == 1, (
            f"{phase_label}: expected one activity group")
        g = next(n for n in order if n["kind"] == "group")
        assert g["key"] == run_key, (
            f"{phase_label}: group must carry the stable per-run key")
        # Exactly one terminal fallback closer, after ITS group.
        assert kinds.count("closer") == 1, (
            f"{phase_label}: expected exactly one terminal fallback closer")
        ci = next(i for i, n in enumerate(order) if n["kind"] == "closer")
        assert order[ci - 1]["kind"] == "group", (
            f"{phase_label}: fallback closer must be immediately after its group")
        assert order[ci - 1]["key"] == run_key, (
            f"{phase_label}: fallback closer must follow ITS OWN group (exact key)")
        # The closer content is a real fallback, never a blank bubble.
        assert order[ci]["text"].strip(), (
            f"{phase_label}: fallback closer must have content (never blank)")
        # Stop -> Continue: the run is terminal, so no live Stop control remains.
        assert order[ci].get("stop") is not True, (
            f"{phase_label}: a completed run must not show Stop")

    # Case A: empty completion but the group already has a working message
    # (progress text like "FINAL RUN TEAL / FINAL RUN GOLD"). The fallback closer
    # uses that last non-empty working message.
    group_a = group_with_last_message("FINAL RUN TEAL / FINAL RUN GOLD")
    phase1_a = [user_row, group_a, emit_fallback_closer(group_a)]
    assert_settled(phase1_a, "phase1 (closer writing, has working msg)")

    # Settle/reconcile reorder: the closer must not jump above the group.
    phase2_a = _run_owner_reorder([user_row, group_a, emit_fallback_closer(group_a)])
    assert_settled(phase2_a, "phase2 (settled reorder, has working msg)")

    # Case B: empty completion with NO working message → deterministic failure
    # closer ("No response"), still preserving the user turn + keyed group.
    group_b = group_with_last_message("")
    phase1_b = [user_row, group_b, emit_fallback_closer(group_b)]
    assert_settled(phase1_b, "phase1 (closer writing, no working msg)")
    phase2_b = _run_owner_reorder([user_row, group_b, emit_fallback_closer(group_b)])
    assert_settled(phase2_b, "phase2 (settled reorder, no working msg)")

    # The no-working-message fallback must carry the deterministic label.
    assert phase1_b[-1]["text"] == "No response"
    assert phase2_b[-1]["text"] == "No response"


def test_empty_completion_no_response_closer_respects_chat_ui_debug_flag():
    """Empty-completion diagnostics are visible only with chat_ui debug enabled."""
    stream = _source("ui/chat/js/chat-stream.js")
    live = _function(stream, "finalizeAgentResponse", "finalizeAgentStep")
    assert "if (isDebugMode())" in live
    assert "_renderTerminalCloser(" in live
    assert "'No response'" in live
    assert live.index("if (isDebugMode())") < live.index("_renderTerminalCloser(")

    session = _source("ui/chat/js/session-load.js")
    empty_start = session.index("// Empty/no-output completion:")
    empty_end = session.index("\n        }\n      }\n      continue;", empty_start)
    reload = session[empty_start:empty_end]
    assert "if (isDebugMode())" in reload
    assert "content: 'No response'" in reload
    assert reload.index("if (isDebugMode())") < reload.index("content: 'No response'")


def test_timed_out_cancellation_keeps_one_closer_and_clears_stop():
    """Deterministic regression for a TIMED-OUT/cancelled run.

    A run cancelled because it timed out is surfaced by the backend as
    status='interrupted' (watchdog stop_cause zombie/frozen/crash), so the
    frontend reaches the SAME canonical terminal boundary as a user stop:
    markAgentInterrupted -> _renderTerminalCloser -> endRunControls. This pins
    the acceptance criterion that a timed-out cancellation also produces EXACTLY
    ONE terminal closer and ALWAYS clears Stop to Continue. The settled transcript
    must keep exactly one stable-key activity group, exactly one terminal closer
    immediately after it (same exact run key), and no live Stop control.

    Models both the live path (markAgentInterrupted building the closer) and the
    reconcile/settle reorder (idempotent; the closer never jumps above its group)
    .
    """
    run_key = "userTurnTimeout::7"

    def group_with_partial(partial_text):
        return {"kind": "group", "key": run_key, "preview": partial_text}

    def terminal_closer(label, content):
        return {"kind": "closer", "key": run_key, "text": content, "label": label}

    def last_complete_message(group):
        # Mirrors _lastCompleteMessageInRun: newest non-empty, non-placeholder row.
        import re
        placeholder = re.compile(r"^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$")
        text = (group.get("preview") or "").strip()
        return text if text and not placeholder.match(text) else ""

    def emit_terminal_closer(group, label):
        # Mirrors _renderTerminalCloser fallback: last complete working message
        # in the run's own group, else the deterministic terminal label. Never blank.
        msg = last_complete_message(group)
        return terminal_closer(label, msg if msg else label)

    def assert_settled(order, phase_label):
        kinds = [n["kind"] for n in order]
        # The submitted user turn is preserved (never removed).
        assert kinds.count("user") == 1, f"{phase_label}: user turn must be preserved"
        # Exactly one activity group with the stable per-run key.
        assert kinds.count("group") == 1, f"{phase_label}: expected one activity group"
        g = next(n for n in order if n["kind"] == "group")
        assert g["key"] == run_key, f"{phase_label}: group must carry the stable run key"
        # Exactly one terminal closer, immediately after ITS OWN group.
        assert kinds.count("closer") == 1, f"{phase_label}: expected exactly one terminal closer"
        ci = next(i for i, n in enumerate(order) if n["kind"] == "closer")
        assert order[ci - 1]["kind"] == "group", f"{phase_label}: closer must be after its group"
        assert order[ci - 1]["key"] == run_key, f"{phase_label}: closer must follow ITS OWN group"
        # Terminal run: Stop must be cleared (never a live Stop control alongside it).
        assert order[ci].get("stop") is not True, f"{phase_label}: timed-out run must clear Stop"
        # The closer is never a blank bubble.
        assert order[ci]["text"].strip(), f"{phase_label}: closer must have content"

    user_row = {"kind": "user", "key": "userTurnTimeout"}

    # Case A: timeout with a partial/in-flight working draft -> fallback uses it.
    group_a = group_with_partial("FROZEN RUN AMBER / QUICK RIVER")
    phase1_a = [user_row, group_a, emit_terminal_closer(group_a, "Stopped")]
    assert_settled(phase1_a, "phase1 (timeout, has working msg)")
    # Settle/reconcile reorder must be idempotent and keep closer after its group.
    phase2_a = _run_owner_reorder([user_row, group_a, emit_terminal_closer(group_a, "Stopped")])
    assert_settled(phase2_a, "phase2 (settled reorder, has working msg)")

    # Case B: timeout with NO working draft -> deterministic label closer, never blank.
    group_b = group_with_partial("")
    phase1_b = [user_row, group_b, emit_terminal_closer(group_b, "Stopped")]
    assert_settled(phase1_b, "phase1 (timeout, no working msg)")
    phase2_b = _run_owner_reorder([user_row, group_b, emit_terminal_closer(group_b, "Stopped")])
    assert_settled(phase2_b, "phase2 (settled reorder, no working msg)")
    # With no real message the closer carries the deterministic terminal label.
    assert phase1_b[-1]["text"] == "Stopped"
    assert phase2_b[-1]["text"] == "Stopped"

    # Pin the canonical terminal funnel a timed-out cancellation reaches so a
    # regression that stops clearing Stop / stops rendering the closer fails loudly.
    stream = _source("ui/chat/js/chat-stream.js")
    interrupted = _function(stream, "markAgentInterrupted", "_renderTerminalCloser")
    assert "app._stopPending = false" in interrupted
    assert "_renderTerminalCloser(" in interrupted
    assert "app.endRunControls(ownerTurnId)" in interrupted
    closer = _function(stream, "_renderTerminalCloser", "notifyExecutionMode")
    assert "group.insertAdjacentElement('afterend', bubble)" in closer
    assert "'terminal-closer'" in closer
    assert "_lastCompleteMessageInRun(group)" in closer


def test_terminal_closer_path_defines_every_called_helper():
    """Regression for the reported runtime failure (no closer, Stop persists).

    The interrupt/error/timeout terminal path (markAgentInterrupted ->
    _renderTerminalCloser) calls helpers that were REFERENCED but never DEFINED
    (e.g. _removeLivePreview). At runtime that undefined call throws, so the
    closer never renders and Stop stays engaged. This pins that every helper the
    terminal-closer path depends on is actually declared in chat-stream.js, not
    merely named — a source-assertion suite that only checks for the presence of
    strings cannot catch this class of breakage.
    """
    import re as _re
    stream = _source("ui/chat/js/chat-stream.js")

    def _defined(name):
        # Function declaration, const/let/var binding, or a named import.
        if _re.search(r"(?m)^\s*(?:export\s+)?function\s+" + _re.escape(name) + r"\s*\(", stream):
            return True
        if _re.search(r"(?m)^\s*(?:const|let|var)\s+" + _re.escape(name) + r"\s*=", stream):
            return True
        imports = "\n".join(_re.findall(r"import\s*\{([^}]*)\}\s*from", stream))
        return _re.search(r"\b" + _re.escape(name) + r"\b", imports) is not None

    for helper in [
        "markAgentInterrupted",
        "_renderTerminalCloser",
        "_lastCompleteMessageInRun",
        "_removeLivePreview",
        "_dropPendingStreamRender",
        "_coalesceActivityGroups",
        "_coalesceLiveActivityGroups",
        "attachActivityEntries",
        "positionActivityGroupAfterOwner",
    ]:
        assert _defined(helper), (
            f"{helper} is referenced from the terminal-closer path but never "
            f"defined in chat-stream.js — the interrupt/error/timeout closer "
            f"silently breaks (no closer rendered, Stop stays engaged)."
        )


def test_reload_progress_rows_are_durable_group_metadata_only():
    """A restore never renders persisted progress prose as an activity child row."""
    load = _source("ui/chat/js/session-load.js")
    progress_start = load.index("// Progress is activity, never a standalone answer.")
    start = load.index("const group = ensureActivity(msg);", progress_start)
    end = load.index("if (phase === 'system')", start)
    body = load[start:end]
    assert "kind: 'progress'" not in body
    assert "show_mid_turn_messages" not in load
    assert "appendCalls(group);" in body



def test_reload_progress_keeps_its_tool_calls_in_the_activity_group():
    """Text-bearing progress rows retain persisted tool payloads in activity."""
    load = _source("ui/chat/js/session-load.js")
    progress_start = load.index("// Progress is activity, never a standalone answer.")
    calls = load.index("const calls = _buildCallsForMessage", progress_start)
    normal_group = load.index("const group = ensureActivity(msg);", calls)
    assert progress_start < calls < normal_group
    branch = load[normal_group:load.index("if (phase === 'system')", normal_group)]
    assert "appendCalls(group);" in branch


def test_reload_terminal_status_emits_nonempty_closer():
    load = _source("ui/chat/js/session-load.js")
    start = load.index("if (phase === 'system')")
    end = load.index("continue;", start)
    body = load[start:end]
    assert "_agentCloser: true" in body
    assert "_emptyCloser: true" in body
    assert "_closerKey: group.runKey" in body
    assert "content: msg.status === 'error'" in body
    assert "renderables.push(fallback);" in body
    emit = _function(load, "_emitRealBubble", "_emitToolGroupBubble")
    assert "app.addChatBubble('agent', text || ' '," in emit


def test_run_error_closer_requires_explicit_chat_debug_flag():
    prompts = _source("ui/shared/js/app-prompts.js")
    stream = _source("ui/chat/js/chat-stream.js")
    load = _source("ui/chat/js/session-load.js")
    config = _source("data/config/chat_ui.json")

    assert "debug?.enabled === true" in prompts
    assert '"enabled": false' in config
    assert "if (isDebugMode()) {" in stream
    assert "if (isDebugMode()) renderables.push(fallback);" in load


def test_activity_counter_resynchronizes_when_panel_rows_change():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function _buildToolCallsContainer(")
    end = stream.index("function _addToolCallsToBubble(", start)
    body = stream[start:end]
    assert "new MutationObserver" in body
    assert "childList: true" in body
    assert "_updateToolCallsHead(head, panel)" in body


def test_activity_counter_uses_exact_semantic_row_totals():
    stream = _source("ui/chat/js/chat-stream.js")
    body = _function(stream, "_updateToolCallsHead", "_syncToolCallPreviews")
    assert "const toolRows" in body
    assert "bubble-tool-calls-jump-nav" in body
    assert "const messageRows" in body
    assert "const modeRows" in body
    assert "const compactionRows" in body
    assert "toolRows.length" in body
    assert "messageRows.length" in body
    assert "const messages = deferred.filter" in body
    assert "summaryMessageCount" not in body

    load = _source("ui/chat/js/session-load.js")
    assert "summaryMessageCount" not in load
    assert "Turn " not in body


def test_windowed_render_virtualizes_activity_groups_with_messages():
    """A long transcript must not leave old collapsed groups live above virtual users."""
    load = _source("ui/chat/js/session-load.js")
    start = load.index("function _renderSessionWindowed(")
    end = load.index("// ── Terminal Chat auto-activation", start)
    body = load[start:end]
    assert "_virtualActivityGroups.set(msg.id, msg)" in body
    assert "if (i >= realStart && i <= realEnd)" in body
    assert "_makePlaceholder(msg.id, _getBubbleHeight(msg.id))" in body
    assert "app._rehydrateVirtualActivityGroup" in load

    virtual = _source("ui/chat/js/chat-virtual-scroll.js")
    recycle = _function(virtual, "_recyclePlaceholderToBubble", "_recycleBubbleToPlaceholder")
    assert "app._rehydrateVirtualActivityGroup(msgId, placeholder)" in recycle


def test_complete_turn_boundaries_are_owned_by_user_rows_not_system_notices():
    """A system/debug row cannot safely start a user-run activity projection."""
    backend = _source("app/api/db_viewer.py")
    start = backend.index("# A newest-N slice can start halfway")
    boundary = backend[start:backend.index("# Map id -> role", start)]
    assert "AND role = 'user'" in boundary
    assert "role = 'system'" not in boundary

    storage = _source("ui/chat/js/storage/storage-adapter.js")
    start = storage.index("function _isHardRenderBoundary(message)")
    end = storage.index("// Heal legacy cached rows", start)
    body = storage[start:end]
    assert "return message.role === 'user';" in body

def test_user_messages_snap_to_their_anchor_after_a_manual_scroll_settles():
    source = _source("ui/chat/js/chat-ui.js")
    assert "const _USER_ANCHOR_SNAP_PX = 100;" in source
    assert "function _snapToNearbyUserAnchor(el, direction = 0)" in source
    assert "const distance = Math.abs(target - current);" in source
    assert "distance <= _USER_ANCHOR_SNAP_PX" in source
    assert "el.scrollTo({ top: target, behavior: 'smooth' });" in source
    assert "_queueUserAnchorSnap(messagesEl, _lastTranscriptScrollDirection);" in source


def test_header_turn_navigation_uses_canonical_targets_and_durable_fallbacks():
    source = _source("ui/chat/js/chat-ui.js")
    next_target = _function(source, "_nextUserTarget", "_scrollToBottomIfNear")
    assert "const target = Math.max(0, offset - _USER_STEP_PAD);" in next_target
    assert "if (target > cur + 2)" in next_target

    header_nav = source[source.index("const _configureAnchorNav = () => {"):]
    assert "? _prevUserTarget(messagesEl)" in header_nav
    assert ": _nextUserTarget(messagesEl);" in header_nav
    assert "app._stepToPrevUserMessage : app._stepToNextUserMessage" in header_nav
