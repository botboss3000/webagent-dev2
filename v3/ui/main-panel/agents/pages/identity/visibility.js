'use strict';
// V2 identity-settings.js Message visibility; V3 revision-fenced transport.
export function renderVisibility(content, agent, persist) {
  const settings = agent.message_visibility;
  if (!settings) return;
  const section = document.createElement('section');
  section.className = 'agent-message-visibility-settings agent-closing-message-settings';
  const list = document.createElement('div'); list.className = 'ac-list agent-closing-message-list'; section.append(list);
  const card = document.createElement('div'); card.className = 'ac-row agent-message-visibility-route';
  const row = document.createElement('div'); row.className = 'ac-ability-row agent-closing-message-row';
  const label = document.createElement('div'); label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Route working messages</div><div class="ac-ability-desc">Enabled by default. The agent chooses internal activity updates or plain standalone messages without pausing the run.</div>';
  const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.className = 'conn-toggle'; toggle.checked = settings.enabled; toggle.disabled = !agent.can_manage; toggle.setAttribute('aria-label','Route working messages');
  const track = document.createElement('span'); track.className = 'conn-toggle-track'; wrap.append(toggle,track);
  const glyph = document.createElement('span'); glyph.className='ac-ability-icon'; glyph.setAttribute('aria-hidden','true');
  glyph.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M7 7h10M7 11h7"/></svg>';
  row.tabIndex=0; row.setAttribute('role','button'); row.setAttribute('aria-expanded','false'); row.setAttribute('aria-label','Route working messages configuration');
  row.append(glyph,label,wrap); card.append(row);
  const body=document.createElement('div'); body.className='ac-ability-body'; body.hidden=true; card.append(body); list.append(card);
  const expand=()=>{body.hidden=!body.hidden;card.classList.toggle('expanded',!body.hidden);row.setAttribute('aria-expanded',String(!body.hidden));};
  row.addEventListener('click',event=>{if(!event.target.closest('.conn-toggle-wrap'))expand();});
  row.addEventListener('keydown',event=>{if(event.target===row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();expand();}});
  const field=(title,rows)=>{const label=document.createElement('label');label.className='agent-closing-message-guidance';const span=document.createElement('span');span.textContent=title;const input=document.createElement('textarea');input.className='ac-input';input.rows=rows;label.append(span,input);body.append(label);return input;};
  const fixed=field('Fixed protocol',5); fixed.readOnly=true; fixed.value=settings.fixed_protocol||'';
  const guidance=field('Additional working-message guidance',2);guidance.value=settings.guidance||'';guidance.disabled=!agent.can_manage;guidance.placeholder='Optional guidance for when this agent should use a standalone working message.';
  const format=field('Message format guidance (every call)',3);format.value=settings.format||'';format.disabled=!agent.can_manage;format.placeholder='Optional format for user-facing messages, including the final reply.';
  const save=document.createElement('button');save.type='button';save.className='ac-btn ac-btn-ghost';save.textContent='Save';save.disabled=!agent.can_manage;body.append(save);
  const status=document.createElement('span');status.setAttribute('role','status');status.setAttribute('aria-live','polite');body.append(status);
  const markDirty=()=>{save.textContent='Save';save.classList.remove('is-saved');status.textContent='';};
  guidance.addEventListener('input',markDirty);format.addEventListener('input',markDirty);
  let enabled=settings.enabled, savedGuidance=settings.guidance||'', savedFormat=settings.format||'';
  async function commit(nextEnabled,nextGuidance,nextFormat){toggle.disabled=true;guidance.disabled=true;format.disabled=true;save.disabled=true;status.textContent='';save.textContent='Saving…';save.classList.remove('is-saved');try{const result=await persist({message_visibility:{enabled:nextEnabled,guidance:nextGuidance,format:nextFormat}});enabled=result.message_visibility.enabled;savedGuidance=result.message_visibility.guidance;savedFormat=result.message_visibility.format;save.textContent='Saved';save.classList.add('is-saved');}catch(error){toggle.checked=enabled;guidance.value=savedGuidance;format.value=savedFormat;save.textContent='Save';status.textContent='Save failed: '+error.message;}finally{toggle.disabled=!agent.can_manage;guidance.disabled=!agent.can_manage;format.disabled=!agent.can_manage;save.disabled=!agent.can_manage;}}
  toggle.addEventListener('change',()=>commit(toggle.checked,savedGuidance,savedFormat));
  save.addEventListener('click',()=>commit(enabled,guidance.value,format.value));
  content.append(section);
}
