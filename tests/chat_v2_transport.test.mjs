import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createChatClient} from '../ui/chat/core/transport.js';

function cache() {
  const states=new Map(),pending=new Map();
  return {states,pending,
    async getState(id){return states.get(id);},
    async putState(state){const old=states.get(state.sessionId);if(old?.cursor>state.cursor)return old;
      states.set(state.sessionId,structuredClone(state));return state;},
    async removeState(id){states.delete(id);},
    async getPending(id){return pending.get(id);},
    async putPending(row){pending.set(row.id,structuredClone(row));},
    async removePending(id){pending.delete(id);},
    async allPending(){return [...pending.values()];},
  };
}
const row=(id='u',position=1,extra={})=>({id,session_id:'s',run_id:'r',position,revision:1,
  schema_version:1,kind:'user',payload:{text:'hello'},created_at:'2026-09-28',
  deleted:false,terminal:false,...extra});
const snap=(items=[],cursor=items.length)=>({schema_version:1,items,cursor,runs:[],has_more:false});
const response=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function idle(signal) {
  return new Response(new ReadableStream({start(controller){
    signal.addEventListener('abort',()=>controller.close(),{once:true});
  }}));
}

test('cached items render before a slow authoritative snapshot',async()=>{
  const store=cache();await store.putState({...snap([row()]),sessionId:'s'});
  const network=deferred(),shown=[];
  const client=createChatClient({cache:store,request:()=>network.promise});
  client.subscribe('s',value=>shown.push(value));
  const opened=client.openSession('s',{subscribe:false});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(shown[0].items[0].id,'u');
  network.resolve(response(snap([row(),row('answer',2,{kind:'answer',terminal:true})])));
  await opened;
  assert.equal(shown.at(-1).items.length,2);
  assert.equal(store.states.get('s').cursor,2);
  client.dispose();
});
test('account disposal fences a snapshot already in flight',async()=>{
  const store=cache(),network=deferred();
  const client=createChatClient({cache:store,request:()=>network.promise});
  const opened=client.openSession('s',{subscribe:false});
  client.dispose();network.resolve(response(snap([row()])));
  await assert.rejects(opened);
  assert.equal(store.states.size,0);
});
test('revocation removes cached content and informs existing renderers',async()=>{
  const store=cache(),seen=[];
  const client=createChatClient({cache:store,request:async()=>response(snap([row()]))});
  client.subscribe('s',state=>seen.push(state));
  await client.openSession('s',{subscribe:false});
  await client.receive('s','revoked',{});
  assert.equal(store.states.has('s'),false);
  assert.equal(seen.at(-1).revoked,true);
  assert.deepEqual(seen.at(-1).items,[]);
  client.dispose();
});
test('publication and cursor are committed together, duplicate delivery is harmless',async()=>{
  const store=cache();
  const client=createChatClient({cache:store,request:async()=>response(snap())});
  await client.openSession('s',{subscribe:false});
  await Promise.all([client.receive('s','publication',{cursor:1,item:row()}),
                     client.receive('s','publication',{cursor:1,item:row()})]);
  assert.equal(client.stateFor('s').items.length,1);
  assert.equal(store.states.get('s').cursor,1);
  client.dispose();
});
test('delayed progress cannot revive controls after terminal publication',async()=>{
  const client=createChatClient({cache:cache(),request:async()=>response(snap())});
  await client.openSession('s',{subscribe:false});
  await client.receive('s','state',{runs:[{id:'r',state:'running'}]});
  await client.receive('s','publication',{cursor:1,item:row('a',1,{kind:'answer',terminal:true})});
  await client.receive('s','state',{runs:[{id:'r',state:'running'}]});
  assert.equal(client.stateFor('s').runs[0].state,'completed');
  client.dispose();
});
test('terminal compaction keeps its run kind when publication precedes state',async()=>{
  const client=createChatClient({cache:cache(),request:async()=>response(snap())});
  await client.openSession('s',{subscribe:false});
  await client.receive('s','publication',{cursor:1,item:row('result',1,{
    kind:'notice',terminal:true,payload:{text:'Nothing eligible',compaction:{status:'no_op'}}})});
  await client.receive('s','state',{runs:[{id:'r',kind:'compaction',state:'completed'}]});
  assert.deepEqual(client.stateFor('s').runs[0],{id:'r',kind:'compaction',state:'completed'});
  client.dispose();
});

