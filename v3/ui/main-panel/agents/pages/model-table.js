'use strict';

// Phase-one adaptation of ui/shared/js/model-table.js: ac-list configurator,
// expandable ac-row/ac-ability-body, MODEL-ALIASES form reuse and unlink semantics.
// V3's separate records replace v2 roster blobs; order belongs to the backup group.
// Do not import the v2 auth-aware dom-utils/transport or expose unported role controls.
const node=(tag,cls='',text='')=>{const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;};
const button=(text,fn)=>{const el=node('button','ac-btn ac-btn-ghost ac-btn-sm',text);el.type='button';el.onclick=fn;return el;};
const clone=value=>structuredClone(value);
const uid=prefix=>prefix+crypto.randomUUID();

export async function mountSelectionTable(host,{request,agentId,onAgent,onAssignment}) {
  let catalog,owner,agents,busy=false,openId='',destroyed=false;
  const status=node('p','ac-hint');status.setAttribute('role','status');
  const table=node('div','ac-list ac-model-config');
  const roster=node('div','ac-list');
  const agentSelect=node('select','ac-input ac-config-sel');agentSelect.setAttribute('aria-label','Agent for model assignment');
  const chooser=node('div','ac-ability-row');chooser.append(node('span','ac-ability-name','Agent'),agentSelect);
  const reload=button('Reload models',()=>act(reload,async()=>{await load();await onAssignment(agentId,catalog.assignments[agentId]);render();}));chooser.append(reload);
  agentSelect.onchange=async()=>{agentId=agentSelect.value;openId='';await onAgent(agentId);await onAssignment(agentId,catalog.assignments[agentId]);render();};
  const nameInput=node('input','ac-input');nameInput.placeholder='New agent name';nameInput.maxLength=200;nameInput.setAttribute('aria-label','New agent name');
  const create=button('Create agent',()=>act(create,async()=>{
    const name=nameInput.value.trim();if(!name)throw Error('Enter an agent name');
    const result=await request('/models/agents','POST',{client_request_id:crypto.randomUUID(),name});
    agentId=result.id;nameInput.value='';await load();await onAgent(agentId);await onAssignment(agentId,catalog.assignments[agentId]);render();
  }));
  const createRow=node('div','ac-ability-row');createRow.append(nameInput,create);
  const explanation=node('p','ac-hint','Explicit primary assignment and ordered backups. Only ChatGPT subscription transport is executable; API-key providers and role routing are not ported. Each backup needs its own authorization.');
  host.replaceChildren(chooser,createRow,status,explanation,roster,table);
  async function load(){
    const [data,list]=await Promise.all([request('/models/catalog'),request('/models/agents')]);
    if(destroyed)return;
    catalog=data.catalog;owner=data.caller_id;agents=list.agents;
    if(!agents.some(a=>a.id===agentId))throw Error('Selected agent does not exist');
    agentSelect.replaceChildren(...agents.map(a=>{const option=node('option','',a.name);option.value=a.id;return option;}));
    agentSelect.value=agentId;
  }
  async function act(control,fn){
    if(busy)return;busy=true;control.disabled=true;status.textContent='Saving…';
    const check=node('span','agents-autosave-check');check.innerHTML='<span class="agents-spinner"></span>';control.after(check);
    try{await fn();check.textContent='✓';check.classList.add('saved');status.textContent='Saved';}
    catch(error){check.textContent='⚠';check.classList.add('error');status.textContent=error.message;}
    finally{busy=false;control.disabled=false;setTimeout(()=>check.remove(),2500);}
  }
  async function save(next){
    const result=await request('/models/catalog','PUT',next);catalog=result.catalog;
    await onAssignment(agentId,catalog.assignments[agentId]);render();
  }
  const assignment=()=>catalog.assignments[agentId];
  const members=()=>{const a=assignment();return a?.backup_group_id?catalog.groups[a.backup_group_id]?.entry_ids||[]:[];};
  function assign(next,id){
    const current=next.assignments[agentId];
    if(current?.backup_group_id)next.groups[current.backup_group_id].entry_ids=next.groups[current.backup_group_id].entry_ids.filter(value=>value!==id);
    next.assignments[agentId]=current?{...current,primary_entry_id:id}:{agent_id:agentId,owner_id:owner,primary_entry_id:id,backup_group_id:null,revision:1};
  }
  function link(next,id){
    const a=next.assignments[agentId];if(!a)throw Error('Assign a primary first');
    if(id===a.primary_entry_id)throw Error('A backup must be a different model entry');
    if(!a.backup_group_id){const groupId=uid('group-');a.backup_group_id=groupId;next.groups[groupId]={id:groupId,owner_id:owner,entry_ids:[],revision:1};}
    const group=next.groups[a.backup_group_id];if(group.entry_ids.includes(id))throw Error('Already linked');
    if(group.entry_ids.length>=7)throw Error('At most seven backups');group.entry_ids.push(id);
  }
  // Reuse one live configurator for normal adds and primary-row backup creation,
  // as MODEL-ALIASES does in v2. Never copy credentials from the primary.
  const form=node('form','');let backupTarget=null;
  const model=node('input','ac-input ac-input-sm');model.required=true;model.pattern='[A-Za-z0-9._-]+';model.maxLength=128;model.setAttribute('aria-label','Upstream model ID');
  const display=node('input','ac-input ac-input-sm');display.required=true;display.maxLength=200;display.setAttribute('aria-label','Model display name');
  function formRow(label,control){const row=node('div','ac-ability-row ac-model-cfg-row');row.append(node('label','ac-ability-label',label),control);form.append(row);}
  formRow('Provider',node('span','ac-model-cfg-control','ChatGPT Subscription'));
  formRow('Model ID',model);formRow('Display name',display);
  const add=node('button','ac-btn ac-btn-primary ac-btn-sm','Add connection');add.type='submit';
  const cancel=button('Cancel',()=>{form.reset();backupTarget=null;form.hidden=true;table.append(form);});
  const actions=node('div','ac-ability-row');actions.append(add,cancel);form.append(actions);form.hidden=true;
  form.onsubmit=event=>{event.preventDefault();act(add,async()=>{
    if(!form.reportValidity())return;
    const next=clone(catalog),id=uid('model-'),connectionId=uid('connection-');
    next.connections[connectionId]={id:connectionId,owner_id:owner,transport:'openai_subscription',credential_ref:uid('provider_credential:'),allowed_agents:[agentId],revision:1,revoked:false};
    next.entries[id]={id,owner_id:owner,connection_id:connectionId,upstream_model:model.value.trim(),display_name:display.value.trim(),capabilities:['text'],allowed_agents:[agentId],revision:1};
    if(backupTarget)link(next,id);else if(!assignment())assign(next,id);
    openId=id;form.hidden=true;form.reset();backupTarget=null;await save(next);
  });};
  function openForm(destination,primary=null){
    form.reset();backupTarget=primary;add.textContent=primary?'Add backup connection':'Add connection';
    destination.append(form);form.hidden=false;model.focus();
  }
  const plus=button('Add model connection',()=>openForm(table));
  table.append(plus,form);
  function render(){
    if(!catalog||destroyed)return;
    // Restore shared form before replacing any expanded row that may contain it.
    table.append(form);form.hidden=true;backupTarget=null;
    agentSelect.value=agentId;roster.replaceChildren();
    const current=assignment();
    status.textContent=current?'Primary: '+catalog.entries[current.primary_entry_id].display_name:'No explicit primary assigned. New agents cannot run until assigned.';
    const entries=Object.values(catalog.entries).filter(e=>e.allowed_agents.includes(agentId));
    if(!entries.length)roster.append(node('div','ac-ability-row','No model connections assigned to this agent.'));
    for(const entry of entries){
      const row=node('div','ac-row ac-saved-injected');row.dataset.entryId=entry.id;
      row.classList.toggle('expanded',openId===entry.id);
      const head=node('div','ac-ability-row');
      const expand=button(entry.display_name,()=>{const opened=row.classList.toggle('expanded');openId=opened?entry.id:'';expand.setAttribute('aria-expanded',String(opened));});
      expand.classList.add('ac-saved-name-click');expand.setAttribute('aria-expanded',String(openId===entry.id));
      const choice=node('input','');choice.type='radio';choice.name='primary-model';choice.checked=current?.primary_entry_id===entry.id;choice.setAttribute('aria-label','Use '+entry.display_name+' as primary');
      choice.onchange=()=>act(choice,async()=>{const next=clone(catalog);assign(next,entry.id);await save(next);});
      head.append(choice,expand,node('span','ac-ability-desc',entry.upstream_model));
      const body=node('div','ac-ability-body');const meta=node('div','ac-model-meta-list');
      for(const [label,value]of [['Model name:',entry.upstream_model],['Provider:','ChatGPT Subscription'],['Connection:',entry.connection_id]]){
        const line=node('div','ac-model-meta-line');line.append(node('span','ac-model-meta-k',label),node('span','ac-model-meta-v',value));meta.append(line);
      }
      body.append(meta);
      const connection=catalog.connections[entry.connection_id];
      const connectionState=node('span','ac-hint',connection.revoked?'Revoked':'Checking connection…');
      const authorization=node('div','ac-subscription-auth');
      const authorize=button('Authorize this connection',()=>act(authorize,async()=>{
        await request('/models/connections/'+encodeURIComponent(connection.id)+'/authorization','POST',{});
        await load();await updateConnection();
      }));authorize.disabled=connection.revoked;
      async function updateConnection(){
        const data=await request('/models/connections/'+encodeURIComponent(connection.id));
        if(destroyed)return;connectionState.textContent=data.revoked?'Revoked':data.connected?'Connected':'Authorization required';authorization.replaceChildren();
        if(data.authorization?.verification_url){const link=node('a','','Open ChatGPT authorization');link.href=data.authorization.verification_url;link.target='_blank';link.rel='noopener noreferrer';authorization.append(link,node('p','','Code: '+data.authorization.user_code+' — '+data.authorization.status));}
      }
      body.append(connectionState,authorize,authorization);
      const check=button('Refresh connection status',()=>act(check,updateConnection));body.append(check);
      updateConnection().catch(error=>{connectionState.textContent=error.message;});
      if(current?.primary_entry_id===entry.id){
        const region=node('div','ac-model-alias-region');
        const backupLine=node('div','ac-model-meta-line ac-model-meta-aliases');
        const addBackup=button('Add backup connection',()=>openForm(region,entry.id));
        backupLine.append(node('span','ac-model-meta-k','Ordered backups:'),addBackup);meta.append(backupLine);
        const list=node('div','ac-list ac-model-alias-members');list.setAttribute('aria-label','Ordered backup connections');
        members().filter(id=>id!==entry.id).forEach((id,index,array)=>{
          const member=catalog.entries[id],item=node('div','ac-ability-row');item.append(node('span','ac-ability-name',(index+1)+'. '+member.display_name));
          for(const [label,delta]of [['Move earlier',-1],['Move later',1]]){
            const move=button(label,()=>act(move,async()=>{const next=clone(catalog),group=next.groups[next.assignments[agentId].backup_group_id];const from=group.entry_ids.indexOf(id),to=from+delta;[group.entry_ids[from],group.entry_ids[to]]=[group.entry_ids[to],group.entry_ids[from]];await save(next);}));
            move.disabled=delta<0?index===0:index===array.length-1;item.append(move);
          }
          const unlink=button('Unlink',()=>act(unlink,async()=>{const next=clone(catalog),group=next.groups[next.assignments[agentId].backup_group_id];group.entry_ids=group.entry_ids.filter(value=>value!==id);await save(next);}));unlink.title='Keep the connection, but remove it from the backup group';item.append(unlink);list.append(item);
        });region.append(list);
        const existing=node('select','ac-input ac-config-sel');existing.setAttribute('aria-label','Existing backup connection');existing.append(node('option','','Choose existing connection'));
        for(const candidate of entries.filter(e=>e.id!==entry.id&&!members().includes(e.id))){const option=node('option','',candidate.display_name);option.value=candidate.id;existing.append(option);}
        existing.onchange=()=>{if(existing.selectedIndex>0)act(existing,async()=>{const next=clone(catalog);link(next,existing.value);await save(next);});};region.append(existing,node('p','ac-hint','Backups run in this explicit order only for eligible failures before a response starts. Unlink does not delete the connection.'));body.append(region);
      }
      row.append(head,body);roster.append(row);
    }
  }
  await load();await onAssignment(agentId,catalog.assignments[agentId]);render();
  return {reload:async()=>{await load();render();},destroy(){destroyed=true;host.replaceChildren();},getAgent:()=>agentId};
}
