// Actual recorder control behavior: configuration, preference, and detached jobs.
// node --experimental-vm-modules plugins/recorder/tests/panel.cjs
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const timers=new Map(),events={},docEvents={},requests=[],storage=new Map(),session=new Map(),recoveredUploads=[];
let timerId=0,armCalls=0,detachCalls=0,captures=0,failFirst=true;
const app={currentUserId:'admin',currentSessionId:'session-a',currentAgentId:'agent-a',isProcessing:true};
function element(tag){
 const classes=new Set(),listeners={};
 const e={tag,children:[],dataset:{},style:{},attrs:{},className:'',hidden:false,
 classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,v)=>v?classes.add(x):classes.delete(x)},
 append(...items){this.children.push(...items)},insertBefore(item){this.children.unshift(item)},remove(){},
 setAttribute(k,v){this.attrs[k]=String(v)},getAttribute(k){return this.attrs[k]},focus(){},
 addEventListener(k,fn){listeners[k]=fn},click(){return listeners.click?.({target:e})},change(){return listeners.change?.({target:e})},
 contains(n){return n===this || this.children.some(x=>x.contains?.(n))},
 querySelector(sel){return this.querySelectorAll(sel)[0] || null},
 querySelectorAll(sel){const all=[];for(const x of this.children){if((sel[0]==='.' && x.className?.split(' ').includes(sel.slice(1))) || (sel==='[data-recorder-prebuffer]' && x.dataset?.recorderPrebuffer))all.push(x);all.push(...(x.querySelectorAll?.(sel)||[]));}return all;}
 };return e;
}
const control=element('span'),button=element('button'),panel=element('span');
panel.className='render-recorder-popover';control.append(button,panel);
for(const name of ['title','detail','limits']){const e=element('span');e.className='render-recorder-status-'+name;panel.append(e);}
const context=vm.createContext({console,URL,Blob,Map,Set,Date,Math,JSON,Number,String,Object,Array,Promise,Error,AbortSignal,
 performance:{now:()=>10000,getEntriesByType:()=>[]},location:{pathname:'/app'},
 document:{body:{classList:{contains:()=>true,toggle(){}},append(){}},head:{append(){}},
 getElementById:id=>id==='render-recorder-control'?control:id==='render-recorder-toggle'?button:null,
 createElement:element,addEventListener:(n,fn)=>{docEvents[n]=fn}},
 navigator:{},sessionStorage:{getItem:k=>session.get(k),setItem:(k,v)=>session.set(k,v),removeItem:k=>session.delete(k)},
 localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId},clearTimeout:id=>timers.delete(id),setInterval:()=>1,clearInterval(){},
 addEventListener:(n,fn)=>{events[n]=fn},dispatchEvent(){},
 CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail}},
 app,persistenceAllowed:true,recoverUpload:options=>{recoveredUploads.push(options);return {renew(){},drain:async()=>true};},arm:()=>{armCalls++},detach:()=>{
   const n=++detachCalls;return {recording_id:'capture-'+n,last_seq:n*10,renew(){},finish:async()=>({recording_id:'capture-'+n,last_seq:n*10,acknowledged:n===1 && failFirst?(failFirst=false):true})};
 },
 fetch:async(url,options={})=>{
   const body=options.body?JSON.parse(options.body):null;requests.push({url,body});
   let data={available:true,config:{}};
   if(url.endsWith('/armed'))data={armed:body.enabled};
   if(url.endsWith('/enabled') && body)data={...data,recording_id:'capture-'+(++captures),lease_token:'lease-'+captures,lease_renew_after_seconds:480};
   if(url.endsWith('/stop'))data={capture:{gaps:[]}};
   return {ok:true,status:200,json:async()=>data};
 }});
