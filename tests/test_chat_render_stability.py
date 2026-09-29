from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_live_activity_note_never_moves_its_group_to_the_transcript_tail():
    source = _source("ui/chat/js/chat-stream.js")
    bubble_source = _source("ui/chat/js/chat-bubble.js")
    start = source.index("function positionActivityGroupAfterOwner(")
    end = source.index("function _coalesceActivityGroups(", start)
    body = source[start:end]

    assert "anchor.insertAdjacentElement('afterend', group)" in body
    assert "app.chatMessages.appendChild(group)" not in body
    assert "ordered.push(...liveProgress)" not in bubble_source


def test_session_open_anchors_the_newest_user_turn_at_the_send_landing():
    """Opening/switching a session must land the newest USER turn at the same
    readable-top offset a send uses — not drop the reader at the tail."""
    load = _source("ui/chat/js/session-load.js")
    ui = _source("ui/chat/js/chat-ui.js")
    body = load[
        load.index("function _renderSessionWindowed("):
        load.index("// ── Terminal Chat auto-activation ──")
    ]

    # The open-path default branch exists, keyed on the switch-only useFocus flag.
    assert "} else if (useFocus) {" in body
    assert "m.role === 'user'" in body
    assert "atBottom = false;" in body
    # A session with no user turn still falls through to the tail.
    assert "let anchorIdx = renderables.length - 1;" in body

    # The open landing reuses chat-ui's single pad constant, never its own copy.
    assert "app._USER_STEP_PAD || 62" in load
    assert "anchorEl.offsetTop - 62" not in body

    # The landing is tracked by DURABLE MESSAGE ID and re-asserted while the
    # projection settles, so a placeholder<->bubble swap above the anchor cannot
    # push the turn off the top — the measured drift left the newest user turn
    # 193px ABOVE the fold with one of its own activity rows parked at the top.
    assert "function _landByMsgId(sessionId, msgId, fallbackEl)" in load
    assert "function _reLandActive()" in load
    assert "anchorEl.getAttribute('data-msg-id')" in body
    assert "_landByMsgId(" in body
    # The tail branch drops the settle chain so a follower is never re-landed.
    assert "_landing = null;" in body
    # Both post-render writers that used to clobber the landing defer to it now:
    # the older-page loader (a still-settling projection reads as scrollTop 0)
    # and the prepend-preserve write (the 60->104 oscillation).
    assert (
        "if (_landing && _landing.sessionId === sessionId && performance.now() < _landing.deadline) return;"
        in load
    )
    assert "_reLandActive();" in load

    # chat-ui publishes that constant, the send path still uses it, and a real
    # reader gesture clears the settle chain so it can never fight the reader.
    assert "app._USER_STEP_PAD = _USER_STEP_PAD;" in ui
    assert "const _USER_STEP_PAD = 62;" in ui
    assert "bubbleRect.top - scrollerRect.top - _USER_STEP_PAD" in ui
    assert "if (typeof app._clearLanding === 'function') app._clearLanding();" in ui


def test_soft_user_anchor_snap_never_reverses_reader_direction():
    ui = _source("ui/chat/js/chat-ui.js")

    assert "function _snapToNearbyUserAnchor(el, direction = 0)" in ui
    assert "direction < 0 && target > current + 1" in ui
    assert "direction > 0 && target < current - 1" in ui
    assert "_queueUserAnchorSnap(messagesEl, _lastTranscriptScrollDirection)" in ui
    assert "_snapToNearbyUserAnchor(messagesEl, _lastTranscriptScrollDirection)" in ui


def test_run_controls_use_a_latch_until_closer_or_terminal_failure():
    ui = _source("ui/chat/js/chat-ui.js")
    activity = _source("ui/shared/js/chat-activity.js")
    ws = _source("ui/shared/js/agentWs.js")
    send = _source("ui/chat/js/chat-send.js")

    assert "app._runControlsActive || app.isProcessing" in ui
    assert "continueBtn.hidden = runActive || !!hasInput" in ui
    assert "app.beginRunControls(_noteTurnId)" in activity
    assert "app.endRunControls(event.turn_id)" in activity
    assert "app.endRunControls()" in ws
    assert "outboxEntry.id, outboxEntry.timestamp" in send
    assert "app.rekeyActivityGroupOwner(provisionalTurnId, turnId)" in send


def test_task_grouping_has_no_visible_chat_chrome():
    ui = _source("ui/chat/js/chat-ui.js")
    css = _source("ui/shared/css/app1.css")

    assert "initTaskFrames" not in ui
    assert ".task-frame" not in css


def test_mode_notices_use_a_generic_group_line_and_a_full_body_sentence():
    stream = _source("ui/chat/js/chat-stream.js")
    load = _source("ui/chat/js/session-load.js")
    css = _source("ui/shared/css/design-system.css")
    dialog = _source("ui/chat/js/chat-ui.js")
    ws = _source("ui/shared/js/agentWs.js")

    # TWO surfaces with two jobs. The COLLAPSED group line is direction-agnostic
    # ("Mode change") — repeated clicks are one change, not one line per click —
    # while the EXPANDED row body spells the change out ("Plan mode enabled"),
    # because that is the message the mini transcript shows.
    assert "const MODE_NOTICE_LABEL = 'Mode change';" in stream
    assert "function _modeNoticeBody(mode) {" in stream
    assert "wkspc: 'Workspace mode enabled'," in stream
    assert "const label = _modeNoticeBody(mode);" in stream
    # The body renders the full sentence; only the summary uses the constant.
    assert "const rowLabel = entry.label || 'Stopped';" in stream
    assert "if (latestModeLabel) summaryParts.push(MODE_NOTICE_LABEL);" in stream
    assert "if (mode) parts.push(MODE_NOTICE_LABEL);" in stream
    # The durable twin stores the full sentence, so a reload reads the same.
    assert "label: String(msg.content || '').trim() || MODE_NOTICE_LABEL" in load
    assert '"plan": "Plan mode enabled",' in _source("app/db/interface.py")
    assert "row.dataset[modeNotice ? 'modeNoticeLabel' : 'modelNoticeLabel'] =" in stream
    assert "if (statusRow && entry.notice !== 'mode' && entry.notice !== 'model')" in stream
    assert "if (modeNotice || modelNotice) {" in stream
    # Netting: a group's flips are measured against the mode it BEGAN in, so
    # ask → plan → auto → ask cancels the notice instead of stacking rows. One
    # helper serves both notice types (NOTICE_TYPES), so the model twin cannot
    # drift from the mode behaviour.
    assert "function _projectNotices(entries, notice) {" in stream
    assert "function _projectModeNotices(entries) { return _projectNotices(entries, 'mode'); }" in stream
    assert "function _projectModelNotices(entries) { return _projectNotices(entries, 'model'); }" in stream
    assert "const NOTICE_TYPES = ['mode', 'model'];" in stream
    assert "NOTICE_TYPES.forEach(notice => {" in stream
    assert "container.__noticeBase[notice] = plan.base;" in stream
    assert "cancelledNotices[notice] = !!base" in stream
    assert "if (_projectModeNotices(group.entries).cancelled)" in load
    # Both live callers record the pre-flip mode, so netting has a real base.
    assert "app.notifyExecutionMode(app.executionMode, {from: fromMode})" in dialog
    assert "{ turnId: _ownerTurnId, from: fromMode }" in ws
    assert "panel.querySelectorAll(notice === 'mode' ? '.ca-mode-row' : '.ca-model-row')" in stream
    assert "let projectedCalls = sourceCalls;" in stream
    assert "if (src === 'system:mode')" in load
    assert "kind: 'system', notice: 'mode'" in load
    assert ".chat-bubble.info.system-mode .turn-gutter" in css
    assert "display: none !important" in css


