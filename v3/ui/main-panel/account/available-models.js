'use strict';
// Account selections and aliases are independent of explicit agent appointments.
export function mountAvailableModels(host, request) {
  let disposed=false, generation=0, busy=false;
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
  async function load(){
    const current=++generation;
    host.replaceChildren(node('p','Loading available models…','account-status'));
    try {
      const data=await request('/models/me/catalog');
      if(disposed||current!==generation)return;
      const title=node('h2','Available models','account-section-title');
      const hint=node('p','Account choices do not assign agent roles. Add catalog or BYO models from an authorized agent’s Models page.','account-pref-desc');
      const status=node('p','','account-status');status.setAttribute('role','status');
      const form=node('form');const selected=new Set(data.selected_platform_ids||[]);
      const aliases={...data.aliases};const rows=[...(data.available_platform_models||[]),...(data.entries||[])];
      const defaults=node('select');defaults.setAttribute('aria-label','Default for new agents');
      const none=node('option','No default');none.value='';defaults.append(none);
      for(const choice of rows){
        const row=node('div',null,'account-field');
        const label=node('label',choice.display_name+' · '+choice.origin);
        if(choice.origin==='platform'){
          const check=node('input');check.type='checkbox';check.checked=selected.has(choice.id);
          check.setAttribute('aria-label','Available '+choice.display_name);
          check.onchange=()=>{if(check.checked)selected.add(choice.id);else selected.delete(choice.id);};label.prepend(check);
        }
        const alias=node('input');alias.maxLength=200;alias.value=aliases[choice.id]||'';
        alias.placeholder='Personal alias';alias.setAttribute('aria-label','Alias for '+choice.display_name);
        alias.oninput=()=>{if(alias.value.trim())aliases[choice.id]=alias.value.trim();else delete aliases[choice.id];};
        row.append(label,alias);form.append(row);
        const option=node('option',aliases[choice.id]||choice.display_name);option.value=choice.id;defaults.append(option);
      }
      defaults.value=data.default_entry_id||'';
      const label=node('label','Default for new agents');label.append(defaults);form.append(label);
      if(!rows.length)form.append(node('p','No entitled models are available yet.','account-status'));
      const save=node('button','Save available models','account-btn account-btn-primary');save.type='submit';form.append(save);
      form.onsubmit=async event=>{
        event.preventDefault();if(busy||disposed)return;
        busy=true;save.disabled=true;status.textContent='Saving…';
        try{
          if(defaults.value.startsWith('platform:')&&!selected.has(defaults.value))throw Error('Select the platform default as available first.');
          await request('/models/me/catalog','PUT',{revision:data.revision,selected_platform_ids:[...selected],aliases,default_entry_id:defaults.value||null});
          if(!disposed){await load();host.querySelector('[role=status]').textContent='Available models saved';}
        }catch(error){if(!disposed){status.textContent=error.message;save.disabled=false;}}
        finally{busy=false;}
      };
      host.replaceChildren(title,hint,form,status);
    }catch(error){if(!disposed&&current===generation)host.replaceChildren(node('p',error.message,'account-status'));}
  }
  load();
  return()=>{disposed=true;generation++;host.replaceChildren();};
}
