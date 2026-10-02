"use strict";
// Adapted from V2 users/users.js: members roster sections, table and access radios.
// Billing and browser-cache controls deliberately excluded; all requests are V3.
const el=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
async function request(path,method='GET',body){
  const auth=await fetch('/api/v3/auth/me',{credentials:'same-origin',cache:'no-store'});
  if(!auth.ok)throw Error('Sign in required');const me=await auth.json();
  if(!me.is_admin)throw Error('Administrator access required');
  const r=await fetch('/api/v3'+path,{method,credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'x-csrf-token':me.csrf_token})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await r.json().catch(()=>({}));if(!r.ok)throw Error(data.error||data.message||'Request failed ('+r.status+')');return data;
}
function button(text,action){const b=el('button','members-btn',text);b.type='button';b.onclick=action;return b;}
export async function mountUsers(host){
  const status=el('p','members-loading','Loading users…');status.setAttribute('role','status');host.replaceChildren(status);
  try{
    const [data,tiers,me]=await Promise.all([request('/admin/access/accounts'),request('/admin/access/tiers'),fetch('/api/v3/auth/me').then(r=>r.json())]);
    host.replaceChildren();host.append(button('Refresh',()=>mountUsers(host)),status);status.textContent='';
    for(const [title,filter] of [['Admins',a=>a.role==='administrator'],['Users',a=>a.role==='user'],['Anonymous',a=>a.role==='guest']]){
      const section=el('section','members-section');section.append(el('div','members-policy-title',title));
      const rows=data.accounts.filter(filter);if(!rows.length){section.append(el('p','members-empty','No '+title.toLowerCase()+' yet.'));host.append(section);continue;}
      const wrap=el('div','members-table-wrap'),table=el('table','members-table');
      const head=el('thead'),tr=el('tr');for(const label of ['User','Experience','Sessions','Messages','Last login','Status','Actions'])tr.append(el('th','',label));head.append(tr);table.append(head);const tbody=el('tbody');table.append(tbody);
      for(const account of rows){
        const row=el('tr'),name=el('td');name.append(el('div','members-user-name',account.profile?.display_name||account.username),el('div','members-user-sub',account.username));row.append(name);
        const tierCell=el('td'),select=el('select','ac-input ac-config-sel');select.setAttribute('aria-label','Tier for '+account.username);
        for(const tier of tiers.tiers){const option=el('option','',tier.id);option.value=tier.id;select.append(option);}select.value=account.tier_id;select.disabled=account.user_id==='admin';tierCell.append(select);row.append(tierCell);
        row.append(el('td','members-num',String(account.session_count??'—')),el('td','members-num',String(account.message_count??'—')),el('td','',account.profile?.last_login_at?new Date(account.profile.last_login_at*1000).toLocaleString():'—'),el('td','members-status',account.status));
        const actions=el('td','members-actions');row.append(actions);
        async function save(patch,control){control.disabled=true;status.textContent='Saving…';try{await request('/admin/access/accounts/'+encodeURIComponent(account.user_id),'PUT',{expected_revision:account.access_revision,status:account.status,role:account.role,tier_id:account.tier_id,...patch});await mountUsers(host);}catch(e){status.textContent=e.message;control.disabled=false;select.value=account.tier_id;}}
        select.onchange=()=>save({tier_id:select.value},select);
        if(account.user_id!==me.user_id&&account.user_id!=='admin'){
          const access=button(account.status==='active'?'Restrict':'Authorize',()=>save({status:account.status==='active'?'suspended':'active'},access));actions.append(access);
          if(account.role!=='guest'){const role=button(account.role==='administrator'?'Remove admin':'Make admin',()=>save({role:account.role==='administrator'?'user':'administrator'},role));actions.append(role);}
        }
        const manage=button('Profile & sign-ins',()=>openAccountManager(account,host,status));actions.append(manage);
        tbody.append(row);
      }
      wrap.append(table);section.append(wrap);host.append(section);
    }
  }catch(e){status.textContent='Users unavailable: '+e.message;}
}
export async function mountPolicies(host){
  const status=el('p','members-loading','Loading policies…');status.setAttribute('role','status');host.replaceChildren(status);
  try{
    const [policy,tiers]=await Promise.all([request('/admin/access/registration'),request('/admin/access/tiers')]);host.replaceChildren(status);status.textContent='';
    const access=el('section','members-policy');access.append(el('div','members-policy-title','Access policy'));const choices=el('div','members-policy-choices');access.append(choices);host.append(access);
    for(const [value,label,hint] of [['admin_approval','Private','New users can register, but an administrator must approve access.'],['public_registered','Open Registration','Anyone may create an account and sign in.']]){
      const opt=el('label','members-policy-opt'+(policy.access_mode===value?' active':'')),input=el('input');input.type='radio';input.name='v3-registration-policy';input.value=value;input.checked=policy.access_mode===value;
      const body=el('div','members-policy-opt-body');body.append(el('div','members-policy-opt-label',label),el('div','members-policy-opt-hint',hint));opt.append(input,body);choices.append(opt);
      input.onchange=async()=>{for(const n of choices.querySelectorAll('input'))n.disabled=true;try{await request('/admin/access/registration','PUT',{access_mode:value,expected_revision:policy.revision});await mountPolicies(host);}catch(e){status.textContent=e.message;for(const n of choices.querySelectorAll('input')){n.disabled=false;n.checked=n.value===policy.access_mode;}}};
    }
    const section=el('section','members-policy');section.append(el('div','members-policy-title','Experience tiers'));host.append(section);
    for(const tier of tiers.tiers){const details=el('details','ac-row'),summary=el('summary','ac-ability-row',tier.id),form=el('form','ac-ability-body');details.append(summary,form);section.append(details);
      const fields={};for(const [key,label,value] of [['allowed_models','Allowed models',tier.allowed_models.join(', ')],['tools','Tool permissions (JSON)',JSON.stringify(tier.tools)],['max_sessions','Maximum sessions',tier.max_sessions],['max_active_runs','Maximum active runs',tier.max_active_runs]]){const lab=el('label','',label),input=el('input','ac-input');input.value=String(value);input.required=true;if(key.startsWith('max_')){input.type='number';input.min='0';input.max=key==='max_sessions'?'10000':'200';}input.disabled=tier.id==='operator';lab.append(input);form.append(lab);fields[key]=input;}
      if(tier.id!=='operator'){const save=el('button','members-btn','Save tier');save.type='submit';form.append(save);form.onsubmit=async ev=>{ev.preventDefault();save.disabled=true;try{const next={...tier,allowed_models:fields.allowed_models.value.split(',').map(s=>s.trim()).filter(Boolean),tools:JSON.parse(fields.tools.value),max_sessions:Number(fields.max_sessions.value),max_active_runs:Number(fields.max_active_runs.value)};await request('/admin/access/tiers/'+encodeURIComponent(tier.id),'PUT',{expected_revision:tier.revision,tier:next});await mountPolicies(host);}catch(e){status.textContent=e.message;save.disabled=false;}};}
    }
  }catch(e){status.textContent='Policies unavailable: '+e.message;}
}

