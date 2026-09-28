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
   if(id==='recorder.js')code+='\nexport {_start,_record,_flush,_trace,_sampleWitnesses,_progressEvidence,_checkpoint,_onMutations}; export function __pendingMirrorCount(){return _pendingMirrorPatches.length;}';
 }
 const m=new vm.SourceTextModule(code,{context,identifier:id,initializeImportMeta:meta=>{meta.url="https://dev.webagent.live/plugin-assets/recorder/v-test/"+id;}});modules[id]=m;
 await m.link(spec=>mod(spec.startsWith('./')?spec.slice(2):spec));return m;
}

(async()=>{
 const m=await mod('recorder.js');await m.evaluate();
 const ringModule=modules['ring.js'].namespace;
 // Full-value mirror patches advance expired history, including masked fields.
 let nid=0;const ids=new WeakMap();
 const id=node=>{if(!ids.has(node))ids.set(node,++nid);return ids.get(node);};
 const text=value=>({nodeType:3,textContent:value});
 const el=(tag,children=[],attrs={})=>({nodeType:1,tagName:tag.toUpperCase(),childNodes:children,
   attributes:Object.entries(attrs).map(([name,value])=>({name,value})),
   getAttribute:name=>attrs[name]??null});
 const first=text('old'),editable=text('SECRET');
 const tree=el('div',[first,el('textarea',[editable])]);
 const mirror=ringModule.createMirror(tree,{id,excluded:()=>false,sensitive:n=>n===editable});
 assert.equal(mirror.html(),'<div>old<textarea>[masked]</textarea></div>');
 let tick=0;const ring=new ringModule.Prebuffer({now:()=>tick,advance:mirror.advance,baselineBytes:mirror.size()});
 tick=1000;first.textContent='after';
 const patches=mirror.patches([{type:'characterData',target:first}]);
 ring.push([{kind:'mutation'}],patches,100);
 tick=4000;ring.push([{kind:'trace',label:'keep'}],[],50);
 tick=6001;const coverage=ring.freeze();
 assert.equal(coverage.available_ms,5000,'exact five-second window after warmup');
 assert.equal(mirror.html(),'<div>after<textarea>[masked]</textarea></div>','expired mutations advance baseline');
 assert.equal(ring.groups.length,1,'old evidence expires with its patches');
 const invalid=ringModule.createMirror(tree,{id,excluded:()=>false,sensitive:()=>false});
 assert.doesNotThrow(()=>invalid.advance([{type:'children',target:id(first),removed:[],added:[],next:null}]));
 assert.equal(invalid.invalid,true,'wrong-type stale patch invalidates mirror without timer exception');
 tick=6100;ring.push([{kind:'trace',label:'handshake'}],[],50);
 assert.equal(ring.groups.length,2,'post-click events retained during handshake');
 const capped=new ringModule.Prebuffer({now:()=>tick,limit:100,advance:()=>0});
 capped.push([{kind:'snapshot'}, {kind:'html.part'}],[],120);
 assert.equal(capped.groups.length,0,'complete payload group evicted atomically');
 assert(capped.coverage().limitations.includes('prebuffer-memory-limit'));
 // Browser collector never persists or sends while armed, then promotes once.
 root.nodeType=1;root.tagName='DIV';root.childNodes=[];root.attributes=[];
 root.matches=()=>false;root.closest=()=>null;
 const cfg={capture_js_errors:false,capture_console:false,capture_network:false,capture_lag:false,capture_snapshots:false};
 m.namespace.initRecorder();
 const sentBefore=received.length;
 m.namespace.armRecorder({client_id:'doc-pre',config:cfg});
 m.namespace._record('trace',{label:'too-old'});
 now+=1000;m.namespace._record('trace',{label:'before-trigger'});
 now+=5000;
 const pre=m.namespace.prepareRecorderCapture();
 assert.equal(pre.available_ms,5000);
 assert.equal(received.length,sentBefore,'armed collection has zero uploads');
 const outbox=modules['outbox.js'].namespace;
 assert.equal((await outbox.pendingChunks('pre-one')).length,0,'armed collection has no outbox');
 now+=200;m.namespace._record('trace',{label:'during-handshake'});
 const inserted={nodeType:3,textContent:'streamed during handshake',parentElement:root};
 m.namespace._onMutations([{type:'childList',target:root,addedNodes:[inserted],removedNodes:[],nextSibling:null,previousSibling:null}]);
 assert.equal(m.namespace.__pendingMirrorCount(),1,'handshake retains pending mirror patch before animation frame');
 listeners['render-recorder-changed']({detail:{enabled:true,client_id:'doc-pre',recording_id:'pre-one',lease_token:'lease-one',config:cfg}});
 assert.equal(m.namespace.__pendingMirrorCount(),0,'promotion consumes handshake patches before sink changes');
 fail=false;
 await m.namespace._flush(false);
 const captured=received.flatMap(x=>x.records).filter(x=>x.recording_id==='pre-one');
 assert(!captured.some(x=>x.label==='too-old'));
 assert(captured.some(x=>x.label==='before-trigger'));
 assert(captured.some(x=>x.label==='during-handshake'));
 const trigger=captured.find(x=>x.label==='prebuffer-trigger');
 assert.equal(trigger.detail.prebuffer.available_ms,5000);
 assert(captured.every(x=>x.elapsed_ms>=0));
 assert.deepEqual(captured.map(x=>x.seq),Array.from({length:captured.length},(_,i)=>i),'promoted rows contiguous');
 // Finish/retry old upload while a second capture runs. No hook or lease mix.
 fail=true;m.namespace._record('trace',{label:'old-pending'});
 const oldJob=m.namespace.detachRecorder('test');
 const pending=await oldJob.finish();assert.equal(pending.acknowledged,false);
 assert.equal(m.namespace.__pendingMirrorCount(),0,'old capture cannot leak patches across node-ID reset');
 m.namespace.armRecorder({client_id:'doc-pre',config:cfg});
 listeners['render-recorder-changed']({detail:{enabled:false}});
 now+=1000;m.namespace._record('trace',{label:'next-pre'});
 assert(m.namespace.prepareRecorderCapture(),'disabled saved state leaves armed observers active');
 listeners['render-recorder-changed']({detail:{enabled:true,client_id:'doc-pre',recording_id:'pre-two',lease_token:'lease-two',config:cfg}});
 m.namespace.disarmRecorder(); // Preference off must leave current capture alone.
 oldJob.renew('lease-one-renewed');fail=false;now+=3000;
 assert.equal((await oldJob.finish()).acknowledged,true);
 m.namespace._record('trace',{label:'new-still-recording'});
 const newJob=m.namespace.detachRecorder('test');
 assert.equal((await newJob.finish()).acknowledged,true);
 const successful=received.filter(x=>x.records.some(r=>r.label==='old-pending') && x.lease_token==='lease-one-renewed');
 assert(successful.length,'detached retry uses its own renewed token');
 assert(received.some(x=>x.lease_token==='lease-two' && x.records.some(r=>r.label==='new-still-recording')),'old finish did not stop new capture');
 m.namespace.armRecorder({client_id:'doc-pre',config:cfg});
 m.namespace.disarmRecorder();assert.equal(m.namespace.prepareRecorderCapture(),null);
 const getRoot=context.document.getElementById;
 context.document.getElementById=()=>null;
 m.namespace.armRecorder({client_id:'doc-pre',config:cfg});
 now+=6000;context.document.getElementById=getRoot;
 assert.equal(m.namespace.prepareRecorderCapture().available_ms,0,'late chat mount begins honest baseline coverage');
 m.namespace.cancelPreparedCapture();now+=5000;
 assert.equal(m.namespace.prepareRecorderCapture().available_ms,5000,'late chat mount recovers full window');
 m.namespace.disarmRecorder();
 console.log('PASS five-second mirror eviction/redaction, atomic limits, memory-only arming, promotion and concurrent detached uploads');
})().catch(error=>{console.error(error);process.exitCode=1;});
