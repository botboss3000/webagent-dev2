// v2 dom-utils imports v1 auth and uploads; v3 uses text nodes and its own transport.
const $=id=>document.getElementById(id);
const agentId='shared_default';
let agentModel='';
async function mountAgents(){
  const response=await fetch('/ui/main-panel/agents/agents.html',{credentials:'same-origin'});
  if(!response.ok)throw Error('Agents page unavailable');
  const template=document.createElement('template');
  template.innerHTML=await response.text();
  const grid=template.content.querySelector('#agents-grid');
  if(!grid)throw Error('Agents page is incomplete');
  grid.classList.remove('agents-phantom');
  grid.classList.add('carousel');
  grid.querySelector('.agents-phantom-detail')?.remove();
  const squares=grid.querySelector('.agents-squares');
  squares.replaceChildren();
  const row=document.createElement('div');
  row.className='agent-row';
  row.dataset.agentId=agentId;
  row.innerHTML='<div class="agent-card agent-square activated"><div class="agent-card-top"><div class="agent-card-icon-wrap color-purple"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="12" cy="12" r="3"/></svg></div><div class="agent-card-meta"><div class="agent-card-name-row"><span class="agent-card-name">WebAgent</span><span class="agent-status-dot"></span></div></div></div></div>';
  squares.append(row);
  const detail=document.createElement('div');
  detail.className='agent-card active agent-card-tabs-only';
  detail.innerHTML=`<div class="agent-card-tabs-wrap"><div class="agent-card-tabs" role="tablist"><button type="button" role="tab" aria-selected="true">Model</button></div></div>
    <div class="agent-detail-panel"><div class="agent-detail-content"><div class="agent-detail-body" id="model-page">
    <h2>Model</h2><div class="ac-model-table">
      <div class="ac-ability-row ac-model-cfg-row ac-row-static"><span class="ac-ability-label"><span class="ac-ability-name">Provider</span><span class="ac-ability-desc">Use a ChatGPT subscription for this agent.</span></span><span class="ac-model-cfg-control">ChatGPT Subscription</span></div>
      <div class="ac-ability-row ac-model-cfg-row ac-row-static"><span class="ac-ability-label"><span class="ac-ability-name">Connection</span></span><span class="ac-model-cfg-control"><span id="connection" class="ac-hint"></span><button id="connect" type="button" class="ac-btn ac-btn-primary ac-btn-sm">Click to Issue verification code</button><button id="disconnect" type="button" class="ac-btn ac-btn-sm">Disconnect</button><span id="device" class="ac-subscription-auth"></span></span></div>
      <form id="model-form" class="ac-ability-row ac-model-cfg-row"><label for="model" class="ac-ability-label"><span class="ac-ability-name">Model</span><span class="ac-ability-desc">Model ID used for new chat turns.</span></label><span class="ac-model-cfg-control"><input id="model" class="ac-input ac-input-sm" placeholder="gpt-6-luna" required pattern="[A-Za-z0-9._-]+"><button class="ac-btn ac-btn-primary ac-btn-sm">Save model</button></span></form>
    </div><details id="settings"><summary>Tool policies</summary><form id="policy-form"><div id="rules"></div><button type="submit">Save policies</button></form></details>
    </div></div></div>`;
  const panel=detail.querySelector('.agent-detail-panel');
  panel.remove();
  const region=document.createElement('div');
  region.id='agents-detail-region';
  region.className='open';
  const detailRow=document.createElement('div');
  detailRow.className='agent-row';
  detailRow.dataset.agentId=agentId;
  detailRow.append(detail,panel);
  region.append(detailRow);
  grid.append(region);
  $('agents-grid').replaceWith(grid);
  const route=new URL(location.href);
  route.searchParams.set('tab','agents');
  route.searchParams.set('agent',agentId);
  history.replaceState({},'',route);
  row.onclick=()=>region.scrollIntoView({block:'start',behavior:'smooth'});
}
await mountAgents();