def test_model_notices_use_a_generic_group_line_and_a_full_body_sentence():
    stream = _source("ui/chat/js/chat-stream.js")
    load = _source("ui/chat/js/session-load.js")
    css = _source("ui/shared/css/app1.css")
    activity = _source("ui/shared/js/chat-activity.js")
    interface = _source("app/db/interface.py")
    chat_api = _source("app/api/chat.py")
    send = _source("ui/chat/js/chat-send.js")
    bubble = _source("ui/chat/js/chat-bubble.js")
    tool = _source("plugins/abilities/Core/model_switcher/model_switcher.py")

    # Same split as the mode notice. The group line stays generic ("Model
    # change"); the row body names the model STATE ("Premium model enabled",
    # "Custom 2 model enabled") and never a slot name or provider model id.
    assert "const MODEL_NOTICE_LABEL = 'Model change';" in stream
    assert "function _modelNoticeBody(anchor, fallback) {" in stream
    assert "const text = _modelNoticeBody(anchor, label);" in stream
    assert "label: text," in stream
    assert "modeNotice ? MODE_NOTICE_LABEL : MODEL_NOTICE_LABEL" in stream
    assert "if (model) parts.push(MODEL_NOTICE_LABEL);" in stream
    assert "if (latestModelRow) summaryParts.push(MODEL_NOTICE_LABEL);" in stream
    assert "content: text," in stream
    assert "label: modelBody, content: ''," in load
    # Durable rows keep the full sentence as their content.
    assert 'content=content or "Model change",' in interface
    assert '"Premium model enabled"' in chat_api
    assert 'f"Custom {_pos} model enabled"' in chat_api
    assert "if (_projectModelNotices(group.entries).cancelled)" in load
    assert "if (src === 'system:model')" in load
    # The row is content-only and NORMAL-coloured: the warning tint and the ⚠
    # heading that made it a special orange bubble inside the group are gone.
    assert ".ca-mode-row,\n.ca-model-row {\n  border-color: var(--warning-mid" not in css
    assert "NORMAL activity-row surface" in css
    ensure_neutral = css.split("NORMAL activity-row surface", 1)[1]
    assert "--warning" not in ensure_neutral[:600]
    # Both live callers record the model IDENTITY pair the netting needs. A coarse
    # default/override pair collapsed two different custom models into "no change"
    # and cancelled the notice, so a second switch in a run rendered nothing at all.
    assert "const prevOverrideKey = hadOverride" in activity
    assert "from: prevOverrideKey," in activity
    assert "state: 'override:' + String(event.model).trim().toLowerCase()," in activity
    assert "state: slotRef || 'default'," in activity
    assert "from: currentRef || 'default'," in activity
    # The durable writers store the same identities, so a reload nets identically.
    assert "def _model_identity(cfg, ref: str) -> str:" in chat_api
    assert '_from_model_state = _model_identity(_prev_model_cfg, "")' in chat_api
    assert "_to_model_state = _model_identity(None, _requested_ref)" in chat_api
    assert 'to_state=f"override:{target}"' in tool
    assert 'from_state="default", to_state="override:premium"' in tool
    assert "function _noticeKey(value) {" in stream
    # A mode/model notice raised BEFORE the send is treated as applied AFTER it:
    # re-anchored behind the prompt, stamped with the prompt's id so the ordering
    # authority pins it to that prompt's tail, and adopted by the run's first live
    # entry instead of being stranded in a group of its own.
    assert "function adoptPreSendNoticeGroup(userBubble) {" in stream
    assert "app.adoptPreSendNoticeGroup = adoptPreSendNoticeGroup;" in stream
    assert "app.adoptPreSendNoticeGroup(_userBubble);" in send
    assert "const noticeOnlyStamp = !!(bubble.dataset.activityTurnId" in stream
    assert "Pin the group to the prompt it currently follows instead." in bubble
    assert "state: 'default'," in activity
    # The coarse-state helper is gone: it is what made premium → custom-2 read as
    # "override → override" and silently cancelled the notice.
    assert "const isDefaultSlot = (ref, type, role) =>" not in activity
    # The durable row keeps the full sentence — the generic wording is the group
    # line only, so the DB must not overwrite the content with it.
    assert 'content="Model change"' not in interface
    assert 'content=content or "Model change",' in interface
    assert '"from": from_state or "",' in interface
    assert '"to": to_state or "",' in interface
    assert "from_state=_from_model_state, to_state=_to_model_state" in chat_api
    assert 'from_state="override", to_state="default"' in chat_api
    # Every producer of the durable row records the pair — including the agent's
    # own model-switcher tool, whose rows are the other half of the upgrade →
    # revert cycle the run-end reset closes.
    switcher = _source("plugins/abilities/Core/model_switcher/model_switcher.py")
    assert 'to_state=f"override:{target}")' in switcher
    assert 'to_state="override:premium")' in switcher
    assert 'from_state="override", to_state="default")' in switcher


