// V2-style panel/client separation; markup and message protocol remain V3.
import { createChatClient } from './client.js';
import {mountTranscriptAnchor} from './transcript-anchor.js';
import {mountComposerMedia} from '../controls/composer-media.js';
import {mountSessionControls} from '../controls/session-controls.js';
import {mountUsageStats} from '../controls/usage-stats.js';
import {createMessage} from './message-view.js';
import {closeGutterOverlays,updateGutterTimes} from './bubble-gutters.js';
import {mountComposerProgress} from './composer-progress.js';
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
  const expandedApprovals=new Set();
  const gutterRuns=new Map();
  function rememberRuns(items){for(const run of items)gutterRuns.set(run.id,run);for(const [id] of [...gutterRuns.entries()].sort((a,b)=>(b[1].updated_at||0)-(a[1].updated_at||0)).slice(100))gutterRuns.delete(id);}
  async function refreshGutterRuns(id=session){const epoch=sessionGeneration;try{const data=await client.snapshot(id);if(id===session&&epoch===sessionGeneration)rememberRuns(data.runs||[]);}catch(error){if(id===session&&epoch===sessionGeneration)note('Message statistics unavailable: '+error.message);}}
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
const anchor=mountTranscriptAnchor({scroller:$('chat-messages'),inner:$('chat-messages-inner'),header:$('chat-header'),getProfile:currentChatProfile});
const progress=mountComposerProgress({root,getProfile:currentChatProfile,isAdmin:()=>!!getIdentity().is_admin});
const stats=mountUsageStats({root,client,note});
const gutterClock=setInterval(()=>{if(document.visibilityState==='visible')updateGutterTimes(root);},1000);
const controls=mountSessionControls({root,client,guard,note,ensureSession,onState:data=>{media?.setAvailable(data?.composer?.attachments===true,data?.composer?.attachment_limits);}});
let submitting=false,sessionGeneration=0,creatingSession=null,media=null;
media=mountComposerMedia({root,client,ensureSession,note,guard,getState,refresh,changed:()=>{applyChatConfig();runs(activityRuns);}});
let sessionItems=[];
const dropdown=mountSessionDropdown($('session-dropdown'),{refresh:()=>sessions(),select:open,guard,resetSearch:()=>sessionList.reset()});
const sessionList=createSessionList($('session-dropdown'),{guard,refresh:()=>sessions(),select:open,client,recycled:()=>{anchor.reset();progress.reset();activity.disconnect();resetReads();session='';stats.disconnect();++sessionGeneration;controls.open('');media.open('');client.disconnect();$('chat-messages-inner').replaceChildren();$('runs').replaceChildren();const url=new URL(location.href);url.searchParams.delete('session_id');history.replaceState({},'',url);}});
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
function message(item){const target=$('chat-messages-inner');if([...target.children].some(row=>row.dataset.publicId===item.id))return;const row=createMessage(item,{getRun:()=>gutterRuns.get(item.run_id),client,getSession:()=>session,note,showContext:run=>stats.show(run)});if(!row)return;target.append(row);readObserver.observe(row);paintActivity();}
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

