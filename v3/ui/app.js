// Port of V2 private-access shell; authentication remains exclusively V3.
const privateResponse=await fetch('/v3-ui/private-access/private-access-page/private-access-page.html');
if(!privateResponse.ok)throw Error('Private access page failed to load');
document.getElementById('app-restricted-overlay').innerHTML=await privateResponse.text();
const privateHost=document.querySelector('[data-private-access-form]');
const privateReveal=document.querySelector('[data-private-access-reveal]');
privateReveal.onclick=()=>{privateHost.hidden=false;privateReveal.hidden=true;privateReveal.setAttribute('aria-expanded','true');document.getElementById('login-username').focus({preventScroll:true});};
function privateVisible(visible){document.body.classList.toggle('app-restricted',visible);document.getElementById('app-restricted-overlay').setAttribute('aria-hidden',String(!visible));privateHost.hidden=true;privateReveal.hidden=false;privateReveal.setAttribute('aria-expanded','false');}
import { initHeaderCarousel } from './shared/header-carousel.js';
initHeaderCarousel();
import { wirePageTabDrag } from './shared/page-tabs.js';
import { purgeAllBrowserData } from './browser-data-reset.js';
import { initChatConfig } from './chat/controls/chat-config.js';
import { mountChatPanel } from './chat/core/panel.js';

