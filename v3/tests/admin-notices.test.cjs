const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../ui/chat/core/administrator-notices.js'),'utf8');
function load(){const ctx={};vm.createContext(ctx);vm.runInContext(source.replaceAll('export function','function'),ctx);return ctx;}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate);};
const notice=(sha='a')=>({notice:{snapshot_sha256:sha.repeat(64),dirty:true,ahead:0,observed_at:123,notification:'Repository changed. Review before editing.'}});
function timers(){const pending=new Map();let serial=0;return {
 setIntervalFn(fn,ms){assert.equal(ms,10000);pending.set(++serial,fn);return serial;},
 clearIntervalFn(id){pending.delete(id);},
 tick(){for(const fn of pending.values())fn();},size(){return pending.size;}};}
test('current-session poll is single-flight, deduplicated, revocation clears only its region',async()=>{
 const events=[],requests=[],timer=timers();let response=notice(),hold=null;
 const helper=load().createAdministratorNotices({...timer,render:text=>events.push(text),
 request:async url=>{requests.push(url);return hold?hold.promise:response;}});
 helper.start('own session');await flush();assert.equal(requests[0],'/sessions/own%20session/administrator/notices');
 assert.equal(events.at(-1),response.notice.notification);const count=events.length;
 timer.tick();await flush();assert.equal(events.length,count);
 hold=deferred();timer.tick();timer.tick();timer.tick();assert.equal(requests.length,3);
 hold.resolve({notice:null});await flush();assert.equal(events.at(-1),'');
 response=notice('b');hold=null;timer.tick();await flush();assert.equal(events.at(-1),response.notice.notification);
 helper.stop();assert.equal(timer.size(),0);assert.equal(events.at(-1),'');
});
test('delayed switched/disconnected replies cannot publish and replacement polling waits',async()=>{
 const timer=timers(),events=[],calls=[],old=deferred(),fresh=deferred();
 const helper=load().createAdministratorNotices({...timer,render:text=>events.push(text),
 request:url=>{calls.push(url);return calls.length===1?old.promise:fresh.promise;}});
 helper.start('old');helper.start('new');timer.tick();assert.equal(calls.length,1);
 old.resolve(notice('a'));await flush();assert.equal(calls.length,2);assert(calls[1].includes('/new/'));
 assert.equal(events.at(-1),'');helper.stop();fresh.resolve(notice('b'));await flush();
 assert.equal(events.at(-1),'');assert.equal(timer.size(),0);
});
test('invalid/failed live notice withdraws prior FYI without unsafe text',async()=>{
 const timer=timers(),events=[];let response=notice();
 const helper=load().createAdministratorNotices({...timer,render:text=>events.push(text),
 request:async()=>{if(response instanceof Error)throw response;return response;}});
 helper.start('s');await flush();response={notice:{...notice().notice,notification:'x'.repeat(151)}};
 timer.tick();await flush();assert.equal(events.at(-1),'');
 response=notice('b');timer.tick();await flush();response=new Error('private transport detail');
 timer.tick();await flush();assert.equal(events.at(-1),'');assert(!events.some(x=>x.includes('private')));
 helper.stop();
});
test('dedicated DOM renderer never overwrites chat connection errors or uses HTML',()=>{
 const error={textContent:'Connection interrupted'},parent={children:[],prepend(el){el.parentNode=this;this.children.unshift(el);}};
 const document={getElementById:id=>id==='chat-above-pill'?parent:error,
 createElement:()=>({setAttribute(){},textContent:'',hidden:false})};
 const render=load().repositoryNoticeRenderer(document);
 render('<script>literal</script>');assert.equal(parent.children[0].textContent,'<script>literal</script>');
 assert.equal(error.textContent,'Connection interrupted');render('');assert(parent.children[0].hidden);
 assert.equal(error.textContent,'Connection interrupted');assert.equal(parent.children.length,1);
});
test('chat client clears timer and stale EventSource epoch while preserving note callbacks',async()=>{
 const ctx=load(),notes=[],timer=timers(),sources=[];class Events{constructor(){sources.push(this);}close(){this.closed=true;}}
 ctx.EventSource=Events;
 ctx.createAdministratorNotices=args=>load().createAdministratorNotices({...args,...timer,render:()=>{}});
 const clientSource=fs.readFileSync(path.join(__dirname,'../ui/chat/core/client.js'),'utf8')
 .replace(/^import .*;\n/m,'').replace('export function','function');
 vm.runInContext(clientSource,ctx);
 const client=ctx.createChatClient({request:async url=>url.endsWith('/administrator/notices')?{notice:null}:{last_seq:0},
 note:text=>notes.push(text),EventSourceClass:Events});
 await client.open('first',()=>{},()=>{});sources[0].onerror();assert.equal(notes.at(-1),'Connection interrupted. Reconnecting…');
 await client.open('second',()=>{},()=>{});const count=notes.length;sources[0].onopen();assert.equal(notes.length,count);
 sources[1].onopen();assert.equal(notes.at(-1),'');assert.equal(timer.size(),1);client.disconnect();assert.equal(timer.size(),0);
});
