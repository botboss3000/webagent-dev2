'use strict';
// V2 identity-settings.js Message visibility; V3 revision-fenced transport.
export function renderVisibility(content, agent, persist) {
  const settings = agent.message_visibility;
  if (!settings) return;
  const section = document.createElement('section');
  section.className = 'agent-message-visibility-settings agent-closing-message-settings';
  section.innerHTML = '<div class="agent-closing-message-settings-copy"><strong>Message visibility</strong><p>Choose whether working-agent messages stay in activity groups or can appear as ordinary assistant bubbles while work continues.</p></div>';
  const list = document.createElement('div'); list.className = 'ac-list agent-closing-message-list'; section.append(list);
  const card = document.createElement('div'); card.className = 'ac-row agent-message-visibility-route';
  const row = document.createElement('div'); row.className = 'ac-ability-row agent-closing-message-row';
  const label = document.createElement('div'); label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Route working messages</div><div class="ac-ability-desc">Enabled by default. The agent chooses internal activity updates or plain standalone messages without pausing the run.</div>';
  const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.className = 'conn-toggle'; toggle.checked = settings.enabled; toggle.disabled = !agent.can_manage; toggle.setAttribute('aria-label','Route working messages');
  const track = document.createElement('span'); track.className = 'conn-toggle-track'; wrap.append(toggle,track);
  const status = document.createElement('span'); status.className = 'agent-closing-message-save-status'; status.setAttribute('role','status');
  const expand = document.createElement('button'); expand.type='button'; expand.className='ac-btn ac-btn-ghost'; expand.textContent='Protocol and guidance'; expand.setAttribute('aria-expanded','false');
  row.append(label,wrap,expand,status); card.append(row);
  const body=document.createElement('div'); body.className='ac-ability-body'; body.hidden=true; card.append(body); list.append(card);
  expand.addEventListener('click',()=>{body.hidden=!body.hidden;card.classList.toggle('expanded',!body.hidden);expand.setAttribute('aria-expanded',String(!body.hidden));});
  const field=(title,rows)=>{const label=document.createElement('label');label.className='agent-closing-message-guidance';const span=document.createElement('span');span.textContent=title;const input=document.createElement('textarea');input.className='ac-input';input.rows=rows;label.append(span,input);body.append(label);return input;};
  const fixed=field('Fixed protocol',5); fixed.readOnly=true; fixed.value=settings.fixed_protocol||'';
  const guidance=field('Additional agent guidance',2);guidance.value=settings.guidance||'';guidance.disabled=!agent.can_manage;guidance.placeholder='Optional guidance for when this agent should use a standalone working message.';
  const save=document.createElement('button');save.type='button';save.className='ac-btn ac-btn-ghost';save.textContent='Save';save.disabled=!agent.can_manage;body.append(save);
  let enabled=settings.enabled, savedGuidance=settings.guidance||'';
  async function commit(nextEnabled,nextGuidance){toggle.disabled=true;guidance.disabled=true;save.disabled=true;status.textContent='Saving…';try{await persist({message_visibility:{enabled:nextEnabled,guidance:nextGuidance}});enabled=nextEnabled;savedGuidance=nextGuidance;status.textContent='Saved';}catch(error){toggle.checked=enabled;guidance.value=savedGuidance;status.textContent='Save failed: '+error.message;}finally{toggle.disabled=!agent.can_manage;guidance.disabled=!agent.can_manage;save.disabled=!agent.can_manage;}}
  toggle.addEventListener('change',()=>commit(toggle.checked,savedGuidance));
  save.addEventListener('click',()=>commit(enabled,guidance.value));
  content.append(section);
}
