'use strict';

// Browser outbox owned by the optional recorder. Pending chunks survive reload;
// upload acknowledgement, never dispatch, is the deletion boundary. This stores
// transcript evidence only; bearer leases remain in tab-local sessionStorage.
import { browserPersistenceAllowed } from '/ui/shared/js/browser-storage-policy.js';
const DB = 'webagent-recorder-outbox-v1';
let dbPromise;
const memory = new Map();
let memoryBytes=0;
const MAX_MEMORY_BYTES=8*1024*1024;
const STORAGE_TIMEOUT_MS=5000;
export async function outbox() {
  if (!browserPersistenceAllowed()) throw new Error('Browser persistence disabled');
  if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    // RECORDER-STORAGE-BOUNDARY: blocked/open and stalled transactions must not
    // hold Stop forever. Late opens are closed; retry may create a fresh handle.
    let settled=false;
    const fail=error=>{
      if(settled)return;
      settled=true;clearTimeout(timer);dbPromise=null;reject(error);
    };
    const timer=setTimeout(()=>fail(new Error('outbox-open-timeout')),STORAGE_TIMEOUT_MS);
    request.onupgradeneeded = () => request.result.createObjectStore('chunks', {keyPath:'id'});
    request.onsuccess = () => {
      if(settled){request.result.close();return;}
      settled=true;clearTimeout(timer);
      request.result.onversionchange=()=>{request.result.close();dbPromise=null;};
      resolve(request.result);
    };
    request.onerror = () => fail(request.error);
    request.onblocked = () => fail(new Error('outbox-open-blocked'));
  });
  return dbPromise;
}
async function operation(mode, action) {
  const db = await outbox();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('chunks', mode);
    const request = action(tx.objectStore('chunks'));
    const timer=setTimeout(()=>{
      reject(new Error('outbox-transaction-timeout'));
      try {tx.abort();} catch (_) {}
    },STORAGE_TIMEOUT_MS);
    tx.oncomplete = () => {clearTimeout(timer);resolve(request.result);};
    tx.onerror = tx.onabort = () => {clearTimeout(timer);reject(tx.error || new Error('outbox-transaction-failed'));};
  });
}
export async function saveChunk(chunk) {
  // Write durable storage first. The emergency memory ceiling must not reject
  // an otherwise healthy IndexedDB write during a burst of render evidence.
  try { await operation('readwrite', store => store.put(chunk)); }
  catch(error) {
    const bytes=new Blob([JSON.stringify(chunk)]).size;
    const previous=memory.get(chunk.id);
    const priorBytes=previous?new Blob([JSON.stringify(previous)]).size:0;
    if(memoryBytes-priorBytes+bytes>MAX_MEMORY_BYTES) throw new Error('outbox-memory-limit');
    memory.set(chunk.id,chunk);memoryBytes+=bytes-priorBytes;
    throw error;
  }

}
export async function pendingChunks(recordingId) {
  let rows = [];
  // An unreadable durable queue is unknown, never empty: do not finalize and
  // strand previously saved events. Disabled persistence uses only memory.
  if (browserPersistenceAllowed()) rows = await operation('readonly', store => store.getAll());
  const merged = new Map(rows.map(row => [row.id,row]));
  for (const [key,row] of memory) merged.set(key,row);
  return [...merged.values()].filter(row => row.recording_id === recordingId).sort((a,b) => a.created_at-b.created_at);
}
export async function deleteChunk(id) {
  try { await operation('readwrite', store => store.delete(id)); } catch (error) {
    if (!memory.has(id)) throw error;
  }
  if (memory.has(id)) memoryBytes-=new Blob([JSON.stringify(memory.get(id))]).size;
  memory.delete(id);
}
