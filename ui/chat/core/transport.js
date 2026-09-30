import {applyPublication, applySnapshot, mergeLatestSnapshot, prependHistory, canonical, emptyState} from './state.js';

// Each client owns one authenticated subject and instance. Storage is injected
// so browser persistence and deterministic delivery tests exercise the same code.
export function createChatClient({request, cache, onStatus = () => {}}) {
  const states = new Map(), listeners = new Map(), streams = new Map(), lanes = new Map();
  let alive = true, draining = null;
  function assertAlive() { if (!alive) throw new Error('Chat identity expired'); }
  function serial(id, operation) {
    const next = (lanes.get(id) || Promise.resolve()).catch(()=>{}).then(() => {
      assertAlive(); return operation();
    });
    lanes.set(id,next);
    return next;
  }
  function emit(state) {
    assertAlive();
    states.set(state.sessionId,state);
    for (const fn of listeners.get(state.sessionId) || []) fn(state);
  }
  async function save(state, controller) {
    assertAlive();
    if (controller?.signal.aborted) return;
    const committed = await cache.putState(state);
    assertAlive();
    if (!controller?.signal.aborted) emit(committed || state);
    return committed || state;
  }
  const path = id => '/sessions/' + encodeURIComponent(id);
  async function json(url, options) {
    assertAlive();
    const response = await request(url, options);
    assertAlive();
    if (!response.ok) {
      const error = new Error('Chat request failed ('+response.status+')');
      error.status=response.status;
      throw error;
    }
    return response;
  }
  async function command(url, body) {
    return (await json(url,{method:'POST',body:JSON.stringify(body)})).json();
  }
  function stateFor(id) { return states.get(id) || emptyState(id); }
  function subscribe(id, fn) {
    assertAlive();
    if (!listeners.has(id)) listeners.set(id,new Set());
    listeners.get(id).add(fn);
    if (states.has(id)) fn(states.get(id));
    return () => listeners.get(id)?.delete(fn);
  }
  function closeSession(id) {
    streams.get(id)?.abort();
    streams.delete(id);
  }
  async function revoke(id) {
    closeSession(id);
    await serial(id,async()=>{
      await cache.removeState(id);
      if (alive) emit({...emptyState(id),revoked:true});
    });
  }
  async function replace(id, controller) {
    const body=await (await json(path(id),{signal:controller?.signal})).json();
    return serial(id,()=>save(mergeLatestSnapshot(stateFor(id),body),controller));
  }
  async function loadOlder(id) {
    assertAlive();
    const controller=streams.get(id);
    const state=stateFor(id);
    if (!controller || controller.signal.aborted || !state.has_more || !state.items.length) return state;
    const before=state.items[0].position;
    const body=await (await json(path(id)+'?before='+before,{signal:controller.signal})).json();
    return serial(id,()=>{
      const current=stateFor(id);
      if (controller.signal.aborted || streams.get(id)!==controller ||
          !current.has_more || current.items[0]?.position!==before) return current;
      return save(prependHistory(current,body),controller);
    });
  }
  async function openSession(id, {fresh = true, subscribe: live = true} = {}) {
    assertAlive();
    closeSession(id);
    const controller=new AbortController();
    streams.set(id,controller);
    const current=()=>alive && !controller.signal.aborted && streams.get(id)===controller;
    try {
      const cached=states.get(id) || await cache.getState(id);
      assertAlive();
      if (!current()) return stateFor(id);
      if (cached) emit(applySnapshot(id,cached));
      try {
        const state = fresh ? await replace(id,controller) : stateFor(id);
        if (current() && live) follow(id,controller);
        return state;
      } catch(error) {
        if (!current()) return stateFor(id);
        if ([401,403,404].includes(error.status)) { await revoke(id); throw error; }
        if (!cached) throw error;
        if (live) follow(id,controller);
        return stateFor(id);
      }
    } finally {
      if (!live && streams.get(id)===controller) streams.delete(id);
    }
  }
  async function receive(id, type, payload, controller) {
    if (!alive || controller?.signal.aborted) return;
    if (type==='revoked') { await revoke(id); return; }
    if (type==='reset') { await replace(id,controller); return; }
    await serial(id,async()=>{
      if (controller?.signal.aborted) return;
      const previous=stateFor(id);
      if (type==='publication') {
        await save(applyPublication(previous,payload),controller);
      } else if (type==='state') {
        // A committed terminal publication also governs controls. A delayed
        // progress frame must never make a completed run active again.
        const terminal=new Map(previous.runs.filter(r=>['completed','failed','cancelled'].includes(r.state))
          .map(r=>[r.id,r]));
        const runs=(payload.runs || []).map(r=>({...r,...(terminal.get(r.id) || {})}));
        if (canonical(previous.runs)!==canonical(runs)) await save({...previous,runs},controller);
      }
    });
  }
  async function consume(response,controller,id) {
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let buffer='';
    try {
      while(!controller.signal.aborted) {
        const {done,value}=await reader.read();
        if(done) break;
        buffer+=decoder.decode(value,{stream:true});
        let match;
        while((match=/\r?\n\r?\n/.exec(buffer))) {
          const block=buffer.slice(0,match.index);buffer=buffer.slice(match.index+match[0].length);
          let type='message';const data=[];
          for(const line of block.split(/\r?\n/)) {
            if(line.startsWith('event:')) type=line.slice(6).trim();
            if(line.startsWith('data:')) data.push(line.slice(5).trimStart());
          }
          if(data.length) await receive(id,type,JSON.parse(data.join('\n')),controller);
        }
      }
    } finally { await reader.cancel().catch(()=>{}); reader.releaseLock(); }
  }
  function follow(id,controller) {
    if(!alive || controller.signal.aborted || streams.get(id)!==controller) return;
    (async()=>{
      let delay=0;
      while(alive && !controller.signal.aborted) {
        try {
          const response=await json(path(id)+'/events?after='+stateFor(id).cursor,{signal:controller.signal});
          await consume(response,controller,id);
          delay=0;
        } catch(error) {
          if(controller.signal.aborted || !alive) break;
          if([401,403,404].includes(error.status)) { await revoke(id); break; }
          // A missing/conflicting revision requires authoritative replacement,
          // never text-length or timestamp reconciliation.
          try { await replace(id,controller); } catch (_) {}
          delay=Math.min(8000,Math.max(250,delay*2));
        }
        if(alive && !controller.signal.aborted) await new Promise(resolve=>{
          const finish=()=>{
            clearTimeout(timer);
            controller.signal.removeEventListener('abort',finish);
            resolve();
          };
          const timer=setTimeout(finish,delay);
          controller.signal.addEventListener('abort',finish,{once:true});
        });
      }
      if(streams.get(id)===controller) streams.delete(id);
    })().catch(error=>{if(alive) onStatus({sessionId:id,status:'unavailable',error});});
  }
  async function queueMessage(sessionId,agentId,message) {
    assertAlive();
    const afterPosition=stateFor(sessionId).items.reduce((position,item)=>
      Math.max(position,item.position),0);
    const entry={id:sessionId+':'+message.client_message_id,sessionId,agentId,
                 message,createdAt:Date.now(),afterPosition,status:'pending'};
    const old=await cache.getPending(entry.id);
    assertAlive();
    if(old && JSON.stringify(old.message)!==JSON.stringify(message)) throw new Error('Pending message identity conflict');
    if(!old) await cache.putPending(entry);
    assertAlive();
    onStatus({sessionId,id:message.client_message_id,status:'pending'});
    return old || entry;
  }
  async function pendingMessages(id) {
    assertAlive();
    return (await cache.allPending()).filter(row=>row.sessionId===id).sort((a,b)=>a.createdAt-b.createdAt);
  }
  async function drainOutbox() {
    if(draining) return draining;
    draining=(async()=>{
      const entries=(await cache.allPending()).sort((a,b)=>a.createdAt-b.createdAt);
      for(const entry of entries) {
        assertAlive();
        if(entry.status==='rejected') continue;
        try {
          await command('/sessions',{id:entry.sessionId,agent_id:entry.agentId});
          const receipt=await command(path(entry.sessionId)+'/messages',entry.message);
          assertAlive();
          await cache.removePending(entry.id);
          assertAlive();
          onStatus({sessionId:entry.sessionId,id:entry.message.client_message_id,status:'accepted',receipt});
          await openSession(entry.sessionId);
        } catch(error) {
          if(!alive) return;
          const rejected=error.status && error.status<500 && error.status!==429;
          if(rejected) await cache.putPending({...entry,status:'rejected',error:error.status});
          assertAlive();
          onStatus({sessionId:entry.sessionId,id:entry.message.client_message_id,
                    status:rejected?'rejected':'offline'});
          if(!rejected) break;
        }
      }
    })();
    try { await draining; } finally {draining=null;}
  }
  function dispose() {
    alive=false;
    for(const controller of streams.values()) controller.abort();
    streams.clear();listeners.clear();states.clear();
  }
  return {command,stateFor,subscribe,openSession,closeSession,loadOlder,queueMessage,pendingMessages,
          drainOutbox,dispose,receive};
}
