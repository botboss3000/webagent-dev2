// V2-style panel/client separation; markup and message protocol remain V3.
import { createChatClient } from './client.js';
import {createMessage} from './message-view.js';
import {createActivityView} from './activity-view.js';
import {closeChatElementFullView} from './full-view.js';
import {currentChatProfile} from '../controls/chat-config.js';
import { applyChatConfig } from '../controls/chat-config.js';
import { mountSessionDropdown } from '../controls/session-dropdown/controller.js';
import { createSessionList } from '../controls/session-dropdown/list.js';
const mountedPanels = new WeakMap();
export async function mountChatPanel({request, guard, note, keyId, forgetKey, refresh, getState, getAgentId, getIdentity=()=>({})}) {
  const root = document.getElementById('chat-panel');
  mountedPanels.get(root)?.destroy();
  const $ = id => root.querySelector('#' + id);
  let session = '';
  const activity=createActivityView({app:{get currentSessionId(){return session;}},isAdmin:()=>!!getIdentity().is_admin,chatUiFlag:(key,fallback)=>currentChatProfile()?.[key]??fallback});
  let activityRuns=[];
  const paintActivity=()=>activity.render({sessionId:session,runs:activityRuns.map(r=>({...r,state:({completed:'completed',failed:'failed',stopped:'cancelled',waiting_approval:'running',waiting_gate:'finalizing'})[r.status]||r.status}))},$('chat-messages-inner'));
  const client = createChatClient({request, note});
  function button(text,fn){const node=document.createElement('button');node.type='button';node.textContent=text;node.onclick=guard(fn);return node;}
async function mountChat(){
  const response=await fetch('/v3-ui/chat/chat.html?v=chat-partial-20260930',{credentials:'same-origin'});
  if(!response.ok)throw Error('Chat shell unavailable');
  const template=document.createElement('template');
  template.innerHTML=await response.text();
  for(const id of ['chat-header','chat-messages','chat-messages-inner','sessions','new','runs','composer','chat-input','chat-send','chat-stop-btn']){
    if(!template.content.querySelector('#'+id))throw Error('Chat shell is incomplete: '+id);
  }
  root.replaceChildren(template.content);
}
await mountChat();
let sessionItems=[];
const dropdown=mountSessionDropdown($('session-dropdown'),{refresh:()=>sessions(),select:open,guard,resetSearch:()=>sessionList.reset()});
const sessionList=createSessionList($('session-dropdown'),{guard,refresh:()=>sessions(),select:open,client,recycled:()=>{activity.disconnect();resetReads();session='';client.disconnect();$('chat-messages-inner').replaceChildren();$('runs').replaceChildren();const url=new URL(location.href);url.searchParams.delete('session_id');history.replaceState({},'',url);}});
$('session-dropdown').configure=cfg=>sessionList.configure(cfg);
const chatPanel=root;
const updateChatClearance=()=>{
  const header=$('chat-header').getBoundingClientRect().height;
  const footer=$('chat-input-area').getBoundingClientRect().height + (parseFloat(getComputedStyle($('chat-input-area')).bottom)||0);
  if(header)chatPanel.style.setProperty('--v3-chat-header-clearance',`${Math.ceil(header)+12}px`);
  if(footer)chatPanel.style.setProperty('--v3-chat-footer-clearance',`${Math.ceil(footer)+16}px`);
};
const chatClearanceObserver=new ResizeObserver(updateChatClearance);
chatClearanceObserver.observe($('chat-header'));
chatClearanceObserver.observe($('chat-input-area'));
let readGeneration=0, readThrough=0, readPending=0, readTimer=0;
const readAbort=new AbortController();
const visibleRows=new Set();
function queueRead(){
  if(document.visibilityState!=='visible'||!session)return;
  for(const row of visibleRows){if(row.isConnected)readPending=Math.max(readPending,Number(row.dataset.seq)||0);}
  clearTimeout(readTimer);
  if(readPending<=readThrough)return;
  const id=session, generation=readGeneration, through=readPending;
  readTimer=setTimeout(async()=>{try{const result=await client.read(id,through);if(generation!==readGeneration||id!==session)return;readThrough=Math.max(readThrough,result.session?.read_through_seq||through);await sessions();}catch(error){if(generation===readGeneration)note('Read acknowledgment failed: '+error.message);}},250);
}
const readObserver=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting&&entry.intersectionRect.height>0)visibleRows.add(entry.target);else visibleRows.delete(entry.target);}queueRead();},{root:$('chat-messages'),threshold:0});
document.addEventListener('visibilitychange',queueRead,{signal:readAbort.signal});
function resetReads(){++readGeneration;clearTimeout(readTimer);readThrough=0;readPending=0;visibleRows.clear();readObserver.disconnect();}
function message(item){const target=$('chat-messages-inner');if([...target.children].some(row=>row.dataset.publicId===item.id))return;const row=createMessage(item);if(!row)return;target.append(row);readObserver.observe(row);paintActivity();}
// Base presentation is display-only. Never turn model-supplied choice or
// approval descriptions into an action or an authorization control.
function components(items){
  if(!Array.isArray(items))return;
  for(const row of items.slice(0,64)){
    const spec=row?.component;
    if(!spec||spec.authority!=='display_only'||!['status','todo_list','choice','approval','form'].includes(spec.type))continue;
    const card=document.createElement('article');card.className='v3-message assistant';
    const body=document.createElement('div');body.className='message-content';
    const heading=document.createElement('strong');heading.textContent=spec.title||'Session display';
    body.append(heading);
    const data=spec.data||{};
    const lines=spec.type==='status'?data.sections?.map(item=>[item.label,item.value].filter(Boolean).join(': ')):
      spec.type==='todo_list'?data.items?.map(item=>(item.done?'☑ ':'☐ ')+item.label):
      spec.type==='choice'?data.options?.map(item=>'Option: '+item.label):
      spec.type==='form'?data.fields?.map(item=>'Field: '+item.label):
      [data.message];
    for(const value of Array.isArray(lines)?lines.slice(0,100):[]){
      if(typeof value!=='string')continue;
      const line=document.createElement('p');line.textContent=value;body.append(line);
    }
    if(['choice','approval','form'].includes(spec.type)){
      const note=document.createElement('small');note.textContent='Display only — no action submitted';body.append(note);
    }
    card.append(body);$('chat-messages-inner').append(card);
  }
}
function tail(){const box=$('chat-messages');box.scrollTop=box.scrollHeight;}
async function sessions(){const data=await client.sessions();$('sessions').replaceChildren();for(const item of data.sessions){const option=document.createElement('option');option.value=item.id;option.textContent=(item.title||'Conversation')+' — '+(item.agent_id||'shared_default');$('sessions').append(option);}$('sessions').value=session;sessionItems=data.sessions;sessionList.update(sessionItems,session);return data.sessions;}
async function open(id){activity.disconnect();closeChatElementFullView();resetReads();session=id;const url=new URL(location.href);url.searchParams.set('session_id',id);history.replaceState({},'',url);await client.open(id,data=>{readThrough=data.session.read_through_seq||0;const sessionAgentId=data.session.agent_id||'shared_default';$('chat-input-row').title='Conversation agent: '+sessionAgentId;$('sessions').value=id;sessionList.update(sessionItems,id);document.dispatchEvent(new Event('v3:session-open'));$('chat-messages-inner').replaceChildren();for(const item of data.messages)message(item);components(data.components||[]);tail();runs(data.runs||[]);},item=>{message(item);tail();});}
async function cancel(id){await client.cancel(id,{client_request_id:keyId('cancel:'+id)});await refresh();}
function runs(items){activityRuns=items.filter(run=>run.session_id===session||!run.session_id);if(session)paintActivity();$('runs').replaceChildren();const relevant=items.filter(run=>run.session_id===session||!run.session_id);const active=relevant.find(run=>['queued','running','waiting_approval','waiting_gate'].includes(run.status));$('chat-input-row').dataset.runStatus=active?.status||relevant[0]?.status||'Ready';$('chat-stop-btn').hidden=!active||$('chat-stop-btn').dataset.configEnabled==='false';$('chat-stop-btn').dataset.run=active?.id||'';$('chat-stop-btn').style.display=$('chat-stop-btn').hidden?'none':'';$('chat-send').disabled=!!active||!!getState().shutoff;
// CONTEXT-REPORT-UI: render only backend allowlisted counts as text, including
// completed runs. Bytes are logical envelope measurements, not billed tokens.
for(const run of relevant){if(run.context_summary){const summary=run.context_summary;const detail=document.createElement('details');detail.className='run-card';const heading=document.createElement('summary');heading.textContent='Context usage — measured bytes';const text=document.createElement('p');text.textContent='Envelope '+summary.envelope_bytes+' / '+summary.max_bytes+' bytes. History '+summary.history_json_bytes+'; instructions '+summary.instruction_json_bytes+'; tools '+summary.schemas_json_bytes+'. Not provider tokens. Turn '+summary.turn+', call '+summary.call+'.';detail.append(heading,text);$('runs').append(detail);}if(run.status==='completed')continue;const card=document.createElement('section');card.className='run-card';const title=document.createElement('strong');title.textContent=run.status+(run.error?' — '+run.error:'');card.append(title);
if(run.pending && run.status==='waiting_approval'){const p=run.pending,pre=document.createElement('pre');pre.textContent=p.name+'\n'+JSON.stringify(p.arguments,null,2);card.append(pre);for(const decision of ['approve','deny'])card.append(button(decision==='approve'?'Approve action':'Deny action',async()=>{await client.decide(p.id,{decision,client_request_id:keyId('approval:'+p.id+':'+decision)});await refresh();}));}
if(run.uncertain || run.status==='recovery'){const text=document.createElement('p');text.textContent='The previous action may have completed. Verify its outcome before retrying.';const output=document.createElement('textarea');output.placeholder='Known result to accept';output.setAttribute('aria-label','Known action result');card.append(text,output);for(const decision of ['accept','retry','abandon'])card.append(button({accept:'Accept known result',retry:'Authorize retry',abandon:'Abandon run'}[decision],async()=>{await client.recover(run.id,{decision,output:output.value,client_request_id:keyId('recovery:'+run.id+':'+decision+':'+output.value)});await refresh();}));}
if(!run.uncertain && ['paused','stopped','failed'].includes(run.status))card.append(button('Resume run',async()=>{await client.resume(run.id,{client_request_id:crypto.randomUUID()});await refresh();}));if(active?.id===run.id)card.append(button('Cancel run',()=>cancel(run.id)));$('runs').append(card);}}
$('sessions').onchange=guard(()=>open($('sessions').value));
$('new').onclick=guard(async()=>{const item=await client.create({title:'New conversation',agent_id:getAgentId(),client_request_id:crypto.randomUUID()});await sessions();await open(item.id);});
$('composer').onsubmit=guard(async()=>{const text=$('chat-input').value.trim();if(!text)return;if(!session){const item=await client.create({title:text.slice(0,60),agent_id:getAgentId(),client_request_id:keyId('session:'+text)});await sessions();await open(item.id);}await client.send(session,{text,client_message_id:keyId('message:'+session+':'+text)});forgetKey('message:'+session+':'+text);$('chat-input').value='';applyChatConfig();await refresh();});
$('chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('chat-send').disabled)$('composer').requestSubmit();}};
$('chat-stop-btn').onclick=guard(()=>cancel($('chat-stop-btn').dataset.run));

  function disconnect(){activity.disconnect();closeChatElementFullView();window.speechSynthesis?.cancel();resetReads();client.disconnect();}
  function destroy(){
    disconnect();sessionList.destroy();dropdown.destroy();readAbort.abort();chatClearanceObserver.disconnect();
    $('sessions').onchange=null;$('new').onclick=null;$('composer').onsubmit=null;
    $('chat-input').onkeydown=null;$('chat-stop-btn').onclick=null;
  }
  const panel = {open, sessions, runs, disconnect, destroy};
  mountedPanels.set(root, panel);
  return panel;
}
