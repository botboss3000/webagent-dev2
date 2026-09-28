import {apiPath} from '/ui/shared/js/config.js';
import {authHeaders} from '/ui/shared/js/left-login.js';
import {batches} from './transport.js';
import {saveChunk,pendingChunks,deleteChunk} from './outbox.js';

// RECORDER-DETACHED-UPLOAD: every closure belongs to ONE immutable capture.
// New observers may start while this job drains; no token/sequence globals.
export function createUploader({recordingId,clientId,token,health}) {
  const headers={'Content-Type':'application/json',...authHeaders()};
  const intake=apiPath('/api/v1/recordings');
  const leaseKey=id=>'recorder.lease.'+recordingId+':'+id;
  let chain=Promise.resolve(),retryAfter=0,deadline=0,queuedRows=0,persistenceLost=false;
  const report=(reason,resolved=false)=>health(reason,resolved,recordingId);
  return {
    get queuedRows(){return queuedRows;},
    renew(value){if(value)token=value;},
    drain(){deadline=Date.now()+20000;return this.flush([],false,true);},
    flush(records,useBeacon=false,retryNow=false) {
      if(useBeacon && records.length)try{
        const body=new Blob([JSON.stringify({lease_token:token,records})],{type:'application/json'});
        if(body.size<=60000)navigator.sendBeacon(intake,body);
      }catch(_){}
      try{sessionStorage.setItem(leaseKey(clientId),token);}catch(_){}
      queuedRows+=records.length;
      const persisted=(async()=>{
        if(!records.length)return;
        try{await saveChunk({id:recordingId+':'+clientId+':'+records[0].seq,recording_id:recordingId,client_id:clientId,records,created_at:Date.now()});}
        catch(error){persistenceLost ||= error.message==='outbox-memory-limit';report(error.message==='outbox-memory-limit'?'outbox-memory-limit-dropped-events':'outbox-not-durable');}
      })();
      const task=async()=>{
        await persisted;
        try {
          if(!retryNow && Date.now()<retryAfter)return false;
          const remaining=()=>deadline?deadline-Date.now():15000;
          const chunks=await pendingChunks(recordingId);
          for(const chunk of chunks){
            let oldToken='';
            try{oldToken=sessionStorage.getItem(leaseKey(chunk.client_id))||sessionStorage.getItem('recorder.lease.'+chunk.client_id)||'';}catch(_){}
            const lease=()=>chunk.client_id===clientId?token:oldToken;
            if(!lease()){report('unacknowledged-previous-document');return false;}
            const send=async rows=>{
              if(remaining()<=0)throw new Error('stop-drain-timeout');
              const response=await fetch(intake,{method:'POST',
                headers,
                body:JSON.stringify({records:rows,lease_token:lease()}),
                signal:AbortSignal.timeout(Math.max(1,Math.min(15000,remaining())))});
              if(response.status===413 && rows.length>1){
                const mid=Math.ceil(rows.length/2);await send(rows.slice(0,mid));await send(rows.slice(mid));return;
              }
              if(!response.ok)throw new Error('upload-'+response.status);
            };
            for(const rows of batches(chunk.records))await send(rows);
            await deleteChunk(chunk.id);
          }
          report('upload-pending',true);return !persistenceLost;
        }catch(_){retryAfter=Date.now()+2000;report('upload-pending');return false;}
        finally{queuedRows=Math.max(0,queuedRows-records.length);}
      };
      chain=chain.catch(()=>{}).then(task);return chain;
    }
  };
}
