const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const tick=()=>new Promise(setImmediate);
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.dataset={};this.style={};this.hidden=false;this.classes=new Set();this.classList={add:n=>this.classes.add(n),remove:n=>this.classes.delete(n),toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}}replaceChildren(...nodes){this.children=[];this.append(...nodes);}before(){}setAttribute(k,v){this[k]=v;}addEventListener(k,fn){(this.listeners[k]??=[]).push(fn);}querySelectorAll(selector){return this.children.filter(c=>selector==='[data-proposal]'&&c.dataset.proposal!==undefined);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}click(){return this.onclick?.();}
}
async function modules(extra={}){
 const context=vm.createContext({AbortController,Uint8Array,Int16Array,Float32Array,ArrayBuffer,Blob,setTimeout,clearTimeout,btoa:s=>Buffer.from(s,'binary').toString('base64'),crypto:require('node:crypto').webcrypto,...extra});
 const audio=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../ui/chat/controls/audio-capture.js'),'utf8'),{context});
 const media=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../ui/chat/controls/composer-media.js'),'utf8'),{context});
 await audio.link(()=>{});await audio.evaluate();await media.link(()=>audio);await media.evaluate();return{audio:audio.namespace,media:media.namespace};
}
test('PCM capture clamps samples, obeys server turn-end, and cancels delayed microphone acquisition',async()=>{
 let process,stopped=0,sent=[],completed=[];
 class Context{constructor(){this.sampleRate=16000;}async resume(){}async close(){}createMediaStreamSource(){return {connect(){},disconnect(){}};}createGain(){return {gain:{value:0},connect(){},disconnect(){}};}createScriptProcessor(){process={connect(){},disconnect(){}};return process;}}
 const stream={getTracks:()=>[{stop(){stopped++;}}]};
 const navigator={mediaDevices:{getUserMedia:async()=>stream}};
 const {audio}=await modules({window:{isSecureContext:true,AudioContext:Context},navigator});
 const samples=new Int16Array(audio.downsampleToPcm16(new Float32Array([-2,0,2]),16000));assert.deepEqual([...samples],[-32768,0,32767]);
 const c=audio.createAudioCapture({send:async(sid,body)=>{sent.push([sid,body]);if(body.op==='start')return{capture_id:'capture'};if(body.op==='chunk')return{speaking:true,turn_end:true};if(body.op==='finish')return{text:'Quick question?'};return{};},onState(){},onPartial(){},onComplete:(text,sid)=>completed.push([text,sid]),onError:e=>{throw e;}});
 await c.start('session-a');process.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(16000).fill(.1)}});await tick();await tick();
 assert.deepEqual(completed,[['Quick question?','session-a']]);assert.equal(c.active,false);assert.equal(stopped,1);assert.deepEqual(sent.map(x=>x[1].op),['start','chunk','finish']);
 let resolve;navigator.mediaDevices.getUserMedia=()=>new Promise(r=>resolve=r);const pending=c.start('old');c.cancel();resolve(stream);await pending;
 assert.equal(stopped,2);assert.equal(sent.filter(x=>x[0]==='old').length,0);
});
test('staged upload completion is fenced by session, restores on return, and survives upload retry',async()=>{
 const nodes=new Map(['chat-input-row','chat-preview-bar','chat-voice-btn','chat-attach-btn','chat-file-input'].map(id=>[id,new Element()]));
 const document={createElement:tag=>new Element(tag)},storage=new Map();let active='a',finishUpload,calls=[],changed=0,errors=[];
 const {media}=await modules({document,window:{speechSynthesis:{cancel(){}}},navigator:{},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},URL:{createObjectURL:()=>'',revokeObjectURL(){}}});
 const client={upload:async(sid,body)=>{calls.push(['upload',sid,body]);return new Promise(r=>finishUpload=r);},removeUpload:async(sid,id)=>calls.push(['delete',sid,id])};
 const controls=media.mountComposerMedia({root:{querySelector:s=>nodes.get(s.slice(1))},client,ensureSession:async()=>active,note:t=>errors.push(t),changed:()=>changed++,refresh:async()=>{},guard:fn=>async(...args)=>{try{return await fn(...args);}catch(e){errors.push(e.message);}},getState:()=>({shutoff:false})});
 controls.setAvailable(true);controls.open('a');const file={name:'notes.md',type:'',size:8,arrayBuffer:async()=>Uint8Array.from(Buffer.from('Evidence')).buffer};
 // Use an exact byte buffer, independent of pooled Node Buffer offsets.
 file.arrayBuffer=async()=>Uint8Array.from(Buffer.from('Evidence')).buffer;
 nodes.get('chat-file-input').files=[file];const upload=nodes.get('chat-file-input').onchange();await tick();assert.equal(controls.uploading,true);assert.throws(()=>controls.snapshot(),/Finish or remove/);
 active='b';controls.open('b');finishUpload({preview:false});await upload;assert.equal(controls.count,0);assert.deepEqual([...controls.snapshot()],[]);
 active='a';controls.open('a');assert.equal(controls.count,1);const ids=[...controls.snapshot()];assert.equal(ids.length,1);assert.equal(calls[0][1],'a');assert.equal(calls[0][2].media_type,'text/markdown');
 controls.committed('a',ids);assert.equal(controls.count,0);
 client.upload=async(sid,body)=>{calls.push(['upload',sid,body]);throw Error('temporary failure');};nodes.get('chat-file-input').files=[file];await nodes.get('chat-file-input').onchange();assert.equal(controls.uploading,true);assert.throws(()=>controls.snapshot());
 client.upload=async()=>({preview:false});const retry=nodes.get('chat-preview-bar').children[0].children.find(c=>c.textContent==='Retry');await retry.onclick();assert.equal(controls.uploading,false);
 controls.destroy();await tick();assert.equal(calls.filter(c=>c[0]==='delete').length,1);assert(changed>0);
});
test('v2 structured-text/media formats and speech redaction retain their behavior',async()=>{
 const {media}=await modules();for(const name of ['n.md','n.json','n.csv','n.pdf','n.png','n.mp3','n.mp4','n.yaml'])assert(media.allowedFile({name,type:''}));
 assert.equal(media.allowedFile({name:'program.exe',type:''}),false);
 const speech=media.textForSpeech('Read this '+String.fromCharCode(96).repeat(3)+'private code'+String.fromCharCode(96).repeat(3)+' https://example.test Bearer sk-1234567890abcdef');
 assert(!speech.includes('private code'));assert(!speech.includes('example.test'));assert(!speech.includes('sk-'));assert(speech.includes('[redacted]'));
});