// v2 dom-utils imports v1 auth and uploads; v3 uses text nodes and its own transport.
import { renderAbilities } from './main-panel/agents/pages/abilities/page.js';
import { loadAgentPages, renderAgentDirectory } from './main-panel/agents/pages/catalog.js';
import { mountSelectionTable } from './main-panel/agents/pages/model-table.js';
let selectionTable;
let agentPages=[];
const $=id=>document.getElementById(id);
let agentId=new URL(location.href).searchParams.get('agent')||'shared_default';
let explicitAssignment=false;
const initialTab=new URL(location.href).searchParams.get('tab');
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
  detail.innerHTML=`<div class="agent-card-tabs-wrap"><div class="agent-card-tabs" role="tablist" aria-label="Agent configuration pages"></div></div>
    <div class="agent-detail-panel"><div class="agent-detail-content"><div class="agent-detail-body" id="model-page">
    <h2>Model</h2><div class="ac-model-table">
      <div class="ac-ability-row ac-model-cfg-row ac-row-static"><span class="ac-ability-label"><span class="ac-ability-name">Provider</span><span class="ac-ability-desc">Use a ChatGPT subscription for this agent.</span></span><span class="ac-model-cfg-control">ChatGPT Subscription</span></div>
      <div class="ac-ability-row ac-model-cfg-row ac-row-static"><span class="ac-ability-label"><span class="ac-ability-name">Connection</span></span><span class="ac-model-cfg-control"><span id="connection" class="ac-hint"></span><button id="connect" type="button" class="ac-btn ac-btn-primary ac-btn-sm">Click to Issue verification code</button><button id="disconnect" type="button" class="ac-btn ac-btn-sm">Disconnect</button><span id="device" class="ac-subscription-auth"></span></span></div>
      <form id="model-form" class="ac-ability-row ac-model-cfg-row"><label for="model" class="ac-ability-label"><span class="ac-ability-name">Model</span><span class="ac-ability-desc">Model ID used for new chat turns.</span></label><span class="ac-model-cfg-control"><input id="model" class="ac-input ac-input-sm" placeholder="gpt-6-luna" required pattern="[A-Za-z0-9._-]+"><button class="ac-btn ac-btn-primary ac-btn-sm">Save model</button></span></form>
    </div><details id="settings"><summary>Tool policies</summary><form id="policy-form"><div id="rules"></div><button type="submit">Save policies</button></form></details>
    </div><div id="abilities-page" hidden></div><div id="config-page" class="agent-detail-body" hidden></div></div></div>`;
  agentPages=await loadAgentPages();
  const tabs=detail.querySelector('.agent-card-tabs');
  for(const page of [{id:'config',title:'Config',panelId:'config-page'},...agentPages]){
    const tab=document.createElement('button');
    tab.id='agent-tab-'+page.id;tab.type='button';tab.textContent=page.title;
    tab.setAttribute('role','tab');tab.setAttribute('aria-controls',page.panelId);
    tab.setAttribute('aria-selected','false');
    tabs.append(tab);
    const body=detail.querySelector('#'+page.panelId);
    if(!body)throw Error('Agent page panel unavailable: '+page.id);
    body.setAttribute('role','tabpanel');body.setAttribute('aria-labelledby',tab.id);
  }
  const selectionHost=document.createElement('div');selectionHost.id='selection-model-table';detail.querySelector('#model-page h2').after(selectionHost);
  detail.querySelector('#model-page h2').textContent=agentPages.find(page=>page.id==='model').title;
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
  if(activeMainPage==='agents'){
    route.searchParams.set('tab','agents');
    route.searchParams.set('agent',agentId);
    history.replaceState({},'',route);
  }
  row.onclick=()=>region.scrollIntoView({block:'start',behavior:'smooth'});
}
let mainPages=[];
let activeMainPage='';
async function loadMainPages(){
  const response=await fetch('/api/v3/pages/catalog',{credentials:'same-origin',cache:'no-store'});
  if(!response.ok)throw Error('Main page catalog unavailable');
  const catalog=await response.json();
  if(!Array.isArray(catalog.main)||!catalog.main.length)throw Error('No main pages configured');
  mainPages=catalog.main;
  activeMainPage=mainPages.some(page=>page.id===initialTab)?initialTab:mainPages[0].id;
  for(const page of mainPages){
    for(const href of page.css||[]){
      const link=document.createElement('link');
      link.rel='stylesheet';link.href=href;
      document.head.insertBefore(link,document.querySelector('link[href^="/v3-ui/app.css"]'));
    }
    const tab=document.createElement('button');
    tab.id='tab-button-'+page.id;tab.type='button';tab.className='main-tab';
    tab.dataset.value=page.id;tab.setAttribute('role','tab');tab.textContent=page.label;
    tab.onclick=()=>showMainPage(page.id);
    $('main-tabs').append(tab);
    const option=document.createElement('option');
    option.value=page.id;option.textContent=page.label;
    $('main-tab-select').append(option);
    const mobile=document.createElement('button');
    mobile.id='mobile-nav-'+page.id;mobile.type='button';mobile.className='mobile-nav-item';
    mobile.textContent=page.label;mobile.onclick=()=>showMainPage(page.id);
    $('mobile-nav-items').append(mobile);
    const mount=document.createElement('div');
    mount.id='tab-'+page.id;mount.className='tab-content';mount.hidden=page.id!==activeMainPage;
    $('main-panel').append(mount);
    if(page.html){
      const partial=await fetch(page.html,{credentials:'same-origin'});
      if(!partial.ok)throw Error(page.label+' page unavailable');
      mount.innerHTML=await partial.text();
    }
    if(page.id==='agents')await mountAgents();
    if(page.entry){
      const module=await import(page.entry);
      if(typeof module.start!=='function')throw Error(page.label+' page has no start function');
      await module.start(mount);
    }
  }
  updateMainPageState();
}
function updateMainPageState(){
  for(const page of mainPages){
    const selected=page.id===activeMainPage;
    $('tab-'+page.id).hidden=!selected;
    $('tab-'+page.id).classList.toggle('active',selected);
    $('tab-button-'+page.id).classList.toggle('active',selected);
    $('tab-button-'+page.id).setAttribute('aria-selected',String(selected));
  }
  $('main-tab-select').value=activeMainPage;
}
let csrf='',state={},poll;
const ids=new Map();
const keyId=key=>{if(!ids.has(key))ids.set(key,crypto.randomUUID());return ids.get(key);};
const note=text=>{$('notice').textContent=text;};
async function api(path,method='GET',body){const response=await fetch('/api/v3'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'x-csrf-token':csrf})},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await response.json().catch(()=>({}));if(!response.ok){if(response.status===401)out();const error=Error(data.error||data.message||'Request failed ('+response.status+')');error.status=response.status;throw error;}return data;}
const guard=fn=>async event=>{event?.preventDefault();try{await fn(event);note('');}catch(error){note(error.message);}};
const chat = await mountChatPanel({request:api, guard, note, keyId, forgetKey:key=>ids.delete(key), refresh, getState:()=>state, getAgentId:()=>agentId});
await loadMainPages();

