// Focused browser-recorder lifecycle contract. Run from the server checkout:
// node --experimental-vm-modules plugins/recorder/tests/frontend.cjs
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
let fail=true,now=10000;const received=[];const beacons=[],listeners={};const animationFrames=[];
let contentNodes=[],transientNodes=[],noteVisible=false;
const makeElement=(kind,id,text)=>({
  dataset:{msgId:id,sessionId:'session-a',sessionSeq:kind==='user'?'1':'2'},textContent:text,
  childNodes:[{nodeType:3,textContent:text}],matches:()=>false,querySelector:()=>null,querySelectorAll:()=>[],
  classList:{contains:name=>name===kind},closest:()=>null,
  getBoundingClientRect:()=>({x:0,y:10,width:100,height:40,top:10,bottom:50,left:0,right:100}),
});
const transcriptNodes=[makeElement('user','u1','Question'),makeElement('terminal-closer','a1','Answer')];
contentNodes=[transcriptNodes[1]];
const root={id:'chat-messages-inner',querySelectorAll:selector=>{
  if(selector==='.system-compaction-transient,.system-compaction-progress') return transientNodes;
  if(selector==='.system-compaction-transient') return transientNodes;
  if(selector==='.live-activity-note') return [];
  if(selector.includes(':not(.activity-group)')) return contentNodes;
  return transcriptNodes;
}};
class Clock extends Date { static now(){return now;} }
const context=vm.createContext({
 console,URL,Blob,Map,Set,WeakMap,Date:Clock,Math,JSON,Number,String,Object,Array,Promise,Error,AbortSignal,
 performance:{now:()=>now,getEntriesByType:()=>[]},location:{pathname:'/app',search:'',href:'https://dev.webagent.live/app'},
 navigator:{userAgent:'test',sendBeacon:(url,body)=>{beacons.push({url,body});return true;}},requestAnimationFrame:fn=>animationFrames.push(fn),setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},
 innerWidth:1000,innerHeight:800,devicePixelRatio:1,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'}),
 CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
 sessionStorage:{setItem(){},getItem(){return null;}},
 document:{body:{classList:{contains(){return false;}}},getElementById:id=>id==='chat-messages-inner'?root:null,
   querySelector:()=>null,querySelectorAll:()=>noteVisible?[makeElement('progress','note','Thinking…')]:[],addEventListener(){},removeEventListener(){}},
});
context.window=context;context.addEventListener=(name,callback)=>{listeners[name]=callback;};context.removeEventListener=(name)=>{delete listeners[name];};
context.dispatchEvent=()=>{};context.fetch=async(url,options)=>{received.push(JSON.parse(options.body));const large=new Blob([options.body]).size>1024*1024;return {ok:!fail && !large,status:large?413:fail?503:200,json:async()=>({saved:true})};};
const modules={};
async function mod(id) {
 if(modules[id])return modules[id];
 let code;
 if(id.endsWith('state.js'))code='export const app={currentSessionId:"session-a",currentAgentId:"agent-a"}';
 else if(id.endsWith('chat-message-cache.js'))code='export const _messageCache=new Map()';
 else if(id.endsWith('config.js'))code='export const apiPath=value=>value';
 else if(id.endsWith('uuid.js'))code='export const randomUUID=()=>"test-id"';
 else if(id.endsWith('left-login.js'))code='export const authHeaders=()=>({})';
 else if(id.endsWith('browser-storage-policy.js'))code='export const browserPersistenceAllowed=()=>false; export const getBrowserStorageContext=()=>({owner_scope:"test"})';
 else {
   code=fs.readFileSync(path.join(__dirname,'../frontend',id),'utf8');
   if(id==='toggle.js')code+='\nexport {_captureIssue}; export function __primeTest(){_enabled=true;_recordingId="capture-a";_leaseToken="test-lease";_screenVideo=null;}';
   if(id==='recorder.js')code+='\nexport {_start,_record,_flush,_trace,_sampleWitnesses,_progressEvidence,_checkpoint};';
 }
 const m=new vm.SourceTextModule(code,{context,identifier:id,initializeImportMeta:meta=>{meta.url="https://dev.webagent.live/plugin-assets/recorder/v-test/"+id;}});modules[id]=m;
 await m.link(spec=>mod(spec.startsWith('./')?spec.slice(2):spec));return m;
}
(async()=>{
 const m=await mod('recorder.js');await m.evaluate();
 m.namespace._start({lease_token:'test-lease',client_id:'doc-a',recording_id:'capture-a',config:{capture_js_errors:false,capture_console:false,capture_network:false,capture_lag:false,capture_snapshots:false}});
 m.namespace._record('trace',{label:'test-preserve'});
 assert.equal(await m.namespace._flush(false),false,'failed upload must not acknowledge');
 const rows=received.flatMap(x=>x.records),first=rows.find(x=>x.label==='test-preserve');
 assert(first,'test evidence was sent');
 const checkpoint=rows.find(x=>x.kind==='checkpoint');
 assert(checkpoint,'semantic checkpoint is recorded');
 assert.equal(checkpoint.detail.elements[0].id,'user:u1');
 assert.equal(checkpoint.detail.elements[1].id,'closer:a1');
 assert.equal(checkpoint.detail.elements[1].text_length,6);
 assert.equal(checkpoint.detail.elements[1].finalized,true);
 assert.equal(checkpoint.detail.document_id,'doc-a');
 fail=false;now+=3000;
 assert.equal(await m.namespace._flush(false),true,'retry must acknowledge');
 assert(received.slice(1).flatMap(x=>x.records).some(x=>x.seq===first.seq && x.label==='test-preserve'),'retry preserves sequence and payload');
 m.namespace._trace('run.started',{session_id:'session-a',run_id:'run-1'});
 noteVisible=true;animationFrames.splice(0).forEach(fn=>fn());
 now+=100;contentNodes.push(makeElement('content','new-1','New answer'));
 m.namespace._sampleWitnesses(false);
 assert.equal(m.namespace._progressEvidence().observed_from_send,true);
 assert(m.namespace._progressEvidence().progress_ms<m.namespace._progressEvidence().first_content_ms);
 m.namespace._trace('run.started',{session_id:'session-a',run_id:'run-2'});
 contentNodes.push(makeElement('content','new-2','Fast answer'));
 animationFrames.splice(0).forEach(fn=>fn());
 assert.equal(m.namespace._progressEvidence().observed_from_send,false,'same-frame order must remain blocked');
 const transient=makeElement('notice','notice-1','Compacted 100 to 50 tokens');
 transient.dataset.compactionInteractionId='compact-1';transientNodes=[transient];
 m.namespace._sampleWitnesses(false);now+=2000;transientNodes=[];m.namespace._sampleWitnesses(false);
 await m.namespace._flush(false);
 assert(received.flatMap(x=>x.records).some(x=>x.kind==='compaction.notice' && x.detail.visible_duration_ms===2000),'compaction duration measured from observed DOM presence');
 const appState=modules['/ui/shared/js/state.js'].namespace.app;
 appState._runControlsActive=true;appState.isProcessing=false;
 context.document.querySelector=()=>makeElement('stop','chat-stop-btn','Stop');
 m.namespace._checkpoint('control-transfer');await m.namespace._flush(false);
 assert(received.flatMap(x=>x.records).some(x=>x.detail?.reason==='control-transfer' && x.detail.run_state==='running' && x.detail.stop_visible===true),
   'run ownership transfer uses the same active-control state as chat UI');
 m.namespace._trace('run.completed',{session_id:'session-a',run_id:'run-2'});
 m.namespace._checkpoint('terminal-control-check');await m.namespace._flush(false);
 assert(received.flatMap(x=>x.records).some(x=>x.detail?.reason==='terminal-control-check' && x.detail.run_state==='completed'),
   'terminal signal cannot be hidden by a stale active-control flag');
 appState._runControlsActive=false;context.document.querySelector=()=>null;
 m.namespace._record('trace',{label:'before-unload'});
 listeners.pagehide();
 assert.equal(beacons.length,1,'unload boundary bypasses the asynchronous outbox');
 const unloadRows=JSON.parse(await beacons[0].body.text()).records;
 assert(unloadRows.some(x=>x.label==='before-unload'),'unload beacon includes preceding pending events');
 const boundary=unloadRows.find(x=>x.label==='document-pagehide');
 assert.equal(boundary.label,'document-pagehide');assert.equal(boundary.client_id,'doc-a');
 // A real rejected batch caused zero browser evidence in the user's report.
 // Model the server's 1 MiB boundary and exceed the old 8 MiB batch ceiling.
 const beforeLarge=received.length;
 const html='😀<p>完整内容</p>'.repeat(100000);
 m.namespace._record('snapshot',{html,detail:{reason:'large-fixture'}});
 for(let i=0;i<300;i++) {
   m.namespace._record('trace',{detail:{value:'x'.repeat(30000)}});
   if(i%50===49)await m.namespace._flush(false); // bounded memory-only fallback
 }
 const wideElements=Array.from({length:1000},(_,i)=>({id:'wide-'+i,kind:'user',memo:'x'.repeat(1500)}));
 m.namespace._record('checkpoint',{detail:{complete:true,elements:wideElements}});
 await m.namespace._flush(false);
 const uploaded=received.slice(beforeLarge);
 const checkpointParts=uploaded.flatMap(body=>body.records).filter(row=>row.kind==='checkpoint.part');
 assert(checkpointParts.length>1,'large semantic checkpoints use bounded parts');
 assert.equal(checkpointParts.flatMap(row=>row.detail.elements).length,wideElements.length);
 assert(checkpointParts.every(row=>row.detail.metadata.complete===true));
 assert(uploaded.every(body=>new Blob([JSON.stringify(body)]).size<1024*1024),'wire bodies stay below server JSON limit');
 const parts=uploaded.flatMap(body=>body.records).filter(row=>row.kind==='html.part');
 assert.equal(parts.map(row=>row.html).join(''),html,'large HTML and Unicode survive linked parts');
 assert(!uploaded.flatMap(body=>body.records).some(row=>row.kind==='capture.gap' && row.detail.reason==='payload-truncated'));
 const fastFetch=context.fetch;let slowUploads=0;
 context.fetch=async(...args)=>{const response=await fastFetch(...args);now+=16000;slowUploads++;return response;};
 for(let i=0;i<30;i++)m.namespace._record('trace',{label:'slow-drain',detail:{value:'x'.repeat(30000)}});
 const boundedStop=await m.namespace.stopRecorder('slow-drain');
 assert.equal(boundedStop.acknowledged,false,'aggregate stop deadline leaves remaining chunks retryable');
 assert(slowUploads<=2,'stop deadline applies across all queued batches');
 context.fetch=fastFetch;
 fail=true;m.namespace._record('trace',{label:'stop-retry-evidence'});
 const pendingStop=await m.namespace.stopRecorder('test-outage');
 assert.equal(pendingStop.acknowledged,false,'outage stops observers but retains upload state');
 assert.equal(context.__recorderTrace,undefined);
 fail=false;
 const result=await m.namespace.stopRecorder('test');
 assert.equal(result.last_seq,pendingStop.last_seq,'retry does not create a new capture or sequence');

 assert.equal(result.acknowledged,true);
 assert.equal(context.__recorderTrace,undefined,'stop removes optional hook');
 const elements=[];
 function element(tag) {
   const el={tag,dataset:{},style:{},children:[],value:'',textContent:'',append(...items){this.children.push(...items);},setAttribute(){},addEventListener(){},focus(){},showModal(){},close(){this.closed=true;},remove(){this.removed=true;}};
   if(tag==='canvas') {el.getContext=()=>({drawImage(){}});el.toDataURL=()=> 'data:image/png;base64,original-frame';}
   elements.push(el);return el;
 }
 context.document.createElement=element;context.document.head={append(){}};context.document.body.append=()=>{};
 const toggle=await mod('toggle.js');await toggle.evaluate();toggle.namespace.__primeTest();
 context.navigator.mediaDevices={getDisplayMedia:async()=>{
   screenRequests++;return {getTracks:()=>[],getVideoTracks:()=>[{getSettings:()=>({displaySurface:'browser'}),addEventListener(){}}]};
 }};
 let screenRequests=0;
 const createElement=context.document.createElement;
 context.document.createElement=tag=>{
   const el=createElement(tag);
   if(tag==='video'){el.videoWidth=10;el.videoHeight=10;el.play=async()=>{};}
   return el;
 };
 await toggle.namespace._captureIssue();
 assert.equal(screenRequests,1,'first camera click enables screenshot access');
 const dialog=elements.find(el=>el.tag==='dialog');const save=elements.find(el=>el.textContent==='Save issue');
 const note=elements.find(el=>el.tag==='textarea');note.value='Original visual issue';
 fail=true;await save.onclick();assert(!dialog.removed,'failed upload retains editor and screenshot');
 assert.equal(save.textContent,'Retry save');const firstMarker=received.at(-1);
 fail=false;now+=1000;await save.onclick();
 assert(dialog.removed,'successful acknowledgement closes editor');
 assert.equal(received.at(-1).screenshot_data_url,firstMarker.screenshot_data_url);
 assert.equal(received.at(-1).elapsed_ms,firstMarker.elapsed_ms);
 assert.equal(received.at(-1).marker_id,firstMarker.marker_id);
 const loaderSource=fs.readFileSync(path.join(__dirname,'../../../ui/shared/js/application-plugins.js'),'utf8');
 let catalogCalls=0;const loaderListeners={};
 const loaderContext=vm.createContext({URL,console,Set,document:{body:{classList:{contains:()=>false}}},location:{origin:'https://dev.webagent.live'},window:{addEventListener(name,callback){loaderListeners[name]=callback;}},
 fetch:async()=>{catalogCalls++;return {ok:true,json:async()=>({plugins:[]})};}});
 const loader=new vm.SourceTextModule(loaderSource,{context:loaderContext});
 await loader.link(async(spec)=>{
   const stub=new vm.SourceTextModule(spec.includes('left-login')?'export const authHeaders=()=>({}); export const isAdmin=()=>false;':spec.includes('partial-loader')?'export const partialsReady=Promise.resolve();':'export const apiPath=x=>x;',{context:loaderContext});
   await stub.link(()=>{});return stub;
 });
 await loader.evaluate();await Promise.resolve();
 assert.equal(catalogCalls,0);
 loaderListeners['admin-status-loaded']({detail:{is_admin:true}});await Promise.resolve();
 assert.equal(catalogCalls,1,'resolved admin loads plugin before main applies admin CSS');
 console.log('PASS checkpoint/retry lifecycle, conservative progress timing, observed compaction timing, screenshot Retry preserves original frame/id/time');
})().catch(e=>{console.error(e);process.exitCode=1});
