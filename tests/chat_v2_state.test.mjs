import {test} from 'node:test';
import assert from 'node:assert/strict';
import {applyPublication,applySnapshot,emptyState,mergeCachedState} from '../ui/chat/core/state.js';

const item = (id,position,extra={}) => ({
  id,session_id:'s',run_id:'r',position,revision:1,schema_version:1,
  kind:'notice',payload:{text:id},created_at:'2026-09-28',deleted:false,terminal:false,...extra,
});
const event = (row,cursor=row.position) => ({item:row,cursor});
const snapshot = items => ({schema_version:1,cursor:items.length,items,runs:[],has_more:false});

test('live, snapshot, serialized cache and duplicate replay are identical',()=>{
  const rows=[item('u',1,{kind:'user'}),item('n',2),item('a',3,{kind:'answer',terminal:true})];
  const live=rows.reduce((state,row)=>applyPublication(state,event(row)),emptyState('s'));
  const refreshed=applySnapshot('s',snapshot(rows));
  assert.deepEqual(live,refreshed);
  const cached=JSON.parse(JSON.stringify(live));
  assert.deepEqual(applyPublication(cached,event(rows[2])),refreshed);
  assert.equal(live.runs[0].state,'completed');
});
test('object property order cannot create a conflict',()=>{
  const row=item('a',1);
  const state=applyPublication(emptyState('s'),event(row));
  const reordered=Object.fromEntries(Object.entries(row).reverse());
  assert.deepEqual(applyPublication(state,event(reordered)),state);
});
test('late replay cannot resurrect items removed by authoritative replacement',()=>{
  const state=applySnapshot('s',{...snapshot([]),cursor:20});
  assert.deepEqual(applyPublication(state,event(item('old',1),10)),state);
});
test('conflicting identities, revisions and positions fail closed',()=>{
  const state=applyPublication(emptyState('s'),event(item('one',1)));
  for (const row of [item('two',1),item('one',2),item('one',1,{revision:3}),
                     item('one',1,{payload:{text:'changed'}})]) {
    assert.throws(()=>applyPublication(state,event(row,2)));
  }
});
test('ordinary text cannot be revised',()=>{
  const state=applyPublication(emptyState('s'),event(item('one',1)));
  assert.throws(()=>applyPublication(state,event(item('one',1,{revision:2}),2)));
});
test('component revisions and tombstones survive snapshot replacement',()=>{
  const first=item('c',1,{kind:'component',payload:{type:'choice',state:'open'}});
  const revised={...first,revision:2,payload:{type:'choice',state:'answered'}};
  let state=applyPublication(emptyState('s'),event(first));
  state=applyPublication(state,event(revised,2));
  assert.deepEqual(state,applySnapshot('s',{...snapshot([revised]),cursor:2}));
  state=applyPublication(state,event({...revised,revision:3,deleted:true},3));
  assert.equal(state.items[0].deleted,true);
});
test('unknown items, foreign sessions and invalid cursors are never accepted',()=>{
  for(const row of [item('x',1,{kind:'raw_tool'}),item('x',1,{session_id:'other'}),
                    item('x',0),item('x',1,{payload:null})]) {
    assert.throws(()=>applyPublication(emptyState('s'),event(row,1)));
  }
  assert.throws(()=>applySnapshot('s',{...snapshot([]),cursor:-1}));
});
test('terminal failure clears active run in the same publication reduction',()=>{
  const state={...emptyState('s'),runs:[{id:'r',state:'running'}]};
  const next=applyPublication(state,event(item('f',1,{kind:'failure',terminal:true,
    payload:{text:'Safe notice',system_outcome:'failed'}})));
  assert.equal(next.runs[0].state,'failed');
});

test('two tabs cannot replace a terminal cached run with an older phase',()=>{
  const published=applyPublication({...emptyState('s'),runs:[{id:'r',state:'running'}]},
    event(item('a',1,{kind:'answer',terminal:true})));
  const stale={...published,runs:[{id:'r',state:'running'}]};
  const merged=mergeCachedState(published,stale);
  assert.equal(merged.runs[0].state,'completed');
  assert.deepEqual(merged.items,published.items);
  assert.equal(mergeCachedState(merged,{...stale,cursor:0}).cursor,1);
});
test('same-cursor cache merges progress but rejects conflicting public content',()=>{
  const base={...emptyState('s'),runs:[{id:'r',state:'queued'}]};
  const running={...base,runs:[{id:'r',state:'running'}]};
  assert.equal(mergeCachedState(base,running).runs[0].state,'running');
  assert.equal(mergeCachedState(running,base).runs[0].state,'running');
  assert.throws(()=>mergeCachedState(base,{...base,items:[item('other',1)]}));
  assert.throws(()=>mergeCachedState(
    {...base,runs:[{id:'r',state:'failed'}]},
    {...base,runs:[{id:'r',state:'completed'}]}));
});
