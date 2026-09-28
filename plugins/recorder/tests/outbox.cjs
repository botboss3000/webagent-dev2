// Recorder storage stalls must release Stop without falsely acknowledging evidence.
// Run: node --experimental-vm-modules plugins/recorder/tests/outbox.cjs
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 const timers=new Map();let next=0,request,tx,mode='stall-open';
 const context=vm.createContext({Map,Blob,JSON,Error,Promise,
   setTimeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id),
   indexedDB:{open(){request={};if(mode==='ok')queueMicrotask(()=>{
     request.result={close(){},transaction(){tx={objectStore:()=>({put:()=>({}),getAll:()=>({}),delete:()=>({})}),abort(){tx.onabort?.();}};return tx;}};
     request.onsuccess();
   });return request;}}});
 const m=new vm.SourceTextModule(fs.readFileSync(__dirname+'/../frontend/outbox.js','utf8'),{context});
 await m.link(async()=>{const s=new vm.SourceTextModule('export const browserPersistenceAllowed=()=>true',{context});await s.link(()=>{});return s;});await m.evaluate();
 const tick=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
 const expire=async()=>{for(const fn of [...timers.values()])fn();await tick();};
 let result='pending';
 const write=m.namespace.saveChunk({id:'one',recording_id:'capture',records:[{seq:1}]}).then(()=>result='saved',()=>result='retry');
 await tick();await expire();
 assert.equal(result,'retry','blocked IDB open must release the caller');
 await write;
 mode='ok';result='pending';
 const read=m.namespace.pendingChunks('capture').then(()=>result='ack',()=>result='retry');
 await tick();await expire();await read;
 assert.equal(result,'retry','a stalled durable read must never become an empty acknowledged outbox');
 console.log('PASS blocked IndexedDB open and transaction release callers; unknown durable evidence cannot be acknowledged');
})().catch(e=>{console.error(e);process.exitCode=1});