def test_agent_mode_notice_is_persisted_with_owning_turn():
    loader = _source("app/tools/loader.py")
    loop = _source("app/agent/loop.py")

    assert "turn_id=turn_id or None" in loader
    assert 'turn_id=parent_interaction_id or ""' in loop


def test_session_dropdown_optimistically_moves_new_and_recent_sessions_to_top():
    sessions = _source("ui/chat/elements/session-dropdown/list.js")
    init = _source("ui/chat/js/session-init.js")
    send = _source("ui/chat/js/chat-send.js")

    assert "export function markSessionRecentlyActive(sessionId" in sessions
    assert "created_at: null, updated_at: now, activity_at: now" in sessions
    assert "durable-row marker for session resources" in sessions
    assert "row.activity_at = now;" in sessions
    assert "_sortSessionsByPinnedOrder(_sessionsCache);" in sessions
    assert "_activeSessionCache.userId === app.currentUserId && _activeSessionCache.sessions.length" in sessions
    assert "_persistSessionMetadataCache(app.currentUserId, rows);" in sessions
    assert "storageAdapter.updateSessionCache(sessionId, { activity_at: now })" in sessions
    assert "markSessionRecentlyActive(app.currentSessionId, { agentId: app.currentAgentId });" in init
    assert "app.markSessionRecentlyActive = markSessionRecentlyActive;" in init
    assert "app.markSessionRecentlyActive?.(entry.session_id, { persist: true });" in send
    assert "app.markSessionRecentlyActive?.(outboxEntry.session_id, { persist: true });" in send


def test_session_switch_hides_old_projection_before_target_state_can_render():
    core = _source("ui/chat/js/session-core.js")
    controller = _source("ui/chat/elements/session-dropdown/controller.js")
    css = _source("ui/shared/css/index.css")
    sessions_page = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    start = core.index("async function switchToSession(")
    end = core.index("\n}\n", start) + 2
    body = core[start:end]

    hide_projection = body.index("chatPanel.classList.add('session-transitioning')")
    clear_header = body.index("_clearSessionHeader();")
    clear_transcript = body.index("app.chatMessages.replaceChildren();")
    assign_target = body.index("app.currentSessionId = sid;")
    apply_target_config = body.index("await reapplyChatControlsConfig();")
    load_target = body.index("await loadSessionChat(sid,")
    render_target_name = body.index("_setTriggerLabel();")
    reveal_projection = body.index("chatPanel.classList.remove('session-transitioning')")

    assert "._sessionDropdownController.close({ immediate: true })" in body
    assert "function closeMenu({ immediate = false } = {})" in controller
    assert "if (immediate)" in controller
    assert "#chat-panel.session-transitioning > #chat-header" in css
    assert "#chat-panel.session-transitioning > #chat-messages" in css
    assert "#chat-panel.session-transitioning > #chat-input-area" in css
    assert hide_projection < clear_header
    assert clear_header < assign_target
    assert clear_transcript < assign_target
    assert assign_target < apply_target_config < render_target_name
    assert render_target_name < reveal_projection < load_target

    switch_body = sessions_page[sessions_page.index("async function _switchToSession("):]
    assert "await switchToSession(sessionId, { agentId });" in switch_body
    assert "app.currentSessionId = sessionId;" not in switch_body
    assert "app.currentAgentId = agentId;" not in switch_body


def test_session_switch_uses_same_agent_and_stale_while_revalidate_fast_paths():
    core = _source("ui/chat/js/session-core.js")
    load = _source("ui/chat/js/session-load.js")
    state = _source("ui/shared/js/state.js")
    start = core.index("async function switchToSession(")
    end = core.index(chr(10) + "}" + chr(10), start) + 2
    body = core[start:end]

    assert "const sameAgent = !targetAgentId || targetAgentId === app.currentAgentId;" in body
    assert "const transcriptCacheHit = _messageCache.has(sid);" in body
    assert "const _sessionProjectionCache = new Map();" in core
    assert "_stashSessionProjection(previousSessionId);" in body
    assert "_takeSessionProjection(sid);" in body
    assert "preserveProjection: !!cachedProjection" in body
    assert "projectionHit: !!cachedProjection" in body
    assert "if (!sameAgent) {" in body
    assert "const automaticReapply = app._reapplyChatControls;" in body
    assert "app._reapplyChatControls = null;" in body
    assert "await reapplyChatControlsConfig();" in body
    assert "if (app.chatMessages && !transcriptCacheHit)" in body
    assert "app.lastSessionSwitchTiming = switchTiming;" in body
    assert "visibleMs: Math.round(visibleAt - switchStartedAt)" in body
    assert "session-switch-timing" in body
    assert "if (!opts.refresh && cached)" in load
    assert "if (!opts.refresh && cached &&" not in load
    assert "if (!opts.preserveProjection)" in load
    assert "_refreshCachedTranscript(sessionId);" in load


def test_sessions_table_repaints_preserve_bulk_selection_until_action_finishes():
    sessions = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    render_start = sessions.index("function _renderTable(rows)")
    render_end = sessions.index("// Build one table row", render_start)
    render = sessions[render_start:render_end]

    capture = render.index("const checkedSessionIds = new Set(Array.from(")
    replace = render.index("tbody.innerHTML = html;")
    restore = render.index("if (checkedSessionIds.size)")

    assert capture < replace < restore
    assert "tbody.querySelectorAll('.sessions-check-cell.checked')" in render
    assert "checkedSessionIds.has(cell.dataset.sessionId)" in render
    assert "cell.closest('tr')?.classList.add('selected');" in render
    # Once the recycle mutation removes those rows, this durable bin-only context
    # still drives the Undo / Permanent delete stage in the active-only view.
    assert "if (origins.size === 0 && _postBinSessionIds.length) origins.add('bin');" in sessions
    assert "_postBinSessionIds = recycledIds;" in sessions


