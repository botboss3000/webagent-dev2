// Independent event/geometry contract for the optional measurement producer.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let time=0, listeners=new Map(), stored=new Map(), events=[], nav='navigate', focused=null;
const context=vm.createContext({
  console,Map,Set,JSON,Date,performance:{now:()=>time,getEntriesByType:()=>[{type:nav}]},
  sessionStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
  document:{get activeElement(){return focused;},getElementById:()=>null},
  getComputedStyle:()=>({outlineStyle:'solid',outlineWidth:'2px',boxShadow:'none'}),
  setTimeout:fn=>fn(),
});
context.window={addEventListener:(name,fn)=>{const list=listeners.get(name)||[];list.push(fn);listeners.set(name,list);},
  removeEventListener:(name,fn)=>listeners.set(name,(listeners.get(name)||[]).filter(x=>x!==fn))};
function fire(name,event={}){for(const fn of [...(listeners.get(name)||[])])fn(event);}
const source=fs.readFileSync(__dirname+'/../frontend/witnesses.js','utf8');
(async()=>{
  const m=new vm.SourceTextModule(source,{context});await m.link(()=>{});await m.evaluate();
  const make=()=>m.namespace.createWitnesses({record:(kind,row)=>events.push({kind,...row}),
    geometry:()=>({viewport_visible:true}),captureId:'cap'});
  const frame=(y=10,text_hash='abc')=>({active_session_id:'s',complete:true,run_state:'idle',
    stop_visible:false,transient_count:0,viewport:{width:900,height:700,visibility:'visible'},
    elements:[{id:'user:u',kind:'user',stable_id:true,visible:true,viewport_visible:true,
      text_hash,text_length:3,rect:{y}}]});
  let w=make(), a=frame();w.checkpoint(a);assert.equal(a.scroll.comparable,false);
  time=500;a=frame();w.checkpoint(a);assert.equal(a.scroll.comparable,true);assert.equal(a.scroll.anchor_delta_px,0);
  time=1000;a=frame(40);w.checkpoint(a);assert.equal(a.scroll.anchor_delta_px,30);
  fire('wheel');time=1100;a=frame(80);w.checkpoint(a);assert.equal(a.scroll.user_scrolled,true);
  w.trace('cache.session.load');a=frame();w.checkpoint(a);assert.equal(a.scroll.intentional_navigation,true);
  const control={id:'chat-send',tagName:'BUTTON',labels:[],textContent:'Send',disabled:false,
    getAttribute:()=>null,matches:()=>true,closest:selector=>selector.includes('render-recorder')?null:control};
  focused=control;fire('pointerdown',{target:control});fire('keydown',{target:control,key:'Enter'});
  const interactions=events.filter(e=>e.kind==='journey.keyboard');
  assert.deepEqual(interactions.map(e=>e.detail.method),['pointer','keyboard']);
  assert(interactions.every(e=>e.detail.accessible_name && e.detail.focus_visible && e.detail.focus_indicator));
  // Offline stability must never masquerade as successful reconnect.
  w.trace('connection.closed');time+=2000;w.checkpoint(frame());
  assert.equal(events.filter(e=>e.detail.phase==='after').length,0);
  w.trace('connection.opened');w.checkpoint(frame());time+=1100;w.checkpoint(frame());
  assert.equal(events.filter(e=>e.detail.phase==='after').length,1);
  fire('pagehide');assert.equal(stored.size,1);
  const copy=new Map(stored);w.stop();assert.equal([...listeners.values()].flat().length,0);
  stored=copy;nav='reload';w=make();w.checkpoint(frame());time+=1100;w.checkpoint(frame());
  const recovery=events.filter(e=>e.kind==='journey.recovery' && e.detail.phase==='after');
  assert.equal(recovery.length,2);assert.equal(recovery[1].detail.elements[0].text_hash,'abc');
  assert(recovery[1].detail.boundary_id.includes(':reload:'));
  w.stop();assert.equal(stored.size,0);
  console.log('PASS scroll intent, keyboard/pointer evidence, reconnect gating, reload fingerprints, teardown');
})().catch(e=>{console.error(e);process.exitCode=1});
