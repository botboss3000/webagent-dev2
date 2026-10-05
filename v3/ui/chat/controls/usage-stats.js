// V2 stats-strip and context-panel presentation; Rust supplies every measured value.
export function formatCount(value) {
  if (!Number.isFinite(value)) return '—';
  return value >= 1000000 ? (value / 1000000).toFixed(2) + 'M'
    : value >= 1000 ? (value / 1000).toFixed(1) + 'k' : String(value);
}
export function mountUsageStats({root,client,note}) {
  const $ = id => root.querySelector('#' + id);
  const anchor = $('chat-pill-stats'), abort = new AbortController();
  let session = '', generation = 0, data = null, pending = null, lastLoad = 0, selectedRun = null;
  const panel = document.createElement('div');
  panel.className = 'chat-model-picker v3-context-panel';
  panel.hidden = true; panel.setAttribute('role','dialog'); panel.setAttribute('aria-label','Context statistics');
  document.body.append(panel);
  anchor.tabIndex = 0; anchor.setAttribute('role','button');
  anchor.setAttribute('aria-label','Context statistics'); anchor.setAttribute('aria-expanded','false');
  function close() { panel.hidden = true; selectedRun = null; anchor.setAttribute('aria-expanded','false'); }
  const num = value => Number.isFinite(value) ? value.toLocaleString() : 'Not reported';
  function row(parent,label,value) {
    const node=document.createElement('div'); node.className='cmp-cc-top';
    const name=document.createElement('span'), val=document.createElement('span');
    name.textContent=label; val.textContent=value; node.append(name,val); parent.append(node);
  }
  function paintPanel() {
    panel.replaceChildren();
    const content=document.createElement('div'); content.className='cmp-compact';
    const title=document.createElement('div'); title.className='cmp-cx-title'; title.textContent="What's in this context"; content.append(title);
    const run=selectedRun||data?.latest, ctx=run?.context_summary, usage=data?.usage;
    if(selectedRun)row(content,'Run',selectedRun.id);
    row(content,'Model',run?.model?.model || 'No model call yet');
    row(content,'Last dispatched context',ctx ? num(ctx.envelope_bytes)+' bytes / '+num(ctx.max_bytes)+' byte budget' : 'Not measured yet');
    if(ctx) {
      const bar=document.createElement('div'); bar.className='cmp-cx-bar';
      for(const [label,field,kind] of [['Instructions','instruction_json_bytes','prompt'],['Tools','schemas_json_bytes','runtime'],['History','history_json_bytes','compactable']]) {
        const segment=document.createElement('span'); segment.className='cmp-cx-seg cmp-cx-'+kind;
        segment.style.width=(Math.min(100,ctx[field]/Math.max(1,ctx.envelope_bytes)*100))+'%';
        segment.title=label+': '+num(ctx[field])+' bytes'; bar.append(segment);
        row(content,label,num(ctx[field])+' bytes');
      }
      content.insertBefore(bar,title.nextSibling);
      row(content,'Call / turn',num(ctx.call)+' / '+num(ctx.turn));
    }
    const caption=document.createElement('p'); caption.className='cmp-cx-hint';
    caption.textContent='Context composition is measured serialized JSON bytes at dispatch. Provider token usage is reported separately; this is not a tokenizer estimate. Totals cover recorded calls; usage from older, unrecorded calls cannot be recovered.';
    content.append(caption);
    row(content,'Recorded session input / output tokens',usage?.reported_calls ? num(usage.input_tokens)+' / '+num(usage.output_tokens) : 'Not reported yet');
    row(content,'Cached input tokens',usage?.reported_calls && !usage.unknown_cache_calls ? num(usage.cached_input_tokens) : 'Not fully reported');
    row(content,'Last call input / output',run?.usage?.latest ? num(run.usage.latest.input_tokens)+' / '+num(run.usage.latest.output_tokens) : 'Not reported');
    row(content,'Reported / unreported calls',num(usage?.reported_calls)+' / '+num(usage?.unreported_calls));
    const known=usage?.known_cost_usd;
    row(content,'Session cost',usage && !usage.unknown_cost_calls ? '$'+Number(known||0).toFixed(4) : 'Not fully reported');
    row(content,'Compactions this run',num(run?.compactions));
    const head=document.createElement('div'); head.className='cmp-cc-head'; head.textContent='Context compaction'; content.append(head);
    for(const [label,key] of [['Target history','trigger_tokens'],['Keep verbatim','tail_tokens']]) {
      row(content,label,Number.isFinite(run?.context_controls?.[key]) ? num(run.context_controls[key])+' estimated tokens' : 'Not measured yet');
      const slider=document.createElement('input'); slider.type='range';slider.disabled=true;
      slider.className='cmp-cc-slider v3-backend-pending';slider.setAttribute('aria-label',label+' — settings backend port pending');
      slider.min='0';slider.max=String(Math.max(1000,run?.context_controls?.[key]||100000));slider.value=String(run?.context_controls?.[key]||0);
      slider.title='Per-session compaction settings backend port pending';content.append(slider);
    }
    const compact=document.createElement('button');compact.type='button';compact.className='cmp-cc-btn v3-backend-pending';compact.disabled=true;
    compact.innerHTML='<i data-lucide="minimize-2"></i><span>Compact now</span>';compact.title='Composer compaction action backend port pending';content.append(compact);
    panel.append(content);window.lucide?.createIcons();
  }
  function paint() {
    const totals=data?.usage, ctx=data?.latest?.context_summary;
    $('chat-tokens-in').textContent=totals?.reported_calls ? formatCount(totals.input_tokens) : '—';
    $('chat-tokens-out').textContent=totals?.reported_calls ? formatCount(totals.output_tokens) : '—';
    $('chat-token-bar').title=totals?.reported_calls ? 'Recorded provider-reported session tokens; older unrecorded calls are unavailable. '+(totals.unreported_calls||0)+' calls did not report usage.' : 'Provider token usage has not been reported';
    const context=$('chat-model-ctx');context.textContent=ctx ? 'ctx '+formatCount(ctx.envelope_bytes)+'B / '+formatCount(ctx.max_bytes)+'B' : 'ctx —';
    context.title='Last dispatched context in measured JSON bytes — click for breakdown';
    const cost=$('chat-cost');cost.textContent=totals && !totals.unknown_cost_calls ? '$'+Number(totals.known_cost_usd||0).toFixed(4) : 'cost —';
    cost.title='Provider-reported cost; unavailable prices are not invented';
    cost.classList.toggle('v3-backend-pending',!totals || !!totals.unknown_cost_calls);
    for(const node of [context,cost]) {
      const cfg=anchor._statsConfig;
      const visible=!cfg || cfg.some(item=>item.el===node);
      node.hidden=!visible;node.style.display=visible?'':'none';
    }
    if(!panel.hidden)paintPanel();
  }
  async function refresh(force=false) {
    if(!session || pending || (!force && Date.now()-lastLoad<1500))return pending;
    const sid=session,epoch=generation;
    const request=client.usage(sid).then(value=>{
      if(sid!==session || epoch!==generation)return;
      data=value;lastLoad=Date.now();paint();
    }).catch(error=>{if(sid===session&&epoch===generation)note('Usage unavailable: '+error.message);})
      .finally(()=>{if(pending===request)pending=null;});
    pending=request;return request;
  }
  function toggle(run=null) {
    if(!panel.hidden){close();return;}
    selectedRun=run;
    panel.hidden=false;anchor.setAttribute('aria-expanded','true');paintPanel();
    const measured=anchor.getBoundingClientRect();
    const rect=measured.bottom>measured.top?measured:root.querySelector('#chat-input-row')?.getBoundingClientRect()||{top:innerHeight-16,left:8};
    const bottom=Math.max(8,Math.min(innerHeight-8-panel.getBoundingClientRect().height,innerHeight-rect.top+6));
    panel.style.bottom=bottom+'px';panel.style.left=Math.max(8,Math.min(rect.left,innerWidth-360))+'px';
    void refresh(true);
  }
  anchor.addEventListener('click',event=>{if(!event.target.closest('.chat-stats-chev'))toggle();},{signal:abort.signal});
  anchor.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();toggle();}},{signal:abort.signal});
  for(const direction of ['left','right']) {
    anchor.querySelector('.chat-stats-chev.'+direction)?.addEventListener('click',event=>{
      event.stopPropagation();$('chat-pill-stats-strip').scrollBy({left:direction==='left'?-160:160,behavior:'smooth'});
    },{signal:abort.signal});
  }
  document.addEventListener('click',event=>{if(!panel.contains(event.target)&&!anchor.contains(event.target))close();},{signal:abort.signal});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')close();},{signal:abort.signal});
  window.addEventListener('resize',close,{signal:abort.signal});
  paint();
  return {
    open(id){generation++;session=id;data=null;pending=null;lastLoad=0;close();paint();void refresh(true);},
    refresh,
    show(run=null){close();toggle(run);},
    disconnect(){this.open('');},
    destroy(){this.disconnect();abort.abort();panel.remove();}
  };
}