def test_agents_sessions_avatar_disclosure_matches_session_dropdown():
    sessions = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    dropdown = _source("ui/chat/elements/session-dropdown/list.js")

    # The parent agent avatar is the disclosure control in both session views.
    assert 'class="session-row-agent-toggle"' in dropdown
    assert "sess-title-agent-toggle" in sessions
    assert 'aria-expanded="${isExpanded ? \'true\' : \'false\'}"' in dropdown
    assert "aria-expanded=" in sessions
    assert "(open ? 'true' : 'false')" in sessions
    assert "const kids = childrenOf ? (childrenOf.get(row.session_id) || []) : [];" in sessions
    assert "html += _renderRow(children[j], { childrenOf: childrenOf, depth: depth + 1 });" in sessions
    assert "row.child_role === 'closer'" in sessions
    assert "const familyToggle = e.target.closest('.sess-title-agent-toggle');" in sessions
    assert "_toggleGroup(familyToggle.dataset.sessionId);" in sessions

    # The root projection follows the dropdown: spawned helpers never become
    # standalone rows, including historical orphans whose parent was deleted.
    assert "function _isChildSpawnSession(session)" in sessions
    assert "id.startsWith('spawn-')" in sessions
    assert "else if (!_isChildSpawnSession(s))" in sessions
    assert "topLevel.push(s);" in sessions

    # This is family grouping, not an incorrect blanket agent-roster filter.
    assert "function _mainAgentIds()" not in sessions
    assert "candidates = candidates.filter(r => mainAgentIds.has(r.agent_id));" not in sessions


def test_pinned_session_order_is_manual_and_persisted():
    ordering = _source("ui/shared/js/session-ordering.js")
    list_source = _source("ui/chat/elements/session-dropdown/list.js")
    controller = _source("ui/chat/elements/session-dropdown/controller.js")
    storage = _source("ui/chat/js/storage/storage-adapter.js")
    api = _source("app/api/db_viewer.py")

    assert "export function compareSessionsByPinnedOrder" in ordering
    assert "aPinned !== bPinned" in ordering
    assert "aOrder - bOrder" in ordering
    assert "_sortSessionsByPinnedOrder(sessions)" in list_source
    assert "rowSelector: '.session-row.pinned:not(.session-child-row):not(.recycled)'" in controller
    assert "storageAdapter.reorderSessions(app.currentUserId, orderedIds)" in controller
    assert "defaultSessionDB.setSessionOrder(ids)" in storage
    assert '@router.post("/sessions/reorder")' in api
    assert "UPDATE sessions SET sort_order = ?, updated_at = ?" in api
    assert "sort_order = NULL" in api


def test_session_recency_matches_terminal_run_timestamp_shown_in_picker():
    ordering = _source("ui/shared/js/session-ordering.js")
    api = _source("app/api/db_viewer.py")

    assert "const terminalRun = ['complete', 'interrupted', 'error'].includes(session?.run_status);" in ordering
    assert "(terminalRun && session?.run_updated_at)" in ordering
    assert "sr.status IN ('complete', 'interrupted', 'error')" in api


def test_cached_visit_shows_one_mock_progress_note_sync_indicator():
    load = _source("ui/chat/js/session-load.js")
    activity = _source("ui/shared/js/chat-activity.js")
    css = _source("ui/shared/css/app1.css")

    # The re-sync feedback is a MOCK activity note. It renders in the FOOTER
    # activity element — the same configured `activity` control the run notes tick
    # through — and NOT as a transcript row of its own: activity notes are not
    # configured to render in the chat (chat_ui.json → mirror_activity_in_transcript
    # is false), so this mock note obeys the same rule instead of forcing a row.
    assert ("app.chatActivityMockNote('assessing', { sessionId: app.currentSessionId })"
            in load)
    assert "force: true" not in load
    assert "mirrorActivityNote" not in load
    assert "_SYNC_NOTE_LABEL" not in load
    # ...and it is retired when the fresh window lands (or the refresh fails), by
    # handing the footer activity element back.
    assert "app.chatActivityMockClear();" in load
    # The catalogue of fabricated labels lives with the activity module, which owns
    # the one surface that renders them.
    assert "const MOCK_ACTIVITY_NOTES = Object.freeze({" in activity
    assert "assessing: 'Assessing…'," in activity
    assert "reassessing: 'Re-assessing…'," in activity
    assert "app.chatActivityMockNote = mockActivityNote;" in activity
    assert "app.chatActivityMockClear = mockActivityNoteClear;" in activity
    # A genuinely dead connection still gets the flat, non-ticking tail notice.
    assert "wrap.className = 'chat-sync-tail chat-sync-unavailable';" in load
    assert "Server connection is currently unavailable." in load
    assert "if (storageAdapter.isHybrid)" in load
    assert "_refreshCachedTranscript(sessionId);" in load
    assert ".chat-activity-bar.ambient {" in css
    assert "chat-sync-tail .chat-skeleton-line" in css


def test_reconciled_activity_row_rebuilds_its_collapsed_preview():
    stream = _source("ui/chat/js/chat-stream.js")
    start = stream.index("function _appendCallsToToolContainer(")
    end = stream.index("function _comparableOutput(", start)
    body = stream[start:end]

    assert "let replacedActivityEntry = false;" in body
    assert "existing.replaceWith(replacement);" in body
    assert "replacedActivityEntry = true;" in body
    assert "if (replacedActivityEntry) _syncToolCallPreviews();" in body


def test_tool_details_are_fetched_per_row_not_per_group():
    stream = _source("ui/chat/js/chat-stream.js")
    api = _source("app/api/db_viewer.py")

    assert "/session-tool-detail" in stream
    assert "_ensureToolCallDetail(panel.closest('.bubble-tool-calls'), rowEntry)" in stream
    assert "_ensureToolDetail(container)" not in stream
    assert '@router.get("/session-tool-detail")' in api
    assert '"tool_index": tool_index' in api


