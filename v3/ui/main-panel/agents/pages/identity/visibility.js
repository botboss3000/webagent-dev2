'use strict';
// Working-message publication belongs to one agent-wide mode; chat-mode prompts
// continue to provide call-scoped message formatting.
export function renderVisibility(content, agent, persist) {
  const settings = agent.message_visibility;
  if (!settings) return;
  const section = document.createElement('section');
  section.className = 'agent-message-visibility-settings agent-closing-message-settings';
  const list = document.createElement('div'); list.className = 'ac-list agent-closing-message-list'; section.append(list);
  const card = document.createElement('div'); card.className = 'ac-row agent-message-visibility-route';
  const row = document.createElement('div'); row.className = 'ac-ability-row agent-closing-message-row';
  const label = document.createElement('div'); label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Working-message promotion</div><div class="ac-ability-desc">Choose how nonfinal assistant messages become standalone chat bubbles.</div>';
  const modeWrap = document.createElement('label'); modeWrap.className = 'agent-promotion-mode-wrap';
  const mode = document.createElement('select'); mode.className = 'ac-input agent-promotion-mode';
  mode.setAttribute('aria-label','Working-message promotion');
  for (const [value,title] of [['none','No promotion'],['manual','Selected through tool'],['auto','Auto promote all']]) {
    const option=document.createElement('option'); option.value=value; option.textContent=title; mode.append(option);
  }
  mode.value=settings.mode; mode.disabled=!agent.can_manage;
  const modeStatus=document.createElement('span'); modeStatus.className='agent-promotion-mode-status';
  modeStatus.setAttribute('role','status'); modeStatus.setAttribute('aria-live','polite'); modeWrap.append(mode,modeStatus);
  const glyph = document.createElement('span'); glyph.className='ac-ability-icon'; glyph.setAttribute('aria-hidden','true');
  glyph.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1-2-2h14a2 2 0 0 1 2 2z"/><path d="M7 7h10M7 11h7"/></svg>';
  row.tabIndex=0; row.setAttribute('role','button'); row.setAttribute('aria-expanded','false'); row.setAttribute('aria-label','Working-message promotion configuration');
  row.append(glyph,label,modeWrap); card.append(row);
  const body=document.createElement('div'); body.className='ac-ability-body'; body.hidden=true; card.append(body); list.append(card);
  const expand=()=>{body.hidden=!body.hidden;card.classList.toggle('expanded',!body.hidden);row.setAttribute('aria-expanded',String(!body.hidden));};
  row.addEventListener('click',event=>{if(!event.target.closest('.agent-promotion-mode-wrap'))expand();});
  row.addEventListener('keydown',event=>{if(event.target===row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();expand();}});
  const note=document.createElement('p'); note.className='ac-hint'; body.append(note);
  const fields=document.createElement('div'); body.append(fields);
  const field=(title,rows)=>{const label=document.createElement('label');label.className='agent-closing-message-guidance';const span=document.createElement('span');span.textContent=title;const input=document.createElement('textarea');input.className='ac-input';input.rows=rows;label.append(span,input);fields.append(label);return input;};
  const fixed=field('Fixed promotion protocol',5); fixed.readOnly=true; fixed.value=settings.fixed_protocol||'';
  const guidance=field('Additional promotion guidance',2); guidance.value=settings.guidance||''; guidance.disabled=!agent.can_manage; guidance.placeholder='Optional guidance for selecting a standalone working message.';
  const save=document.createElement('button');save.type='button';save.className='ac-btn ac-btn-ghost';save.textContent='Save guidance';save.disabled=!agent.can_manage;fields.append(save);
  const status=document.createElement('span');status.setAttribute('role','status');status.setAttribute('aria-live','polite');fields.append(status);
  const applyMode=()=>{fields.hidden=mode.value!=='manual';note.textContent=mode.value==='auto'
    ? 'Every nonfinal assistant message is promoted automatically. Promotion protocol and guidance are inactive.'
    : mode.value==='none' ? 'Nonfinal assistant messages stay in private activity. Promotion protocol and guidance are inactive.'
      : 'The agent chooses standalone working messages with the promotion tool.';};
  applyMode();
  guidance.addEventListener('input',()=>{save.textContent='Save guidance';save.classList.remove('is-saved');status.textContent='';});
  let savedMode=settings.mode, savedGuidance=settings.guidance||'';
  async function commit(nextMode,nextGuidance) {
    mode.disabled=true;guidance.disabled=true;save.disabled=true;status.textContent='';modeStatus.textContent='Saving…';save.textContent='Saving…';save.classList.remove('is-saved');
    try {
      const result=await persist({message_visibility:{mode:nextMode,guidance:nextGuidance}});
      savedMode=result.message_visibility.mode;savedGuidance=result.message_visibility.guidance;
      modeStatus.textContent='Saved';
      mode.value=savedMode;guidance.value=savedGuidance;applyMode();save.textContent='Saved';save.classList.add('is-saved');
    } catch(error) {
      mode.value=savedMode;guidance.value=savedGuidance;applyMode();save.textContent='Save guidance';modeStatus.textContent='Save failed';status.textContent='Save failed: '+error.message;
    } finally {
      mode.disabled=!agent.can_manage;guidance.disabled=!agent.can_manage;save.disabled=!agent.can_manage;
    }
  }
  mode.addEventListener('change',()=>{applyMode();commit(mode.value,guidance.value);});
  save.addEventListener('click',()=>commit(mode.value,guidance.value));
  content.append(section);
}
