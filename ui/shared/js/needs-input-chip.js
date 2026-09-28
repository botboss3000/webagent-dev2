'use strict';

// Shared three-stage interaction for a session's action-required flag. Both the
// chat dropdown and Agents → Sessions use this so the prompt, timeout, and
// confirmation semantics remain identical.
//
//   idle ──click──▶ note ──click──▶ armed ──click──▶ confirm (onConfirm)
//
// The NOTE stage shows the closer's own short account of what the run did
// (ui/shared/js/session-flag.js `closingCta` — the same line the pop-up
// notification carries), so the flag explains itself before it is acknowledged.
// A session that carries no closer note skips straight to ARMED, which keeps the
// old two-click contract for a run the gate never stamped.
//
// BOTH intermediate stages auto-reset to the idle flag after two seconds, so a
// half-finished interaction can never strand the chip in a transient label.
//
// The chip shows the session's RESOLVED status flag (ui/shared/js/session-flag.js)
// rather than a constant "Needs input", so the restore below puts back whatever
// label the caller rendered — read from `data-needs-input-label`.
// The chip's verb: 'read' (default) for the coloured unread prompt, 'unread'
// for the grey read-but-unfinished marker, whose click puts the session back to
// unread (restoring the coloured flag). Read from `data-needs-input-verb` so it
// survives a re-render exactly like the restore label does.
const STAGE_TIMEOUT_MS = 2000;

const STAGE_IDLE = 'idle';
const STAGE_NOTE = 'note';
const STAGE_ARMED = 'armed';

function _label(button) {
  return (button && button.dataset.needsInputLabel) || 'Needs input';
}

function _note(button) {
  return (button && button.dataset.needsInputNote) || '';
}

function _verb(button) {
  return button && button.dataset.needsInputVerb === 'unread' ? 'unread' : 'read';
}

function _idleTitle(button) {
  return `${_label(button)} — click to mark ${_verb(button)}`;
}

function _stage(button) {
  const stage = button && button.dataset.needsInputStage;
  return stage === STAGE_NOTE || stage === STAGE_ARMED ? stage : STAGE_IDLE;
}

/** Stamp the current stage on the chip (dataset + classes) so the CSS can style
 *  it and a re-entrant click can tell where the cycle sits. */
function _setStage(button, stage) {
  button.dataset.needsInputStage = stage;
  button.classList.toggle('noting', stage === STAGE_NOTE);
  button.classList.toggle('armed', stage === STAGE_ARMED);
}

/** Back to the idle flag label; also clears any pending auto-reset. */
function _restore(button) {
  if (!button) return;
  clearTimeout(button._needsInputDisarmTimer);
  _setStage(button, STAGE_IDLE);
  button.textContent = _label(button);
  button.title = _idleTitle(button);
  button.setAttribute('aria-label', _idleTitle(button));
}

/** Auto-reset the current TRANSIENT stage (note or armed) back to the flag after
 *  two seconds, so neither stage can outlive a half-finished interaction. */
function _armStageTimeout(button) {
  clearTimeout(button._needsInputDisarmTimer);
  button._needsInputDisarmTimer = setTimeout(() => {
    if (button.isConnected) _restore(button);
  }, STAGE_TIMEOUT_MS);
}

/** Stage 1 — the closer's note: what the run actually did, in the agent's words. */
function _showNote(button, note) {
  _setStage(button, STAGE_NOTE);
  button.textContent = note;
  button.title = `${note} — click again to mark ${_verb(button)}`;
  button.setAttribute('aria-label', `${note}. Click again to mark this session ${_verb(button)}`);
  _armStageTimeout(button);
}

/** Stage 2 — the visible confirmation. */
function _arm(button) {
  _setStage(button, STAGE_ARMED);
  button.textContent = `Mark ${_verb(button)}?`;
  button.title = `Click again to mark this session ${_verb(button)}`;
  button.setAttribute('aria-label', `Mark ${_verb(button)}? Click again to confirm`);
  _armStageTimeout(button);
}

/**
 * Advance a flag chip through note → armed → confirm. `onConfirm` runs on the
 * final click and resolves truthy when the read mutation succeeded; a failed
 * mutation restores the chip for a safe retry.
 */
export async function activateNeedsInputChip(button, onConfirm) {
  if (!button || button.dataset.needsInputPending === '1') return false;
  const stage = _stage(button);

  // Idle — first click. Prefer the closer's note; without one, arm directly so
  // the two-click contract still holds for a session the gate never stamped.
  if (stage === STAGE_IDLE) {
    const note = _note(button);
    if (note) _showNote(button, note);
    else _arm(button);
    return false;
  }

  // Note — second click arms the confirmation.
  if (stage === STAGE_NOTE) {
    _arm(button);
    return false;
  }

  // Armed — final click confirms.
  clearTimeout(button._needsInputDisarmTimer);
  button.dataset.needsInputPending = '1';
  button.disabled = true;
  try {
    const changed = await onConfirm();
    if (!changed && button.isConnected) _restore(button);
    return Boolean(changed);
  } catch (_) {
    if (button.isConnected) _restore(button);
    return false;
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      delete button.dataset.needsInputPending;
    }
  }
}
