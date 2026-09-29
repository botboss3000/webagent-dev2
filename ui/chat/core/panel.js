import {app} from '../../shared/js/state.js';
import * as chat from './client.js';
import {reconcilePublicNodes} from './projection.js';
import {addChatBubble} from '../js/chat-bubble.js';
import {renderActivity, disconnectActivity} from './activity-view.js';

let session=null,unsubscribe=null,epoch=0,pending=[],lastState=null;
function current(id,generation) {
  return id===session && id===app.currentSessionId && generation===epoch;
}
function createNode(item) {
  if(!['user','answer','notice','failure'].includes(item.kind) || typeof item.payload.text!=='string') return null;
  const role=item.kind==='user'?'user':'agent';
  const node=addChatBubble(role,item.payload.text,'published'+(item.pending?' pending':''),
    undefined,item.run_id,item.id,item.created_at);
  if(item.pending) {
    const label=document.createElement('span');
    label.dataset.submissionStatus='pending';
    label.setAttribute('role','status');
    label.textContent='Pending acceptance';
    node.appendChild(label);
  }
  return node;
}
function render(state) {
  if(!app.chatMessages || state.sessionId!==session || session!==app.currentSessionId) return;
  lastState=state;
  if(state.revoked) app.loadChatComponents?.(null);
  else if(state.items.length) app.ensureChatComponents?.(session);
  const accepted=new Set(state.items.filter(x=>x.kind==='user').map(x=>x.payload.client_message_id));
  pending=pending.filter(entry=>!accepted.has(entry.message.client_message_id));
  reconcilePublicNodes(app.chatMessages,state,pending,createNode);
  const active=state.runs.filter(run=>['queued','running','finalizing','stopping'].includes(run.state));
  app.isProcessing=active.length>0;
  if(active.length) {
    const phase=active.at(-1).state;
    app.chatActivityStart?.({queued:'Message queued…',running:'Working…',
      finalizing:'Checking final response…',stopping:'Stopping…'}[phase]);
  } else {
    app._stopPending=false;
    app.chatActivityStop?.();
  }
  renderActivity(state,app.chatMessages);
  app._reLandAnchor?.();
  window.dispatchEvent(new CustomEvent('webagent-public-chat-state',{detail:{sessionId:session,runs:state.runs}}));
}
export function disconnectPanel() {
  epoch++;
  disconnectActivity();
  app.loadChatComponents?.(null);
  unsubscribe?.();unsubscribe=null;
  if(session) chat.closeSession(session);
  session=null;pending=[];lastState=null;
}
export async function loadPanel(id,{persisted=true,onReady=()=>{}}={}) {
  if(session===id && lastState) {onReady();return refreshPanel();}
  disconnectPanel();
  session=id;
  const generation=epoch;
  await chat.initialize(app.currentUserId);
  if(!current(id,generation)) return;
  app.chatMessages.dataset.publicConversation='true';
  app.chatMessages.replaceChildren();
  const restoredPending=await chat.pendingMessages(id);
  if(!current(id,generation)) return;
  pending=restoredPending;
  unsubscribe=chat.subscribe(id,state=>{render(state);if(current(id,generation)) onReady();});
  if(!persisted) {render(chat.stateFor(id));onReady();}
  if(persisted) {
    // Background resources must not hold up the public transcript.
    void app.loadChatComponents?.(id);
    await chat.openSession(id);
  }
  if(current(id,generation)) void chat.drainOutbox();
}
export async function refreshPanel() {
  if(session) return chat.openSession(session);
}
export async function queueSubmission(message) {
  const id=app.currentSessionId,agent=app.currentAgentId;
  await chat.initialize(app.currentUserId);
  if(id!==app.currentSessionId) throw new Error('The active session changed before submission');
  const entry=await chat.queueMessage(id,agent,message);
  if(id!==app.currentSessionId) {void chat.drainOutbox();return entry;}
  if(session!==id) await loadPanel(id,{persisted:false});
  if(!pending.some(row=>row.id===entry.id)) pending.push(entry);
  app.beginUserTurnAnchor?.();
  render(chat.stateFor(id));
  const node=[...app.chatMessages.children].find(el=>el.dataset.clientMessageId===message.client_message_id);
  if(node) app.anchorUserTurn?.(node);
  void chat.drainOutbox();
  return entry;
}
export async function stopPanel() {
  const id=app.currentSessionId;
  await chat.initialize(app.currentUserId);
  await chat.command('/sessions/'+encodeURIComponent(id)+'/stop',{});
  if(id===session) await refreshPanel();
}
export function publicMessages() {
  if(!lastState) return [];
  return lastState.items.filter(item=>!item.deleted).map(item=>({
    id:item.id,role:item.kind==='user'?'user':'assistant',content:item.payload.text || '',
    created_at:item.created_at,session_seq:item.position,metadata:{},
  }));
}
window.addEventListener('webagent-chat-send-status',event=>{
  if(event.detail.sessionId!==session) return;
  const entry=pending.find(row=>row.message.client_message_id===event.detail.id);
  if(entry) {
    entry.status=event.detail.status;
    const node=[...app.chatMessages.children].find(el=>el.dataset.clientMessageId===event.detail.id);
    const label=node?.querySelector('[data-submission-status]');
    if(label) label.textContent=({pending:'Pending acceptance',offline:'Waiting for connection',
      accepted:'Accepted',rejected:'Message was not accepted'})[entry.status] || 'Pending acceptance';
  }
});
document.addEventListener('webagent-auth-transition-start',disconnectPanel);
window.addEventListener('admin-status-loaded',()=>{
  if(lastState && app.chatMessages) renderActivity(lastState,app.chatMessages);
});
