import {app} from '../../shared/js/state.js';
import * as chat from './client.js';
import {reconcilePublicNodes} from './projection.js';
import {addChatBubble} from '../js/chat-bubble.js';
import {renderActivity, disconnectActivity} from './activity-view.js';
import {getPendingAttachments, renderAttachmentElement} from '../../shared/js/attachments.js';
import {apiPath} from '../../shared/js/config.js';
import {authHeaders} from '../../shared/js/left-login.js';
import {renderPublishedArtifact, requestPublishedArtifactGrant} from '../../shared/js/published-artifact.js';

// Session entry restores the reader's position, otherwise starts at the latest message.
import {_sessionFocus, _captureSessionFocus} from '../js/chat-message-cache.js';
let openingPosition=null;
let stopOpeningSettle=null;
function settleOpeningPosition(focus) {
  stopOpeningSettle?.();
  const id=session,generation=epoch;
  const land=()=>{
    if(!current(id,generation) || !scroller) return;
    const anchor=focus.anchorMsgId && [...app.chatMessages.children].find(node=>
      node.dataset.publicId===focus.anchorMsgId || node.dataset.msgId===focus.anchorMsgId);
    const target=focus.atBottom===false && anchor
      ? scroller.scrollTop+anchor.getBoundingClientRect().top-scroller.getBoundingClientRect().top-(app._USER_STEP_PAD || 62)
      : Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    if(app.setTranscriptScrollTop) app.setTranscriptScrollTop(target);
    else scroller.scrollTop=target;
  };
  // Activity and composer layout arrive after the first projection. Keep entry
  // position stable during that bounded settle, but never override reader input.
  const observer=new ResizeObserver(land);
  observer.observe(app.chatMessages);
  observer.observe(scroller);
  const events=['wheel','touchstart','pointerdown','keydown'];
  const stop=()=>{
    observer.disconnect();clearTimeout(timer);
    for(const event of events) scroller?.removeEventListener(event,stop);
    if(stopOpeningSettle===stop) stopOpeningSettle=null;
  };
  for(const event of events) scroller.addEventListener(event,stop,{passive:true});
  const timer=setTimeout(stop,1500);
  stopOpeningSettle=stop;
  land();
}
let session=null,unsubscribe=null,epoch=0,pending=[],lastState=null,scroller=null,loadingOlder=false;
const retrying=new Set();
function current(id,generation) {
  return id===session && id===app.currentSessionId && generation===epoch;
}
function appendUserAttachment(node, attachment) {
  const id=attachment?.attachment_id;
  if(!id || [...node.children].some(child=>child.dataset.attachmentId===id)) return;
  const element=renderAttachmentElement(attachment,{thumbnail:true});
  if(!element) return;
  element.dataset.attachmentId=id;
  node.appendChild(element);
}
function renderUserAttachments(node, item) {
  const ids=Array.isArray(item.payload.attachment_ids)
    ? item.payload.attachment_ids.filter(id=>typeof id==='string' && id) : [];
  if(!ids.length) return;
  const sessionId=session, generation=epoch;
  if(item.pending) {
    for(const attachment of getPendingAttachments()) {
      if(ids.includes(attachment.attachment_id)) appendUserAttachment(node,attachment);
    }
  }
  // Published messages carry stable IDs, while the upload API owns MIME type
  // and current content grants. Resolve those for cold history and outbox restores.
  for(const id of ids) {
    if([...node.children].some(child=>child.dataset.attachmentId===id)) continue;
    fetch(apiPath('/api/v1/upload/'+encodeURIComponent(id)),{headers:authHeaders()})
      .then(response=>response.ok?response.json():null)
      .then(attachment=>{
        if(attachment && current(sessionId,generation) && node.parentNode===app.chatMessages)
          appendUserAttachment(node,{...attachment,attachment_id:id});
      }).catch(()=>{});
  }
}
function createNode(item) {
  if(item.kind==='attachment') {
    const node=addChatBubble('agent','','published',undefined,item.run_id,item.id,item.created_at);
    const artifact=renderPublishedArtifact(item.payload,payload=>
      requestPublishedArtifactGrant(payload,{headers:authHeaders()}));
    if(!artifact) return null;
    node.appendChild(artifact);
    return node;
  }
  if(!['user','answer','notice','failure'].includes(item.kind) || typeof item.payload.text!=='string') return null;
  const role=item.kind==='user'?'user':'agent';
  const node=addChatBubble(role,item.payload.text,'published'+(item.pending?' pending':''),
    undefined,item.run_id,item.id,item.created_at);
  if(role==='user') renderUserAttachments(node,item);
  if(item.kind==='failure' && item.payload.retry?.kind==='finalization'
      && typeof item.payload.retry.candidate_id==='string') {
    const candidate=item.payload.retry.candidate_id;
    const button=document.createElement('button');
    button.type='button';button.dataset.finalizationRetry=candidate;
    button.textContent='Retry final response';
    button.addEventListener('click',async()=>{
      const generation=epoch;
      if(retrying.has(candidate) || !current(item.session_id,generation)) return;
      retrying.add(candidate);button.disabled=true;button.textContent='Retrying final response…';
      try {
        await chat.command('/sessions/'+encodeURIComponent(item.session_id)+'/finalization-retry',
          {candidate_id:candidate});
        if(current(item.session_id,generation)) await refreshPanel();
      } catch(error) {
        if(current(item.session_id,generation)) {
          button.textContent='Retry failed — try again';
          button.title=error.message || 'Could not retry final response';
        }
      } finally {
        retrying.delete(candidate);
        if(button.isConnected && current(item.session_id,generation)) button.disabled=false;
      }
    });
    node.appendChild(button);
  }
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
  for(const button of app.chatMessages.querySelectorAll?.('[data-finalization-retry]') || []) {
    const failure=state.items.find(item=>item.payload.retry?.candidate_id===button.dataset.finalizationRetry);
    if(retrying.has(button.dataset.finalizationRetry)) {
      button.disabled=true;button.textContent='Retrying final response…';
    } else if(failure && state.runs.some(run=>run.parent_run_id===failure.run_id)) {
      button.disabled=true;button.textContent='Final response retry requested';
    }
  }
  const active=state.runs.filter(run=>['queued','running','finalizing','stopping'].includes(run.state));
  app._v2RunActive=active.length>0;
  app.isProcessing=active.length>0;
  if(active.length) {
    const latest=active.at(-1);
    const phase=latest.state;
    app.chatActivityStart?.(latest.kind==='compaction'
      ? (phase==='queued' ? 'Compaction queued…' : 'Compacting…')
      : {queued:'Message queued…',running:'Working…',
          finalizing:'Checking final response…',stopping:'Stopping…'}[phase]);
  } else {
    app._stopPending=false;
    app.endRunControls?.();
    app.chatActivityStop?.();
  }
  app.refreshRunControls?.();
  renderActivity(state,app.chatMessages);
  if(openingPosition && scroller && state.items.length) {
    const focus=openingPosition;
    openingPosition=null;
    settleOpeningPosition(focus);
  } else app._reLandAnchor?.();
  window.dispatchEvent(new CustomEvent('webagent-public-chat-state',{detail:{sessionId:session,runs:state.runs}}));
}
async function onHistoryScroll() {
  if(!scroller || loadingOlder || !lastState?.has_more || scroller.scrollTop>150) return;
  const id=session,generation=epoch;
  const anchor=[...app.chatMessages.children].find(node=>node.dataset.publicId);
  const top=anchor?.getBoundingClientRect().top;
  loadingOlder=true;
  let loaded=false;
  try {
    await chat.loadOlder(id);
    loaded=true;
    if(current(id,generation) && anchor?.isConnected && Number.isFinite(top))
      scroller.scrollTop+=anchor.getBoundingClientRect().top-top;
  } catch(error) {
    if(current(id,generation)) app.chatActivityStart?.('Could not load older messages. Scroll up to retry.');
  } finally {
    loadingOlder=false;
    if(loaded && current(id,generation) && scroller?.clientHeight>0 &&
        scroller.scrollHeight<=scroller.clientHeight && lastState?.has_more)
      queueMicrotask(onHistoryScroll);
  }
}
export function disconnectPanel() {
  stopOpeningSettle?.();
  if(session && lastState) _captureSessionFocus(session);
  openingPosition=null;
  epoch++;
  scroller?.removeEventListener('scroll',onHistoryScroll);scroller=null;loadingOlder=false;
  retrying.clear();
  disconnectActivity();
  app.loadChatComponents?.(null);
  unsubscribe?.();unsubscribe=null;
  if(session) chat.closeSession(session);
  session=null;pending=[];lastState=null;
  app._v2RunActive=null;
  app.refreshRunControls?.();
}
export async function loadPanel(id,{persisted=true,onReady=()=>{}}={}) {
  if(session===id && lastState) {onReady();return refreshPanel();}
  disconnectPanel();
  session=id;
  openingPosition=_sessionFocus.get(id) || {atBottom:true};
  const generation=epoch;
  await chat.initialize(app.currentUserId);
  if(!current(id,generation)) return;
  app.chatMessages.dataset.publicConversation='true';
  app.chatMessages.replaceChildren();
  scroller=app._chatScroller || app.chatMessages.parentElement;
  scroller?.addEventListener('scroll',onHistoryScroll,{passive:true});
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
  if(current(id,generation) && scroller && scroller.scrollHeight<=scroller.clientHeight)
    void onHistoryScroll();
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