// V2 users/policies/page-access.js: audience rows separated into page scopes.
export async function mountPageAccess(host){
  const status=el('p','members-loading','Loading page audiences…');status.setAttribute('role','status');host.replaceChildren(status);
  try{
    const [policy,catalog,app]=await Promise.all([request('/admin/access/page-policy'),fetch('/api/v3/pages/catalog').then(r=>r.json()),fetch(new URL('../catalog.json',import.meta.url)).then(r=>r.json())]);
    host.replaceChildren(status);status.textContent='';
    for(const [scope,title,pages] of [['main','Main panel pages',catalog.main],['app','App pages',app.pages]]){
      const section=el('section','members-policy');section.append(el('div','members-policy-title',title));const list=el('div','ac-list');section.append(list);host.append(section);
      for(const page of pages){const row=el('label','ac-ability-row'),name=el('span','ac-ability-name',page.label||page.title),select=el('select','ac-input ac-config-sel');select.setAttribute('aria-label','Audience for '+(page.label||page.title));
        for(const [value,text] of [['everyone','Everyone'],['registered','Registered users'],['admin','Admins only']]){const option=el('option','',text);option.value=value;select.append(option);}
        const locked=scope==='app'&&['users','user-policies','page-access'].includes(page.id);select.value=locked?'admin':policy.pages?.[scope]?.[page.id]||'everyone';select.disabled=locked;row.append(name,select);list.append(row);
        select.onchange=async()=>{const previous=policy.pages?.[scope]?.[page.id]||'everyone';select.disabled=true;status.textContent='Saving…';const next=structuredClone(policy.pages);next[scope]??={};next[scope][page.id]=select.value;try{await request('/admin/access/page-policy','PUT',{expected_revision:policy.revision,pages:next});await mountPageAccess(host);}catch(error){status.textContent=error.message;select.value=previous;select.disabled=false;}};
      }
    }
  }catch(error){status.textContent='Page audiences unavailable: '+error.message;}
}

async function openAccountManager(account,host,status){
  host.querySelector('[data-account-manager]')?.remove();
  const panel=el('section','members-policy');panel.dataset.accountManager=account.user_id;panel.append(el('div','members-policy-title','Manage '+account.username));
  const form=el('form','v3-user-form'),username=el('input','ac-input'),display=el('input','ac-input');username.value=account.username;display.value=account.profile?.display_name||account.username;
  for(const [title,input] of [['Username',username],['Display name',display]]){const label=el('label','',title);input.required=true;input.maxLength=128;label.append(input);form.append(label);}
  const save=el('button','members-btn','Save profile');save.type='submit';form.append(save);panel.append(form,button('Close',()=>panel.remove()));host.append(panel);
  form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{await request('/admin/access/accounts/'+encodeURIComponent(account.user_id)+'/profile','PUT',{username:username.value,display_name:display.value});await mountUsers(host);}catch(error){status.textContent=error.message;save.disabled=false;}};
  const sessions=el('div','members-loading','Loading sign-ins…');panel.append(sessions);
  try{const data=await request('/admin/access/accounts/'+encodeURIComponent(account.user_id)+'/sessions');sessions.replaceChildren();if(!data.sessions.length)sessions.textContent='No active sign-ins.';
    for(const session of data.sessions){const row=el('div','ac-ability-row');row.append(el('span','ac-ability-name',session.user_agent||'Browser session'));const revoke=button('Revoke',async()=>{revoke.disabled=true;try{await request('/admin/access/accounts/'+encodeURIComponent(account.user_id)+'/sessions/'+encodeURIComponent(session.id),'DELETE');row.remove();}catch(error){status.textContent=error.message;revoke.disabled=false;}});row.append(revoke);sessions.append(row);}
  }catch(error){sessions.textContent=error.message;}
  username.focus();
}
