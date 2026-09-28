'use strict';

// One flag vocabulary for every session-status surface.
//
// The `closing_message` gate has the WORKING agent stamp its own verdict —
// needs_input / complete / error / partial / unverified — on the run's promoted
// final row (app/agent/closing_message.py). The runtime's own `run_status`
// (session_runs) is the coarser truth: it knows running / complete / error /
// interrupted / queued, but it has NO word for "finished, but not cleanly".
//
// This module is the ONE place that reconciles the two, so the chat notification
// pill, the session dropdown and the Agents → Sessions page can never disagree:
//
//   1. the closer's flag WINS whenever the session carries one — it is the
//      agent's own account of work the runtime cannot see;
//   2. otherwise the runtime status is normalized into the same vocabulary, so
//      a run with no closer record (gate off, or a run that predates it) still
//      produces a flag;
//   3. a flag may only ESCALATE severity, never soften it — a stray `complete`
//      can never repaint an errored or interrupted run green.
//
// SYNC: app/agent/closing_message.py (DEFAULT_FLAGS / SAFE_FLAG) and the
// `closing_flag` / `closing_cta` fields the session list carries
// (app/api/db_viewer.py, _fetch_closing_flags).

export const FLAG_LABELS = {
  running: 'Running',
  queued: 'Queued',
  needs_input: 'Needs input',
  complete: 'Complete',
  partial: 'Partial',
  unverified: 'Unverified',
  error: 'Error',
  interrupted: 'Stopped',
};

// Semantic tone per flag — the design-system status token each surface maps to
// its own CSS class. error=danger, complete=success, running=info, and the
// "finished, but look at it" family (needs_input / partial / unverified) is
// warning. interrupted is neutral: intentionally stopped is not a failure.
export const FLAG_TONES = {
  running: 'info',
  queued: 'neutral',
  needs_input: 'warning',
  complete: 'success',
  partial: 'warning',
  unverified: 'warning',
  error: 'danger',
  interrupted: 'neutral',
};

// Escalation rank. A closing flag below the runtime flag's rank is ignored, so
// only equal-or-worse may replace the runtime verdict. Unknown flags rank 1:
// above a clean completion (the agent's own verdict is still worth showing) and
// below every attention state.
const FLAG_SEVERITY = {
  complete: 0,
  running: 0,
  queued: 0,
  needs_input: 2,
  partial: 2,
  unverified: 2,
  interrupted: 3,
  error: 4,
};

const UNKNOWN_SEVERITY = 1;

/** Escalation rank for a flag key; unknown flags get the neutral-unknown rank. */
export function flagSeverity(flag) {
  const key = String(flag || '').trim().toLowerCase();
  if (!key) return -1;
  return Object.prototype.hasOwnProperty.call(FLAG_SEVERITY, key)
    ? FLAG_SEVERITY[key]
    : UNKNOWN_SEVERITY;
}

/** Human-readable label. An off-taxonomy flag still renders (the flag is
 *  advisory) with its token title-cased, so a custom taxonomy is never dropped. */
export function flagLabel(flag) {
  const key = String(flag || '').trim().toLowerCase();
  if (!key) return '';
  if (FLAG_LABELS[key]) return FLAG_LABELS[key];
  return key.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Semantic tone for a flag; unknown flags are neutral. */
export function flagTone(flag) {
  const key = String(flag || '').trim().toLowerCase();
  return FLAG_TONES[key] || 'neutral';
}

/** The runtime status normalized into the flag vocabulary — the FALLBACK used
 *  when a session carries no closer record. Mirrors the pill's original
 *  derivation (a completed Plan-mode turn genuinely paused for the user). */
export function fallbackFlag(session) {
  if (!session) return '';
  const status = String(session.run_status || '').trim().toLowerCase();
  if (!status) return '';
  if (status === 'running' || status === 'resuming') return 'running';
  if (status === 'queued') return 'queued';
  if (status === 'error') return 'error';
  if (status === 'interrupted' || status === 'stopped'
      || status === 'needs_manual_resume') {
    return 'interrupted';
  }
  if (status === 'complete') {
    return session.execution_mode === 'plan' ? 'needs_input' : 'complete';
  }
  return status;
}

/** The effective flag for one session: the closer's verdict when present, with
 *  the runtime status as the fallback — and never softened below it. */
export function resolveSessionFlag(session) {
  const fallback = fallbackFlag(session);
  const closing = String((session && session.closing_flag) || '').trim().toLowerCase();
  if (!closing) return fallback;
  if (!fallback) return closing;
  return flagSeverity(closing) < flagSeverity(fallback) ? fallback : closing;
}

/** The closer's notification / call-to-action line, when the session carries
 *  one — the short note the agent wrote for the pop-up. */
export function closingCta(session) {
  return String((session && session.closing_cta) || '').trim();
}

/** The acknowledgement flag for one session — what its status chip shows while
 *  the run still awaits the user's confirmation. Non-empty only for a session
 *  the user has NOT read whose resolved flag is a SETTLED state; running and
 *  queued are live states that keep their own loader, so they never raise the
 *  chip. Clicking the chip marks the session read (which clears it); marking the
 *  session unread again restores the same flag. Driven by the DB-backed
 *  ``has_unread`` (sessions.read_at / unread_at), never client-only state. */
export function sessionAckFlag(session) {
  if (!session || !session.has_unread) return '';
  const flag = resolveSessionFlag(session);
  if (!flag || flag === 'running' || flag === 'queued') return '';
  return flag;
}

/** The flag a LIST CHIP shows for one session — the same vocabulary as
 *  `sessionAckFlag`, but with a longer life.
 *
 *  A CLEAN completion (`complete`) drops its chip the moment the user reads it:
 *  the work is done and acknowledged, so there is nothing left to say. Any other
 *  settled flag — needs_input / partial / unverified / error / interrupted — keeps
 *  its chip AFTER being read, rendered in the neutral tone: the session is read
 *  but still not finished, so it still wants attention. That is the difference
 *  between "you have not looked at this yet" and "this never came out clean".
 *
 *  Live states (running / queued) never raise a chip; they carry their own
 *  loader and are handled by the callers' status branches.
 *
 *  NOTE: the notification panel deliberately keeps the stricter unread-only
 *  `sessionAckFlag` — a notification is dismissed by reading it, so a read
 *  session must leave the panel even when its flag lingers on the list. */
export function sessionAttentionFlag(session) {
  if (!session) return '';
  const flag = resolveSessionFlag(session);
  if (!flag || flag === 'running' || flag === 'queued') return '';
  if (session.has_unread) return flag;
  return flag === 'complete' ? '' : flag;
}

/** True when a chip raised by `sessionAttentionFlag` marks an already-READ
 *  session — the grey "read, but still unfinished" marker rather than the
 *  coloured unread prompt. Callers use it to pick the neutral tone and to make
 *  the chip's click put the session back to unread. */
export function sessionFlagIsRead(session) {
  return !!session && !session.has_unread;
}