test('submission stores the public boundary before later events arrive',async()=>{
  const store=cache();
  const client=createChatClient({cache:store,request:async()=>response(snap([
    row('first',1),row('answer',2,{kind:'answer'})]))});
  await client.openSession('s',{subscribe:false});
  const queued=await client.queueMessage('s','agent',{client_message_id:'next',text:'hello'});
  assert.equal(queued.afterPosition,2);
  await client.receive('s','publication',{cursor:3,item:row('update',3,{kind:'answer'})});
  assert.equal((await client.pendingMessages('s'))[0].afterPosition,2);
  client.dispose();
});
test('offline submissions survive client recreation and retry their original identity',async()=>{
  const store=cache(),sent=[];
  const first=createChatClient({cache:store,request:async()=>{throw new TypeError('offline');}});
  await first.queueMessage('s','agent',{client_message_id:'one',text:'hello'});
  await first.drainOutbox();first.dispose();
  assert.equal(store.pending.size,1);
  const second=createChatClient({cache:store,request:async(path,options)=>{
    if(path.includes('/events')) return idle(options.signal);
    if(path.endsWith('/messages')) {sent.push(JSON.parse(options.body));return response({message_id:'u',cursor:1});}
    if(path==='/sessions') return response({id:'s'});
    return response(snap([row()]));
  }});
  await second.drainOutbox();
  assert.equal(store.pending.size,0);
  assert.equal(sent[0].client_message_id,'one');
  assert.equal(second.stateFor('s').items.length,1);
  second.dispose();
});
test('fragmented SSE delivers one complete validated item',async()=>{
  const store=cache(),delivered=deferred();
  const client=createChatClient({cache:store,request:async(path)=>{
    if(!path.includes('/events'))return response(snap());
    const wire='event: publication\r\ndata: '+JSON.stringify({cursor:1,item:row()})+'\r\n\r\n';
    return new Response(new ReadableStream({start(controller){
      for(let index=0;index<wire.length;index+=7) controller.enqueue(new TextEncoder().encode(wire.slice(index,index+7)));
      controller.close();
    }}));
  }});
  client.subscribe('s',state=>{if(state.items.length){delivered.resolve(state);client.dispose();}});
  await client.openSession('s');
  const state=await delivered.promise;
  assert.equal(state.items[0].id,'u');
  assert.equal(store.states.get('s').cursor,1);
});

test('closing during a snapshot prevents rendering and reconnecting the old session',async()=>{
  const store=cache(),network=deferred(),started=deferred(),requests=[],seen=[];
  const client=createChatClient({cache:store,request:async(path,options)=>{
    requests.push(path);started.resolve(options.signal);return network.promise;
  }});
  client.subscribe('s',state=>seen.push(state));
  const opened=client.openSession('s');
  const signal=await started.promise;
  client.closeSession('s');
  assert.equal(signal.aborted,true);
  network.resolve(response(snap([row()])));
  await opened;
  assert.equal(store.states.size,0);
  assert.equal(seen.length,0);
  assert.equal(requests.length,1);
  client.dispose();
});
test('a second open owns the session even when the first request finishes last',async()=>{
  const store=cache(),first=deferred(),started=deferred();let calls=0;
  const client=createChatClient({cache:store,request:async()=>{
    if(++calls===1){started.resolve();return first.promise;}
    return response(snap([row('new')]));
  }});
  const old=client.openSession('s',{subscribe:false});
  await started.promise;
  await client.openSession('s',{subscribe:false});
  first.resolve(response(snap([row('old')])));
  await old;
  assert.equal(client.stateFor('s').items[0].id,'new');
  assert.equal(store.states.get('s').items[0].id,'new');
  client.dispose();
});
test('logout during pending lookup cannot write a submission into the old account',async()=>{
  const store=cache(),lookup=deferred();
  store.getPending=()=>lookup.promise;
  const client=createChatClient({cache:store,request:async()=>response({})});
  const queued=client.queueMessage('s','agent',{client_message_id:'one',text:'hello'});
  client.dispose();lookup.resolve(undefined);
  await assert.rejects(queued,/identity expired/);
  assert.equal(store.pending.size,0);
});
test('logout during pending removal suppresses accepted UI callbacks',async()=>{
  const store=cache(),removing=deferred(),started=deferred(),seen=[];
  const client=createChatClient({cache:store,onStatus:e=>seen.push(e),request:async()=>response({})});
  await client.queueMessage('s','agent',{client_message_id:'one',text:'hello'});
  store.removePending=()=>{started.resolve();return removing.promise;};
  const draining=client.drainOutbox();
  await started.promise;client.dispose();removing.resolve();
  await draining;
  assert.deepEqual(seen.map(e=>e.status),['pending']);
});

test('bounded event responses reconnect immediately at the committed cursor',async()=>{
  const store=cache(),reconnected=deferred(),requests=[];
  const client=createChatClient({cache:store,request:async(path,options)=>{
    if(!path.includes('/events')) return response(snap());
    requests.push(path);
    if(path.includes('after=0')) return new Response(
      'event: publication\ndata: '+JSON.stringify({cursor:1,item:row()})+'\n\n');
    reconnected.resolve(path);
    return new Promise((_,reject)=>options.signal.addEventListener('abort',
      ()=>reject(new Error('closed')),{once:true}));
  }});
  await client.openSession('s');
  assert.equal(await reconnected.promise,'/sessions/s/events?after=1');
  assert.equal(store.states.get('s').cursor,1);
  assert.equal(requests.length,2);
  client.dispose();
});

test('older page loads once, then live publication keeps loaded history',async()=>{
  const store=cache(),calls=[];
  const client=createChatClient({cache:store,request:async(path)=>{
    calls.push(path);
    if(path.includes('?before=3')) return response({...snap([row('one',1),row('two',2)],4),has_more:false});
    return response({...snap([row('three',3),row('four',4)],4),has_more:true});
  }});
  await client.openSession('s',{subscribe:false});
  // A non-subscribed open releases its controller; reopen live to page.
  await client.openSession('s',{fresh:false});
  await client.loadOlder('s');
  assert.deepEqual(client.stateFor('s').items.map(item=>item.id),['one','two','three','four']);
  await client.receive('s','publication',{cursor:5,item:row('five',5)});
  assert.deepEqual(client.stateFor('s').items.map(item=>item.id),['one','two','three','four','five']);
  assert.equal(calls.filter(path=>path.includes('?before=3')).length,1);
  client.dispose();
});
