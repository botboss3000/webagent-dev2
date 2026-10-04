// V2 cycle/picker presentation with session-scoped, revision-fenced Rust writes.
export function mountSessionControls({root,client,guard,note,ensureSession,onState=()=>{}}) {
  const $=id=>root.querySelector('#'+id);
  const mode=$('chat-mode-btn'), model=$('chat-model-changer');
  const abort=new AbortController();
  let session='',generation=0,state=null,busy=false,inflight=null,active=null,confirmation=null;
  const picker=document.createElement('div');
  picker.className='chat-skill-picker chat-model-list-panel v3-chat-model-list';
  picker.hidden=true;picker.setAttribute('role','dialog');picker.setAttribute('aria-label','Conversation model');
  document.body.append(picker);
  model.setAttribute('aria-haspopup','dialog');model.setAttribute('aria-expanded','false');
  function close(){picker.hidden=true;model.setAttribute('aria-expanded','false');}
  function cancelConfirmation(){confirmation?.close();confirmation?.remove();confirmation=null;}
  function paint(){
    const modes=state?.execution_modes||[];
    const definition=modes.find(item=>item.id===state?.mode.execution_mode);
    mode.textContent=definition?.label||(state?.mode.mode==='write'?'Write':'Ask');
    mode.title=definition?definition.description+' · '+(state.mode.mode==='write'?'Write-capable':'Read-only'):'Choose execution mode';
    mode.disabled=busy||!state;model.disabled=busy||!state;
    const selected=state?.models.find(item=>item.entry_id===state.model.entry_id);
    const running=active?.model?.model;
    model.querySelector('.cmc-name').textContent=selected?.label||running||'Choose model';
    model.title=running?'Running: '+running+'. Next loop turn: '+(selected?.label||'Unavailable'):
      'Next loop turn: '+(selected?.label||'Unavailable')+' — click for models; arrows cycle';
    model.classList.toggle('no-model',!!state&&!selected);
  }
  async function load(){
    if(!session)return;
    if(inflight)return inflight;
    const sid=session,epoch=generation;
    const pending=client.controls(sid).then(data=>{
      if(sid!==session||epoch!==generation)return;
      state=data;onState(data);paint();
    }).catch(error=>{if(error.status===404){if(sid===session&&epoch===generation){state=null;onState(null);paint();}return;}throw error;}).finally(()=>{if(inflight===pending)inflight=null;});
    inflight=pending;
    return pending;
  }
  async function ready(){
    if(!session)await ensureSession();
    await load();
    if(!state)throw Error('Conversation controls are not ready');
  }
  function confirmWrite(next){
    cancelConfirmation();
    return new Promise(resolve=>{
      const dialog=document.createElement('dialog');
      dialog.className='v3-mode-confirm';dialog.setAttribute('aria-label','Confirm execution mode');
      const title=document.createElement('h3');title.textContent='Switch to '+next.label+'?';
      const text=document.createElement('p');text.textContent='This conversation will allow write-capable tools. Existing tool policies and approvals still apply.';
      const actions=document.createElement('div');
      for(const [label,value] of [['Cancel',false],['Confirm write-capable mode',true]]){
        const button=document.createElement('button');button.type='button';button.textContent=label;
        button.onclick=()=>{dialog.close(value?'confirm':'cancel');};actions.append(button);
      }
      dialog.append(title,text,actions);document.body.append(dialog);confirmation=dialog;
      dialog.addEventListener('close',()=>{const accepted=dialog.returnValue==='confirm';dialog.remove();if(confirmation===dialog)confirmation=null;resolve(accepted);},{once:true});
      const restoreComposer=document.activeElement===$('chat-input');
      dialog.addEventListener('close',()=>{
        if(restoreComposer && $('chat-input')?.isConnected)$('chat-input').focus({preventScroll:true});
      },{once:true});
      dialog.showModal();actions.firstChild.focus();
    });
  }
  async function cycleMode(){
    if(busy)return;
    await ready();
    const sid=session,epoch=generation,snapshot=state.mode;
    const modes=state.execution_modes;
    if(!modes.length)return;
    const index=modes.findIndex(item=>item.id===snapshot.execution_mode);
    const next=modes[(index+1)%modes.length];
    const widening=snapshot.mode==='read_only'&&next.permission_policy==='write';
    if(widening&&!await confirmWrite(next))return;
    if(sid!==session||epoch!==generation)return;
    state={...state,mode:{...snapshot,execution_mode:next.id,mode:next.permission_policy}};
    busy=true;paint();
    try{
      await client.mode(sid,{mode:next.id,expected_revision:snapshot.revision,confirm_write:widening});
      if(sid===session&&epoch===generation)note('Execution mode changed to '+next.label);
    }finally{if(sid===session&&epoch===generation){busy=false;await load();paint();}}
  }
  async function choose(entry){
    if(busy)return;
    const sid=session,epoch=generation,revision=state.model.revision;
    busy=true;paint();
    try{
      await client.model(sid,{entry_id:entry.entry_id,expected_revision:revision,client_request_id:crypto.randomUUID()});
      if(sid===session&&epoch===generation){close();note('Next loop turn will use '+entry.label);}
    }finally{if(sid===session&&epoch===generation){busy=false;await load();paint();}}
  }
  function show(){
    picker.replaceChildren();
    const header=document.createElement('div');header.className='csp-header';header.textContent='Models';picker.append(header);
    const list=document.createElement('div');list.className='csp-list';picker.append(list);
    for(const entry of state.models){
      const button=document.createElement('button');button.type='button';button.className='cml-item';
      button.textContent=entry.label+(entry.connected?'':' · Connection required');
      button.title=entry.model;button.disabled=!entry.connected;
      button.setAttribute('aria-pressed',String(entry.entry_id===state.model.entry_id));
      button.onclick=guard(()=>choose(entry));list.append(button);
    }
    if(!state.models.length){const text=document.createElement('p');text.className='csp-empty';text.textContent='No permitted chat models. Configure this conversation’s agent on Primary Models.';list.append(text);}
    const rect=model.getBoundingClientRect();
    picker.style.bottom=Math.max(8,innerHeight-rect.top+6)+'px';
    picker.style.right=Math.max(8,innerWidth-rect.right)+'px';
    picker.hidden=false;model.setAttribute('aria-expanded','true');
    // Pointer activation keeps typing focus; keyboard activation enters the list.
    if(document.activeElement!==$('chat-input'))list.querySelector('button:not(:disabled)')?.focus();
  }
  async function cycleModel(delta){
    if(busy)return;
    await ready();
    const entries=state.models.filter(item=>item.connected);
    if(!entries.length)return;
    const index=entries.findIndex(item=>item.entry_id===state.model.entry_id);
    await choose(entries[(index+delta+entries.length)%entries.length]);
  }
  mode.addEventListener('click',guard(cycleMode),{signal:abort.signal});
  model.addEventListener('click',guard(async event=>{
    const direction=event.target.closest('.cmc-chev-left')?-1:event.target.closest('.cmc-chev-right')?1:0;
    if(direction){await cycleModel(direction);return;}
    if(!picker.hidden){close();return;}
    await ready();show();
  }),{signal:abort.signal});
  model.addEventListener('keydown',event=>{
    if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();guard(()=>cycleModel(event.key==='ArrowLeft'?-1:1))();}
  },{signal:abort.signal});
  document.addEventListener('click',event=>{if(!picker.contains(event.target)&&!model.contains(event.target))close();},{signal:abort.signal});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!picker.hidden){close();model.focus();}},{signal:abort.signal});
  window.addEventListener('resize',close,{signal:abort.signal});
  return {
    async open(id){generation++;session=id;state=null;inflight=null;active=null;busy=false;close();cancelConfirmation();paint();await load();},
    updateRuns(items){active=items.find(run=>['queued','running','waiting_approval','waiting_gate'].includes(run.status));paint();if(session&&!busy)void load().catch(error=>note(error.message));},
    disconnect(){generation++;session='';state=null;inflight=null;active=null;busy=false;close();cancelConfirmation();paint();},
    destroy(){this.disconnect();abort.abort();picker.remove();}
  };
}