// Agent pages resolve tenant authority independently; members see effective read-only abilities.
async function showAgentView(view){
  const pages=[{id:'config',panelId:'config-page'},...agentPages];
  const selected=accountMe&&!accountMe.is_admin&&!['model','abilities'].includes(view)?'model':pages.some(page=>page.id===view)?view:'config';
  for(const page of pages){
    $(page.panelId).hidden=page.id!==selected;
    const tab=$('agent-tab-'+page.id);
    tab.setAttribute('aria-selected',String(page.id===selected));
    tab.classList.toggle('active',page.id===selected);
    tab.hidden=!agentTabOrder.includes(page.id)&&page.id!==selected;
    tab.classList.toggle('ephemeral',!agentTabOrder.includes(page.id)&&page.id===selected);
    if(!tab.hidden&&!agentTabOrder.includes(page.id))tab.parentElement.append(tab);
  }
  const route=new URL(location.href);
  route.searchParams.set('agentView',selected);
  history.replaceState({},'',route);
  if(selected==='abilities')await renderAbilities($('abilities-page'),api,{agentId});
}
for(const page of [{id:'config'},...agentPages]){
  $('agent-tab-'+page.id).onclick=guard(()=>showAgentView(page.id));
}
// Like V2, the favorite list is also the tab order; Config cannot be removed.
const agentTabsKey='v3:agent-page-tabs:v1';
let agentTabOrder;
try{agentTabOrder=JSON.parse(localStorage.getItem(agentTabsKey));}catch{}
const validAgentTabs=new Set(['config',...agentPages.map(page=>page.id)]);
agentTabOrder=Array.isArray(agentTabOrder)?[...new Set(agentTabOrder)].filter(id=>validAgentTabs.has(id)):['config'];
if(!agentTabOrder.includes('config'))agentTabOrder.push('config');
function syncAgentTabs(){
  const strip=document.querySelector('#agents-grid .agent-card-tabs');
  for(const id of agentTabOrder)strip.append($('agent-tab-'+id));
  for(const page of [{id:'config'},...agentPages])$('agent-tab-'+page.id).hidden=!agentTabOrder.includes(page.id);
  renderAgentDirectory($('config-page'),agentPages,view=>guard(()=>showAgentView(view))(),agentTabOrder,id=>{
    if(agentTabOrder.includes(id))agentTabOrder=agentTabOrder.filter(item=>item!==id);
    else agentTabOrder.unshift(id);
    saveAgentTabs();
  });
}
function saveAgentTabs(){
  try{localStorage.setItem(agentTabsKey,JSON.stringify(agentTabOrder));}catch{}
  syncAgentTabs();
}
syncAgentTabs();
wirePageTabDrag(document.querySelector('#agents-grid .agent-card-tabs'), { getOrder: () => agentTabOrder, save: saveAgentTabs, prefix: 'agent-tab-' });
async function loadGuestPolicy(){try{const policy=await api('/auth/access-mode');$('guest-login').hidden=!policy.guest_access;document.querySelector('.private-access-approval').hidden=!!policy.guest_access;}catch{$('guest-login').hidden=true;}}
$('guest-login').onclick=async()=>{const btn=$('guest-login');btn.disabled=true;try{const result=await api('/auth/guest-login','POST',{});csrf=result.csrf_token;await boot();}catch(error){$('registration-status').textContent=error.message;}finally{btn.disabled=false;}};
function out(){window.dispatchEvent(new Event('v3-identity-changed'));loadGuestPolicy();csrf='';chat.disconnect();clearInterval(poll);closeMobileNav();closeAccountMenu();$('app').hidden=true;privateVisible(true);window.__boot?.reveal();}
async function refresh(){if(accountMe&&!accountMe.is_admin){state=await api('/user/status');$('shutoff').hidden=true;chat.runs(state.runs||[]);return;}state=await api('/status');agentModel=state.model||agentModel;$('shutoff').textContent=state.shutoff?'Execution shut off — enable':'Shut off execution';$('connection').textContent=state.provider?.connected?'Connected':'Connection required';$('connect').hidden=!!state.provider?.connected;$('disconnect').hidden=!state.provider?.connected;if(document.activeElement!==$('model'))$('model').value=agentModel||state.model||'';$('device').replaceChildren();const login=state.provider?.login;if(login?.verification_url){const link=document.createElement('a');link.href=login.verification_url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Open ChatGPT authorization';const code=document.createElement('p');code.textContent='Code: '+login.user_code+' — '+(login.status||'Waiting');$('device').append(link,code);}if(!$('rules').children.length)for(const [tool,rule]of Object.entries(state.policy?.rules||{})){const label=document.createElement('label');label.textContent=tool;const select=document.createElement('select');select.name=tool;for(const value of ['allow','deny','require_approval']){const option=document.createElement('option');option.value=value;option.textContent=value.replaceAll('_',' ');select.append(option);}select.value=rule;label.append(select);$('rules').append(label);}chat.runs(state.runs||[]);}
// Explicit role administration replaces the legacy default-model form for every agent.
async function boot(){const me=await api('/auth/me');csrf=me.csrf_token;await initChatConfig().catch(() => {});renderAccount(me);window.dispatchEvent(new Event('v3-identity-changed'));privateVisible(false);$('app').hidden=false;await refresh();selectionTable?.destroy();$('model-page').querySelector('.ac-model-table').hidden=true;$('settings').hidden=true;try{selectionTable=await mountSelectionTable($('selection-model-table'),{request:api,agentId,onAgent:async id=>{agentId=id;const route=new URL(location.href);route.searchParams.set('agent',id);history.replaceState({},'',route);await showAgentView(route.searchParams.get('agentView')||'model');},onAssignment:async(id,assignment)=>{explicitAssignment=!!assignment?.roles?.standard;$('model-page').querySelector('.ac-model-table').hidden=true;$('settings').hidden=true;document.querySelector('.agent-card-name').textContent=id==='shared_default'?'WebAgent':id;}});}catch(error){if(error.status===401)throw error;selectionTable=null;const warning=document.createElement('p');warning.className='ac-hint';warning.setAttribute('role','status');warning.textContent=error.status===404?'Model role management requires a backend update.':'Model assignment management unavailable: '+error.message;$('selection-model-table').replaceChildren(warning);if(agentId!=='shared_default')throw Error('This agent requires model-management support; update the backend before chatting.');}await showAgentView(new URL(location.href).searchParams.get('agentView')||(me.is_admin?'config':'model'));const items=await chat.sessions(),route=new URL(location.href).searchParams.get('session_id');if(route||items.length)await chat.open(route||items[0].id);if(initialTab==='account')showAccountPanel();window.__boot?.appReady();clearInterval(poll);poll=setInterval(()=>refresh().catch(error=>note(error.message)),3000);}
// Registration mirrors V2's mode switch; V3 retains independent cookie authentication.
let registerMode=false;
$('register-toggle').onclick=()=>{
  registerMode=!registerMode;
  $('login-title').textContent=registerMode?'Create account':'Sign in';
  $('login-submit').textContent=registerMode?'Create account':'Sign in';
  $('register-toggle').textContent=registerMode?'Back to sign in':'Create an account';
  $('register-name-row').hidden=!registerMode;
  $('register-name').disabled=!registerMode;
  $('register-name').required=registerMode;
  $('login-password').autocomplete=registerMode?'new-password':'current-password';
  $('login-password').minLength=registerMode?8:0;
  $('registration-status').textContent='';
};
$('login-form').onsubmit=async event=>{
  event.preventDefault();
  const submit=$('login-submit');submit.disabled=true;
  $('registration-status').textContent=registerMode?'Creating account…':'Signing in…';
  try{
    const data=new FormData($('login-form'));
    const body={username:String(data.get('username')).trim(),password:data.get('password')};
    if(registerMode)body.display_name=String(data.get('display_name')).trim();
    const result=await api(registerMode?'/auth/register':'/auth/login','POST',body);
    if(result.pending_approval){
      $('login-password').value='';
      $('registration-status').textContent='Account created and waiting for administrator approval.';
      return;
    }
    csrf=result.csrf_token;$('login-form').reset();await boot();
    $('registration-status').textContent='';
  }catch(error){$('registration-status').textContent=error.message;}
  finally{submit.disabled=false;}
};
const accountMenu=$('user-dropdown-menu');
function closeAccountMenu(){accountMenu.hidden=true;$('top-user-id').setAttribute('aria-expanded','false');}
$('top-user-id').onclick=event=>{event.stopPropagation();accountMenu.hidden=!accountMenu.hidden;$('top-user-id').setAttribute('aria-expanded',String(!accountMenu.hidden));};
document.addEventListener?.('click',event=>{if(!accountMenu.hidden&&!event.target.closest('#user-dropdown-menu, #top-user-id'))closeAccountMenu();});
document.addEventListener?.('keydown',event=>{if(event.key==='Escape')closeAccountMenu();});
let accountMe=null;
function renderAccount(me){
  accountMe=me;
  const guest=me.role==='guest';
  for(const id of ['account-profile-form','account-password-form','account-session-policy-form'])$(id).hidden=guest;
  $('shutoff').hidden=!me.is_admin;
  // Agent-scoped Models has its own permission checks and is also useful to members.
  document.querySelector('#agents-grid .agent-card-tabs-wrap').hidden=false;
  $('account-name').textContent=me.display_name||me.user||'Account';
  $('account-username').textContent=me.user||'';
  $('account-identity').textContent=me.user||'';
  $('account-login-name').value=me.user||'';
  $('account-display-name').value=me.display_name||me.user||'';
  $('top-user-id').title=me.user||'Manage account';
}
function showAccountPanel(){showMainPage('account');}
function showMainPage(id){
  if(!mainPages.some(page=>page.id===id))return;
  closeMobileNav();
  activeMainPage=id;
  updateMainPageState();
  if(id==='account')loadAccountControls();
  const route=new URL(location.href);
  route.searchParams.set('tab',id);
  if(id==='agents')route.searchParams.set('agent',agentId);
  history.replaceState({},'',route);
  if(mobileLayout.matches)showMobileView('main');
  updateMobileNavigation();
}
$('manage-account').onclick=showAccountPanel;
$('account-profile-form').onsubmit=async event=>{
  event.preventDefault();
  const status=$('account-profile-status');
  const display_name=$('account-display-name').value.trim();
  const username=$('account-login-name').value.trim();
  if(!display_name||!username){status.textContent='Enter a username and display name.';return;}
  try{
    const profile=await api('/auth/profile','PUT',{username,display_name});
    renderAccount({...accountMe,...profile});
    status.textContent='Saved';
  }catch(error){status.textContent=error.message;}
};
async function loadAccountControls(){
  try{
    const [policy,listing]=await Promise.all([api('/auth/session-policy'),api('/auth/sessions')]);
    $('account-session-days').value=String(policy.days);
    const list=$('account-sessions');
    list.replaceChildren();
    for(const entry of listing.sessions){
      const row=document.createElement('div');row.className='account-signin-row';
      const details=document.createElement('div');details.className='account-signin-details';
      const name=document.createElement('strong');name.textContent=entry.current?'This browser':(entry.user_agent||'Unknown browser');
      const dates=document.createElement('span');
      const when=entry.created_at?new Date(entry.created_at*1000).toLocaleString():'Earlier sign-in';
      dates.textContent=when+' · expires '+new Date(entry.expires_at*1000).toLocaleString();
      details.append(name,dates);
      const revoke=button(entry.current?'Sign out':'Revoke',async()=>{
        await api('/auth/sessions/'+encodeURIComponent(entry.id),'DELETE');
        if(entry.current){out();note('Signed out.');}else await loadAccountControls();
      });
      revoke.className='account-btn';row.append(details,revoke);list.append(row);
    }
    if(!listing.sessions.length)list.textContent='No active sign-ins.';
    $('account-sessions-status').textContent='';
  }catch(error){$('account-sessions-status').textContent=error.message;}
}
$('account-sessions-refresh').onclick=loadAccountControls;
$('account-session-policy-form').onsubmit=async event=>{
  event.preventDefault();
  const status=$('account-session-policy-status');
  try{
    await api('/auth/session-policy','PUT',{days:Number($('account-session-days').value)});
    status.textContent='Saved. Applies to new sign-ins.';
  }catch(error){status.textContent=error.message;}
};
$('account-password-form').onsubmit=async event=>{
  event.preventDefault();
  const status=$('account-password-status');
  const current_password=$('account-current-password').value;
  const new_password=$('account-new-password').value;
  if(new_password!==$('account-confirm-password').value){
    status.textContent='New passwords do not match.';
    return;
  }
  try{
    await api('/auth/change-password','POST',{current_password,new_password});
    $('account-password-form').reset();
    out();
    note('Password changed. Sign in again.');
  }catch(error){status.textContent=error.message;}
};
const mobileLayout=matchMedia('(max-width:800px)');
let mobileView=initialTab==='account'?'main':new URL(location.href).searchParams.has('session_id')?'chat':'main';
let desktopChatVisible=true;
function updateChatControl(visible){$('chat-toggle-btn').classList.toggle('active',visible);$('chat-toggle-btn').setAttribute('aria-expanded',String(visible));}
function showMobileView(view){mobileView=view;$('app').dataset.mobileView=view;updateChatControl(view==='chat');updateMainPageState();}
function syncLayout(){
  document.body.classList.toggle('mobile-mode',mobileLayout.matches);
  $('mobile-nav-toggle').hidden=!mobileLayout.matches;
  $('mobile-back-main-btn').hidden=!mobileLayout.matches;
  if(!mobileLayout.matches)closeMobileNav();
  updateMobileNavigation();
  if(mobileLayout.matches){$('chat-panel').hidden=false;$('chat-resize-handle').hidden=false;showMobileView(mobileView);}
  else{delete $('app').dataset.mobileView;$('chat-panel').hidden=!desktopChatVisible;$('chat-resize-handle').hidden=!desktopChatVisible;updateChatControl(desktopChatVisible);updateMainPageState();}
}
const mobileNav=$('mobile-nav-menu');
function closeMobileNav(){mobileNav.hidden=true;$('mobile-nav-toggle').setAttribute('aria-expanded','false');}
function updateMobileNavigation(){
  $('mobile-back-main-btn').textContent=mainPages.find(page=>page.id===activeMainPage)?.label||'';
  for(const page of mainPages)$('mobile-nav-'+page.id).classList.toggle('active',page.id===activeMainPage);
}
$('mobile-nav-toggle').onclick=event=>{event.stopPropagation();closeAccountMenu();mobileNav.hidden=!mobileNav.hidden;$('mobile-nav-toggle').setAttribute('aria-expanded',String(!mobileNav.hidden));};
$('mobile-back-main-btn').onclick=()=>showMainPage(activeMainPage);
$('mobile-nav-signout').onclick=guard(async()=>{await api('/auth/logout','POST',{});out();});
document.addEventListener('click',event=>{if(!mobileNav.hidden&&!event.target.closest('#mobile-nav-menu, #mobile-nav-toggle'))closeMobileNav();});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeMobileNav();});
mobileLayout.addEventListener('change',syncLayout);syncLayout();
$('chat-toggle-btn').onclick=()=>{if(mobileLayout.matches){showMobileView('chat');}else{desktopChatVisible=!desktopChatVisible;syncLayout();}};

