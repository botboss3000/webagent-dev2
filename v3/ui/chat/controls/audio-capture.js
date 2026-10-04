// V2 PCM16 capture and server VAD, carried by authenticated V3 HTTP frames.
export function downsampleToPcm16(input,inputRate,outputRate=16000){
 if(!input?.length||!inputRate||outputRate>inputRate)return new ArrayBuffer(0);
 const ratio=inputRate/outputRate,out=new Int16Array(Math.max(1,Math.floor(input.length/ratio)));
 for(let i=0;i<out.length;i++){const start=Math.floor(i*ratio),end=Math.min(input.length,Math.max(start+1,Math.floor((i+1)*ratio)));let sum=0;for(let j=start;j<end;j++)sum+=input[j];const sample=Math.max(-1,Math.min(1,sum/(end-start)));out[i]=sample<0?Math.round(sample*32768):Math.round(sample*32767);}return out.buffer;
}
export function encodeBytes(bytes){let s='';for(let at=0;at<bytes.length;at+=8192)s+=String.fromCharCode(...bytes.subarray(at,at+8192));return btoa(s);}
export function createAudioCapture({send,onState,onPartial,onComplete,onError}){
 let active=null,generation=0;
 const valid=c=>active===c&&c.generation===generation;
 function stop(c){clearTimeout(c.timer);c.processor?.disconnect();c.source?.disconnect();c.sink?.disconnect();c.context?.close().catch(()=>{});c.stream?.getTracks().forEach(t=>t.stop());}
 async function request(c,body){for(let n=0;;n++){if(!valid(c))throw Error('Voice cancelled');try{return await send(c.sid,{capture_id:c.id,...body});}catch(e){if(n>=2||!valid(c)||(e.status&&e.status<500))throw e;onState('reconnecting');await new Promise(r=>setTimeout(r,350*2**n));}}}
 function fail(c,error){if(!valid(c))return;stop(c);active=null;++generation;if(c.id)send(c.sid,{op:'cancel',capture_id:c.id}).catch(()=>{});onState('idle');onError(error);}
 async function pump(c){
  if(c.pumping)return;c.pumping=true;
  try{
   while(valid(c)&&c.queue.length){const data=encodeBytes(c.queue.shift());const r=await request(c,{op:'chunk',seq:++c.seq,data});if(!valid(c))return;if(r.partial)onPartial(r.partial);if(r.speaking)onState('user_speaking');if(r.turn_end){c.queue=[];c.pending=new Uint8Array();c.finishing=true;stop(c);break;}}
   if(valid(c)&&c.finishing){onState('transcribing');const r=await request(c,{op:'finish'});if(!valid(c))return;active=null;++generation;onState('idle');await onComplete(r.text||'',c.sid);}
  }catch(e){fail(c,e);}finally{c.pumping=false;}
 }
 function append(c,bytes){const next=new Uint8Array(c.pending.length+bytes.length);next.set(c.pending);next.set(bytes,c.pending.length);c.pending=next;while(c.pending.length>=16000){c.queue.push(c.pending.slice(0,16000));c.pending=c.pending.slice(16000);}if(c.queue.length>64){fail(c,Error('Voice connection is too slow. Try again.'));return;}pump(c);}
 async function finish(){const c=active;if(!c||c.finishing)return;c.finishing=true;if(c.recorder){onState('finishing');c.recorder.stop();return;}stop(c);if(c.pending.length)c.queue.push(c.pending);c.pending=new Uint8Array();onState('finishing');await pump(c);}
 async function start(sid){
  if(active)return false;
  if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('Voice needs microphone access on a secure connection.');
  const c={sid,generation:++generation,id:'',seq:0,queue:[],pending:new Uint8Array(),pumping:false,finishing:false};active=c;
  try{
   onState('requesting_microphone');c.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
   if(!valid(c)){stop(c);return false;}
   onState('connecting');const ready=await send(sid,{op:'start'});c.id=ready.capture_id;
   if(!valid(c)){stop(c);send(sid,{op:'cancel',capture_id:c.id}).catch(()=>{});return false;}
   const Context=window.AudioContext||window.webkitAudioContext;
   if(Context){c.context=new Context();await c.context.resume();c.source=c.context.createMediaStreamSource(c.stream);c.processor=c.context.createScriptProcessor(4096,1,1);c.sink=c.context.createGain();c.sink.gain.value=0;c.processor.onaudioprocess=e=>{if(valid(c)&&!c.finishing)append(c,new Uint8Array(downsampleToPcm16(e.inputBuffer.getChannelData(0),c.context.sampleRate)));};c.source.connect(c.processor);c.processor.connect(c.sink);c.sink.connect(c.context.destination);}
   else{
    if(!window.MediaRecorder)throw Error('Audio recording is unavailable.');
    const mime=['audio/webm','audio/ogg','audio/mp4'].find(t=>MediaRecorder.isTypeSupported(t));if(!mime)throw Error('No supported recording format.');
    c.recorder=new MediaRecorder(c.stream,{mimeType:mime});const chunks=[];let size=0;
    c.recorder.ondataavailable=e=>{if(!valid(c))return;size+=e.data.size;if(size>10*1024*1024){fail(c,Error('Recording exceeds 10 MB.'));return;}chunks.push(e.data);};
    c.recorder.onstop=async()=>{stop(c);if(!valid(c))return;try{onState('transcribing');const blob=new Blob(chunks,{type:mime});const r=await request(c,{op:'recording',media_type:mime,data:encodeBytes(new Uint8Array(await blob.arrayBuffer()))});if(valid(c)){active=null;++generation;onState('idle');await onComplete(r.text||'',sid);}}catch(e){fail(c,e);}};c.recorder.start(500);
   }
   c.timer=setTimeout(()=>finish(),30000);onState('listening');return true;
  }catch(e){fail(c,e);return false;}
 }
 function cancel(){const c=active;if(!c)return;active=null;++generation;if(c.recorder?.state==='recording')c.recorder.stop();stop(c);if(c.id)send(c.sid,{op:'cancel',capture_id:c.id}).catch(()=>{});onState('idle');}
 return {start,finish,cancel,get active(){return !!active;}};
}