context.window=context;
const modules={};
async function mod(id){
 if(modules[id])return modules[id];let code;
 if(id.endsWith('browser-storage-policy.js'))code='export const browserPersistenceAllowed=()=>persistenceAllowed; export const getBrowserStorageContext=()=>({owner_scope:app.currentUserId});';
 else if(id.endsWith('config.js'))code='export const apiPath=x=>x';
 else if(id.endsWith('left-login.js'))code='export const authHeaders=()=>({Authorization:"test-"+app.currentUserId})';
 else if(id.endsWith('uuid.js'))code='export const randomUUID=()=>"document"';
 else if(id.endsWith('state.js'))code='const state=app; export {state as app};';
 else if(id.endsWith('uploader.js'))code='export const createUploader=recoverUpload;';
 else if(id.endsWith('recorder.js'))code='export const initRecorder=()=>{}; export const armRecorder=arm; export const disarmRecorder=()=>{}; export const prepareRecorderCapture=()=>({requested_ms:5000,available_ms:5000,trigger_elapsed_ms:5000}); export const cancelPreparedCapture=()=>{}; export const detachRecorder=detach; export const recorderElapsedMs=()=>0; export const recorderEventSeq=()=>0;';
 else code=fs.readFileSync(path.join(__dirname,'../frontend',id),'utf8')+'\nexport {_showPopover,_closePopover,_render,_setPrebuffer,_startLocal,_stopLocal,_recoverFinishing}; export const state=()=>({enabled:_enabled,armed:_armed,finishing:_finishing.size});';
 const m=new vm.SourceTextModule(code,{context,identifier:id,initializeImportMeta:meta=>{meta.url='https://dev.webagent.live/plugin-assets/recorder/v-test/'+id}});
 modules[id]=m;await m.link(spec=>mod(spec.startsWith('./')?spec.slice(2):spec));return m;
}
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
(async()=>{
 const m=await mod('toggle.js');await m.evaluate();await settle();
 const label=control.querySelector('.render-recorder-label');
 label.click();assert.equal(control.classList.contains('is-panel-open'),true);
 assert(![...timers.values()].some(t=>t.ms===2000),'configuration remains open');
 assert.equal(captures,0,'label does not start a recording');
 docEvents.keydown({key:'Escape'});assert.equal(control.classList.contains('is-panel-open'),false);
 m.namespace._showPopover();const statusTimer=[...timers.values()].find(t=>t.ms===2000);assert(statusTimer);statusTimer.fn();
 m.namespace._render();assert.equal(control.classList.contains('is-panel-open'),false,'polling does not reopen status');
 assert.equal(m.namespace.state().armed,false,'prebuffer defaults off');
 await m.namespace._setPrebuffer(true);assert.equal(m.namespace.state().armed,true);
 assert.equal(storage.get('webagent.recorder.prebuffer.v1.admin'),'true');
 assert.equal(captures,0,'arming creates no saved recording');
 await m.namespace._startLocal();assert.equal(captures,1);
 const start=requests.find(r=>r.url.endsWith('/enabled') && r.body);
 assert.equal(start.body.prebuffer,true);
 assert(requests.some(r=>r.body?.metadata?.prebuffer_browser?.available_ms===5000));
 await m.namespace._stopLocal('test');
 assert.equal(m.namespace.state().enabled,false,'stop frees switch despite pending uploads');
 assert.equal(m.namespace.state().armed,true,'buffer resumes while upload is pending');
 assert.equal(m.namespace.state().finishing,1);
 const intentKey='webagent.recorder.pending-finalization-v1';
 const stoppedIntent=JSON.parse(session.get(intentKey))['capture-1'];
 assert.equal(stoppedIntent.last_seq,10,'stop persists frozen final sequence before background drain');
 assert.equal(stoppedIntent.owner,'admin');
 assert(!JSON.stringify(stoppedIntent).includes('Authorization'),'auth bearer is not persisted');
 await m.namespace._startLocal();assert.equal(captures,2,'new capture allowed during prior upload');
 const retry=[...timers.values()].find(t=>t.ms===5000);assert(retry);await retry.fn();
 assert.equal(m.namespace.state().enabled,true,'old finalization does not stop the new capture');
 const stop=requests.find(r=>r.url.endsWith('capture-1/stop'));assert(stop);assert.equal(stop.body.last_seq,10);
 assert.equal(stop.body.lease_token,'lease-1','old job keeps old proof');
 await m.namespace._stopLocal('test');assert.equal(m.namespace.state().finishing,0);
 await m.namespace._setPrebuffer(false);assert.equal(m.namespace.state().armed,false);
 assert.equal(session.has(intentKey),false,'successful finalization clears pending intent');
 const beforeArms=armCalls;
 session.set(intentKey,JSON.stringify({
   'reload-capture':{id:'reload-capture',owner:'admin',client_id:'old-document',lease_token:'old-lease',last_seq:37,gaps:['known-gap'],scope:{session_id:'session-a',agent_id:'agent-a'}},
   'foreign-capture':{id:'foreign-capture',owner:'someone-else',client_id:'foreign-document',last_seq:9,gaps:[]}
 }));
 m.namespace._recoverFinishing();await settle();await settle();
 assert.equal(recoveredUploads.length,1,'only current owner pending jobs resume');
 assert.equal(recoveredUploads[0].clientId,'old-document','reload retains original document binding');
 const recoveredStop=requests.find(r=>r.url.endsWith('reload-capture/stop'));
 assert(recoveredStop);assert.equal(recoveredStop.body.last_seq,37);
 assert.deepEqual([...recoveredStop.body.gaps],['known-gap']);
 assert.equal(armCalls,beforeArms,'recovering stopped jobs does not activate observers');
 assert.equal(JSON.parse(session.get(intentKey))['reload-capture'],undefined);
 assert(JSON.parse(session.get(intentKey))['foreign-capture'],'other owner metadata is preserved without accessing its capture');
 context.persistenceAllowed=false;
 session.set(intentKey,JSON.stringify({'disabled':{id:'disabled',owner:'admin',client_id:'old-document',last_seq:1}}));
 m.namespace._recoverFinishing();await settle();
 assert.equal(recoveredUploads.length,1,'disabled browser persistence does not recover intents');
 console.log('PASS configuration dismissal, remembered opt-in, no archive on arm, and isolated overlapping finalization');
})().catch(e=>{console.error(e);process.exitCode=1});