let csrf='',session='',source,epoch=0,state={},poll;
const ids=new Map(),streams=new Map();
const keyId=key=>{if(!ids.has(key))ids.set(key,crypto.randomUUID());return ids.get(key);};
const note=text=>{$('notice').textContent=text;};
async function api(path,method='GET',body){const response=await fetch('/api/v3'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'x-csrf-token':csrf})},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await response.json().catch(()=>({}));if(!response.ok){if(response.status===401)out();throw Error(data.error||data.message||'Request failed ('+response.status+')');}return data;}
const guard=fn=>async event=>{event?.preventDefault();try{await fn(event);note('');}catch(error){note(error.message);}};
function button(text,fn){const node=document.createElement('button');node.type='button';node.textContent=text;node.onclick=guard(fn);return node;}
function out(){csrf='';source?.close();clearInterval(poll);$('app').hidden=true;$('login').hidden=false;}
function message(role,content){const row=document.createElement('article');row.className='v3-message '+role;const label=document.createElement('span');label.className='message-role';label.textContent=role==='user'?'You':role==='assistant'?'Agent':role;const body=document.createElement('div');body.className='message-content';body.textContent=typeof content==='string'?content:JSON.stringify(content);row.append(label,body);$('chat-messages-inner').append(row);return body;}
function tail(){const box=$('chat-messages');box.scrollTop=box.scrollHeight;}
async function sessions(){const data=await api('/sessions');$('sessions').replaceChildren();for(const item of data.sessions){const option=document.createElement('option');option.value=item.id;option.textContent=item.title||'Conversation';$('sessions').append(option);}$('sessions').value=session;return data.sessions;}
async function open(id){const turn=++epoch;source?.close();streams.clear();session=id;const url=new URL(location.href);url.searchParams.set('session_id',id);history.replaceState({},'',url);const data=await api('/sessions/'+encodeURIComponent(id));if(turn!==epoch)return;$('sessions').value=id;$('chat-messages-inner').replaceChildren();for(const item of data.messages)message(item.role,item.content);if(data.partial && typeof data.partial.run_id==='string' && typeof data.partial.text==='string')streams.set(data.partial.run_id,message('assistant',data.partial.text));tail();runs(data.runs||[]);let last=data.last_seq||0;source=new EventSource('/api/v3/sessions/'+encodeURIComponent(id)+'/events?after='+last);source.onmessage=event=>{if(turn!==epoch)return;try{const item=JSON.parse(event.data);if(item.seq<=last)return;last=item.seq;const p=item.payload||{};if(item.kind==='delta'){if(!streams.has(p.run_id))streams.set(p.run_id,message('assistant',''));streams.get(p.run_id).textContent+=p.text||'';}if(item.kind==='message'){if(p.role==='assistant'&&streams.has(p.run_id)){streams.get(p.run_id).textContent=p.content;streams.delete(p.run_id);}else message(p.role,p.content);}if(['run_status','approval_required','tool_result'].includes(item.kind))refresh().catch(error=>note(error.message));tail();}catch{note('Unable to read execution event. Reload to recover.');}};source.onerror=()=>note('Connection interrupted. Reconnecting…');source.onopen=()=>note('');}
async function cancel(id){await api('/runs/'+id+'/cancel','POST',{client_request_id:keyId('cancel:'+id)});await refresh();}
function runs(items){$('runs').replaceChildren();const relevant=items.filter(run=>run.session_id===session||!run.session_id);const active=relevant.find(run=>['queued','running','waiting_approval','waiting_gate'].includes(run.status));$('run-status').textContent=active?.status||relevant[0]?.status||'Ready';$('stop').hidden=!active;$('stop').dataset.run=active?.id||'';$('send').disabled=!!active||!!state.shutoff||!state.provider?.connected;
for(const run of relevant){if(run.status==='completed')continue;const card=document.createElement('section');card.className='run-card';const title=document.createElement('strong');title.textContent=run.status+(run.error?' — '+run.error:'');card.append(title);
if(run.pending && run.status==='waiting_approval'){const p=run.pending,pre=document.createElement('pre');pre.textContent=p.name+'\n'+JSON.stringify(p.arguments,null,2);card.append(pre);for(const decision of ['approve','deny'])card.append(button(decision==='approve'?'Approve action':'Deny action',async()=>{await api('/approvals/'+p.id+'/decision','POST',{decision,client_request_id:keyId('approval:'+p.id+':'+decision)});await refresh();}));}
if(run.uncertain || run.status==='recovery'){const text=document.createElement('p');text.textContent='The previous action may have completed. Verify its outcome before retrying.';const output=document.createElement('textarea');output.placeholder='Known result to accept';output.setAttribute('aria-label','Known action result');card.append(text,output);for(const decision of ['accept','retry','abandon'])card.append(button({accept:'Accept known result',retry:'Authorize retry',abandon:'Abandon run'}[decision],async()=>{await api('/runs/'+run.id+'/recovery','POST',{decision,output:output.value,client_request_id:keyId('recovery:'+run.id+':'+decision+':'+output.value)});await refresh();}));}
if(!run.uncertain && ['paused','stopped','failed'].includes(run.status))card.append(button('Resume run',async()=>{await api('/runs/'+run.id+'/resume','POST',{client_request_id:crypto.randomUUID()});await refresh();}));if(active?.id===run.id)card.append(button('Cancel run',()=>cancel(run.id)));$('runs').append(card);}}
async function refresh(){state=await api('/status');agentModel=state.model||agentModel;$('shutoff').textContent=state.shutoff?'Execution shut off — enable':'Shut off execution';$('connection').textContent=state.provider?.connected?'Connected':'Connection required';$('connect').hidden=!!state.provider?.connected;$('disconnect').hidden=!state.provider?.connected;if(document.activeElement!==$('model'))$('model').value=agentModel||state.model||'';$('device').replaceChildren();const login=state.provider?.login;if(login?.verification_url){const link=document.createElement('a');link.href=login.verification_url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Open ChatGPT authorization';const code=document.createElement('p');code.textContent='Code: '+login.user_code+' — '+(login.status||'Waiting');$('device').append(link,code);}if(!$('rules').children.length)for(const [tool,rule]of Object.entries(state.policy?.rules||{})){const label=document.createElement('label');label.textContent=tool;const select=document.createElement('select');select.name=tool;for(const value of ['allow','deny','require_approval']){const option=document.createElement('option');option.value=value;option.textContent=value.replaceAll('_',' ');select.append(option);}select.value=rule;label.append(select);$('rules').append(label);}runs(state.runs||[]);}
async function boot(){const me=await api('/auth/me');csrf=me.csrf_token;$('login').hidden=true;$('app').hidden=false;await refresh();const items=await sessions(),route=new URL(location.href).searchParams.get('session_id');if(route||items.length)await open(route||items[0].id);clearInterval(poll);poll=setInterval(()=>refresh().catch(error=>note(error.message)),3000);}
$('login-form').onsubmit=guard(async()=>{const data=new FormData($('login-form'));csrf=(await api('/auth/login','POST',{username:data.get('username'),password:data.get('password')})).csrf_token;$('login-form').reset();await boot();});
$('logout').onclick=guard(async()=>{await api('/auth/logout','POST',{});out();});
$('theme').onclick=()=>{document.body.classList.toggle('light-mode');localStorage.setItem('v3-theme',document.body.classList.contains('light-mode')?'light':'dark');};document.body.classList.toggle('light-mode',localStorage.getItem('v3-theme')==='light');
$('sessions').onchange=guard(()=>open($('sessions').value));
$('new').onclick=guard(async()=>{const item=await api('/sessions','POST',{title:'New conversation',client_request_id:crypto.randomUUID()});await sessions();await open(item.id);});
$('composer').onsubmit=guard(async()=>{const text=$('chat-input').value.trim();if(!text)return;if(!session){const item=await api('/sessions','POST',{title:text.slice(0,60),client_request_id:keyId('session:'+text)});await sessions();await open(item.id);}await api('/sessions/'+session+'/messages','POST',{text,client_message_id:keyId('message:'+session+':'+text)});ids.delete('message:'+session+':'+text);$('chat-input').value='';await refresh();});
$('chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('send').disabled)$('composer').requestSubmit();}};
$('stop').onclick=guard(()=>cancel($('stop').dataset.run));
$('shutoff').onclick=guard(async()=>{await api('/control','PUT',{shutoff:!state.shutoff,client_request_id:crypto.randomUUID()});await refresh();});
$('connect').onclick=guard(async()=>{await api('/provider/login','POST',{});await refresh();$('device').scrollIntoView({block:'nearest'});});
$('disconnect').onclick=guard(async()=>{await api('/provider/logout','POST',{});await refresh();});
$('model-form').onsubmit=guard(async()=>{agentModel=(await api('/agents/'+agentId+'/model','PUT',{model:$('model').value.trim()})).model;await refresh();});
$('policy-form').onsubmit=guard(async()=>{const rules=Object.fromEntries([...$('rules').querySelectorAll('select')].map(node=>[node.name,node.value]));await api('/policy','PUT',{rules});await refresh();});
api('/auth/setup-status').then(status=>{if(!status.initialized){location.replace('/setup.html');return;}return boot();}).catch(error=>{out();if(!error.message.includes('401'))note(error.message);});