test('composer uses advertised attachment size/count limits instead of 25 MB and 10',async()=>{
 const nodes=new Map(['chat-input-row','chat-preview-bar','chat-voice-btn','chat-attach-btn','chat-file-input'].map(id=>[id,new Element()]));
 const errors=[],uploads=[];
 const {media}=await modules({document:{createElement:tag=>new Element(tag)},window:{speechSynthesis:{cancel(){}}},navigator:{},localStorage:{getItem:()=>null,setItem(){}},URL:{createObjectURL:()=>'',revokeObjectURL(){}}});
 const controls=media.mountComposerMedia({root:{querySelector:s=>nodes.get(s.slice(1))},client:{upload:async(sid,body)=>{uploads.push(body);return{preview:false};},removeUpload:async()=>{}},ensureSession:async()=>'limits',note:t=>errors.push(t),changed(){},refresh:async()=>{},guard:fn=>fn,getState:()=>({shutoff:false})});
 controls.setAvailable(true,{max_file_size_mb:1,max_per_message:1});controls.open('limits');
 const file=size=>({name:'notes.txt',type:'text/plain',size,arrayBuffer:async()=>new Uint8Array([120]).buffer});
 nodes.get('chat-file-input').files=[file(1024*1024+1)];await nodes.get('chat-file-input').onchange();
 assert.equal(uploads.length,0);assert(errors.some(e=>e.includes('1 MB')));
 nodes.get('chat-file-input').files=[file(1),file(1)];await nodes.get('chat-file-input').onchange();
 assert.equal(uploads.length,1);assert(errors.some(e=>e.includes('At most 1')));
 controls.setAvailable(true,{max_file_size_mb:30,max_per_message:2});
 nodes.get('chat-file-input').files=[file(26*1024*1024)];await nodes.get('chat-file-input').onchange();
 assert.equal(uploads.length,2);assert.equal(controls.count,2);
 controls.destroy();
});

test('missing and null limits allow file sizes and counts beyond former caps',async()=>{
 const nodes=new Map(['chat-input-row','chat-preview-bar','chat-voice-btn','chat-attach-btn','chat-file-input'].map(id=>[id,new Element()]));
 const errors=[],uploads=[];
 const {media}=await modules({document:{createElement:tag=>new Element(tag)},window:{speechSynthesis:{cancel(){}}},navigator:{},localStorage:{getItem:()=>null,setItem(){}},URL:{createObjectURL:()=>'',revokeObjectURL(){}}});
 const controls=media.mountComposerMedia({root:{querySelector:s=>nodes.get(s.slice(1))},client:{upload:async(sid,body)=>{uploads.push(body);return{preview:false};},removeUpload:async()=>{}},ensureSession:async()=>'unrestricted',note:t=>errors.push(t),changed(){},refresh:async()=>{},guard:fn=>fn,getState:()=>({shutoff:false})});
 controls.setAvailable(true);controls.open('unrestricted');
 const file=()=>({name:'notes.txt',type:'text/plain',size:300*1024*1024,arrayBuffer:async()=>new Uint8Array([120]).buffer});
 nodes.get('chat-file-input').files=Array.from({length:12},file);await nodes.get('chat-file-input').onchange();
 assert.equal(uploads.length,12);assert.equal(errors.length,0);
 controls.setAvailable(true,{max_file_size_mb:null,max_per_message:null});
 nodes.get('chat-file-input').files=[file()];await nodes.get('chat-file-input').onchange();
 assert.equal(uploads.length,13);assert.equal(errors.length,0);controls.destroy();
});