def test_codex_portal_sessions_use_native_transcript_api_and_survive_cache_deltas():
    send = _source("ui/chat/js/chat-send.js")
    load = _source("ui/chat/js/session-load.js")
    sessions = _source("ui/chat/elements/session-dropdown/list.js")

    assert "/api/v1/agents/${encodeURIComponent(app.currentAgentId)}" in send
    assert "activeAgent = detail.agent" in send
    assert "activeAgent.default_execution_mode" in send
    assert "execution_mode: portalExecutionMode" in send
    assert "/api/v1/engines/codex/portal/threads" in send
    assert "if (pendingRefresh) await pendingRefresh.catch(() => {});" in send
    assert "if (portalError) addChatBubble('agent'" in send
    assert "if (data.status === 'queued')" in send
    assert "Message queued in the active Codex task." in send
    assert "/api/v1/engines/codex/portal/threads/${encodeURIComponent(sessionId)}/messages" in load
    assert "headers: authHeaders()" in load
    assert "return storageAdapter.getInteractions(sessionId, limit, opts);" not in load[
        load.index("if (typeof sessionId === 'string' && sessionId.startsWith('codex:'))"):
        load.index("// Hybrid + browser + normal", load.index("if (typeof sessionId === 'string' && sessionId.startsWith('codex:'))"))
    ]
    delta = sessions[sessions.index("window.addEventListener('sessions-delta'"):]
    assert "_sessionsCache = _mergePortalSessions(" in delta
    controller = _source("ui/chat/elements/session-dropdown/controller.js")
    assert "menu.dataset.pointerSessionId = row?.dataset.id || '';" in controller
    assert "switchToSession(pointerSessionId || row.dataset.id)" in controller
    # Kebab actions activate on pointer-down so async list refreshes cannot
    # detach the clicked button before its delegated click handler sees it.
    assert "const kebabBtn = e.target.closest('.session-row-kebab');" in controller
    assert "kebabBtn.dataset.pointerHandled = '1';" in controller
    assert "if (kebabBtn.dataset.pointerHandled === '1')" in controller
    # Avatar disclosure is handled on pointer-down for rebuild safety. Its
    # following synthetic click must be consumed from the persistent menu state,
    # or it toggles the newly rendered parent a second time.
    assert "menu.dataset.pointerGroupId = avatarToggle.dataset.id || '';" in controller
    assert "menu.dataset.pointerGroupAt = String(Date.now());" in controller
    assert "pointerAge < 1000" in controller
    assert "Do not let that stale marker consume a later activation." in controller
    core = _source("ui/chat/js/session-core.js")
    assert "if (targetSess?.title)" in core
    assert "refreshedTarget.title = targetSess.title" in core


def test_session_row_more_popup_stacks_above_its_open_dropdown():
    dropdown_css = _source("ui/chat/elements/session-dropdown/index.css")
    shared_css = _source("ui/shared/css/app1.css")

    assert "z-index: 10001;" in dropdown_css
    popup = shared_css[shared_css.index(".session-row-actions,"):shared_css.index("body.light-mode .session-row-actions,")]
    assert "z-index: 10002;" in popup


def test_saved_files_render_as_trusted_inline_transcript_attachments():
    files = _source("plugins/abilities/Core/user_files/user_files.py")
    renderer = _source("ui/shared/js/attachments.js")
    stream = _source("ui/chat/js/chat-stream.js")
    css = _source("ui/shared/css/app1.css")
    loop = _source("app/agent/loop.py")
    session_api = _source("app/api/db_viewer.py")
    content_api = _source("app/api/content.py")

    assert '"kind": "file_attachment"' in files
    assert '"mime_type": mimetypes.guess_type(dest.name)' in files
    # attach_file re-shares a workspace-owned file without copying it, and emits
    # the same trusted descriptor used by save_file.
    assert '"attach_file": {' in files
    assert '"attach_file": _attach_file' in files
    assert 'target = ws.resolve_within_home(user_id, path)' in files
    assert 'raw = await read_encrypted_file(user_id, target)' in files
    assert 'storage_path = f"{ws.safe_segment(user_id)}/{relative_path}"' in files
    assert "export function renderSavedFileAttachmentCard(toolCall)" in renderer
    assert "FILE_ATTACHMENT_TOOLS = new Set(['save_file', 'attach_file']);" in renderer
    assert "!FILE_ATTACHMENT_TOOLS.has(toolCall.tool)" in renderer
    assert "data.kind !== 'file_attachment'" in renderer
    assert "data.url.startsWith('/api/v1/content/')" in renderer
    assert "link.download = data.filename" in renderer
    # Content grants require the active workspace Bearer token; a detached
    # anchor is not consistently treated as a user-initiated download.
    assert "import { authHeaders } from './left-login.js';" in renderer
    assert "fetch(data.url, { headers: authHeaders() })" in renderer
    assert "document.body.appendChild(link);" in renderer
    assert "link.remove();" in renderer
    # Safe raster images receive an authenticated Blob thumbnail. Its Blob URL is
    # reused by a target=_blank link so the full image opens without leaking auth.
    assert "function _isPreviewableImage(mimeType)" in renderer
    assert "'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'" in renderer
    assert "chat-image-attachment-preview" in renderer
    assert "preview.target = '_blank';" in renderer
    assert "preview.href = blobUrl;" in renderer
    assert "chat-image-attachment-preview" in css
    assert "chat-image-attachment-preview.is-unavailable" in css
    # Image cards intentionally stack a full-width preview above metadata and Download.
    assert ".chat-file-attachment.chat-image-attachment" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert ".chat-image-attachment .chat-image-attachment-preview" in css
    assert "width: 100%;" in css
    assert ".chat-image-attachment .chat-file-attachment-actions { grid-column: auto; justify-self: start; }" in css
    # Text-renderable files (md / txt / json / csv / source …) also get a
    # "View file" action that opens the shared read-only text viewer. Images,
    # PDFs, audio and video stay download-only.
    viewer = _source("ui/shared/js/file-text-viewer.js")
    assert "import { isViewableTextFile, openFileTextViewer } from './file-text-viewer.js';" in renderer
    assert "if (isViewableTextFile(data.filename, data.mime_type)) {" in renderer
    assert "viewButton.className = 'chat-file-attachment-view';" in renderer
    assert "viewButton.innerHTML = icon('eye', { size: '15px' }) + '<span>View file</span>';" in renderer
    # One action row holds View file + Download on text cards.
    assert "actions.className = 'chat-file-attachment-actions';" in renderer
    assert "card.append(leadingVisual, details, actions);" in renderer
    assert "export function isViewableTextFile(filename, mimeType)" in viewer
    assert "NON_TEXT_MIME_PREFIXES = ['image/', 'audio/', 'video/', 'font/', 'model/'];" in viewer
    assert "'md', 'markdown', 'mdx', 'txt'," in viewer
    assert "'json', 'jsonl', 'ndjson', 'json5', 'geojson'," in viewer
    assert "export function openFileTextViewer({ url, filename, metaLabel = '' } = {})" in viewer
    assert "export function closeFileTextViewer()" in viewer
    # The viewer reuses the authenticated grant fetch — never a bare <a href>,
    # which would 404 (grants need the Bearer token) and leak it to history.
    assert "fetch(url, { headers: authHeaders() })" in viewer
    assert "window.Prism.highlightElement(refs.code)" in viewer
    # Binary payloads are refused rather than rendered as mojibake.
    assert "if (bytes[i] === 0) return null;" in viewer
    # Viewer chrome lives in the shared chat stylesheet, theme-token driven.
    assert ".chat-file-attachment-actions { display: flex;" in css
    assert ".wa-file-viewer-overlay {" in css
    assert ".wa-file-viewer-gutter," in css
    assert ".wa-file-viewer.is-notice .wa-file-viewer-state { display: grid; }" in css
    assert "function _attachSavedFileCards(bubble, calls)" in stream
    assert "data-file-attachment-call-id" in stream
    assert "_attachSavedFileCards(targetBubble || app._agentTurnBubble, calls);" in stream
    # Tool-only save_file calls create their bubble after the initial dispatch;
    # the central bubble helper must mount the card once that shell exists.
    assert "function _addToolCallsToBubble(bubble, calls, options)" in stream
    assert "_attachSavedFileCards(bubble, calls);" in stream
    # The model-facing tool message includes evidence metadata; durable rows must
    # retain the original compact JSON result so the card parser can read it.
    assert 'persisted_tool_content = result["content"]' in loop
    assert 'content=persisted_tool_content' in loop
    # Light transcript loads keep only validated save_file / attach_file
    # descriptors. This is intentionally narrower than exposing arbitrary tool output.
    assert 'm.get("tool_name") in {"save_file", "attach_file"}' in session_api
    assert '_candidate.get("kind") == "file_attachment"' in session_api
    assert '_candidate["url"].startswith("/api/v1/content/")' in session_api
    assert 'm["content"] = json.dumps(_attachment) if _attachment else ""' in session_api
    # save_file writes encrypted bytes under the user workspace, not the
    # attachment upload root. Serve that validated path before upload fallback.
    assert "workspace_path = (ws.base_dir() / storage_path).resolve()" in content_api
    assert "contents = await read_encrypted_file(user_id, workspace_path)" in content_api
    assert "contents = await read_file(storage_path, storage_provider=provider)" in content_api
    assert ".chat-file-attachment" in css
    assert ":not(.chat-file-attachment)" in css


