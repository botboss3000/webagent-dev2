import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import {watchComponents,reconcileComponents} from '../ui/chat/core/components.js';

const response=components=>({ok:true,json:async()=>({session_id:'s',components,ui:{active:'plan'}})});
const component=(id,markdown=id)=>({id,type:'status',title:id,data:{markdown},lifecycle:{}});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function harness(read) {
  const applied=[],errors=[],timers=[];let valid=true,clears=0;
  const watcher=watchComponents({read,apply:x=>applied.push(x),clear:()=>clears++,
    current:()=>valid,onError:x=>errors.push(x),schedule:fn=>{timers.push(fn);return timers.length;},cancel:()=>{}});
  return {watcher,applied,errors,timers,invalidate:()=>{valid=false;},clears:()=>clears};
}
test('snapshots carry all plan elements and live checkpoint changes',async()=>{
 let rows=[component('plan'),component('task-progress')];
 const h=harness(async()=>response(rows));
 await h.watcher.refresh();
 rows=[component('plan'),component('task-progress','Updated')];
 await h.timers.at(-1)();
 assert.equal(h.applied[0].components.length,2);
 assert.equal(h.applied[1].components[1].data.markdown,'Updated');
 h.watcher.stop();
});
test('newer refresh fences older responses even if transport ignores abort',async()=>{
 const first=deferred();let calls=0;
 const h=harness(()=>++calls===1?first.promise:Promise.resolve(response([component('new')])));
 const pending=h.watcher.refresh();
 await h.watcher.refresh();
 first.resolve(response([component('old')]));await pending;
 assert.deepEqual(h.applied.map(x=>x.components[0].id),['new']);
 h.watcher.stop();
});
test('session/account transitions and stop discard pending private resources',async()=>{
 for(const stop of [false,true]) {
  const d=deferred(),h=harness(()=>d.promise),pending=h.watcher.refresh();
  if(stop)h.watcher.stop();else h.invalidate();
  d.resolve(response([component('private')]));await pending;
  assert.equal(h.applied.length,0);assert.equal(h.timers.length,0);
 }
});
test('authorization revocation clears resources and stops polling',async()=>{
 for(const status of [401,403,404]) {
  const h=harness(async()=>({ok:false,status}));
  await h.watcher.refresh();
  assert.equal(h.clears(),1);assert.equal(h.timers.length,0);assert.equal(h.applied.length,0);
 }
});
test('temporary failure retries without discarding valid elements',async()=>{
 let fail=true;
 const h=harness(async()=>{if(fail)throw new Error('offline');return response([]);});
 await h.watcher.refresh();assert.equal(h.clears(),0);assert.ok(h.errors[0]);
 fail=false;await h.timers.at(-1)();assert.equal(h.applied.length,1);
 h.watcher.stop();
});
test('reconciliation preserves unchanged nodes and focused drafts, then catches up',()=>{
 const original=component('scratchpad'),previous=new Map([['scratchpad',original]]);
 const rendered=[],removed=[];
 const operations={render:x=>rendered.push(x),remove:x=>removed.push(x),focused:()=>true};
 reconcileComponents(previous,[{...original,updated_at:'new'}],operations);
 assert.equal(rendered.length,0);
 reconcileComponents(previous,[component('scratchpad','remote')],operations);
 assert.equal(rendered.length,0);
 operations.focused=()=>false;
 reconcileComponents(previous,[component('scratchpad','remote')],operations);
 assert.equal(rendered.length,1);
 reconcileComponents(previous,[],operations);
 assert.deepEqual(removed,['scratchpad']);assert.equal(previous.size,0);
});

