'use strict';
import { renderAgentDirectory } from '../agents/pages/catalog.js';
import { mountUsers, mountPolicies, mountPageAccess } from './users/users.js';
import { mountAppSettings } from './settings.js';
import { mountAppModels } from './models/models.js';
import { wirePageTabDrag } from '../../shared/page-tabs.js';
import { startView as startFiles, stopView as stopFiles, resetExplorerForRemount } from './explorer-view.js';

export async function start(mount) {
  const generation=(mount._appGeneration||0)+1;mount._appGeneration=generation;
  mount._appModelsDispose?.();mount._appModelsDispose=null;
  mount._appFilesDispose?.();mount._appFilesDispose=null;
  const active=()=>mount._appGeneration===generation;
  mount.replaceChildren();
  if (!mount._identityListener) {
    mount._identityListener=()=>start(mount).catch(error=>{mount.textContent=error.message;});
    window.addEventListener('v3-identity-changed',mount._identityListener);
  }
  const response = await fetch(new URL('./catalog.json', import.meta.url), {cache:'no-store'});
  if (!response.ok) throw Error('App catalog unavailable');
  const catalog = await response.json();
  if(!active())return;
  if (!Array.isArray(catalog.pages)) throw Error('Invalid App catalog');
  const meResponse=await fetch('/api/v3/auth/me',{credentials:'same-origin'});
  const me=meResponse.ok?await meResponse.json():null;
  if(!active())return;
  const pages=catalog.pages.filter(page=>!page.admin||me?.is_admin);
  if (!Array.isArray(pages) || pages.some(page => !/^[a-z][a-z0-9-]*$/.test(page.id) || !page.title)) throw Error('Invalid App catalog');
  mount.innerHTML = '<div class="app-page-framework"><div class="agent-card active agent-card-tabs-only"><div class="agent-card-tabs-wrap"><div class="agent-card-tabs" role="tablist" aria-label="App pages"></div></div></div><div class="agent-detail-panel"><div class="agent-detail-content"><div id="app-config-page" class="agent-detail-body"></div><section id="app-files-page" class="agent-detail-body" hidden><h2>File Manager</h2><p role="status">Checking V3 File Manager…</p></section></div></div></div>';
  for(const page of pages) if(!mount.querySelector('#'+page.panelId)){const panel=document.createElement('section');panel.id=page.panelId;panel.className='agent-detail-body';panel.hidden=true;mount.querySelector('.agent-detail-content').append(panel);}
  if(me?.is_admin){mountAppSettings(mount.querySelector('#app-settings-page'));}
  if(me?.is_admin){await mountUsers(mount.querySelector('#app-users-page'));if(!active())return;await mountPolicies(mount.querySelector('#app-user-policies-page'));if(!active())return;await mountPageAccess(mount.querySelector('#app-page-access-page'));if(!active())return;const dispose=await mountAppModels(mount.querySelector('#app-models-page'));if(!active()){dispose();return;}mount._appModelsDispose=dispose;}
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
  let filesMounted=false, filesLoading=false, filesPending=false;
  mount._appFilesDispose=()=>{ stopFiles(); resetExplorerForRemount(); window.__v3FilesCsrf=''; window.__v3FilesAdmin=false; };
  async function ensureFiles() {
    if (filesMounted) { await startFiles(); return; }
    if (filesLoading) { filesPending=true; return; }
    filesLoading=true;
    const panel=mount.querySelector('#app-files-page');
    try {
      if (!me?.is_admin) throw Error('File Manager requires administrator access');
      const probe=await fetch('/api/v3/file-manager',{
        method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','x-csrf-token':me.csrf_token},
        body:JSON.stringify({action:'tree',path:''})
      });
      if (!probe.ok) throw Error('V3 File Manager backend is not ready ('+probe.status+')');
      const response=await fetch('/v3-ui/main-panel/app/files.html',{credentials:'same-origin'});
      if (!response.ok) throw Error('File Manager view unavailable');
      const markup=await response.text();
      if (!active() || selected!=='files') return;
      panel.innerHTML=markup;
      window.__v3FilesCsrf=me.csrf_token;
      window.__v3FilesAdmin=true;
      resetExplorerForRemount();
      filesMounted=true;
      await startFiles();
      if (!active() || selected!=='files') stopFiles();
    } catch (error) {
      if (active() && selected==='files') {
        panel.replaceChildren();
        const note=document.createElement('p');
        note.setAttribute('role','status');
        note.textContent=error.message;
        panel.append(note);
      }
      filesMounted=false;
    } finally {
      filesLoading=false;
      if (filesPending) {
        filesPending=false;
        if (active() && selected==='files' && !filesMounted) void ensureFiles();
      }
    }
  }
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
    if (route) { const url=new URL(location.href);url.searchParams.set('appView',id);if(url.href!==location.href)history.pushState({},'',url); }
    if (id==='files') void ensureFiles(); else if (filesMounted) stopFiles();
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
