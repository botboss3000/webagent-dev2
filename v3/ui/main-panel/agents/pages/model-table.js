'use strict';

// The selected agent owns its roster and roles; connection owners alone grant use.
// Server admission remains authoritative for membership, tier, and live revocation.
const node=(tag,cls='',text='')=>{const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;};
const button=(text,fn)=>{const el=node('button','ac-btn ac-btn-ghost ac-btn-sm',text);el.type='button';el.onclick=fn;return el;};
const clone=value=>structuredClone(value);
const uid=prefix=>prefix+crypto.randomUUID();
const ROLES=[['standard','Standard','Normal chat'],['premium','Premium','Workflow unavailable'],['system','System','Context compaction'],['image_in','Image In','Workflow unavailable'],['image_out','Image Out','Workflow unavailable'],['voice','Voice','Workflow unavailable']];

export async function mountSelectionTable(host,{request,agentId,onAgent,onAssignment}) {
  let catalog,owner,agentOwner,agents=[],available=[],canManage=false,canCreate=false,busy=false,openId='',destroyed=false,members=[],authority={};
  const status=node('p','ac-hint');status.setAttribute('role','status');
  const table=node('div','ac-list ac-model-config'),roster=node('div','ac-list'),roles=node('div','ac-list');
  const agentSelect=node('select','ac-input ac-config-sel');agentSelect.setAttribute('aria-label','Agent for model configuration');
  const chooser=node('div','ac-ability-row');chooser.append(node('span','ac-ability-name','Agent'),agentSelect);
  // Every catalog read and write names the selected agent; switching agents reloads authority.
  const scope=path=>path+'?agent_id='+encodeURIComponent(agentId);
  // MODEL-OVERSIGHT: configuration authority does not grant personal tenant usage.
  const configurable=()=>canManage?Object.values(catalog.entries).filter(entry=>{
    const connection=catalog.connections[entry.connection_id];
    return entry.allowed_agents.includes(agentId)&&connection&&!connection.revoked&&connection.allowed_agents.includes(agentId);
  }).map(entry=>entry.id):available;
  // Show the caller's usage ceiling separately from configuration eligibility.
  const availability=id=>available.includes(id)?'Available to you':'Not available for your account to use';
  const assignment=()=>catalog.agent_models?.[agentId];
  const notify=()=>agentId?onAssignment?.(agentId,assignment()):undefined;
  const reload=button('Reload models',()=>act(reload,async()=>{await load();if(agentId)await onAgent(agentId);await notify();render();}));chooser.append(reload);
  agentSelect.onchange=()=>act(agentSelect,async()=>{const previous=agentId;agentId=agentSelect.value;openId='';try{await load();}catch(error){agentId=previous;agentSelect.value=previous;throw error;}await onAgent(agentId);await notify();render();});
  const nameInput=node('input','ac-input');nameInput.placeholder='New agent name';nameInput.maxLength=200;nameInput.setAttribute('aria-label','New agent name');
  const create=button('Create agent',()=>act(create,async()=>{
    const name=nameInput.value.trim();if(!name)throw Error('Enter an agent name');
    const result=await request('/models/agents','POST',{client_request_id:crypto.randomUUID(),name});
    agentId=result.id;nameInput.value='';await load();await onAgent(agentId);await notify();render();
  }));
  const createRow=node('div','ac-ability-row');createRow.append(nameInput,create);
  const explanation=node('p','ac-hint','Add models to this agent’s roster, then assign roles and ordered backups. Standard serves normal chat; System serves context compaction. Other roles can be configured but their workflows are unavailable.');
  const memberPanel=node('div','ac-list');
  host.replaceChildren(chooser,createRow,status,explanation,roles,roster,table,memberPanel);
  // The API supplies effective authority and eligible records; the UI never infers access from a name.
  async function load(){
    // Resolve the caller's authorized agents first; a stale default route grants no catalog access.
    const list=await request('/models/agents');if(destroyed)return;
    agents=list.agents;owner=list.caller_id;canCreate=!!list.can_create_agent;
    if(!agents.some(a=>a.id===agentId))agentId=agents[0]?.id||'';
    agentSelect.replaceChildren(...agents.map(a=>{const option=node('option','',a.name);option.value=a.id;return option;}));agentSelect.value=agentId;
    if(!agentId){
      // An account with no agents can still create its first one without querying another tenant.
      catalog={connections:{},entries:{},agent_models:{}};agentOwner=null;available=[];canManage=false;members=[];authority={};return;
    }
    const data=await request(scope('/models/catalog'));if(destroyed)return;
    catalog=data.catalog;owner=data.caller_id;agentOwner=data.agent_owner_id;
    available=data.available_entries||[];canManage=!!data.can_manage;canCreate=!!data.can_create_agent;
    const membership=canManage?await request('/models/agents/'+encodeURIComponent(agentId)+'/members'):{members:[],authority:{}};
    members=membership.members;authority=membership.authority;
    for(const option of provider.children)option.disabled=Array.isArray(data.supported_transports)&&!data.supported_transports.includes(option.value);
  }
  async function act(control,fn){
    if(busy)return;busy=true;control.disabled=true;agentSelect.disabled=true;status.textContent='Saving…';
    const check=node('span','agents-autosave-check');check.innerHTML='<span class="agents-spinner"></span>';control.after(check);
    try{await fn();check.textContent='✓';check.classList.add('saved');if(status.textContent==='Saving…')status.textContent='Saved';}
    catch(error){check.textContent='⚠';check.classList.add('error');status.textContent=error.message;}
    finally{busy=false;control.disabled=false;agentSelect.disabled=false;setTimeout(()=>check.remove(),2500);}
  }
  // Submit the filtered snapshot with revisions; the server merges only records this caller may edit.
  async function save(next){
    if(!canManage)throw Error('You cannot manage this agent');
    await request(scope('/models/catalog'),'PUT',next);
    await load();if(agentId)await onAgent(agentId);await notify();render();
  }
  function config(next){
    next.agent_models??={};
    return next.agent_models[agentId]??={agent_id:agentId,owner_id:agentOwner,entry_ids:[],roles:{},revision:1};
  }
  const mutate=(control,fn)=>act(control,async()=>{const next=clone(catalog);fn(next,config(next));await save(next);});
  function selection(label,ids,empty){
    const select=node('select','ac-input ac-config-sel');select.setAttribute('aria-label',label);
    const blank=node('option','',empty);blank.value='';select.append(blank);
    for(const id of ids){const entry=catalog.entries[id];if(!entry)continue;const option=node('option','',entry.display_name);option.value=id;select.append(option);}
    return select;
  }
  const form=node('form','');
  const model=node('input','ac-input ac-input-sm');model.required=true;model.pattern='[A-Za-z0-9._-]+';model.maxLength=128;model.setAttribute('aria-label','Upstream model ID');
  const display=node('input','ac-input ac-input-sm');display.required=true;display.maxLength=200;display.setAttribute('aria-label','Model display name');
  function formRow(label,control){const row=node('div','ac-ability-row ac-model-cfg-row');row.append(node('label','ac-ability-label',label),control);form.append(row);}
  const provider=node('select','ac-input ac-config-sel');provider.setAttribute('aria-label','Provider');
  for(const [value,label]of [['openai_subscription','ChatGPT Subscription'],['deepseek','DeepSeek']]){const option=node('option','',label);option.value=value;provider.append(option);}
  const suggestions=node('datalist','');suggestions.id=uid('model-options-');model.setAttribute('list',suggestions.id);form.append(suggestions);
  provider.onchange=()=>{suggestions.replaceChildren();if(provider.value==='deepseek'){for(const id of ['deepseek-chat','deepseek-reasoner']){const option=node('option','',id);option.value=id;suggestions.append(option);}model.value='deepseek-chat';display.value='DeepSeek Chat';}else{model.value='';display.value='';}};
  formRow('Provider',provider);formRow('Model ID',model);formRow('Display name',display);
  // Capability declarations limit role choices; they do not establish account entitlement.
  const capabilities={};
  for(const [key,title]of [['text','Text'],['image_in','Image input'],['image_out','Image output'],['voice','Voice']]){const input=node('input');input.type='checkbox';input.defaultChecked=key==='text';input.checked=key==='text';input.setAttribute('aria-label',title+' capability');capabilities[key]=input;formRow(title,input);}
  form.append(node('p','ac-hint','Declare supported model capabilities. This metadata does not verify provider entitlement.'));
  const add=node('button','ac-btn ac-btn-primary ac-btn-sm','Add connection');add.type='submit';
  const cancel=button('Cancel',()=>{form.reset();form.hidden=true;});
  const actions=node('div','ac-ability-row');actions.append(add,cancel);form.append(actions);form.hidden=true;
  form.onsubmit=event=>{event.preventDefault();mutate(add,(next,cfg)=>{
    if(!form.reportValidity())throw Error('Complete the model fields');
    const id=uid('model-'),connectionId=uid('connection-');
    next.connections[connectionId]={id:connectionId,owner_id:owner,transport:provider.value,credential_ref:uid('provider_credential:'),allowed_agents:[agentId],revision:1,revoked:false};
    next.entries[id]={id,owner_id:owner,connection_id:connectionId,upstream_model:model.value.trim(),display_name:display.value.trim(),capabilities:Object.keys(capabilities).filter(key=>capabilities[key].checked),allowed_agents:[agentId],revision:1};
    cfg.entry_ids.push(id);openId=id;
  });};
  const plus=button('Add model connection',()=>{form.reset();suggestions.replaceChildren();form.hidden=false;model.focus();});
  table.append(plus,form);
  // Roles are independent appointments on the roster, each with its own ordered fallback list.
  function renderRoles(cfg){
    roles.replaceChildren(node('h3','ac-ability-name','Model roles'));
    const ids=(cfg?.entry_ids||[]).filter(id=>configurable().includes(id));
    for(const [key,label,purpose]of ROLES){
      const role=cfg?.roles?.[key],section=node('div','ac-row expanded'),head=node('div','ac-ability-row');
      head.append(node('span','ac-ability-name',label),node('span','ac-ability-desc',purpose));
      const eligible=ids.filter(id=>catalog.entries[id]?.capabilities?.includes(['standard','premium','system'].includes(key)?'text':key));
      const select=selection(label+' model',eligible,'Unassigned');
      if(role&&!eligible.includes(role.entry_id)){const option=node('option','',(catalog.entries[role.entry_id]?.display_name||'Model')+' · Unavailable');option.value=role.entry_id;option.disabled=true;select.append(option);}
      select.value=role?.entry_id||'';select.disabled=!canManage;
      select.onchange=()=>mutate(select,(_next,c)=>{if(!select.value)delete c.roles[key];else c.roles[key]={entry_id:select.value,backup_entry_ids:(c.roles[key]?.backup_entry_ids||[]).filter(id=>id!==select.value)};});
      head.append(select);section.append(head);
      const body=node('div','ac-ability-body');body.append(node('p','ac-hint','Ordered backups'));
      (role?.backup_entry_ids||[]).forEach((id,index,list)=>{
        const line=node('div','ac-ability-row');line.append(node('span','ac-ability-name',(index+1)+'. '+(catalog.entries[id]?.display_name||'Unavailable model')));
        if(canManage){
          for(const [title,delta]of [['Move earlier',-1],['Move later',1]]){
            const move=button(title,()=>mutate(move,(_next,c)=>{const order=c.roles[key].backup_entry_ids;[order[index],order[index+delta]]=[order[index+delta],order[index]];}));
            move.disabled=delta<0?index===0:index===list.length-1;line.append(move);
          }
          const unlink=button('Unlink backup',()=>mutate(unlink,(_next,c)=>{c.roles[key].backup_entry_ids=c.roles[key].backup_entry_ids.filter(value=>value!==id);}));line.append(unlink);
        }body.append(line);
      });
      if(canManage&&role){
        const backup=selection(label+' backup',eligible.filter(id=>id!==role.entry_id&&!role.backup_entry_ids.includes(id)),'Add existing backup');
        backup.onchange=()=>{if(backup.value)mutate(backup,(_next,c)=>c.roles[key].backup_entry_ids.push(backup.value));};body.append(backup);
      }
      section.append(body);roles.append(section);
    }
  }
  // Agent managers assign existing accounts; only the owner may grant agent administrator authority.
  function renderMembers(){
    memberPanel.replaceChildren();memberPanel.hidden=!canManage;if(!canManage)return;
    memberPanel.append(node('h3','ac-ability-name','Agent members'),node('p','ac-hint','Assign an existing account ID to this agent. This does not invite or create an account.'));
    const isOwner=authority.owner_id===owner;
    function editor(current){
      const row=node('div','ac-ability-row'),account=node('input','ac-input');account.value=current?.user_id||'';account.placeholder='Existing account ID';account.setAttribute('aria-label','Member account ID');account.disabled=!!current;account.maxLength=200;
      const relationship=node('select','ac-input ac-config-sel');relationship.setAttribute('aria-label','Agent relationship');
      for(const [value,label]of [['member','Member'],['caller','Visitor'],['revoked','Revoked']]){const option=node('option','',label);option.value=value;relationship.append(option);}relationship.value=current?.relationship||'member';
      const admin=node('input');admin.type='checkbox';admin.checked=!!current?.agent_administrator;admin.disabled=!isOwner;admin.setAttribute('aria-label','Agent administrator');
      const adminLabel=node('label','ac-hint','Agent administrator');adminLabel.append(admin);
      const saveMember=button(current?'Save member':'Add existing account',()=>act(saveMember,async()=>{
        const userId=account.value.trim();if(!userId)throw Error('Enter an existing account ID');
        const existing=members.find(member=>member.user_id===userId);
        if(userId===authority.owner_id)throw Error('The agent owner cannot be edited here');
        await request('/models/agents/'+encodeURIComponent(agentId)+'/members','PUT',{user_id:userId,relationship:relationship.value,agent_administrator:relationship.value==='revoked'?false:isOwner?admin.checked:!!existing?.agent_administrator,revision:existing?.revision||0});
        await load();render();
      }));
      row.append(account,relationship,adminLabel,saveMember);memberPanel.append(row);
    }
    for(const member of members){if(member.user_id===authority.owner_id){memberPanel.append(node('div','ac-ability-row',member.user_id+' · Owner'));}else editor(member);}
    editor(null);
  }
  function removeRoster(control,id){return mutate(control,(_next,c)=>{c.entry_ids=c.entry_ids.filter(value=>value!==id);for(const key of Object.keys(c.roles)){if(c.roles[key].entry_id===id)delete c.roles[key];else c.roles[key].backup_entry_ids=c.roles[key].backup_entry_ids.filter(value=>value!==id);}});}
  function render(){
    if(!catalog||destroyed)return;
    form.hidden=true;agentSelect.value=agentId;createRow.hidden=!canCreate;table.hidden=!canManage;roster.replaceChildren();
    roles.hidden=!agentId;explanation.hidden=!agentId;
    if(!agentId){roles.replaceChildren();memberPanel.replaceChildren();memberPanel.hidden=true;status.textContent=canCreate?'Create your first agent to configure models.':'No agents available.';return;}
    const cfg=assignment(),ids=cfg?.entry_ids||[];
    status.textContent=(cfg?.roles?.standard?'Standard: '+(catalog.entries[cfg.roles.standard.entry_id]?.display_name||'Unavailable model'):'Standard model required')+(canManage?'':' · View only');
    renderRoles(cfg);renderMembers();
    // Removing a roster item clears its appointments while retaining the separately owned connection.
    roster.append(node('h3','ac-ability-name','Agent model roster'));
    if(canManage){
      const existing=selection('Add existing model to roster',configurable().filter(id=>!ids.includes(id)),'Add existing model');
      existing.onchange=()=>{if(existing.value)mutate(existing,(_next,c)=>c.entry_ids.push(existing.value));};roster.append(existing);
    }
    if(!ids.length)roster.append(node('div','ac-ability-row','No models in this agent’s roster.'));
    for(const id of ids){
      const entry=catalog.entries[id],connection=entry&&catalog.connections[entry.connection_id];
      // Revoked shared records may disappear from the filtered catalog while roster references remain.
      if(!entry||!connection){const missing=node('div','ac-ability-row');missing.append(node('span','ac-ability-name','Unavailable model · '+id));if(canManage){const remove=button('Remove from roster',()=>removeRoster(remove,id));missing.append(remove);}roster.append(missing);continue;}
      const row=node('div','ac-row ac-saved-injected');row.dataset.entryId=entry.id;row.classList.toggle('expanded',openId===entry.id);
      const head=node('div','ac-ability-row');
      const expand=button(entry.display_name,()=>{const opened=row.classList.toggle('expanded');openId=opened?entry.id:'';expand.setAttribute('aria-expanded',String(opened));});
      expand.classList.add('ac-saved-name-click');expand.setAttribute('aria-expanded',String(openId===entry.id));
      head.append(expand,node('span','ac-ability-desc',entry.upstream_model+' · '+availability(id)));
      if(canManage){const remove=button('Remove from roster',()=>removeRoster(remove,id));remove.title='Clears this model’s role assignments and backups; keeps the connection';head.append(remove);}
      const body=node('div','ac-ability-body'),meta=node('div','ac-model-meta-list');
      for(const [label,value]of [['Model name:',entry.upstream_model],['Provider:',connection.transport==='deepseek'?'DeepSeek':'ChatGPT Subscription'],['Connection:',connection.id],['Connection owner:',connection.owner_id]]){
        const line=node('div','ac-model-meta-line');line.append(node('span','ac-model-meta-k',label),node('span','ac-model-meta-v',value));meta.append(line);
      }body.append(meta);
      // Shared use never grants access to credentials or provider authorization codes.
      const owned=canManage&&owner===connection.owner_id,connectionState=node('span','ac-hint',connection.revoked?'Revoked':'Checking connection…'),authorization=node('div','ac-subscription-auth');
      const connectionPath='/models/connections/'+encodeURIComponent(connection.id);
      async function updateConnection(){
        const data=await request(scope(connectionPath));
        if(destroyed||!row.isConnected)return;
        connectionState.textContent=data.revoked?'Revoked':data.connected?(connection.transport==='deepseek'?'API key saved':'Connected'):(connection.transport==='deepseek'?'API key required':'Authorization required');authorization.replaceChildren();
        if(owned&&data.authorization?.verification_url){const link=node('a','','Open ChatGPT authorization');link.href=data.authorization.verification_url;link.target='_blank';link.rel='noopener noreferrer';authorization.append(link,node('p','','Code: '+data.authorization.user_code+' — '+data.authorization.status));}
      }
      body.append(connectionState);
      if(owned&&connection.transport==='deepseek'){
        const key=node('input','ac-input ac-input-sm');key.type='password';key.autocomplete='new-password';key.placeholder='DeepSeek API key';key.maxLength=512;key.setAttribute('aria-label','DeepSeek API key');key.disabled=connection.revoked;
        const saveKey=button('Save API key',()=>act(saveKey,async()=>{try{await request(connectionPath+'/api-key','PUT',{api_key:key.value.trim()});await updateConnection();}finally{key.value='';}}));saveKey.disabled=connection.revoked;
        const line=node('div','ac-ability-row');line.append(key,saveKey);body.append(line,node('p','ac-hint','Stored encrypted in v3. Saving confirms storage, not provider access.'));
      }else if(owned){
        const authorize=button('Authorize this connection',()=>act(authorize,async()=>{await request(connectionPath+'/authorization','POST',{});await updateConnection();}));authorize.disabled=connection.revoked;body.append(authorize);
      }
      body.append(authorization);const check=button('Refresh connection status',()=>act(check,updateConnection));body.append(check);
      // Connection owners control revocation and agent grants; dispatch rechecks them on the server.
      if(owned){
        const revoke=button(connection.revoked?'Restore connection':'Revoke connection',()=>mutate(revoke,next=>{next.connections[connection.id].revoked=!connection.revoked;}));body.append(revoke);
        if(owner===entry.owner_id){
          const grants=node('div','ac-list');grants.append(node('p','ac-hint','Share this model and connection with agents you can see. Connection access also affects other models using it. Revoking access takes effect at the next dispatch.'));
          for(const agent of agents){
            const line=node('label','ac-ability-row'),toggle=node('input');toggle.type='checkbox';toggle.checked=entry.allowed_agents.includes(agent.id)&&connection.allowed_agents.includes(agent.id);toggle.setAttribute('aria-label','Share '+entry.display_name+' with '+agent.name);
            toggle.onchange=()=>mutate(toggle,next=>{for(const record of [next.entries[id],next.connections[connection.id]])record.allowed_agents=toggle.checked?[...new Set([...record.allowed_agents,agent.id])]:record.allowed_agents.filter(value=>value!==agent.id);});
            line.append(toggle,node('span','ac-ability-name',agent.name));grants.append(line);
          }body.append(grants);
        }
      }
      row.append(head,body);roster.append(row);updateConnection().catch(error=>{connectionState.textContent=error.message;});
    }
  }
  await load();if(agentId)await onAgent(agentId);await notify();render();
  return {reload:async()=>{await load();if(agentId)await onAgent(agentId);await notify();render();},destroy(){destroyed=true;host.replaceChildren();},getAgent:()=>agentId};
}
