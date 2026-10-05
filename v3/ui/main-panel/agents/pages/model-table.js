 'use strict';
import { mountModelTable } from '../../../shared/models/model-table.js';
import { initModelAnalytics } from '../../../shared/models/model-analytics.js';
import { createModelAdapter, MISSING } from '../../../shared/models/v3-adapter.js';

// Actual v2_ref Models mount: analytics sibling + shared renderer; only the adapter is v3-specific.
export async function mountSelectionTable(host,{request,agentId,onAssignment}) {
  const bridge=createModelAdapter(request,agentId,onAssignment);await bridge.ready();
  host.replaceChildren();
  const css=document.createElement('link');css.rel='stylesheet';css.href='/v3-ui/shared/models/reference.css';
  const pendingCss=document.createElement('link');pendingCss.rel='stylesheet';pendingCss.href='/v3-ui/shared/models/pending.css';
  const group=document.createElement('div');group.className='v3-reference-models';
  const analyticsHost=document.createElement('div');analyticsHost.className='agent-model-analytics-host';
  const tableHost=document.createElement('div');tableHost.style.marginTop='8px';group.append(analyticsHost,tableHost);host.append(css,pendingCss,group);
  const analytics=initModelAnalytics(analyticsHost,{fetchFn:bridge.blockedFetch,apiPath:p=>p,scope:'user',agentId,description:'Compare the daily usage of every model used with this agent. '+MISSING});
  const table=mountModelTable(tableHost,{adapter:bridge.adapter,agentLocalRoster:true,userCatalog:true,subscriptionScope:'agent',directivePresentation:'inline',directivesEditable:false,manualModelEntry:true,
    fetchFn:bridge.blockedFetch,onRoster:p=>analytics?.setModelLabels(p),onModelFocus:m=>analytics?.setModelFocus(m),onModelChange:()=>analytics?.reload(),
    advancedExtra:body=>{
      const row=document.createElement('div');row.className='ac-row';row.innerHTML='<div class="ac-ability-row">Reset Usage Counters <span class="v3-contract-pending">'+MISSING+'</span></div><div class="ac-ability-body"><select class="ac-input" disabled><option>All (input + output + cost)</option><option>Input Tokens only</option><option>Output Tokens only</option><option>Cost only</option></select><button class="ac-btn" disabled>Reset</button></div>';row.firstElementChild.onclick=()=>row.classList.toggle('expanded');body.append(row);
    }});
  let stopped=false;
  function badge(el){if(!el||el.querySelector(':scope > .v3-contract-pending'))return;const b=document.createElement('span');b.className='v3-contract-pending';b.textContent=MISSING;el.append(b);}
  function pending(){
    if(stopped)return;
    for(const el of group.querySelectorAll('.ac-saved-usage-th,.ac-saved-usage,.ac-model-meta-aliases,.ac-directives-section-title,.ac-directive-hint,.ac-saved-credentials .ac-ability-name'))badge(el);
    for(const el of group.querySelectorAll('.ac-model-alias-add'))el.disabled=true;
    for(const el of group.querySelectorAll('.ac-model-meta-editable')){
      if(el.closest('.ac-model-meta-nameline'))continue;
      const line=el.closest('.ac-model-meta-line');badge(line);el.setAttribute('aria-disabled','true');el.tabIndex=-1;el.style.pointerEvents='none';
    }
    for(const row of tableHost.querySelectorAll('.ac-model-cfg-row')){
      const name=row.querySelector('.ac-ability-name')?.textContent;
      if(name==='Provider'||name==='Base URL'||name==='Model name')badge(row.querySelector('.ac-ability-label')||row);
      for(const btn of row.querySelectorAll('button'))if(['Load','Save'].includes(btn.textContent)){btn.disabled=true;badge(btn.parentElement);}
    }
    badge(analyticsHost);
    for(const row of group.querySelectorAll('.ac-directive-row'))badge(row.querySelector('.ac-ability-name')||row);
    if(!bridge.state()?.can_manage){for(const el of tableHost.querySelectorAll('.ac-model-meta-editable,.ac-saved-drag-handle')){el.style.pointerEvents='none';el.tabIndex=-1;el.setAttribute('aria-disabled','true');}}
    if(!bridge.state()?.can_manage){for(const el of tableHost.querySelectorAll('input,select,textarea,button'))el.disabled=true;}
  }
  // Rebuilt rows and expanded details get their own adjacent contract markers.
  const observer=new MutationObserver(pending);observer.observe(group,{childList:true,subtree:true});pending();
  return {reload:()=>table.reload(),getState:()=>table.getState(),destroy:()=>{stopped=true;observer.disconnect();bridge.destroy();table.destroy();analytics?.destroy?.();host.replaceChildren();}};
}
