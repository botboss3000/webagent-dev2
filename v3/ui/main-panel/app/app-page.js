'use strict';
import { renderAgentDirectory } from '../agents/pages/catalog.js';
import { mountUsers, mountPolicies, mountPageAccess } from './users/users.js';
import { wirePageTabDrag } from '../../shared/page-tabs.js';

export async function start(mount) {
  if (!mount._identityListener) {
    mount._identityListener=()=>start(mount).catch(error=>{mount.textContent=error.message;});
    window.addEventListener('v3-identity-changed',mount._identityListener);
  }
  const response = await fetch(new URL('./catalog.json', import.meta.url), {cache:'no-store'});
  if (!response.ok) throw Error('App catalog unavailable');
  const catalog = await response.json();
  if (!Array.isArray(catalog.pages)) throw Error('Invalid App catalog');
  const meResponse=await fetch('/api/v3/auth/me',{credentials:'same-origin'});
  const me=meResponse.ok?await meResponse.json():null;
  const pages=catalog.pages.filter(page=>!page.admin||me?.is_admin);
  if (!Array.isArray(pages) || pages.some(page => !/^[a-z][a-z0-9-]*$/.test(page.id) || !page.title)) throw Error('Invalid App catalog');
  mount.innerHTML = '<div class="app-page-framework"><div class="agent-card active agent-card-tabs-only"><div class="agent-card-tabs-wrap"><div class="agent-card-tabs" role="tablist" aria-label="App pages"></div></div></div><div class="agent-detail-panel"><div class="agent-detail-content"><div id="app-config-page" class="agent-detail-body"></div><section id="app-files-page" class="agent-detail-body" hidden><h2>File Manager</h2><p role="status">Pending V3 port: the V2 File Manager requires independent filesystem APIs before browsing or editing is available.</p><p>V2 filesystem, Git, and deployment actions are not connected to this application.</p></section></div></div></div>';
  for(const page of pages) if(!mount.querySelector('#'+page.panelId)){const panel=document.createElement('section');panel.id=page.panelId;panel.className='agent-detail-body';panel.hidden=true;mount.querySelector('.agent-detail-content').append(panel);}
  if(me?.is_admin){await mountUsers(mount.querySelector('#app-users-page'));await mountPolicies(mount.querySelector('#app-user-policies-page'));await mountPageAccess(mount.querySelector('#app-page-access-page'));}
  const strip = mount.querySelector('[role=tablist]');
  const all = [{id:'config',title:'Config',panelId:'app-config-page'}, ...pages];
  const key = 'v3:app-page-tabs:v1';
  let order;
  try { order = JSON.parse(localStorage.getItem(key)); } catch {}
  order = Array.isArray(order) ? [...new Set(order)].filter(id => all.some(p=>p.id===id)) : ['config'];
  if (!order.includes('config')) order.push('config');
  let selected = new URL(location.href).searchParams.get('appView') || 'config';
  if (!all.some(p=>p.id===selected)) selected='config';
  const tabs = new Map();
  function show(id, route = true) {
    selected=id;
    for (const page of all) {
      mount.querySelector('#'+page.panelId).hidden=page.id!==id;
      const tab=tabs.get(page.id);
      tab.hidden=!order.includes(page.id)&&page.id!==id;
      tab.classList.toggle('ephemeral',!order.includes(page.id));
      tab.classList.toggle('active',page.id===id);
      tab.setAttribute('aria-selected',String(page.id===id));
      if (!tab.hidden&&!order.includes(page.id)) strip.append(tab);
    }
    if (route) { const url=new URL(location.href);url.searchParams.set('appView',id);history.replaceState({},'',url); }
  }
  function sync() {
    for (const id of order) strip.append(tabs.get(id));
    const host=mount.querySelector('#app-config-page');
    renderAgentDirectory(host,pages,id=>show(id),order,id=>{
      order=order.includes(id)?order.filter(item=>item!==id):[id,...order];
      if(selected===id&&!order.includes(id)) selected='config';
      save();
    });
    host.querySelector('.agent-config-landing').setAttribute('aria-label','App page directory');
    host.querySelector('.agent-config-settings-group-label').textContent='App pages';
    show(selected,false);
  }
  function save() { try {localStorage.setItem(key,JSON.stringify(order));}catch{} sync();show(selected); }
  for (const page of all) {
    const tab=document.createElement('button');tab.type='button';tab.id='app-tab-'+page.id;tab.textContent=page.title;
    tab.setAttribute('role','tab');tab.setAttribute('aria-controls',page.panelId);tab.onclick=()=>show(page.id);
    tabs.set(page.id,tab);strip.append(tab);
    const panel=mount.querySelector('#'+page.panelId);panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tab.id);
  }
  sync();
  wirePageTabDrag(strip,{getOrder:()=>order,save,prefix:'app-tab-'});
}
