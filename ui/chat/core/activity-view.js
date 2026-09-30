import {app} from '../../shared/js/state.js';
import {apiPath} from '../../shared/js/config.js';
import {authHeaders, isAdmin} from '../../shared/js/left-login.js?v=253';
import {chatUiFlag} from '../../shared/js/app-prompts.js';
import {activityGroups, watchActivity} from './activity.js';

let session=null, stop=null, journal=null, publicState=null, container=null, error='';
const nodes=new Map();
function styles() {
  if(document.getElementById('v2-activity-styles')) return;
  const link=document.createElement('link');link.id='v2-activity-styles';
  link.rel='stylesheet';link.href=new URL('./activity.css',import.meta.url).href;
  document.head.appendChild(link);
}
function textNode(tag, text, className='') {
  const node=document.createElement(tag);node.className=className;node.textContent=text;
  return node;
}
function detailsFor(row) {
  const detail=document.createElement('details');
  detail.className='v2-activity-row';detail.dataset.category=row.category;
  detail.appendChild(textNode('summary',row.label));
  if(row.text) detail.appendChild(textNode('p',row.text));
  // Full payloads stay behind an explicit diagnostic disclosure, never become
  // public message bubbles and never run HTML supplied by an execution event.
  detail.appendChild(textNode('pre',JSON.stringify(row.events.map(e=>({
    kind:e.kind,time:e.created_at,payload:e.payload})),null,2)));
  return detail;
}
function updateRows(node,group) {
  const body=node.querySelector('.v2-activity-body');
  const signature=JSON.stringify(group.events);
  if(body.dataset.signature===signature) return;
  body.dataset.signature=signature;
  const existing=new Map([...body.children].map(n=>[n.dataset.rowId,n]));
  const wanted=[];
  for(const row of group.rows) {
    let detail=existing.get(row.id);
    if(!detail) {detail=detailsFor(row);detail.dataset.rowId=row.id;}
    const rowSignature=JSON.stringify(row.events);
    if(detail.dataset.signature!==rowSignature) {
      detail.dataset.signature=rowSignature;
      // Keep the focused disclosure and its open state as results arrive.
      detail.firstElementChild.textContent=row.label;
      for(const child of [...detail.children].slice(1)) child.remove();
      if(row.text) detail.appendChild(textNode('p',row.text));
      detail.appendChild(textNode('pre',JSON.stringify(row.events.map(e=>({
        kind:e.kind,time:e.created_at,payload:e.payload})),null,2)));
    }
    detail.dataset.category=row.category;wanted.push(detail);
  }
  let raw=existing.get('raw');
  if(!raw) {
    raw=document.createElement('details');raw.dataset.rowId='raw';
    raw.appendChild(textNode('summary',''));
    raw.appendChild(textNode('pre',''));
    raw.addEventListener('toggle',()=>{
      raw.lastElementChild.textContent=raw.open?JSON.stringify(raw._events,null,2):'';
    });
  }
  raw._events=group.events;
  raw.firstElementChild.textContent='All '+group.events.length+' diagnostic events';
  if(raw.open) raw.lastElementChild.textContent=JSON.stringify(group.events,null,2);
  wanted.push(raw);
  for(const child of [...body.children]) if(!wanted.includes(child)) child.remove();
  let before=body.firstChild;
  for(const child of wanted) {
    if(child!==before) body.insertBefore(child,before);
    before=child.nextSibling;
  }
}
function paint() {
  if(!journal || !publicState || !container || session!==app.currentSessionId) return;
  const groups=activityGroups(journal,publicState.runs), keep=new Set(), tails=new Map();
  for(const group of groups) {
    const boundary=[...container.children].find(n=>n.dataset.publicId===group.boundary_item_id);
    if(!boundary) continue; // The corresponding public page has not loaded.
    keep.add(group.id);
    let node=nodes.get(group.id);
    if(!node) {
      node=document.createElement('details');node.className='v2-activity-group';
      node.dataset.activityGroupId=group.id;
      node.appendChild(textNode('summary','Activity'));
      node.appendChild(textNode('div','','v2-activity-body'));
      node.addEventListener('toggle',()=>{if(node.open && node._group) updateRows(node,node._group);});
      nodes.set(group.id,node);
    }
    node._group=group;
    const parts=Object.entries(group.counts).filter(([,count])=>count)
      .map(([kind,count])=>count+' '+(count===1?kind.replace(/s$/,''):kind));
    const phase={running:'Working…',queued:'Queued…',finalizing:'Checking response…',
      stopping:'Stopping…',completed:'Completed',failed:'Failed',cancelled:'Stopped',
      finished:'Finished'}[group.state] || group.state;
    const progress=group.kind==='compaction' && ['queued','running','finalizing'].includes(group.state)
      ? (group.state==='queued' ? 'Compaction queued…' : 'Compacting…') : phase;
    node.firstElementChild.textContent='Activity'+(parts.length?' · '+parts.join(' · '):'')+
      ' · '+(error || progress);
    node.setAttribute('aria-label','Activity group');
    if(node.open) updateRows(node,group);
    const anchor=tails.get(group.boundary_item_id) || boundary;
    if(anchor.nextSibling!==node) container.insertBefore(node,anchor.nextSibling);
    tails.set(group.boundary_item_id,node);
  }
  for(const [id,node] of nodes) if(!keep.has(id)) {node.remove();nodes.delete(id);}
  app._reLandAnchor?.();
}
export function disconnectActivity() {
  stop?.();stop=null;
  for(const node of nodes.values()) node.remove();
  nodes.clear();session=null;journal=null;publicState=null;container=null;error='';
}
export function renderActivity(state,target) {
  if(state.revoked || !chatUiFlag('show_activity_groups',false) || !isAdmin()) {
    disconnectActivity();return;
  }
  if(session!==state.sessionId) {
    disconnectActivity();session=state.sessionId;styles();
  }
  container=target;publicState=state;
  if(!stop) {
    const id=session;
    stop=watchActivity({sessionId:id,
      request:(cursor,signal)=>fetch(apiPath('/api/v2/chat/sessions/'+encodeURIComponent(id)+'/activity?after='+cursor),
        {headers:authHeaders(),signal,cache:'no-store'}),
      onChange:(value,more)=>{
        if(session!==id) return;
        journal=value;error=more?'Loading activity…':'';paint();
      },
      onError:failure=>{
        if(session!==id) return;
        if([401,403,404].includes(failure.status)) {disconnectActivity();return;}
        error='Activity connection lost; retrying…';paint();
      }});
  }
  paint();
}
window.addEventListener('admin-status-loaded',()=>{
  if(!isAdmin()) disconnectActivity();
});
document.addEventListener('webagent-auth-transition-start',disconnectActivity);
window.addEventListener('webagent-browser-storage-purge',disconnectActivity);