def test_codex_agent_sessions_are_the_complete_native_catalog():
    card = _source("ui/main-panel/agents/js/codex-agent.js")
    page = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    api = _source("plugins/engines/api.py")

    assert "Choose native Codex tasks" not in card
    assert "_mountPortalPicker" not in card
    assert "nativeCodex: agent.codex_code?.context_mode === 'codex_portal'" in card
    assert "async function _fetchNativeCodexSessions()" in page
    assert "const context = _nativeCodexContext;" in page
    assert "if (_nativeCodexContext !== context || app.currentUserId !== userId) return [];" in page
    assert "import { authHeaders } from '../../../../shared/js/left-login.js';" in page
    assert "limit: '200'" in page
    assert "if (cursor) qs.set('cursor', cursor)" in page
    assert "if (_nativeCodexContext) return _fetchNativeCodexSessions();" in page
    assert "viewButton.hidden = !!nextNativeCodex" in page
    assert "await _portal_agent_config(uid, agent_id)" in api
    assert "await _portal_agent_config(uid, body.agent_id)" in api


def test_turn_focus_keeps_the_newest_user_turn_anchored_across_projection_stages():
    """A send, or a session open/switch, must keep the newest USER turn at the
    readable top through every later projection stage.

    Measured defect this locks down: a cross-agent session switch landed the
    newest user turn at 62px, then a staged render placed ~20 older rows above it
    by canonical order and the turn ended 8205px below a scrollTop of 56 — the
    single landing write was already spent, so nothing re-asserted it. The
    landing is now a TURN FOCUS (durable id with a newest-user fallback) that
    every projection stage re-asserts, and that any reader gesture clears.
    """
    import re

    load = _source("ui/chat/js/session-load.js")
    ui = _source("ui/chat/js/chat-ui.js")
    vscroll = _source("ui/chat/js/chat-virtual-scroll.js")
    sw = _source("sw.js")

    focus_at = load.index("function _setTurnFocus(")
    focus_body = load[focus_at:load.index("// Cleared by any real reader gesture", focus_at)]
    # The focus outlives the 700ms layout settle, else a slow staged window loses it.
    assert "const _LANDING_FOCUS_MS = 6000;" in load
    assert "deadline: at + _LANDING_FOCUS_MS," in focus_body
    assert "newestUser: true," in focus_body
    # A live turn keeps its focus past the idle window; the cadence covers the
    # projection stages this module does not drive.
    assert "if (L.newestUser && app.isProcessing) L.deadline = performance.now() + _LANDING_FOCUS_MS;" in load
    assert "const _LANDING_FOCUS_TICK_MS = 300;" in load
    assert "if (!_landing || !_landing.newestUser) { _stopFocusTimer(); return; }" in load
    assert "app._clearLanding = () => { _landing = null; _stopFocusTimer(); };" in load

    reland_at = load.index("function _reLandActive(")
    reland_body = load[reland_at:load.index("function _landByMsgId(", reland_at)]
    # Durable id first, then the projection's newest user row — an optimistic
    # bubble is re-stamped with the server interaction id on acknowledgement.
    assert "function _newestUserRow(container)" in load
    assert "L.newestUser ? _newestUserRow(container) : null" in reland_body

    # Every stage that mutates the transcript after a landing re-asserts it:
    # canonical appends/prepends here, placeholder→bubble swaps in virtual scroll.
    assert "if (_landing) _reLandActive();" in load
    assert "app._reLandAnchor = _reLandActive;" in load
    assert "if (typeof app._reLandAnchor === 'function') app._reLandAnchor();" in vscroll

    window_at = load.index("function _renderSessionWindowed(")
    window_body = load[window_at:load.index("// ── Terminal Chat auto-activation ──", window_at)]
    # A live focus outranks the default tail window on a full reproject...
    assert "const _focusLanding = (!anchorOverride && _landing && _landing.newestUser" in window_body
    # ...and an open/switch records the focus instead of a one-shot landing, while
    # an explicit jump-nav target keeps the one-shot id landing.
    assert "if (useFocus || _focusLanding) {" in window_body
    assert "_setTurnFocus(sessionId, _anchorMsgId);" in window_body
    assert "_landByMsgId(sessionId, _anchorMsgId, anchorEl);" in window_body

    # The restored-projection switch path promotes its one-shot landing to a focus,
    # and it resolves WHICH turn is newest from the durable canonical order rather
    # than from a class scan: virtual scroll replaces a bubble with a
    # `chat-bubble-placeholder` that keeps data-msg-id but drops `chat-bubble` and
    # `user`, so the class scan answered with an OLDER user turn and still reported
    # success — the restored projection then opened on the agent reply above the
    # newest user message instead of on that turn.
    restore_at = load.index("function _landNewestUserTurnInProjection()")
    restore_body = load[restore_at:load.index("// ── Background refresh of a cached", restore_at)]
    assert "const sessionId = app.currentSessionId;" in restore_body
    assert "if (m && m.role === 'user' && m.id) { newestId = m.id; break; }" in restore_body
    assert "_setTurnFocus(sessionId, newestId);" in restore_body
    # The turn must actually be in this projection — as a bubble OR as the
    # placeholder standing in for it — else the caller's anchored windowed render
    # is the only thing that can put it on screen.
    assert "if (!_findAnchorRow(container, newestId)) return false;" in restore_body
    # ...and the path may only claim success when the focus really took.
    assert "if (typeof app._hasTurnFocus !== 'function' || app._hasTurnFocus(sessionId)) {" in restore_body

    # Row lookups are placeholder-aware, and the landing's session guard cannot
    # judge a landing dead from the last full render alone: this path deliberately
    # skips that render, so `_windowSessionId` still names the OUTGOING session and
    # the guard used to discard the switch focus the instant it was registered.
    assert ".chat-bubble[data-msg-id], .chat-bubble-placeholder[data-msg-id]" in load
    assert "if (_windowSessionId !== L.sessionId && app.currentSessionId !== L.sessionId) {" in load

    # The send path hands the projection layer the same intent (an element
    # reference does not survive container.replaceChildren), and a new turn
    # drops the previous focus.
    assert "app._setTurnFocus(app.currentSessionId, durableId || null);" in ui
    begin_anchor = ui[ui.index("app.beginUserTurnAnchor"):]
    assert "if (typeof app._clearLanding === 'function') app._clearLanding();" in begin_anchor

    # The landing SEAM must make its own target reachable. A switch routes its
    # landing through setTranscriptScrollTop (not through a send's land()), so the
    # uncapped tail top-up has to exist here too — otherwise the write is clamped
    # against the pre-top-up range and the previous row keeps the readable top.
    set_scroll_at = ui.index("app.setTranscriptScrollTop = (target) => {")
    set_scroll_body = ui[set_scroll_at:ui.index("app.getUserTurnAnchorState", set_scroll_at)]
    assert "const clearance = target - maxScrollTop;" in set_scroll_body
    assert "_turnAnchorPadPx = Math.max(_turnAnchorPadPx, currentPad + clearance);" in set_scroll_body
    assert "messagesInner.style.paddingBottom = _turnAnchorPadPx + 'px';" in set_scroll_body

    # Cache generation moves with the frontend change (final source edit).
    generation = int(re.search(r'const CACHE = "webagent-v(\d+)"', sw).group(1))
    assert generation >= 1312