class Element {
 constructor(){this.children=[];this.dataset={};this.style={};this.isConnected=true;this.parent=null;
  const classes=new Set();this.classList={contains:x=>classes.has(x),
   add:x=>classes.add(x),remove:x=>classes.delete(x),
   toggle:(x,on)=>on?classes.add(x):classes.delete(x)};}
 appendChild(el){this.children.push(el);el.parent=this;return el;}
 append(...els){els.forEach(el=>this.appendChild(el));}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
 replaceChildren(...els){this.children=[];this.append(...els);}
 setAttribute(){}
 addEventListener(){}
 contains(el){return this===el || this.children.some(x=>x.contains(el));}
 querySelectorAll(selector){return this.children.flatMap(el=>[
  ...(selector==='[data-chat-component-id]' && el.dataset.chatComponentId?[el]:[]),
  ...el.querySelectorAll(selector)]);}
 querySelector(selector){
  if(selector==='[data-component-sync-status]') return this.children.find(x=>'componentSyncStatus' in x.dataset);
  const id=selector.match(/data-chat-component-id="([^"]+)"/)?.[1];
  return this.querySelectorAll('[data-chat-component-id]').find(x=>x.dataset.chatComponentId===id)||null;
 }
}
test('actual element panel loads, opens, updates, removes and clears on account change',async()=>{
 const body=new Element(),panel=new Element(),events=new Map();
 const app={currentSessionId:'s',currentUserId:'alice'};
 let rows=[component('plan'),component('task-progress')];
 const document={activeElement:null,getElementById:id=>({'chat-component-panel':panel,'chat-component-panel-body':body})[id]||null,
  createElement:()=>new Element(),querySelector:selector=>body.querySelector(selector),
  dispatchEvent:()=>{},addEventListener:(name,fn)=>events.set(name,fn)};
 let fetcher=async()=>response(rows);
 const context=vm.createContext({document,window:{addEventListener(){},innerWidth:1200},CSS:{escape:x=>x},
  CustomEvent:class{},AbortController,
  setTimeout:()=>1,clearTimeout(){},fetch:(...args)=>fetcher(...args)});
 const stubs={
  '../../shared/js/state.js':{app},
  '../../shared/js/config.js':{apiPath:x=>x},
  '../../shared/js/left-login.js':{authHeaders:()=>({})},
  '../../shared/js/icons.js':{icon:()=>'',componentTypeIcon:()=>''},
 };
 const source=await fs.readFile(new URL('../ui/chat/js/chat-components.js',import.meta.url),'utf8');
 // Expose the real queue only inside this test VM.
 const module=new vm.SourceTextModule(source+'\nexport { _scratchpadAct as enqueueEdit };',{context});
 await module.link(async spec=>{
  if(spec==='../core/components.js') return new vm.SourceTextModule(
    await fs.readFile(new URL('../ui/chat/core/components.js',import.meta.url),'utf8'),{context});
  const values=stubs[spec];assert.ok(values,spec);
  return new vm.SyntheticModule(Object.keys(values),function(){for(const [key,value] of Object.entries(values))this.setExport(key,value);},{context});
 });
 await module.evaluate();module.namespace.initChatComponents();
 await app.loadChatComponents('s');
 assert.equal(body.querySelectorAll('[data-chat-component-id]').length,2);
 assert.equal(panel.classList.contains('open'),false);
 app.componentPanelShow('plan');assert.equal(panel.classList.contains('open'),true);
 const original=body.querySelector('[data-chat-component-id="plan"]');
 await app.loadChatComponents('s');
 assert.equal(body.querySelector('[data-chat-component-id="plan"]'),original);
 rows=[component('plan','Revised plan'),component('task-progress','Step done')];
 await app.loadChatComponents('s');
 assert.equal(panel.classList.contains('open'),true);
 assert.equal(body.querySelector('[data-chat-component-id="plan"]').children[0].textContent,'Revised plan');
 rows=[component('plan')];await app.loadChatComponents('s');
 assert.equal(body.querySelectorAll('[data-chat-component-id]').length,1);
 app.currentUserId='bob';events.get('webagent-auth-transition-start')();
 assert.equal(body.children.length,0);assert.equal(panel.classList.contains('open'),false);
 app.currentUserId='alice';await app.loadChatComponents('s');
 const pendingSave=deferred();let posts=0;
 fetcher=async(path,options)=>{
  if(options?.method==='POST') {posts++;return pendingSave.promise;}
  return response(rows);
 };
 const first=module.namespace.enqueueEdit('scratchpad','update_row',{row_id:'one',text:'First'});
 await Promise.resolve();
 const second=module.namespace.enqueueEdit('scratchpad','update_row',{row_id:'two',text:'Queued'});
 const rejected=assert.rejects(second,/active session changed/);
 app.currentSessionId='other';
 pendingSave.resolve({ok:true,json:async()=>({component:component('scratchpad')})});
 await first;await rejected;
 assert.equal(posts,1,'Queued edits must not target the replacement session');
 await app.loadChatComponents(null);
});

test('actual V2 panel starts element loading without blocking the transcript',async()=>{
 const calls=[],app={currentSessionId:'s',currentUserId:'alice',
  chatMessages:{dataset:{},replaceChildren(){},children:[]},
  loadChatComponents:id=>{calls.push(id);return id?new Promise(()=>{}):undefined;}};
 let subscriber;
 const chat={initialize:async()=>{},pendingMessages:async()=>[],subscribe:(id,fn)=>{subscriber=fn;return ()=>{};},
  closeSession(){},openSession:async id=>subscriber({sessionId:id,items:[],runs:[]}),drainOutbox:async()=>{}};
 const context=vm.createContext({window:{addEventListener(){},dispatchEvent(){}},
  document:{addEventListener(){}},CustomEvent:class{}});
 const values={
  '../../shared/js/state.js':{app},'./client.js':chat,
  './projection.js':{reconcilePublicNodes(){}},'../js/chat-bubble.js':{addChatBubble(){}},
  './activity-view.js':{renderActivity(){},disconnectActivity(){}},
 };
 const module=new vm.SourceTextModule(await fs.readFile(new URL('../ui/chat/core/panel.js',import.meta.url),'utf8'),{context});
 await module.link(async spec=>{
  const exports=values[spec];assert.ok(exports,spec);
  return new vm.SyntheticModule(Object.keys(exports),function(){for(const [key,value] of Object.entries(exports))this.setExport(key,value);},{context});
 });
 await module.evaluate();
 await module.namespace.loadPanel('s');
 assert.deepEqual(calls,[null,'s']);
 module.namespace.disconnectPanel();
 assert.equal(calls.at(-1),null);
});
