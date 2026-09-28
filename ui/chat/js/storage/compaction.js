'use strict';

/**
 * Client-side history compaction for browser-authority chat.
 *
 * Before sending interactions to the server, if a session has more than
 * ``COMPACT_AT`` messages, the oldest are replaced with a compact summary
 * block.  This dramatically reduces network transfer for long sessions
 * while preserving enough recent context for the LLM.
 *
 * The server-side SessionMessageCache + prompt caching handle the rest:
 * after turn 1, the server already has the full messages[] in RAM, so
 * compaction is only impactful on cold-start first messages of long
 * sessions — or when the cache TTL expires (30 min).
 *
 * Design trade-off
 * ----------------
 * This is a **deterministic text heuristic**, not an LLM call.  That means
 * no compute cost, no latency, and zero server dependency — but the
 * summary is structurally simple (message count + topic from the first
 * user message).  A truly lossless compaction needs the server-side
 * segment train (app/agent/compaction.py), which uses its own lightweight
 * LLM call.  The browser heuristic is good enough to cut 80 % of the
 * payload while keeping the LLM informed of what was compacted.
 */

// ── Tunables ─────────────────────────────────────────────────────────────────

/** Maximum interactions to send verbatim before compaction kicks in. */
const COMPACT_AT = 50;

/** Number of most-recent interactions to keep verbatim (the "hot tail"). */
const HOT_TAIL = 10;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Compact an interactions array for the wire.
 *
 * When ``interactions.length > COMPACT_AT``, the oldest entries up to
 * ``length - HOT_TAIL`` are replaced with a single synthetic interaction
 * that summarises them.  The hot tail passes through unchanged.
 *
 * @param {Array<Object>} interactions — full unordered history (chronological)
 * @param {Object} [opts]
 * @param {number} [opts.compactAt=50] — threshold to trigger compaction
 * @param {number} [opts.hotTail=10] — verbatim trailing messages to keep
 * @returns {Array<Object>} compacted interactions (may be the original array
 *   if no compaction was needed — save the caller a copy in the common case)
 */
export function compactInteractions(interactions, opts = {}) {
  const threshold = (opts.compactAt || COMPACT_AT);
  const tail = (opts.hotTail || HOT_TAIL);

  if (!interactions || interactions.length <= threshold) {
    return interactions;  // no compaction needed
  }

  const compactEnd = interactions.length - tail;
  const compacted = interactions.slice(0, compactEnd);
  const hotTail = interactions.slice(compactEnd);

  // Build a deterministic summary from the compacted span
  const summary = _buildSummary(compacted, interactions.length);

  return [summary, ...hotTail];
}

// ── Internals ────────────────────────────────────────────────────────────────

/**
 * Build a synthetic "EARLIER CONVERSATION" summary interaction.
 *
 * Scans the compacted span for the first user message to infer a topic,
 * counts tool-call patterns, and produces a single entry that the server
 * can insert as contextual history.
 *
 * @param {Array<Object>} span — the interactions being compacted
 * @param {number} totalCount — total interactions in the session (for display)
 * @returns {Object} a single interaction with role='system' and summary text
 */
function _buildSummary(span, totalCount) {
  const firstUserMsg = span.find(m => m.role === 'user');
  const userCount = span.filter(m => m.role === 'user').length;
  const asstCount = span.filter(m => m.role === 'assistant').length;
  const toolCount = span.filter(m => m.role === 'tool').length;

  // Topic: first few words of the first user message
  let topic = 'general conversation';
  if (firstUserMsg && firstUserMsg.content) {
    const cleaned = firstUserMsg.content
      .replace(/[\n\r]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (cleaned) {
      topic = cleaned.length > 80 ? cleaned.slice(0, 80) + '…' : cleaned;
    }
  }

  const summaryText = `[EARLIER CONVERSATION — ${totalCount} total messages, ${userCount} user turns, ` +
    `${asstCount} assistant replies, ${toolCount} tool calls. Topic: ${topic}]`;

  return {
    role: 'system',
    content: summaryText,
    id: 'compacted-' + Date.now(),
    session_seq: -1,  // marker so the server knows this is synthetic
    _compacted: true,
    _original_count: span.length,
  };
}
