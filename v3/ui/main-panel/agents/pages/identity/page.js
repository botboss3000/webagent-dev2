
'use strict';
import { renderVisibility } from './visibility.js';
import { renderClosing } from './closing.js';
// Adapted V2 Identity fields; V3 transport, tenant permissions and revision fencing.
export async function renderIdentity(host, request, {agentId, onSaved = () => {}}) {
  const path = '/models/agents/' + encodeURIComponent(agentId) + '/identity';
  host.replaceChildren();
  const heading = document.createElement('h2'); heading.textContent = 'Identity';
  host.append(heading);
  let agent;
  try { agent = await request(path); }
  catch (error) {
    const message = document.createElement('p'); message.className = 'ac-hint';
    message.setAttribute('role', 'alert'); message.textContent = 'Identity unavailable: ' + error.message;
    host.append(message); return;
  }
  onSaved(agent);
  const group = document.createElement('div'); group.className = 'agent-identity-settings agent-identity-settings--embedded';
  const content = document.createElement('div'); content.className = 'ac-category-body';
  group.append(content); host.append(group);
  // Serialize fields so concurrent name/description saves share the latest revision.
  let queue = Promise.resolve();
  for (const [field, label, placeholder] of [['name','Name','Name this agent…'],['description','Description','Describe what this agent is for…']]) {
    const wrap = document.createElement('div'); wrap.className = 'ac-cfg-field';
    const labelEl = document.createElement('label'); labelEl.className = 'ac-label'; labelEl.textContent = label;
    const indicator = document.createElement('span'); indicator.className = 'ac-cfg-ind';
    indicator.setAttribute('role','status'); indicator.setAttribute('aria-live','polite'); labelEl.append(indicator);
    const input = document.createElement(field === 'description' ? 'textarea' : 'input');
    input.id = 'identity-' + field; labelEl.htmlFor = input.id;
    if(field === 'description') input.rows = 3; else input.type = 'text';
    input.className = 'ac-input'; input.dataset.field = field;
    input.value = agent[field] || ''; input.placeholder = placeholder; input.readOnly = !agent.can_manage;
    wrap.append(labelEl,input); content.append(wrap);
    let timer;
    const status = (text) => { indicator.textContent = text === 'Saved' ? '✓' : text === 'Saving…' ? '…' : '!'; indicator.title = text; indicator.setAttribute('aria-label',text); };
    const save = () => {
      clearTimeout(timer);
      const value = field === 'name' ? input.value.trim() : input.value;
      if(field === 'name' && !value) { input.value = agent.name; status('Name cannot be blank'); return; }
      queue = queue.then(async () => {
        if(value === agent[field]) return;
        status('Saving…');
        try {
          agent = await request(path,'PUT',{revision:agent.revision,[field]:value});
          if(input.value.trim() === value && field === 'name') input.value = agent.name;
          status('Saved'); onSaved(agent);
        } catch(error) { status('Save failed: ' + error.message); }
      });
    };
    if(!input.readOnly) {
      input.addEventListener('input',()=>{clearTimeout(timer); timer=setTimeout(save,700);});
      input.addEventListener('blur',save);
    }
  }
  const title = document.createElement('h3'); title.textContent = 'Chat modes'; content.append(title);
  const hint = document.createElement('p'); hint.className = 'ac-hint';
  hint.textContent = 'Default mode applies to new conversations. Read-only modes cannot execute mutating tools. Each mode adds its own instruction before model calls; existing permissions and approvals still apply.'; content.append(hint);
  let modes = structuredClone(agent.execution_modes);
  const defaultLabel = document.createElement('label'); defaultLabel.className = 'ac-label'; defaultLabel.textContent = 'Default mode';
  const select = document.createElement('select'); select.className = 'ac-input'; select.dataset.field = 'default_execution_mode'; select.disabled = !agent.can_manage;
  const notice = document.createElement('p'); notice.className = 'ac-hint'; notice.setAttribute('role','status');
  const fill = () => { select.replaceChildren(); modes.filter(m=>!m._deleted&&!m._new).forEach(m=>{ const option=document.createElement('option'); option.value=m.id; option.textContent=m.label; select.append(option); }); select.value=agent.default_execution_mode; };
  fill(); defaultLabel.append(select); content.append(defaultLabel,notice);
  const persist = (patch) => {
    const task = queue.then(async()=>{ const saved=await request(path,'PUT',{revision:agent.revision,...patch}); agent=saved; onSaved(saved); return saved; });
    queue=task.catch(()=>{}); return task;
  };
  select.addEventListener('change',async()=>{select.disabled=true;try {await persist({default_execution_mode:select.value});notice.textContent='Default mode saved';} catch(error){notice.textContent='Save failed: '+error.message;} finally {fill();select.disabled=!agent.can_manage;} });
  const editor=document.createElement('div'); editor.style.cssText='display:flex;flex-direction:column;gap:8px;padding:10px 0';content.append(editor);
  const renderModes=()=>{
    editor.replaceChildren();
    modes.forEach(mode=>{
      const card=document.createElement('details');card.dataset.modeId=mode.id;card.style.cssText='border:var(--border-width) solid var(--border);border-radius:8px;padding:8px 10px;background:var(--bg-1)';
      const summary=document.createElement('summary');summary.style.cssText='cursor:pointer;display:flex;gap:8px;font-size:12px;font-weight:650';
      const nameText=document.createElement('span'),badge=document.createElement('span');badge.style.cssText='font-size:10px;color:var(--fg-3)';summary.append(nameText,badge);card.append(summary);
      const fields=document.createElement('div');fields.style.cssText='display:flex;flex-direction:column;gap:8px;margin-top:10px';card.append(fields);
      const save=document.createElement('button');save.type='button';save.className='ac-btn ac-btn-ghost';save.textContent='SAVE CHANGES';save.disabled=!mode._new;save.dataset.modeCardSave='';
      const status=document.createElement('span');status.setAttribute('role','status');
      const updateSummary=()=>{nameText.textContent=(mode.label||mode.id)+(mode._deleted?' (will be removed)':'');badge.textContent=mode.permission_policy==='write'?'Write-capable':'Read-only';};updateSummary();
      for(const [key,label] of [['label','Name'],['permission_policy','Permission policy'],['description','Footer description'],['prompt','Prompt injection']]){
        const wrap=document.createElement('label');wrap.className='ac-label';wrap.textContent=label;
        const input=document.createElement(key==='prompt'?'textarea':key==='permission_policy'?'select':'input');input.className='ac-input';input.dataset.modeField=key;
        if(key==='permission_policy')for(const [value,text] of [['read_only','Read-only'],['write','Write-capable']]){const option=document.createElement('option');option.value=value;option.textContent=text;input.append(option);}
        if(key==='prompt')input.rows=6;input.value=mode[key]||'';input.disabled=!agent.can_manage;
        input.addEventListener(key==='permission_policy'?'change':'input',()=>{mode[key]=input.value;save.disabled=false;status.textContent='Unsaved changes';updateSummary();});wrap.append(input);fields.append(wrap);
      }
      if(!mode.builtin&&agent.can_manage){const remove=document.createElement('button');remove.type='button';remove.className='ac-btn ac-btn-ghost';remove.textContent='Remove mode';remove.addEventListener('click',()=>{mode._deleted=true;fields.querySelectorAll('input,textarea,select').forEach(el=>el.disabled=true);save.disabled=false;remove.disabled=true;updateSummary();});fields.append(remove);}
      if(agent.can_manage){fields.append(save,status);save.addEventListener('click',async()=>{
        save.disabled=true;status.textContent='Saving…';
        const payload=modes.filter(m=>!m._deleted).map(({_deleted,_new,...m})=>m);
        try {await persist({execution_modes:payload});modes=structuredClone(agent.execution_modes);fill();renderModes();notice.textContent='Mode configurations saved';}
        catch(error){status.textContent='Save failed: '+error.message;save.disabled=false;}
      });}
      editor.append(card);
    });
    if(agent.can_manage){const add=document.createElement('button');add.type='button';add.className='ac-btn ac-btn-ghost';add.textContent='Add mode';add.addEventListener('click',()=>{let n=1;while(modes.some(m=>m.id==='custom-'+n))n++;modes.push({id:'custom-'+n,label:'New mode',description:'',permission_policy:'read_only',prompt:'',builtin:false,_new:true});renderModes();});editor.append(add);}
  };renderModes();
  renderVisibility(content, agent, persist);

  renderClosing(content, agent, persist);
}
