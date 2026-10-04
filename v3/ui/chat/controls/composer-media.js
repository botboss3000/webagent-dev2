import {createAudioCapture,encodeBytes} from './audio-capture.js';
// V2 staging and conversation presentation; Rust owns routing and all actions.
export function inferMedia(file){
 if(file.type)return file.type.split(';')[0].toLowerCase();
 return ({txt:'text/plain',md:'text/markdown',csv:'text/csv',tsv:'text/tab-separated-values',json:'application/json',jsonl:'application/x-ndjson',yaml:'application/yaml',yml:'application/yaml',toml:'application/toml',xml:'application/xml',html:'text/html',pdf:'application/pdf',png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',webp:'image/webp',gif:'image/gif',svg:'image/svg+xml',mp3:'audio/mpeg',wav:'audio/wav',ogg:'audio/ogg',m4a:'audio/mp4',mp4:'video/mp4',webm:'video/webm'})[file.name.split('.').pop().toLowerCase()]||'application/octet-stream';
}
export function allowedFile(file){const type=inferMedia(file);return ['image/','audio/','video/','text/'].some(p=>type.startsWith(p))||['application/pdf','application/json','application/ld+json','application/x-ndjson','application/x-jsonlines','application/x-yaml','application/yaml','application/toml','application/xml'].includes(type);}
export function textForSpeech(value,maxChars=1800){
 const text=String(value||'').replace(/\x60\x60\x60[\s\S]*?\x60\x60\x60/g,' Code omitted. ').replace(/\x60([^\x60]+)\x60/g,'$1').replace(/!\[[^\]]*\]\([^)]*\)/g,'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/https?:\/\/\S+/g,' link omitted ').replace(/<[^>]+>/g,' ').replace(/\b(?:bearer\s+)?(?:sk-[a-z0-9_-]{12,}|api[_ -]?key\s*[:=]\s*\S+|token\s*[:=]\s*[a-z0-9._-]{12,})/gi,'[redacted]').replace(/[*_~#>|]/g,'').replace(/\s+/g,' ').trim();
 return text.length<=maxChars?text:text.slice(0,maxChars).replace(/\s+\S*$/,'')+'. The rest is available on screen.';
}
export function mountComposerMedia({root,client,ensureSession,note,changed,refresh,guard,getState}){
 const $=id=>root.querySelector('#'+id),pill=$('chat-input-row'),preview=$('chat-preview-bar'),mic=$('chat-voice-btn');
 const abort=new AbortController(),staged=new Map();
 let attachmentLimits={max_file_size_mb:null,max_per_message:null};
 let available=false,session='',generation=0,disposed=false,voiceBusy=false,proposal=null,awaitingRun='',audio=null,speechGeneration=0;
 const caption=document.createElement('div');caption.className='voice-conversation-caption';caption.hidden=true;caption.setAttribute('role','status');caption.setAttribute('aria-live','polite');
 const voiceControls=document.createElement('div');voiceControls.className='voice-conversation-controls';voiceControls.hidden=true;voiceControls.setAttribute('role','group');voiceControls.setAttribute('aria-label','Voice conversation settings');
 const mute=document.createElement('button');mute.type='button';mute.className='voice-conversation-control';
 const muted=()=>localStorage.getItem('webagent_voice_muted')==='true';
 function syncMute(){mute.textContent=muted()?'Unmute':'Mute';mute.setAttribute('aria-pressed',String(muted()));}
 mute.onclick=()=>{localStorage.setItem('webagent_voice_muted',String(!muted()));stopSpeech();syncMute();};syncMute();voiceControls.append(mute);pill.before(caption,voiceControls);
 function show(text,kind=''){caption.textContent=text;caption.dataset.kind=kind;caption.hidden=!text;}
 function stopSpeech(){++speechGeneration;audio?.pause();audio=null;window.speechSynthesis?.cancel();}
 async function speak(text){
  if(muted())return;stopSpeech();const spoken=textForSpeech(text),epoch=speechGeneration,sid=session;if(!spoken)return;
  try{const capabilities=await client.voiceCapabilities(sid);if(sid!==session||epoch!==speechGeneration)return;if(capabilities.tts_available){const result=await client.voiceSpeech(sid,{text:spoken});if(sid!==session||epoch!==speechGeneration)return;audio=new Audio('data:audio/mpeg;base64,'+result.data);await audio.play();return;}}catch{}
  if(sid===session&&epoch===speechGeneration&&!muted()&&window.speechSynthesis&&window.SpeechSynthesisUtterance){const u=new SpeechSynthesisUtterance(spoken);u.rate=1.03;window.speechSynthesis.speak(u);}
 }
 function paint(){
  const files=staged.get(session)||[];preview.replaceChildren();
  for(const item of files){
   const card=document.createElement('div');card.className='chat-preview-item';
   if(item.url){const image=document.createElement('img');image.src=item.url;image.alt=item.file.name;card.append(image);}
   const label=document.createElement('span');label.textContent=item.file.name+(item.error?' — upload failed':item.pending?' — uploading…':'');label.title=item.issue||item.error||item.file.name;card.append(label);
   if(item.error){const retry=document.createElement('button');retry.type='button';retry.textContent='Retry';retry.onclick=guard(()=>upload(item));card.append(retry);}
   const remove=document.createElement('button');remove.type='button';remove.className='chat-preview-remove';remove.textContent='×';remove.setAttribute('aria-label','Remove '+item.file.name);remove.onclick=()=>removeFile(item);card.append(remove);preview.append(card);
  }
  preview.style.display=files.length?'':'none';pill.dataset.attachmentCount=String(files.length);pill.classList.toggle('has-attachments',!!files.length);pill.dataset.composerAvailable=String(available);$('chat-attach-btn').disabled=!available;mic.disabled=!available||voiceBusy||!!getState().shutoff;changed();
 }
 function removeFile(item){item.removed=true;if(item.url)URL.revokeObjectURL(item.url);staged.set(item.sid,(staged.get(item.sid)||[]).filter(x=>x!==item));if(item.uploaded)client.removeUpload(item.sid,item.id).catch(e=>note(e.message));paint();}
 async function upload(item){
  if(item.pending||item.removed)return;item.pending=true;item.error='';paint();
  try{const data=encodeBytes(new Uint8Array(await item.file.arrayBuffer()));const info=await client.upload(item.sid,{client_upload_id:item.id,name:item.file.name,media_type:inferMedia(item.file),data});item.uploaded=true;if(item.removed){await client.removeUpload(item.sid,item.id);return;}if(info.preview){try{await client.describeUpload(item.sid,item.id);}catch(e){item.issue=e.message;if(!disposed&&item.sid===session&&!item.removed)note(item.file.name+': '+e.message);}}}
  catch(e){item.error=e.message;if(!disposed&&item.sid===session&&!item.removed)note(item.file.name+': '+e.message);}finally{item.pending=false;if(!disposed)paint();}
 }
 async function addFiles(files){
  if(!available)return;
  const candidates=Array.from(files||[]);if(!candidates.length)return;
  const origin=session,epoch=generation,sid=await ensureSession();if(disposed||(origin&&origin!==session)||(epoch!==generation&&session!==sid))return;
  const items=staged.get(sid)||[];
  for(const file of candidates){if(attachmentLimits.max_per_message!=null&&items.length>=attachmentLimits.max_per_message){note('At most '+attachmentLimits.max_per_message+' attachments per message.');break;}if(!allowedFile(file)||!file.size||(attachmentLimits.max_file_size_mb!=null&&file.size>attachmentLimits.max_file_size_mb*1024*1024)){note(file.name+': unsupported type or empty file'+(attachmentLimits.max_file_size_mb!=null?', or larger than '+attachmentLimits.max_file_size_mb+' MB.':'.'));continue;}
   const item={sid,id:crypto.randomUUID(),file,pending:false,uploaded:false,removed:false,error:'',url:inferMedia(file).startsWith('image/')&&inferMedia(file)!=='image/svg+xml'?URL.createObjectURL(file):''};items.push(item);staged.set(sid,items);paint();await upload(item);
  }
 }
 const attach=$('chat-attach-btn'),chooser=$('chat-file-input');attach.type='button';mic.type='button';attach.onclick=()=>chooser.click();
 chooser.onchange=guard(async()=>{const files=Array.from(chooser.files||[]);chooser.value='';await addFiles(files);});
 pill.addEventListener('paste',e=>{const files=e.clipboardData?.files;if(files?.length){e.preventDefault();guard(()=>addFiles(files))();}},{signal:abort.signal});
 pill.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();pill.classList.add('dragover');}},{signal:abort.signal});
 pill.addEventListener('dragleave',()=>pill.classList.remove('dragover'),{signal:abort.signal});
 pill.addEventListener('drop',e=>{if(e.dataTransfer?.files.length){e.preventDefault();pill.classList.remove('dragover');guard(()=>addFiles(e.dataTransfer.files))();}},{signal:abort.signal});
 function state(value){
  const recording=['listening','user_speaking'].includes(value),busy=value!=='idle'&&!recording;mic.classList.toggle('recording',recording);mic.classList.toggle('transcribing',busy);mic.setAttribute('aria-pressed',String(value!=='idle'));mic.title=recording?'Listening — click to finish':busy?'Understanding voice…':'Voice conversation';mic.setAttribute('aria-label',mic.title);pill.classList.toggle('dictating',value!=='idle');
  if(value!=='idle')show({requesting_microphone:'Waiting for microphone permission…',connecting:'Connecting voice…',reconnecting:'Voice interrupted. Reconnecting…',listening:'Listening… Speak naturally; silence finishes the turn.',user_speaking:'Listening…',finishing:'Finishing voice turn…',transcribing:'Understanding…'}[value]||value,'working');
 }
 function clearProposal(){proposal=null;for(const b of voiceControls.querySelectorAll('[data-proposal]'))b.remove();}
 async function route(text,sid){
  if(!text.trim()){show('No speech detected. Tap the microphone to try again.','system');return;}if(voiceBusy)return;
  const epoch=generation;voiceBusy=true;paint();show('Understanding your request…','working');
  try{
   const result=await client.voiceRoute(sid,{utterance:text,proposal_id:proposal?.id});if(sid!==session||epoch!==generation)return;
   if(result.action==='request_confirmation'){clearProposal();proposal=result.proposal;show(result.answer,'system');for(const [label,word] of [['Confirm','yes'],['Cancel','no']]){const b=document.createElement('button');b.type='button';b.className='voice-conversation-control';b.dataset.proposal='';b.textContent=label;b.onclick=guard(()=>route(word,sid));voiceControls.append(b);}return;}
   clearProposal();
   if(['dispatch_work','interrupt'].includes(result.action)){const controlled=await client.voiceControl(sid,{ticket:result.ticket});if(sid!==session||epoch!==generation)return;awaitingRun=controlled.run_id||'';show(controlled.message,'system');await refresh();if(awaitingRun){const snapshot=await client.snapshot(sid);if(sid===session&&epoch===generation){const final=snapshot.messages.find(m=>m.role==='assistant'&&m.run_id===awaitingRun);if(final)published(final);}}return;}
   show(result.answer||'Please rephrase your voice request.','answer');await speak(result.answer);
  }catch(e){if(sid===session&&epoch===generation){clearProposal();show(e.message,'system');}}
  finally{if(sid===session&&epoch===generation){voiceBusy=false;paint();}}
 }
 const capture=createAudioCapture({send:(id,b)=>client.voiceAudio(id,b),onState:state,onPartial:t=>show('“'+t+'”','partial'),onComplete:route,onError:e=>{show(e.name==='NotAllowedError'?'Microphone permission is blocked. Allow it in the browser site controls and try again.':e.message,'system');voiceBusy=false;paint();}});
 mic.onclick=guard(async()=>{if(capture.active){await capture.finish();return;}if(!available||voiceBusy)return;const epoch=generation,origin=session;stopSpeech();const sid=await ensureSession();if(disposed||(origin&&origin!==session)||(epoch!==generation&&session!==sid))return;voiceControls.hidden=false;await capture.start(sid);});
 function open(id){capture.cancel();stopSpeech();clearProposal();++generation;session=id;voiceBusy=false;awaitingRun='';show('');voiceControls.hidden=true;paint();}
 function published(item){if(item.role==='assistant'&&item.run_id===awaitingRun){awaitingRun='';show('The agent finished. The full response is in chat.','answer');speak(item.content).catch(()=>{});}}
 function snapshot(sid=session){const files=staged.get(sid)||[];if(files.some(i=>i.pending||i.error||!i.uploaded))throw Error('Finish or remove failed uploads before sending.');return files.map(i=>i.id);}
 function committed(sid,ids){const files=staged.get(sid)||[];for(const i of files.filter(i=>ids.includes(i.id))){i.removed=true;if(i.url)URL.revokeObjectURL(i.url);}staged.set(sid,files.filter(i=>!ids.includes(i.id)));paint();}
 function destroy(){disposed=true;capture.cancel();stopSpeech();abort.abort();attach.onclick=null;chooser.onchange=null;mic.onclick=null;caption.remove();voiceControls.remove();for(const files of staged.values())for(const i of files){i.removed=true;if(i.url)URL.revokeObjectURL(i.url);if(i.uploaded)client.removeUpload(i.sid,i.id).catch(()=>{});}}
 return {open,published,snapshot,committed,destroy,setAvailable(value,limits){attachmentLimits={max_file_size_mb:null,max_per_message:null,...limits};if(available!==value){available=value;paint();}},disconnect:()=>{capture.cancel();stopSpeech();},get count(){return(staged.get(session)||[]).length;},get uploading(){return(staged.get(session)||[]).some(i=>i.pending||i.error);}};
}
