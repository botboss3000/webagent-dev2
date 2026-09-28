// RECORDER-HOOK: optional browser witnesses owned entirely by plugins/recorder.
// Called by recorder.js only while enabled; stop removes every listener/timer.
// Facts describe observed DOM, never a renderer's claimed success. Recovery
// stores IDs/fingerprints in tab-local sessionStorage, never transcript text.
export function createWitnesses({record, geometry, captureId, requestCheckpoint, persist=true}) {
  const removers=[];
  let last=null, anchor=null, inputAt=-Infinity, navigationAt=-Infinity;
  let pending=null, ready=false, stableAt=0, signature='', disposed=false;
  const key='webagent.recorder.recovery';
  const listen=(target,name,fn,options)=>{
    target.addEventListener(name,fn,options);
    removers.push(()=>target.removeEventListener(name,fn,options));
  };
  const emit=(kind,detail)=>record(kind,{label:'DEBUG-TAG:'+kind,detail});
  const now=()=>performance.now();
  const durable=detail=>(detail?.elements || []).filter(e=>e.stable_id && e.visible &&
    (e.kind==='user' || (e.finalized===true && e.kind!=='activity')))
    .map(({id,text_hash,text_length})=>({id,text_hash,text_length}));
  function boundary(type) {
    requestCheckpoint?.();
    if (!last) return;
    const value={phase:'before',boundary_id:captureId+':'+type+':'+Date.now(),
      session_id:last.active_session_id,elements:durable(last),complete:last.complete,
      run_active:last.run_state==='running',stop_visible:last.stop_visible,
      transient_count:last.transient_count || 0,capture_id:captureId};
    emit('journey.recovery',value);
    return value;
  }
  function resume(value) {
    if (!value || value.capture_id!==captureId) return;
    pending=value;ready=true;stableAt=now();signature='';
    // Re-emitting this small boundary tolerates a lost pagehide upload. IDs
    // bind both observations; the analyzer ignores duplicate before samples.
    emit('journey.recovery',value);
  }
  try {
    const value=persist?JSON.parse(sessionStorage.getItem(key) || 'null'):null;
    if(persist)sessionStorage.removeItem(key);
    if (performance.getEntriesByType('navigation')[0]?.type==='reload') resume(value);
  } catch (_) {}
  listen(window,'pagehide',()=>{
    const value=boundary('reload');
    try { if(persist && value)sessionStorage.setItem(key,JSON.stringify(value)); } catch (_) {}
  });
  const controlFor=event=>(event.composedPath?.()[0] || event.target)?.closest?.(
    'button,input,textarea,select,a[href],[role="button"],[tabindex]');
  function interaction(event,method) {
    const el=controlFor(event);
    if(!el || el.closest('#render-recorder-control,[data-recorder-dialog]')) return;
    if(!el.closest('#chat-panel,#chat-widget,#chat-messages,#chat-footer') &&
       !el.id?.startsWith('chat-') && !el.closest('.chat-bubble')) return;
    const attr=name=>el.getAttribute(name);
    const name=attr('aria-label') || (attr('aria-labelledby') || '').split(/\s+/)
      .map(id=>document.getElementById(id)?.textContent || '').join(' ').trim()
      || [...(el.labels || [])].map(l=>l.textContent).join(' ').trim()
      || (el.matches('button,a,[role="button"]') ? el.textContent?.trim() : '') || attr('title');
    const style=getComputedStyle(el);
    const focused=document.activeElement===el;
    emit('journey.keyboard',{method,control_id:el.id || el.tagName.toLowerCase(),
      accessible_name:!!name,focus_visible:focused && geometry(el).viewport_visible,
      focus_indicator:focused && ((style.outlineStyle!=='none' && parseFloat(style.outlineWidth)>0) ||
        (style.boxShadow && style.boxShadow!=='none')),
      disabled:!!el.disabled || attr('aria-disabled')==='true'});
  }
  listen(window,'pointerdown',e=>{inputAt=now();interaction(e,'pointer');},true);
  listen(window,'wheel',()=>{inputAt=now();},true);
  listen(window,'touchmove',()=>{inputAt=now();},true);
  listen(window,'keydown',e=>{
    if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(e.key)) inputAt=now();
    if(['Tab','Enter',' '].includes(e.key)) {
      // Tab's target is the control being left; observe the newly focused one.
      if(e.key==='Tab') setTimeout(()=>{if(!disposed)interaction({target:document.activeElement},'keyboard');},0);
      else interaction(e,'keyboard');
    }
  },true);
  return {
    checkpoint(detail) {
      const at=now(), prior=anchor;
      const candidate=(detail.elements || []).find(e=>e.viewport_visible && e.stable_id && e.kind==='user')
        || (detail.elements || []).find(e=>e.viewport_visible && e.stable_id);
      const same=prior && (detail.elements || []).find(e=>e.id===prior.id && e.viewport_visible);
      detail.scroll={...detail.scroll,comparable:!!same && prior.session===detail.active_session_id &&
          prior.width===detail.viewport.width && prior.height===detail.viewport.height && prior.visibility!=='hidden' &&
          detail.viewport.visibility!=='hidden',
        anchor_delta_px:same?same.rect.y-prior.y:null,user_scrolled:at-inputAt<1000,
        intentional_navigation:at-navigationAt<1500};
      anchor=candidate?{id:candidate.id,y:candidate.rect.y,session:detail.active_session_id,
        width:detail.viewport.width,height:detail.viewport.height,visibility:detail.viewport.visibility}:null;
      last=detail;
      if(!pending || !ready || pending.session_id!==detail.active_session_id) return;
      const elements=durable(detail), sig=JSON.stringify(elements);
      if(sig!==signature){signature=sig;stableAt=at;}
      if(at-stableAt<1000 || !elements.length || detail.viewport.visibility==='hidden') return;
      emit('journey.recovery',{phase:'after',boundary_id:pending.boundary_id,
        session_id:detail.active_session_id,elements,settled:true,complete:detail.complete,
        stop_visible:detail.stop_visible,run_active:detail.run_state==='running',
        transient_count:detail.transient_count || 0});
      pending=null;
    },
    trace(kind) {
      if(kind==='cache.session.load') {navigationAt=now();anchor=null;}
      if(kind==='connection.closed') {pending=boundary('reconnect');ready=false;}
      if(kind==='connection.opened' && pending) {ready=true;stableAt=now();signature='';}
    },
    stop() {
      disposed=true;removers.forEach(fn=>fn());
      try {if(persist)sessionStorage.removeItem(key);} catch (_) {}
    }
  };
}
