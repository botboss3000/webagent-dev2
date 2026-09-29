import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mergeActivity, activityGroups, watchActivity} from '../ui/chat/core/activity.js';
const event=(seq,kind,payload={})=>({seq,kind,payload,session_id:'s',run_id:'r',event_id:'e'+seq});
const segment=(id,first,last,closed=0)=>({id,run_id:'r',boundary_item_id:id,
  first_seq:first,last_seq:last,closed});
const empty=()=>({events:[],segments:[],cursor:0});
test('pages deduplicate and preserve complete tool/result and message counts after reload',()=>{
 const events=[event(1,'tool_call',{tool:'read',tool_call_id:'t'}),
  event(2,'tool_result',{tool:'read',tool_call_id:'t',result:'ok'}),
  event(3,'agent_step_end',{asst_id:'a',content:'First'}),
  event(4,'response',{asst_id:'a',content:'Final'})];
 let live=mergeActivity(empty(),{events:events.slice(0,2),cursor:2,segments:[segment('u',1,2)]},'s');
 live=mergeActivity(live,{events, cursor:4,segments:[segment('u',1,4,1)]},'s');
 const restored=mergeActivity(empty(),{events,cursor:4,segments:[segment('u',1,4,1)]},'s');
 assert.deepEqual(activityGroups(live,[]),activityGroups(restored,[]));
 const group=activityGroups(live,[])[0];
 assert.deepEqual(group.counts,{tools:1,updates:0,responses:1,modes:0,compactions:0});
 assert.equal(group.rows[0].events.length,2);
 assert.equal(group.rows[1].text,'Final');
});
test('interrupt boundaries keep activity on the correct side of each user message',()=>{
 const journal={events:[event(1,'tool_call'),event(2,'agent_step_end'),event(3,'tool_call')],
  segments:[segment('first-user',1,2,1),segment('interrupt',3,3)],cursor:3};
 const groups=activityGroups(journal,[{id:'r',state:'running'}]);
 assert.deepEqual(groups.map(g=>g.events.map(e=>e.seq)),[[1,2],[3]]);
 assert.deepEqual(groups.map(g=>g.state),['finished','running']);
});
test('only the last segment reports a terminal run outcome',()=>{
 const journal={events:[event(1,'tool_call'),event(2,'tool_result')],
  segments:[segment('u1',1,1,1),segment('u2',2,2,1)],cursor:2};
 for(const outcome of ['completed','failed','cancelled']) {
  const groups=activityGroups(journal,[{id:'r',state:outcome}]);
  assert.deepEqual(groups.map(g=>g.state),['finished',outcome]);
 }
 const active=activityGroups(journal,[{id:'r',state:'finalizing'}]);
 assert.deepEqual(active.map(g=>g.state),['finished','finished']);
});
test('reject foreign-session data and conflicting replay without mutating prior state',()=>{
 const previous=mergeActivity(empty(),{events:[event(1,'tool_call')],segments:[],cursor:1},'s');
 assert.throws(()=>mergeActivity(previous,{events:[{...event(2,'tool_result'),session_id:'other'}],
  segments:[],cursor:2},'s'));
 assert.throws(()=>mergeActivity(previous,{events:[event(1,'response')],segments:[],cursor:1},'s'));
 assert.equal(previous.events.length,1);
});
test('watcher drains all pages and stops without leaking a delayed response after disconnect',async()=>{
 const pages=[{events:[event(1,'tool_call')],segments:[],cursor:1,has_more:true},
  {events:[event(2,'tool_result')],segments:[],cursor:2,has_more:false}];
 const changes=[],requests=[],scheduled=[];
 const stop=watchActivity({sessionId:'s',request:async cursor=>{
  requests.push(cursor);return {ok:true,json:async()=>pages.shift()};},
  onChange:s=>changes.push(s),onError:e=>{throw e;},schedule:fn=>{scheduled.push(fn);return 1;},cancel(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(requests,[0,1]);assert.equal(changes.at(-1).events.length,2);
 assert.equal(scheduled.length,1);stop();
 let release;let leaked=0;
 const disconnect=watchActivity({sessionId:'s',request:()=>new Promise(r=>{release=r;}),
  onChange:()=>leaked++,onError:()=>leaked++});
 disconnect();release({ok:true,json:async()=>pages[0]});
 await new Promise(resolve=>setImmediate(resolve));assert.equal(leaked,0);
});
test('denied diagnostic access stops polling and surfaces the permission result',async()=>{
 let errors=[],polls=0;
 watchActivity({sessionId:'s',request:async()=>({ok:false,status:403}),
  onChange:()=>assert.fail('Private data rendered'),onError:e=>errors.push(e.status),
  schedule:()=>{polls++;}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(errors,[403]);assert.equal(polls,0);
});

test('late stale events remain inspectable without inflating semantic counters',()=>{
 const journal={events:[event(1,'tool_call'),event(2,'stale:tool_call')],
  segments:[segment('u',1,1,1)],cursor:2};
 const group=activityGroups(journal,[{id:'r',state:'completed'}])[0];
 assert.equal(group.events.length,2);
 assert.equal(group.counts.tools,1);
});
