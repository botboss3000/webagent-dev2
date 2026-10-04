const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class E {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.style={};this.dataset={};this.attributes={};this.textContent='';this.hidden=false;this.disabled=false;this.isConnected=true;this.classes=new Set();this.classList={add:n=>this.classes.add(n),remove:n=>this.classes.delete(n),toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  insertBefore(item,before){item.parent=this;const i=this.children.indexOf(before);this.children.splice(i<0?this.children.length:i,0,item);}
  setAttribute(k,v){this.attributes[k]=v;}addEventListener(t,fn){(this.listeners[t]??=[]).push(fn);}
  querySelector(s){return this.children.find(c=>s==='button:not(:disabled)'?c.tag==='button'&&!c.disabled:false)||null;}
  contains(n){return n===this||this.children.some(c=>c.contains(n));}closest(){return null;}
  remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
  getBoundingClientRect(){return {top:500,left:20,right:250,bottom:525};}focus(){}scrollBy(){}
  async emit(t){for(const fn of this.listeners[t]||[])await fn({target:this,preventDefault(){},stopPropagation(){}});}
}
async function load(file,extra={}) {
  const doc=new E();doc.body=new E();doc.createElement=tag=>new E(tag);
  const win=new E();win.lucide={createIcons(){}};
  const context=vm.createContext({document:doc,window:win,AbortController,Date,Number,Error,innerHeight:800,innerWidth:1200,...extra});
  const module=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'..',file),'utf8'),{context});
  await module.link(async spec=>{
    const exports=spec.includes('chat-config')?{currentChatProfile:()=>({})}:spec.includes('clipboard')?{copyText:async()=>{}}:{openChatElementFullView:()=>{}};
    return new vm.SyntheticModule(Object.keys(exports),function(){for(const [k,v]of Object.entries(exports))this.setExport(k,v);},{context});
  });await module.evaluate();return {api:module.namespace,doc,win};
}
const flatten=node=>[node.textContent,...node.children.map(flatten)].join(' ');
const tick=()=>new Promise(setImmediate);
test('composer stats show reported tokens and measured bytes; click opens a fenced context panel',async()=>{
  const {api,doc}=await load('ui/chat/controls/usage-stats.js');
  const nodes=new Map(['chat-pill-stats','chat-tokens-in','chat-tokens-out','chat-token-bar','chat-model-ctx','chat-cost','chat-pill-stats-strip'].map(id=>[id,new E()]));
  const response={usage:{reported_calls:1,input_tokens:42,output_tokens:3,cached_input_tokens:10,unknown_cache_calls:0,unknown_cost_calls:1},latest:{model:{model:'fixture'},context_summary:{envelope_bytes:700,max_bytes:2000,instruction_json_bytes:100,schemas_json_bytes:200,history_json_bytes:350,call:1,turn:0},context_controls:{trigger_tokens:100000,tail_tokens:30000},compactions:0}};
  let resolve;const client={usage:async id=>id==='a'?response:new Promise(r=>resolve=r)};
  const stats=api.mountUsageStats({root:{querySelector:s=>nodes.get(s.slice(1))},client,note:()=>{}});
  stats.open('a');await tick();
  assert.equal(nodes.get('chat-tokens-in').textContent,'42');assert.equal(nodes.get('chat-model-ctx').textContent,'ctx 700B / 2.0kB');
  assert.equal(nodes.get('chat-cost').textContent,'cost —');
  await nodes.get('chat-pill-stats').emit('click');
  const panel=doc.body.children[0];assert.equal(panel.hidden,false);
  assert.match(flatten(panel),/700 bytes \/ 2,000 byte budget/);assert.match(flatten(panel),/Instructions 100 bytes/);
  stats.show({id:'older-run',model:{model:'older-model'},context_summary:{envelope_bytes:300,max_bytes:2000}});
  assert.match(flatten(panel),/older-run/);assert.match(flatten(panel),/older-model/);assert.match(flatten(panel),/300 bytes/);
  await nodes.get('chat-pill-stats').emit('click');await nodes.get('chat-pill-stats').emit('click');
  assert.match(flatten(panel),/700 bytes/);assert.doesNotMatch(flatten(panel),/older-model/);
  stats.open('b');await tick();stats.open('');resolve(response);await tick();
  assert.equal(nodes.get('chat-tokens-in').textContent,'—');assert.equal(panel.hidden,true);stats.destroy();
});
test('both bubble gutters retain v2 control order and mark unsupported mutations red',async()=>{
  const {api}=await load('ui/chat/core/bubble-gutters.js');
  for(const [role,expected]of [['user',['time','full_view','copy','undo','delete','more']],['assistant',['time','full_view','collapse','read_aloud','copy','fork','delete','more']]]) {
    const gutter=api.createBubbleGutter({item:{role,id:'message',created_at:0},body:new E(),bubble:new E(),getText:()=>''});
    assert.deepEqual(Array.from(gutter.children,c=>c.dataset.gutterControl),expected);
    for(const c of gutter.children.filter(c=>['undo','delete','fork'].includes(c.dataset.gutterControl))) {
      assert.equal(c.disabled,true);assert.equal(c.classes.has('v3-backend-pending'),true);
    }
  }
});
