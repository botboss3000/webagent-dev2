import {app} from '../../shared/js/state.js';
import {apiPath} from '../../shared/js/config.js';
import {authHeaders} from '../../shared/js/left-login.js';

let subject='', revisions=new Map(), loadedAt=0, inflight=null, generation=0;

export async function loadNotifications(userId,{force=false}={}) {
  if(!userId) {subject='';revisions.clear();loadedAt=0;generation++;inflight=null;return revisions;}
  if(subject!==userId) {
    subject=userId;revisions=new Map();loadedAt=0;generation++;inflight=null;
  }
  if(!force && Date.now()-loadedAt<5000) return revisions;
  if(inflight) return inflight;
  const ownGeneration=generation;
  const request=(async()=>{
    let after='',next=new Map();
    do {
      const url='/api/v2/chat/notifications'+(after?'?after='+encodeURIComponent(after):'');
      const response=await fetch(apiPath(url),{headers:authHeaders(),cache:'no-store'});
      if(!response.ok) throw new Error('Could not load chat notifications ('+response.status+')');
      const page=await response.json();
      if(!Array.isArray(page.notifications) || typeof page.has_more!=='boolean')
        throw new Error('Invalid chat notification page');
      for(const row of page.notifications) next.set(row.session_id,row);
      if(page.has_more && (!page.next_after || page.next_after===after))
        throw new Error('Invalid chat notification cursor');
      after=page.has_more?page.next_after:'';
    } while(after);
    if(ownGeneration!==generation || subject!==userId) return revisions;
    revisions=next;loadedAt=Date.now();
    return revisions;
  })();
  inflight=request;
  try {return await request;} finally {if(inflight===request) inflight=null;}
}

export async function enrichSessions(rows,userId) {
  const map=await loadNotifications(userId);
  return rows.map(row=>{
    const notification=map.get(row.session_id || row.id);
    if(notification) return {...row,v2_notification:notification,
      has_unread:notification.unread,closing_flag:notification.flag,
      closing_cta:notification.message};
    if(row.v2_notification) return {...row,v2_notification:null,has_unread:false,
      closing_flag:null,closing_cta:null};
    return row;
  });
}

export async function setNotificationUnread(sessionId,unread) {
  await loadNotifications(app.currentUserId);
  let notification=revisions.get(sessionId);
  if(!notification) {
    const response=await fetch(apiPath('/api/v2/chat/sessions/'+encodeURIComponent(sessionId)+'/notification'),
      {headers:authHeaders(),cache:'no-store'});
    if(!response.ok) throw new Error('Could not check chat notification ('+response.status+')');
    notification=(await response.json()).notification;
    if(!notification) return false;
  }
  const response=await fetch(apiPath('/api/v2/chat/sessions/'+encodeURIComponent(sessionId)+'/acknowledgement'),
    {method:'POST',headers:{...authHeaders(),'Content-Type':'application/json'},
      body:JSON.stringify({cursor:notification.cursor,unread})});
  if(!response.ok) throw new Error('Could not update chat notification ('+response.status+')');
  revisions.set(sessionId,{...notification,unread});
  window.dispatchEvent(new CustomEvent('v2-notifications-changed',{detail:{sessionId}}));
  return true;
}

window.addEventListener('webagent-public-chat-state',event=>{
  if(!event.detail?.runs?.some(run=>['completed','failed','cancelled'].includes(run.state))) return;
  if(!app.currentUserId) return;
  const before=JSON.stringify([...revisions]);
  void loadNotifications(app.currentUserId,{force:true}).then(rows=>{
    if(JSON.stringify([...rows])!==before)
      window.dispatchEvent(new CustomEvent('v2-notifications-changed'));
  }).catch(()=>{});
});
document.addEventListener('webagent-auth-transition-start',()=>{
  subject='';revisions.clear();loadedAt=0;generation++;inflight=null;
});
