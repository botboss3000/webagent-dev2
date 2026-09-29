// One reducer for every authorized public-item source.
export const PUBLIC_KINDS = new Set(['user','answer','notice','failure','attachment','component']);

export function emptyState(sessionId) {
  return { sessionId, items: [], cursor: 0, runs: [], has_more: false, schema_version: 1 };
}

export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key)+':'+canonical(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

// IndexedDB writes from two tabs can arrive out of order even at the same
// public cursor. Preserve the furthest known run phase without changing the
// committed public projection.
export function mergeCachedState(stored, incoming) {
  if (!stored || stored.cursor < incoming.cursor) return incoming;
  if (stored.cursor > incoming.cursor) return stored;
  if (canonical(stored.items) !== canonical(incoming.items)) {
    throw new Error('Conflicting public cache at one cursor');
  }
  const phase = {queued:0,running:1,finalizing:2,stopping:3,
    completed:4,cancelled:4,failed:4};
  const runs = new Map((stored.runs || []).map(run => [run.id,run]));
  for (const run of incoming.runs || []) {
    const old = runs.get(run.id);
    if (!old) {runs.set(run.id,run);continue;}
    const oldRank=phase[old.state], newRank=phase[run.state];
    if (oldRank === undefined || newRank === undefined) throw new Error('Unknown cached run phase');
    if (oldRank === 4 && newRank === 4 && old.state !== run.state) {
      throw new Error('Conflicting terminal run state');
    }
    if (newRank >= oldRank) runs.set(run.id,run);
  }
  return {...incoming,runs:[...runs.values()]};
}

function validateItem(sessionId, item) {
  if (!item || typeof item.id !== 'string' || !item.id || item.session_id !== sessionId
      || !PUBLIC_KINDS.has(item.kind) || item.schema_version !== 1
      || !Number.isSafeInteger(item.position) || item.position < 1
      || !Number.isSafeInteger(item.revision) || item.revision < 1
      || typeof item.deleted !== 'boolean' || typeof item.terminal !== 'boolean'
      || !item.payload || typeof item.payload !== 'object' || Array.isArray(item.payload)) {
    throw new Error('Invalid public conversation item');
  }
}
function validateCursor(cursor) {
  if (!Number.isSafeInteger(cursor) || cursor < 0) throw new Error('Invalid public cursor');
}
function upsert(state, item, snapshot = false) {
  validateItem(state.sessionId,item);
  const old = state.items.find(row => row.id === item.id);
  if (old) {
    if (item.position !== old.position || item.kind !== old.kind || item.run_id !== old.run_id) {
      throw new Error('Public item identity changed');
    }
    if (item.revision < old.revision) return state;
    if (item.revision === old.revision) {
      if (canonical(old) !== canonical(item)) throw new Error('Conflicting public revision');
      return state;
    }
    if (!snapshot && item.revision !== old.revision + 1) throw new Error('Missing public revision');
    if (!item.deleted && item.kind !== 'component') throw new Error('Immutable publication changed');
  } else if (!snapshot && item.revision !== 1) {
    throw new Error('Missing initial public revision');
  }
  if (state.items.some(row => row.id !== item.id && row.position === item.position)) {
    throw new Error('Public position is already occupied');
  }
  const items = state.items.filter(row => row.id !== item.id).concat(item)
    .sort((a,b) => a.position - b.position);
  let runs = state.runs;
  if (item.terminal && item.run_id) {
    const outcome = item.payload.system_outcome || 'completed';
    if (!['completed','cancelled','failed'].includes(outcome)) throw new Error('Invalid terminal outcome');
    const existing = runs.find(run => run.id === item.run_id) || {id:item.run_id};
    runs = runs.filter(run => run.id !== item.run_id).concat({...existing,state:outcome});
  }
  return { ...state, items, runs };
}

export function applyPublication(state, event) {
  validateCursor(event?.cursor);
  if (event.cursor < state.cursor) return state;
  if (event.cursor === state.cursor) {
    const old = state.items.find(row => row.id === event.item?.id);
    if (old && canonical(old) === canonical(event.item)) return state;
    throw new Error('Conflicting public cursor');
  }
  return { ...upsert(state,event.item), cursor:event.cursor };
}

export function applySnapshot(sessionId, snapshot) {
  if (snapshot?.schema_version !== 1 || !Array.isArray(snapshot.items) || !Array.isArray(snapshot.runs)) {
    throw new Error('Unsupported conversation snapshot');
  }
  validateCursor(snapshot.cursor);
  let state = {...emptyState(sessionId),runs:snapshot.runs};
  for (const item of snapshot.items) state = upsert(state,item,true);
  return { ...state, cursor:snapshot.cursor, has_more:!!snapshot.has_more };
}