let browserResetInProgress=false;
async function hardRefresh(){
  if(browserResetInProgress)return;
  browserResetInProgress=true;
  try {
    const response=await fetch('/api/v3/auth/logout',{method:'POST',credentials:'same-origin',headers:{'x-csrf-token':csrf}});
    if(!response.ok&&response.status!==401)throw Error('Sign-out failed');
  } catch (_) {
    browserResetInProgress=false;
    $('ptr-indicator').classList.remove('loading');
    $('ptr-indicator').hidden=true;
    note('Could not end this sign-in. Browser data was not cleared. Try again.');
    return;
  }
  try { await purgeAllBrowserData(); } finally {
    const url=new URL(location.href);
    url.searchParams.set('_',String(Date.now()));
    location.replace(url);
  }
}
function setupHeaderPullRefresh(){
  const indicator=$('ptr-indicator'),blue=$('ptr-blue'),red=$('ptr-red'),hint=$('ptr-hint');
  const fullDash=98,normalAt=.5,hardAt=.85,pullZone=50;
  let dragging=false,startY=0,stage=0;
  function end(){
    if(!dragging)return;
    const action=stage;
    dragging=false;startY=0;stage=0;
    if(!action){indicator.hidden=true;hint.textContent='';hint.classList.remove('show');return;}
    indicator.classList.add('loading');
    hint.textContent=action===2?'CLEARING BROWSER DATA':'REFRESHING';hint.classList.add('show');
    if(action===1)setTimeout(()=>location.reload(),120);
    if(action===2)void hardRefresh();
  }
  document.addEventListener('touchstart',event=>{
    if(!mobileLayout.matches||window.scrollY>5||indicator.classList.contains('loading')||!event.touches.length)return;
    const touch=event.touches[0];
    const scroller=event.target.closest('#main-panel, #chat-messages');
    const fromHeader=touch.clientY<=pullZone;
    if(!fromHeader){
      if(!scroller||scroller.scrollTop>4||event.target.closest('button,input,textarea,select,a,summary,[contenteditable]'))return;
      for(let node=event.target;node&&node!==scroller;node=node.parentElement){
        const overflow=getComputedStyle(node).overflowY;
        if((overflow==='auto'||overflow==='scroll')&&node.scrollHeight>node.clientHeight+1)return;
      }
    }
    dragging=true;startY=touch.clientY;stage=0;
    indicator.hidden=false;indicator.style.height='0px';indicator.style.opacity='1';
    blue.setAttribute('stroke-dashoffset',String(fullDash));
    red.style.display='none';red.setAttribute('stroke-dashoffset',String(fullDash));
    hint.textContent='';hint.classList.remove('show');
  },{passive:true});
  document.addEventListener('touchmove',event=>{
    if(!dragging||!event.touches.length)return;
    const raw=event.touches[0].clientY-startY;
    if(raw<=0){indicator.hidden=true;dragging=false;return;}
    const height=window.innerHeight,normal=height*normalAt,hard=height*hardAt;
    indicator.style.height=Math.min(raw*.4,height*.85)+'px';
    if(raw>=hard){
      stage=2;blue.setAttribute('stroke-dashoffset','0');red.style.display='';
      red.setAttribute('stroke-dashoffset',String(fullDash*(1-Math.min((raw-normal)/(hard-normal),1))));
      hint.textContent='HARD REFRESH · SIGN OUT';hint.classList.add('show');
    }else if(raw>=normal){
      stage=1;blue.setAttribute('stroke-dashoffset','0');red.style.display='none';
      hint.textContent='RELEASE TO REFRESH';hint.classList.add('show');
    }else{
      stage=0;blue.setAttribute('stroke-dashoffset',String(fullDash*(1-raw/normal)));
      red.style.display='none';hint.textContent='PULL TO REFRESH';hint.classList.add('show');
    }
    if(event.cancelable)event.preventDefault();
  },{passive:false});
  document.addEventListener('touchend',end,{passive:true});
  document.addEventListener('touchcancel',()=>{dragging=false;indicator.hidden=true;},{passive:true});
}
setupHeaderPullRefresh();
$('logout').onclick=guard(async()=>{await api('/auth/logout','POST',{});out();});
$('theme').onclick=()=>{document.body.classList.toggle('light-mode');localStorage.setItem('v3-theme',document.body.classList.contains('light-mode')?'light':'dark');};document.body.classList.toggle('light-mode',localStorage.getItem('v3-theme')==='light');
$('shutoff').onclick=guard(async()=>{await api('/control','PUT',{shutoff:!state.shutoff,client_request_id:crypto.randomUUID()});await refresh();});
$('connect').onclick=guard(async()=>{await api('/provider/login','POST',{});await refresh();$('device').scrollIntoView({block:'nearest'});});
$('disconnect').onclick=guard(async()=>{await api('/provider/logout','POST',{});await refresh();});
$('model-form').onsubmit=guard(async()=>{agentModel=(await api('/agents/'+agentId+'/model','PUT',{model:$('model').value.trim()})).model;await refresh();});
$('policy-form').onsubmit=guard(async()=>{const rules=Object.fromEntries([...$('rules').querySelectorAll('select')].map(node=>[node.name,node.value]));await api('/policy','PUT',{rules});await refresh();});
api('/auth/setup-status').then(status=>{if(!status.initialized){location.replace('/setup.html');return;}return boot();}).catch(error=>{if(error.status===401)out();note(error.message);window.__boot?.reveal();});
