const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.textContent='';this.hidden=false;this.disabled=false;this.style={};this.attributes={};this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  append(...nodes){for(const node of nodes){this.children.push(node);node.parent=this;}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  get firstChild(){return this.children[0];}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  querySelector(selector){if(selector==='.cmc-name')return this.name;if(selector==='button:not(:disabled)')return this.children.find(c=>c.tag==='button'&&!c.disabled);}
  contains(node){return node===this||this.children.some(c=>c.contains(node));}
  closest(selector){return this.selector===selector?this:null;}
  focus(){this.focused=true;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
  getBoundingClientRect(){return {top:600,right:950};}
  showModal(){this.open=true;}
  close(value=''){this.returnValue=value;this.open=false;this.emit('close');}
  emit(type,details={}){return Promise.all((this.listeners[type]||[]).map(fn=>fn({target:this,preventDefault(){},...details})));}
}
async function fixture(){
  const nodes=new Map(['chat-mode-btn','chat-model-changer'].map(id=>[id,new Element('button')]));
  nodes.get('chat-model-changer').name=new Element('span');
  const body=new Element(),document=new Element(),window=new Element();
  document.body=body;document.createElement=tag=>new Element(tag);
  const state={agent_id:'agent-a',mode:{mode:'read_only',execution_mode:'ask',revision:0},
    execution_modes:[{id:'ask',label:'Ask',description:'Read',permission_policy:'read_only'},{id:'plan',label:'Plan',description:'Plan',permission_policy:'read_only'},{id:'auto',label:'Auto',description:'Write',permission_policy:'write'}],
    model:{entry_id:null,revision:0},models:[{entry_id:null,label:'Standard · A',model:'model-a',connected:true},{entry_id:'b',label:'B',model:'model-b',connected:true}]};
  const calls=[],errors=[];
  const client={
    controls:async id=>{calls.push(['read',id]);return structuredClone(state);},
    mode:async(id,body)=>{calls.push(['mode',id,body]);state.mode={mode:body.mode==='auto'?'write':'read_only',execution_mode:body.mode,revision:state.mode.revision+1};},
    model:async(id,body)=>{calls.push(['model',id,body]);state.model={entry_id:body.entry_id,revision:state.model.revision+1};},
  };
  const context=vm.createContext({document,window,AbortController,innerHeight:800,innerWidth:1000,crypto:require('node:crypto').webcrypto});
  const module=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../ui/chat/controls/session-controls.js'),'utf8'),{context});
  await module.link(()=>{});await module.evaluate();
  const controls=module.namespace.mountSessionControls({root:{querySelector:s=>nodes.get(s.slice(1))},client,
    guard:fn=>async(...args)=>{try{return await fn(...args);}catch(e){errors.push(e.message);}},
    note:()=>{},ensureSession:async()=>controls.open('created')});
  return {controls,nodes,body,state,calls,errors,client};
}
const tick=()=>new Promise(setImmediate);
test('mode cycle uses the loaded session revision; write elevation requires confirmation',async()=>{
  const f=await fixture();await f.controls.open('session-a');
  await f.nodes.get('chat-mode-btn').emit('click');
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls.find(c=>c[0]==='mode').slice(1))),['session-a',{mode:'plan',expected_revision:0,confirm_write:false}]);
  const pending=f.nodes.get('chat-mode-btn').emit('click');await tick();
  const dialog=f.body.children.find(n=>n.tag==='dialog');assert(dialog.open);
  assert.equal(f.calls.filter(c=>c[0]==='mode').length,1);
  dialog.close('cancel');await pending;
  assert.equal(f.calls.filter(c=>c[0]==='mode').length,1);
  const confirm=f.nodes.get('chat-mode-btn').emit('click');await tick();
  f.body.children.find(n=>n.tag==='dialog').close('confirm');await confirm;
  const write=f.calls.filter(c=>c[0]==='mode').at(-1);
  assert.equal(write[2].expected_revision,1);assert.equal(write[2].confirm_write,true);
  f.controls.destroy();
});
test('model arrows show the next-loop preference and retain the running model in the tooltip',async()=>{
  const f=await fixture();await f.controls.open('session-a');
  await f.nodes.get('chat-model-changer').emit('keydown',{key:'ArrowRight'});await tick();
  const write=f.calls.find(c=>c[0]==='model');assert.equal(write[1],'session-a');assert.equal(write[2].entry_id,'b');assert.equal(write[2].expected_revision,0);
  f.controls.updateRuns([{status:'running',model:{model:'model-a'}}]);
  assert.equal(f.nodes.get('chat-model-changer').name.textContent,'B');
  assert.match(f.nodes.get('chat-model-changer').title,/Running: model-a.*Next loop turn: B/);
  f.controls.updateRuns([]);
  assert.equal(f.nodes.get('chat-model-changer').name.textContent,'B');
  f.controls.destroy();
});
test('switching session cancels a write-mode confirmation and fences delayed reads',async()=>{
  const f=await fixture();f.state.mode={mode:'read_only',execution_mode:'plan',revision:8};
  await f.controls.open('session-a');
  const pending=f.nodes.get('chat-mode-btn').emit('click');await tick();
  await f.controls.open('session-b');await pending;
  assert.equal(f.calls.filter(c=>c[0]==='mode').length,0);
  let finish;
  f.client.controls=id=>id==='old'?new Promise(r=>finish=r):Promise.resolve({...structuredClone(f.state),model:{entry_id:'b',revision:3}});
  const old=f.controls.open('old');await f.controls.open('new');
  finish(structuredClone(f.state));await old;
  assert.equal(f.nodes.get('chat-model-changer').name.textContent,'B');
  f.controls.destroy();
});
test('write failure reloads authoritative state and restores usable controls',async()=>{
  const f=await fixture();await f.controls.open('session-a');
  f.client.model=async()=>{throw Error('Conversation model changed; reload the picker');};
  await f.nodes.get('chat-model-changer').emit('keydown',{key:'ArrowRight'});await tick();
  assert.equal(f.errors.length,1);assert.equal(f.nodes.get('chat-model-changer').disabled,false);
  assert.equal(f.nodes.get('chat-model-changer').name.textContent,'Standard · A');
  f.controls.destroy();
});
