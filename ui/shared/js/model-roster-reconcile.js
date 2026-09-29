'use strict';

// MODEL-ROW-IDENTITY: reconcile a server save response into the exact row objects
// owned by the model table. Existing rows bind only by opaque entry_id. New rows
// arrive without an ID and consume one structurally matching server row each, so
// duplicate provider/model/credential combinations remain independent entries.

const text = value => String(value ?? '').trim();
const providerName = value => text(value) === '_custom' ? 'custom' : text(value);

function rowScore(local, saved) {
  const localOrigin = text(local?.catalog_origin);
  const savedOrigin = text(saved?.catalog_origin);
  if (localOrigin && savedOrigin && localOrigin !== savedOrigin) return -1;

  let score = 0;
  for (const [key, normalise] of [
    ['provider', providerName],
    ['model', text],
    ['base_url', text],
    ['auth_mode', text],
    ['display_name', text],
  ]) {
    const left = normalise(local?.[key]);
    const right = normalise(saved?.[key]);
    if (left && right && left !== right) {
      if (key === 'provider' || key === 'model') return -1;
      continue;
    }
    if (left && right) score += key === 'model' ? 8 : 2;
  }

  const key = text(local?.api_key);
  const suffix = text(saved?.credential_suffix);
  if (key && suffix) {
    if (!key.endsWith(suffix)) return -1;
    score += 32;
  }
  return score;
}

function mergeRow(local, saved) {
  const uid = local?._uid;
  Object.assign(local, saved);
  if (uid !== undefined) local._uid = uid;
}

export function reconcileSavedModelRows(localRows, savedRows) {
  if (!Array.isArray(localRows) || !Array.isArray(savedRows)) return localRows;
  const used = new Set();

  // Stable IDs are the sole identity for rows which already exist.
  localRows.forEach((local) => {
    const entryId = text(local?.entry_id);
    if (!entryId) return;
    const index = savedRows.findIndex((saved, candidateIndex) => (
      !used.has(candidateIndex) && text(saved?.entry_id) === entryId
    ));
    if (index < 0) return;
    mergeRow(local, savedRows[index]);
    used.add(index);
  });

  // A new row has no identity until the server assigns one. Structural matching
  // chooses among only unconsumed rows; equal duplicates therefore receive distinct
  // IDs without ever collapsing into a provider/model key.
  localRows.forEach((local) => {
    if (text(local?.entry_id)) return;
    let bestIndex = -1;
    let bestScore = -1;
    savedRows.forEach((saved, candidateIndex) => {
      if (used.has(candidateIndex) || !text(saved?.entry_id)) return;
      const score = rowScore(local, saved);
      if (score > bestScore) {
        bestScore = score;
        bestIndex = candidateIndex;
      }
    });
    if (bestIndex < 0) return;
    mergeRow(local, savedRows[bestIndex]);
    used.add(bestIndex);
  });
  return localRows;
}