async function sessions(){const data=await client.sessions();$('sessions').replaceChildren();for(const item of data.sessions){const option=document.createElement('option');option.value=item.id;option.textContent=(item.title||'Conversation')+' — '+(item.agent_id||'shared_default');$('sessions').append(option);}$('sessions').value=session;sessionItems=data.sessions;sessionList.update(sessionItems,session);return data.sessions;}
async function open(id){anchor.reset();progress.open(id);closeGutterOverlays();gutterRuns.clear();++sessionGeneration;session=id;stats.open(id);media.open(id);activity.disconnect();closeChatElementFullView();resetReads();session=id;const url=new URL(location.href);url.searchParams.set('session_id',id);history.replaceState({},'',url);controls.open(id).catch(error=>{if(id===session)note(error.message);});await client.open(id,data=>{readThrough=data.session.read_through_seq||0;const sessionAgentId=data.session.agent_id||'shared_default';$('chat-input-row').title='Conversation agent: '+sessionAgentId;$('sessions').value=id;sessionList.update(sessionItems,id);document.dispatchEvent(new Event('v3:session-open'));$('chat-messages-inner').replaceChildren();for(const item of data.messages)message(item);components(data.components||[]);runs(data.runs||[]);anchor.focus();},item=>{message(item);media.published(item);if(item.role==='assistant')void refreshGutterRuns();void stats.refresh(true);anchor.published(item);});}
async function cancel(id){progress.note('Stopping…');try{await client.cancel(id,{client_request_id:keyId('cancel:'+id)});}finally{await refresh();}}
function runs(items){anchor.updateRuns(items.filter(run=>run.session_id===session||!run.session_id));progress.update(items);void stats.refresh();controls.updateRuns(items.filter(run=>run.session_id===session||!run.session_id));activityRuns=items.filter(run=>run.session_id===session||!run.session_id);rememberRuns(activityRuns);if(session)paintActivity();$('runs').replaceChildren();const relevant=items.filter(run=>run.session_id===session||!run.session_id);const active=relevant.find(run=>['queued','running','waiting_approval','waiting_gate'].includes(run.status));$('chat-input-row').dataset.runStatus=active?.status||relevant[0]?.status||'Ready';$('chat-stop-btn').hidden=!active||$('chat-stop-btn').dataset.configEnabled==='false';$('chat-stop-btn').dataset.run=active?.id||'';$('chat-stop-btn').style.display=$('chat-stop-btn').hidden?'none':'';$('chat-send').disabled=!!active||!!getState().shutoff||submitting||!!media?.uploading;
const continuing=!active&&!media?.count&&!$('chat-input').value.trim()&&$('chat-continue-btn').dataset.configEnabled!=='false';
$('chat-continue-btn').hidden=!continuing;$('chat-continue-btn').style.display=continuing?'':'none';
$('chat-continue-btn').disabled=!!getState().shutoff||submitting;
// Context reports live in the composer panel and each bubble's More menu.
// Ordinary active runs use the progress control and composer Stop button.
// Approval and recovery cards retain their explicit decisions.
for(const run of relevant){if(run.status==='completed'||(!run.uncertain&&['queued','running','waiting_gate'].includes(run.status)))continue;const card=document.createElement('section');card.className='run-card';const title=document.createElement('strong');title.textContent=run.status+(run.error?' — '+run.error:'');card.append(title);
if(run.pending && run.status==='waiting_approval'){
  const p=run.pending;card.classList.add('approval-pill');title.remove();
  const heading=document.createElement('div');heading.className='approval-heading';
  const label=document.createElement('span');label.textContent='Need approval:';
  const pre=document.createElement('pre');pre.id='approval-details-'+p.id;pre.hidden=!expandedApprovals.has(p.id);pre.textContent=JSON.stringify(p.arguments,null,2);
  const toggle=button(p.name,()=>{pre.hidden=!pre.hidden;if(pre.hidden)expandedApprovals.delete(p.id);else expandedApprovals.add(p.id);toggle.setAttribute('aria-expanded',String(!pre.hidden));});
  toggle.className='approval-tool';toggle.setAttribute('aria-expanded',String(!pre.hidden));toggle.setAttribute('aria-controls',pre.id);toggle.title='Show tool call details';
  heading.append(label,toggle);card.append(heading,pre);
  const actions=document.createElement('div');actions.className='approval-actions';
  for(const decision of ['approve','deny']){const action=button(decision==='approve'?'Approve':'Deny',async()=>{await client.decide(p.id,{decision,client_request_id:keyId('approval:'+p.id+':'+decision)});await refresh();});action.className='approval-chip approval-'+decision;actions.append(action);}
  if(active?.id===run.id){const action=button('Cancel run',()=>cancel(run.id));action.className='approval-chip';actions.append(action);}
  card.append(actions);
}
if(run.uncertain || run.status==='recovery'){const text=document.createElement('p');text.textContent='The previous action may have completed. Verify its outcome before retrying.';const output=document.createElement('textarea');output.placeholder='Known result to accept';output.setAttribute('aria-label','Known action result');card.append(text,output);for(const decision of ['accept','retry','abandon'])card.append(button({accept:'Accept known result',retry:'Authorize retry',abandon:'Abandon run'}[decision],async()=>{await client.recover(run.id,{decision,output:output.value,client_request_id:keyId('recovery:'+run.id+':'+decision+':'+output.value)});await refresh();}));}
if(!run.uncertain && ['paused','stopped','failed'].includes(run.status))card.append(button('Resume run',async()=>{await client.resume(run.id,{client_request_id:crypto.randomUUID()});await refresh();}));if(active?.id===run.id&&!card.classList.contains('approval-pill'))card.append(button('Cancel run',()=>cancel(run.id)));$('runs').append(card);}}
$('sessions').onchange=guard(()=>open($('sessions').value));
async function ensureSession(title='New conversation'){
  if(session)return session;
  if(creatingSession)return creatingSession;
  const epoch=sessionGeneration;
  creatingSession=(async()=>{
    const requestKey='session:'+getAgentId()+':'+title;
    const item=await client.create({title:title.slice(0,60),agent_id:getAgentId(),client_request_id:keyId(requestKey)});
    forgetKey(requestKey);
    if(epoch!==sessionGeneration)throw Error('Conversation changed; repeat the action in the selected conversation.');
    await sessions();
    if(epoch!==sessionGeneration)throw Error('Conversation changed; repeat the action.');
    await open(item.id);return item.id;
  })().finally(()=>{creatingSession=null;});
  return creatingSession;
}
$('new').onclick=guard(async()=>{const item=await client.create({title:'New conversation',agent_id:getAgentId(),client_request_id:crypto.randomUUID()});await sessions();await open(item.id);});
async function submit(text,clearInput=false){
  if((!text&&!media.count)||submitting||getState().shutoff||$('chat-send').disabled)return;
  const origin=session,epoch=sessionGeneration,draft=$('chat-input').value;
  progress.note('Sending…');submitting=true;$('chat-send').disabled=true;$('chat-continue-btn').disabled=true;
  try{
    const sid=await ensureSession(text||'Attachments');
    if((origin&&origin!==session)||(epoch!==sessionGeneration&&session!==sid))throw Error('Conversation changed; message was not sent.');
    const ids=media.snapshot(sid),content=text||'Please review the attached files.';
    const requestKey='message:'+sid+':'+content+':'+ids.join(',');
    await client.send(sid,{text:content,attachment_ids:ids,client_message_id:keyId(requestKey)});
    forgetKey(requestKey);media.committed(sid,ids);
    if(clearInput&&sid===session&&$('chat-input').value===draft)$('chat-input').value='';
    applyChatConfig();await refresh();
  }finally{submitting=false;await refresh();}
}
$('composer').onsubmit=guard(()=>submit($('chat-input').value.trim(),true));
// Continue is a new user turn, not an automatic retry of an exhausted run.
$('chat-continue-btn').onclick=guard(()=>submit('Continue'));
$('chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('chat-send').disabled)$('composer').requestSubmit();}};
$('chat-stop-btn').onclick=guard(()=>cancel($('chat-stop-btn').dataset.run));

  function disconnect(){anchor.reset();progress.reset();closeGutterOverlays();gutterRuns.clear();stats.disconnect();media.disconnect();controls.disconnect();activity.disconnect();closeChatElementFullView();window.speechSynthesis?.cancel();resetReads();client.disconnect();}
  function destroy(){
    disconnect();anchor.destroy();progress.destroy();clearInterval(gutterClock);stats.destroy();media.destroy();controls.destroy();sessionList.destroy();dropdown.destroy();readAbort.abort();chatClearanceObserver.disconnect();
    $('sessions').onchange=null;$('new').onclick=null;$('composer').onsubmit=null;
    $('chat-input').onkeydown=null;$('chat-stop-btn').onclick=null;$('chat-continue-btn').onclick=null;
  }
  const panel = {open, sessions, runs, disconnect, destroy};
  mountedPanels.set(root, panel);
  return panel;
}
