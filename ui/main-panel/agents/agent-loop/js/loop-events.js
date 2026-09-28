'use strict';

import { app } from '../../../../shared/js/state.js';
import { apiPath } from '../../../../shared/js/config.js';

// Shared mapping from a DB interaction row to agent-loop pipeline events.
//
// Both loop views replay session history through this: the runtime sidebar
// (loop.js) and the visual graph (loop-logic.js). The row -> event mapping
// MUST stay identical between them, so it lives here as a single source of
// truth instead of being copy-pasted into both files (where the two copies
// could silently drift apart).

export function interactionToEvents(row) {
  const events = [];
  const role = row.role || 'unknown';

  if (role === 'user') {
    events.push({
      type: 'pipeline', level: 'user',
      step: 'user_message', content: row.content || '',
    });
  } else if (role === 'assistant') {
    let meta = {};
    try { meta = JSON.parse(row.metadata || '{}'); } catch (e) {}
    if (meta.turn) {
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'turn_start', turn: meta.turn, max_turns: 10,
      });
    }
    events.push({
      type: 'response', level: 'agent',
      content: (row.content || '').replace(/\n\n\[Tool calls:.*\]$/s, ''),
      _input_tokens: meta.input_tokens,
      _output_tokens: meta.output_tokens,
      _duration_ms: meta.duration_ms,
      _model: meta.model,
    });
  } else if (role === 'tool') {
    const toolName = row.tool_name || 'unknown';
    let meta = {};
    try { meta = JSON.parse(row.metadata || '{}'); } catch (e) {}

    if (toolName === 'memory_search') {
      let contentObj = {};
      try { contentObj = JSON.parse(row.content || '{}'); } catch (e) {}
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'memory_search_end', results_count: meta.count || contentObj.count || 0,
      });
    } else if (toolName === 'memory_save') {
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'memory_save_end', slug: meta.slug || toolName,
      });
    } else {
      events.push({
        type: 'tool_call', level: 'agent',
        tool: toolName, args: meta.input_params || {},
      });
      events.push({
        type: 'tool_result', level: 'agent',
        tool: toolName, result: row.content || '',
        duration_ms: meta.duration_ms || 0,
        error: !(meta.success !== false),
        error_type: meta.error_message ? 'execution_error' : null,
        recoverable: true,
      });
    }
  }

  return events;
}

/**
 * Fetch interaction rows for the current user/session from the DB API.
 * Returns the rows array, or null if userId/sessionId are unset.
 * Throws on network/API error so callers handle their own error reporting.
 * Deduplicated from loop-logic.js and loop.js (clone-group dup:6eaede63).
 */
export async function fetchInteractionRows() {
  const userId    = app.currentUserId;
  const sessionId = app.currentSessionId;
  if (!userId || !sessionId) return null;
  const url = apiPath(`/api/v1/db/stream/interactions?user_id=${encodeURIComponent(userId)}&session_id=${encodeURIComponent(sessionId)}`);
  const res  = await fetch(url);
  const data = await res.json();
  return data.interactions || [];
}
