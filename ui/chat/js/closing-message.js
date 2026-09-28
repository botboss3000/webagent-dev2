'use strict';

// Closing-message envelope presentation.
//
// The `closing_message` gate makes the WORKING agent author the run's promoted
// final message. That message ends with an envelope:
//
//     <the final message the user will see>
//
//     FLAG: <name>
//     <one short step or status update>
//
// The bubble shows ONLY the statement: this module strips the machine envelope
// from the visible text. The flag itself is NOT rendered on the bubble — it
// surfaces on the session's status chip and the notification pill, which read
// the durable `metadata.closing_message` record the loop writes from this same
// envelope (resolved by ui/shared/js/session-flag.js, served by
// app/api/db_viewer.py). Keeping the envelope in the CONTENT is what makes that
// flag durable, so only the render was retired.
//
// The envelope lives in the message CONTENT, so every rehydration path (live
// stream, cold load, reconcile, session switch) derives the same flag from the
// same source. The durable `metadata.closing_message` record written by the loop
// is the fallback when the visible text has been rewritten.
//
// SYNC: app/agent/closing_message.py — the envelope grammar and the taxonomy
// must stay in step with the backend parser and its safe-default downgrade.
// That includes the INLINE form (`FLAG: <name> — <status>`): the backend accepts
// it, so this parser must too, or a live bubble would print the raw flag line
// while the durable badge showed the flag.

const FLAG_LINE_RE = /^[ \t]*FLAG:[ \t]*(.*?)[ \t]*$/gm;

// A separator divides the flag NAME from an inline status: a space-delimited
// dash (em/en/hyphen) or a colon. The dash must be space-delimited so a
// hyphenated flag name ("needs-input") is never split in two.
const FLAG_INLINE_SEP_RE = /(?:[ \t]+[—–-]+[ \t]+|[ \t]*:[ \t]*)/;

// Split a closing message into statement / flag / call-to-action. Mirrors
// parse_envelope() in app/agent/closing_message.py: line-anchored so ordinary
// prose containing "flag:" is never mistaken for the envelope, last match wins.
// The status may ride the same line as the flag (the inline form) or the next.
export function parseClosingEnvelope(text) {
  const raw = String(text || '');
  const matches = [...raw.matchAll(FLAG_LINE_RE)];
  if (!matches.length) {
    return { valid: false, statement: raw.trim(), flag: '', cta: '' };
  }
  const match = matches[matches.length - 1];
  const payload = (match[1] || '').trim();
  let flag = payload;
  let cta = '';
  const separator = FLAG_INLINE_SEP_RE.exec(payload);
  if (separator && payload.slice(separator.index + separator[0].length).trim()) {
    flag = payload.slice(0, separator.index).trim();
    cta = payload.slice(separator.index + separator[0].length).trim();
  }
  if (!cta) {
    const tail = raw.slice(match.index + match[0].length)
      .split('\n')
      .map(line => line.trim());
    cta = tail.find(line => line) || '';
  }
  return {
    valid: true,
    statement: raw.slice(0, match.index).trim(),
    flag: flag.toLowerCase(),
    cta,
  };
}
