'use strict';

// MODEL-ADD-GUARD: the alias editor reuses the live configurator, so the same
// Add button drives both a normal add and a "add backup connection" add.
//
// A normal add has NO alias target, and its `dupe` lookup is null for every
// provider except the WebAgent Catalog. Comparing those two nulls as
// `dupe === aliasTarget` is therefore ALWAYS true on a normal add and used to
// short-circuit every Add with the backup-only message. Keep the alias-only
// validation gated on a real alias target, and resolve catalog duplicates for
// normal adds as their own case.
//
// The decision is extracted here (pure, DOM-free) so a regression is caught by
// tests/test_model_add_guard.test.mjs instead of only in the browser.
export const MODEL_ADD_DUPLICATE = 'This model is already in your roster';
export const MODEL_ADD_SELF_LINK = 'Choose a different connection for the backup';

export function modelAddGuard({ platformCatalog, providers, selectedModel, aliasTarget }) {
  // A catalog row's entry_id IS the opaque catalog model id, so it can be
  // matched against the picker's selectedModel. BYO/subscription rows have no
  // stable client identity for a not-yet-saved pick, so they never produce a
  // duplicate here.
  const dupe = platformCatalog
    ? (providers || []).find(row => String(row.entry_id) === String(selectedModel)) || null
    : null;
  // Normal Add: a catalog model already in the roster is not re-added as a
  // duplicate; the alias flow is the only way to attach a second connection.
  if (dupe && !aliasTarget) return { dupe, message: MODEL_ADD_DUPLICATE };
  // Alias Add: the backup must be a different connection from its own parent.
  if (aliasTarget && dupe === aliasTarget) return { dupe, message: MODEL_ADD_SELF_LINK };
  return { dupe, message: '' };
}
