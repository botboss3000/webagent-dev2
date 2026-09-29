import {apiPath} from '../../shared/js/config.js';
import {authHeaders} from '../../shared/js/left-login.js';
import {createChatClient} from './transport.js';
import {mergeCachedState} from './state.js';

let identity=null, database=null, client=null, opening=null, epoch=0;
const API='/api/v2/chat';
const status=detail=>window.dispatchEvent(new CustomEvent('webagent-chat-send-status',{detail}));
async function request(path,options={}) {
  return fetch(apiPath(API+path),{...options,
    headers:{...authHeaders(),'Content-Type':'application/json',...(options.headers || {})}});
}
function storage(db) {
  function tx(name,mode,operation) {
    return new Promise((resolve,reject)=>{
      const transaction=db.transaction(name,mode),store=transaction.objectStore(name);
      let result;
      operation(store,value=>{result=value;});
      transaction.oncomplete=()=>resolve(result);
      transaction.onerror=()=>reject(transaction.error);
      transaction.onabort=()=>reject(transaction.error || new Error('Cache transaction aborted'));
    });
  }
  function op(name,mode,action) {
    return tx(name,mode,(store,set)=>{const request=action(store);request.onsuccess=()=>set(request.result);});
  }
  return {
    getState:id=>op('sessions','readonly',s=>s.get(id)),
    removeState:id=>op('sessions','readwrite',s=>s.delete(id)),
    putState:state=>tx('sessions','readwrite',(store,set)=>{
      const read=store.get(state.sessionId);
      read.onsuccess=()=>{
        // A same-cursor write may still carry an older run phase. Reconcile it
        // under the IndexedDB transaction before exposing it to subscribers.
        const next=mergeCachedState(read.result,state);
        store.put(next,state.sessionId);set(next);
      };
    }),
    getPending:id=>op('outbox','readonly',s=>s.get(id)),
    putPending:entry=>op('outbox','readwrite',s=>s.put(entry)),
    removePending:id=>op('outbox','readwrite',s=>s.delete(id)),
    allPending:()=>op('outbox','readonly',s=>s.getAll()),
  };
}
function invalidate({purge=false}={}) {
  epoch++;
  client?.dispose();client=null;
  const name=database?.name;
  database?.close();database=null;
  if(purge && identity) localStorage.removeItem('webagent:v2:identity:'+identity.user_id);
  identity=null;opening=null;
  if(purge && name) indexedDB.deleteDatabase(name);
}
export async function initialize(userId) {
  if(!userId) throw new Error('Chat requires an authenticated subject');
  if(identity?.user_id===userId && client) return identity;
  if(opening?.userId===userId) return opening.promise;
  invalidate();
  const generation=epoch;
  const ensure=()=>{if(generation!==epoch) throw new Error('Chat identity changed');};
  const promise=(async()=>{
    const key='webagent:v2:identity:'+userId;
    let subject;
    try {
      const response=await request('/identity');
      if(!response.ok) {const e=new Error('Identity unavailable');e.status=response.status;throw e;}
      subject=await response.json();
      ensure();
      if(subject.user_id!==userId || subject.schema_version!==1) throw new Error('Unexpected chat identity');
      localStorage.setItem(key,JSON.stringify(subject));
    } catch(error) {
      ensure();
      if(error.status || error.message==='Unexpected chat identity') throw error;
      subject=JSON.parse(localStorage.getItem(key)||'null');
      if(subject?.user_id!==userId || subject.schema_version!==1) throw error;
    }
    const name='webagent:chat-v2:'+subject.instance_id+':'+userId+':1';
    const db=await new Promise((resolve,reject)=>{
      const req=indexedDB.open(name,1);
      req.onupgradeneeded=()=>{
        req.result.createObjectStore('sessions');
        req.result.createObjectStore('outbox',{keyPath:'id'});
      };
      req.onerror=()=>reject(req.error);
      req.onsuccess=()=>resolve(req.result);
    });
    if(generation!==epoch) {db.close();throw new Error('Chat identity changed');}
    db.onversionchange=()=>{db.close();if(database===db) invalidate();};
    database=db;identity=subject;
    client=createChatClient({request,cache:storage(db),onStatus:status});
    return subject;
  })();
  opening={userId,promise};
  try {return await promise;} finally {if(opening?.promise===promise) opening=null;}
}
function active() {if(!client) throw new Error('Chat is not initialized');return client;}
export const command=(...args)=>active().command(...args);
export const subscribe=(...args)=>active().subscribe(...args);
export const stateFor=(...args)=>active().stateFor(...args);
export const openSession=(...args)=>active().openSession(...args);
export const closeSession=(...args)=>client?.closeSession(...args);
export const queueMessage=(...args)=>active().queueMessage(...args);
export const pendingMessages=(...args)=>active().pendingMessages(...args);
export const drainOutbox=(...args)=>active().drainOutbox(...args);
window.addEventListener('online',()=>{if(client) client.drainOutbox().catch(()=>{});});
window.addEventListener('webagent-browser-storage-purge',()=>invalidate({purge:true}));
document.addEventListener('webagent-auth-transition-start',()=>invalidate({purge:true}));