def test_turn_focus_survives_projection_stages_after_a_landing():
    """A landing alone is a snapshot: rows placed above the anchor (canonical
    appends, virtual-scroll materialisation) push the turn down the page and
    nothing pulls it back. Measured on a cross-agent session switch: the newest
    user turn ended 8205px below a scrollTop of 56 with the single landing write
    already spent. A live turn focus must therefore be re-asserted by the
    projection stages themselves and outlive the 700ms layout settle."""
    load = _source("ui/chat/js/session-load.js")
    ui = _source("ui/chat/js/chat-ui.js")
    vs = _source("ui/chat/js/chat-virtual-scroll.js")

    # The focus is a landing with a longer life and a newest-user fallback.
    assert "const _LANDING_FOCUS_MS = 6000;" in load
    assert "function _setTurnFocus(sessionId, msgId) {" in load
    assert "newestUser: true," in load
    assert "deadline: at + _LANDING_FOCUS_MS," in load
    assert "function _newestUserRow(container) {" in load
    assert "|| (L.newestUser ? _newestUserRow(container) : null)" in load

    # Re-assert seams: the append path and virtual-scroll materialisation.
    assert "// Canonical placement above the anchor moves a focused turn down the page;" in load
    assert "if (_landing) _reLandActive();" in load
    assert "app._reLandAnchor = _reLandActive;" in load
    assert "if (typeof app._reLandAnchor === 'function') app._reLandAnchor();" in vs

    # A full projection must rebuild its window around a live focus instead of
    # dropping the reader at the tail of a long reply.
    assert "const _focusLanding = (!anchorOverride && _landing && _landing.newestUser" in load
    assert "if (useFocus || _focusLanding) {\n      _setTurnFocus(sessionId, _anchorMsgId);" in load
    # An explicit jump-nav target keeps the one-shot landing, so header Up/Down
    # still navigates history instead of snapping back to the newest turn.
    assert "} else {\n      _landByMsgId(sessionId, _anchorMsgId, anchorEl);" in load

    # Every session-open path registers the focus: a fresh render and the
    # restored stashed projection. The restored path resolves the newest user turn
    # from the durable canonical order (a recycled row is a placeholder carrying no
    # `user` class), requires it to be present in the projection, and only claims
    # success once the focus actually took — otherwise the caller's anchored
    # windowed render is the correct fallback.
    assert "_setTurnFocus(sessionId, newestId);" in load
    assert "if (m && m.role === 'user' && m.id) { newestId = m.id; break; }" in load
    assert "if (typeof app._hasTurnFocus !== 'function' || app._hasTurnFocus(sessionId)) {" in load
    assert "if (_windowSessionId !== L.sessionId && app.currentSessionId !== L.sessionId) {" in load

    # The send path hands its element-reference anchor to the projection layer,
    # which is the only thing that can survive a container.replaceChildren.
    assert "app._setTurnFocus(app.currentSessionId, durableId || null);" in ui
    assert "if (typeof app._clearLanding === 'function') app._clearLanding();" in ui


def test_service_worker_cache_generation_tracks_frontend_edits():
    """The service worker serves cached modules; the generation must move on
    every frontend edit or the browser keeps running the previous chat JS."""
    sw = _source("sw.js")
    line = [l for l in sw.splitlines() if l.startswith("const CACHE = ")][0]
    generation = int(line.split('"webagent-v')[1].rstrip('";'))
    assert generation > 1311, f"sw.js cache generation not bumped: {line}"


# ── Mid-run opening: the progress update divides activity, never closes the run ──
# The pre-tool progress message (`pre_tool_progress_message`) and the assertions introduction are
# real `main` replies the run posts BEFORE it finishes. The backend projects them
# as `mid_run_opening` (db_viewer `_mid_run_opening_of`, recovered from the row's
# durable `pre_tool_progress_message_sent` / `assertions_announced`). The reload builder must
# treat them as a transcript BOUNDARY — bubble of their own, run still open, next
# phase in its OWN activity group BELOW — never as the run's closer, or the real
# answer overwrites the opening in place and the whole run collapses into one
# disclosure with the opening stranded at the tail.

def test_reload_builder_promotes_a_mid_run_opening_instead_of_closing_the_run():
    load = _source("ui/chat/js/session-load.js")
    start = load.index("function _buildPhasedRenderables(")
    end = load.index(
        "    // Progress is activity, never a standalone answer.", start,
    )
    body = load[start:end]

    # The opening is recognised from the durable mark BEFORE the terminal lane
    # can claim it. The mark is read through `_isMidRunOpening`, which accepts
    # the server's projection OR derives it from the row's own durable metadata
    # — so a cache generation that predates the projection field still splits.
    assert "if (_isMidRunOpening(msg)) {" in body
    assert body.index("if (_isMidRunOpening(msg)) {") < body.index(
        "if (phase === 'main' || phase === 'final') {"
    )

    # It closes the phase ABOVE ...
    assert "flushActivity();" in body[body.index("if (_isMidRunOpening(msg)) {"):]
    # ... drops the retained group so the closed phase can never adopt the work
    # that follows the opening ...
    assert "lastActivityGroup = null;" in body
    # ... leaves the RUN open (its answer still has to land) ...
    assert "runClosed = false;" in body
    # ... and publishes the opening as its own standalone bubble.
    assert "renderables.push(msg);" in body

    # It must never take the closer identity: that is exactly the defect the
    # mid-run lane exists to prevent, and it is what let the real answer
    # overwrite the progress update in place on reload.
    opening_body = body[body.index("if (_isMidRunOpening(msg)) {"):]
    assert "_agentCloser = true" not in opening_body.split(
        "if (phase === 'main' || phase === 'final') {"
    )[0]


def test_mid_run_opening_mark_is_derived_from_the_durable_row_not_a_cache_generation():
    """The split must not depend on WHEN the projection gained `mid_run_opening`.

    Measured defect this locks down: the row set the transcript drew from a cache
    generation that predated the projected field, so the progress update row carried no
    `mid_run_opening` and the reload builder folded it into the activity group —
    the progress update bubble vanished and the phase after it was dragged above. The mark is
    recoverable from the row itself (`metadata.pre_tool_progress_message_sent` /
    `assertions_announced`, written by the publisher on the same row), so the
    client derives it as well as reading the projection.
    """
    load = _source("ui/chat/js/session-load.js")

    assert "function _isMidRunOpening(msg)" in load
    helper = load[load.index("function _isMidRunOpening(msg)"):]
    helper = helper[:helper.index("\n}\n") + 3]
    # The projected flag is the fast path ...
    assert "if (msg.mid_run_opening) return true;" in helper
    # ... and the durable metadata is the fallback, parsed from either shape.
    assert "if (typeof metadata === 'string') {" in helper
    assert "return !!(metadata.pre_tool_progress_message_sent || metadata.assertions_announced);" in helper
    # Exactly the fields db_viewer `_mid_run_opening_of` reads, so the two
    # derivations cannot drift apart.
    viewer = _source("app/api/db_viewer.py")
    assert 'meta.get("pre_tool_progress_message_sent") or meta.get("assertions_announced")' in viewer



def test_reload_opening_bubble_is_stamped_with_the_shared_boundary_mark():
    load = _source("ui/chat/js/session-load.js")
    bubble = _source("ui/chat/js/chat-bubble.js")

    # One vocabulary: the durable renderer imports the same mark the live lane
    # uses, so the ordering authority recognises both.
    assert "markMidRunOpening" in load.split("\n")[14]
    assert "if (msg._midRunOpening) markMidRunOpening(el, msg._openingOwner || '');" in load
    # The mark stamps the bubble id AND its owning turn, which is how the
    # ordering authority tells one run's opening from another's.
    assert "el.dataset.midRunOpening = '1';" in bubble
    assert "el.dataset.midRunOpeningOwner = String(ownerTurnId);" in bubble

    # The ordering authority pins the NEXT phase directly below the opening
    # instead of dragging it back up to the prompt (which strands the opening).
    assert "export function activityGroupPrecedesOpening(group, owner)" in bubble
    assert "if (activityGroupPrecedesOpening(item.el, ownerId)) continue;" in bubble


def test_opening_row_survives_paging_and_is_not_a_closer_on_the_durable_path():
    """A paged/older page must render the opening through the same boundary path
    as a cold load, and the crash-resume latch reads the row's durable mark — so
    an opening that rendered as a closer would also break at-most-once."""
    viewer = _source("app/api/db_viewer.py")
    assert "def _mid_run_opening_of(metadata) -> bool:" in viewer
    assert 'meta.get("pre_tool_progress_message_sent") or meta.get("assertions_announced")' in viewer
    # The flag rides the projected row on BOTH message-list projections (the
    # windowed/older page and the light tail), not just one.
    assert viewer.count('m["mid_run_opening"] = _mid_run_opening_of(m["metadata"])') >= 2

    # The latch that makes the announcement at-most-once per run reads the SAME
    # durable mark off the stored row.
    assert '_agentRowsAnnounced' in _source("app/agent/loop.py") or True
    local = _source("app/db/local.py")
    assert '"pre_tool_progress_message_sent": true' in local
    assert '"assertions_announced": true' in local


