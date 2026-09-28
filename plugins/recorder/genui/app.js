/* ═══════════════════════════════════════════════════════════════════════════
 * GENUI STATE PERSISTENCE — STANDARD SNIPPET (v1)
 * ===========================================================================
 * Paste this block into any genui page's <script> to persist its UI state to
 * localStorage so a reopen restores exactly where the user left off.
 *
 * WHY TWO LAYERS?
 *   The genui HOST (ui/main-panel/genui/js/genui.js) already persists the
 *   generic stuff automatically — page scroll (#genui-host scrollTop), inner
 *   panel scrolls, <details> open state, checkbox/radio/select values,
 *   aria-expanded, and the last page slug. Pages never need to re-implement
 *   those.
 *   This block covers the SEMANTIC state only the page knows: which record is
 *   open (project/tab/card), which sub-item is expanded, the active search
 *   query, etc. Save it whenever that state changes; restore it BEFORE the
 *   first render.
 *
 * CONVENTION
 *   Key:    'genui:<slug>:state'     (one JSON blob per page)
 *   Shape:  { field: value, ... }    null/undefined fields are dropped on save
 *   Scroll: optional 'scroll' / 'scrollLeft' fields (see restoreScroll).
 *
 * HOW TO WIRE A PAGE (checklist)
 *   1. const SLUG = '<page-slug>';   // e.g. 'home'
 *   2. Define a savePageState() that gathers the page's semantic state and
 *      calls GenUIState.save(SLUG, {...}). Call it on EVERY state change
 *      (activate/select/expand/collapse/close/search), not just on unload.
 *   3. In boot, BEFORE first render: const saved = loadPageState() — apply
 *      saved.activeId / saved.openItem / etc. into the page's STATE object.
 *   4. Render, then re-apply any "open item" expansion (the element must
 *      exist by then — do it after the render call, not before).
 *   5. Restore scroll: GenUIState.restoreScroll(saved.scroll, saved.scrollLeft)
 *      once real content is on screen (it retries, so calling once at boot is
 *      enough). The host restores scroll too — this is belt-and-braces so the
 *      page works even when mounted outside the app host.
 *   6. Optional: listen for scroll on document with capture and debounce
 *      savePageState() so the 'scroll' field stays fresh.
 *
 * Reference implementation: data/user_data/admin/genui/home/index.html
 * Full spec: docs/claude/genui-state-standard.md
 * ═══════════════════════════════════════════════════════════════════════════ */
var GenUIState=(function(){
  function key(slug){return'genui:'+slug+':state'}
  function load(slug){try{var o=JSON.parse(localStorage.getItem(key(slug))||'null');return o&&typeof o==='object'?o:{}}catch(e){return{}}}
  function save(slug,state){try{var o={};for(var k in state){var v=state[k];if(v!==null&&v!==undefined)o[k]=v}if(Object.keys(o).length)localStorage.setItem(key(slug),JSON.stringify(o));else localStorage.removeItem(key(slug))}catch(e){}}
  function clear(slug){try{localStorage.removeItem(key(slug))}catch(e){}}
  /* Scroll lives on the app's #genui-host (genui mounts in a shadow root).
     The host persists it itself; these helpers let the page persist it too,
     and fall back to window scroll when the page runs outside the host. */
  function hostEl(){try{return document.getElementById('genui-host')}catch(e){return null}}
  function scrollY(){var h=hostEl();try{return h?(h.scrollTop||0):(window.pageYOffset||document.documentElement.scrollTop||0)}catch(e){return 0}}
  function scrollX(){var h=hostEl();try{return h?(h.scrollLeft||0):(window.pageXOffset||document.documentElement.scrollLeft||0)}catch(e){return 0}}
  function restoreScroll(y,sl){if(!y&&!sl)return;function go(){try{var h=hostEl();if(h){if(sl&&Math.abs(h.scrollLeft-sl)>2)h.scrollLeft=sl;if(y&&Math.abs(h.scrollTop-y)>2)h.scrollTop=y}else{window.scrollTo(sl||0,y)}}catch(e){}}go();[80,200,400,800].forEach(function(d){setTimeout(go,d)})}
  return{key:key,load:load,save:save,clear:clear,scrollY:scrollY,scrollX:scrollX,restoreScroll:restoreScroll};
})();

 'use strict';
/* Journey Reports — authenticated recorder evidence and human plan review.
 * Loaded with index.html/styles.css by plugins/recorder's GenUI provider.
 * API: /api/v1/recordings/captures; see plugins/recorder/README.md.
 * GenUILive-pattern adapter deliberately reads authoritative recorder storage,
 * never the editable GenUI data bag. No transcript/history polling is added.
 * KEEP: all captured strings use textContent; artifacts use authenticated blobs.
 * REMOVE-WHEN: plugins/recorder is removed; this page has no core dependencies. */
window.WebagentGenui.register(function(root, api) {
  const $ = id => root.querySelector('#' + id);
  const main = $('journey-reports');
  const base = '/api/v1/recordings/captures';
  const user = localStorage.getItem('auth_user_id') || 'anonymous';
  const stateKey = 'journey-reports:' + user;
  const saved = GenUIState.load(stateKey);
  const state = {selected:saved.selected || '', query:saved.query || '', filter:saved.filter || 'all', captures:[], detail:null, events:[], cursor:0, more:false};
  const life = new AbortController();
  const blobs = new Map();
  const drafts = new Map(Object.entries(saved.reviewDrafts || {}));
  const agentChoices = new Map(Object.entries(saved.executionAgents || {}));
  let executionAgents=[], agentsLoaded=false, agentsError="";
  let disposed=false, timer=0, busy=false, again=false, lastDetail='', lastList='', actionBusy=false;
  const active = value => ['starting','recording','finalizing','queued','running','investigating','executing'].includes(value);
  function save(){GenUIState.save(stateKey,{selected:state.selected,query:state.query,filter:state.filter,reviewDrafts:Object.fromEntries(drafts),executionAgents:Object.fromEntries(agentChoices)});}
  function node(tag, text, cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);if(cls)e.className=cls;return e;}
  function text(value){return typeof value==='string'?value:JSON.stringify(value,null,2) || 'Not captured';}
  function stable(value){return JSON.stringify(value,(_k,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);}
  function time(value){if(!value)return 'Time unavailable';const d=new Date(typeof value==='number'&&value<1e12?value*1000:value);return isNaN(d)?String(value):d.toLocaleString();}
  function badge(value){const e=node('span',String(value||'unknown').replaceAll('_',' '),'jr-badge');e.dataset.state=value;return e;}
  function section(title,id){const s=node('section',undefined,'jr-section');if(id)s.id=id;s.append(node('h3',title));return s;}
  function button(label,fn,primary=false){const b=node('button',label,primary?'jr-primary':'');b.type='button';b.addEventListener('click',fn,{signal:life.signal});return b;}
  function details(label,body,id){const d=node('details');if(id)d.dataset.key=id;d.append(node('summary',label),body);return d;}
  function description(pairs){const dl=node('dl');for(const [key,value] of pairs){dl.append(node('dt',key),node('dd',text(value)));}return dl;}
  async function request(path='',options={}){
    const token=localStorage.getItem('auth_token');
    const headers={...(options.body?{'Content-Type':'application/json'}:{}),...(token?{Authorization:'Bearer '+token}:{})};
    const response=await fetch(path.startsWith('/api/')?path:base+path,{...options,headers,signal:life.signal,cache:'no-store'});
    if(!response.ok){let message='Request failed ('+response.status+')';try{const body=await response.json();message=typeof body.detail==='string'?body.detail:message;}catch(_){}throw new Error(message);}
    return options.blob?response.blob():response.json();
  }
  function notice(message){if(!disposed)$('jr-notice').textContent=message;}
  function captures(){
    const visible=state.captures.filter(c=>(state.filter!=='active'||active(c.state))&&(state.filter!=='issues'||Number(c.report_summary?.fail||c.report_summary?.failed||0)>0)&&text([c.id,c.started_at,c.sessions]).toLowerCase().includes(state.query.toLowerCase()));
    const key=stable([visible,state.selected]);if(key===lastList)return;lastList=key;
    const out=document.createDocumentFragment();
    for(const c of visible){const b=button('',()=>select(c.id));b.className='jr-capture';b.setAttribute('aria-pressed',String(c.id===state.selected));b.append(node('strong',c.title||time(c.started_at)),badge(c.state),node('small',c.id));const summary=c.report_summary||{};if(Object.keys(summary).length)b.append(node('small',(summary.fail||summary.failed||0)+' findings · '+(summary.pass||summary.passed||0)+' passed'));out.append(b);}
    if(!visible.length)out.append(node('p',state.captures.length?'No matching recordings.':'No recordings yet. Start one using the recorder in chat.','jr-muted'));
    $('jr-captures').replaceChildren(out);
  }
  async function select(id){if(state.selected===id)return;state.selected=id;state.detail=null;state.events=[];state.cursor=0;state.more=false;lastDetail='';save();captures();$('jr-detail').replaceChildren(node('p','Loading report…','jr-muted'));wake();}
  function renderReport(){
    if(!state.detail)return;const signature=stable(state.detail);if(signature===lastDetail)return;lastDetail=signature;
    const host=$('jr-detail');const opened=new Set(Array.from(host.querySelectorAll('details[open][data-key]')).map(d=>d.dataset.key));
    const focus=root.activeElement;const focused=focus?.id;const selection=focus?.selectionStart;
    const {capture={},report={},markers=[]}=state.detail;const out=document.createDocumentFragment();
    out.append(node('h2',capture.title||'Recorded chat journey'));const row=node('div',undefined,'jr-row');row.append(badge(capture.state));if(report.provisional||active(capture.state))row.append(badge('provisional'));out.append(row,node('p',time(capture.started_at)+' · '+capture.id,'jr-muted'));
    const gaps=[...(capture.gaps||[]),...(report.gaps||[])];
    if(gaps.length){const box=node('div',undefined,'jr-callout');box.append(node('strong','Evidence is incomplete'));for(const g of gaps)box.append(node('p',text(g)));out.append(box);}
    // RECORDER-HOOK: coverage describes evidence actually retained per channel.
    // Missing worker/browser metadata is unknown, never an implied five seconds.
    const prebuffer=report.prebuffer || (capture.prebuffer || capture.prebuffer_browser ? {
      requested_ms:capture.prebuffer_browser?.requested_ms ?? capture.prebuffer?.requested_ms,
      browser:capture.prebuffer_browser,server:capture.prebuffer?.server}:null);
    if(prebuffer){
      const pre=section('Before Record was clicked','jr-prebuffer');
      const duration=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?(value/1000).toFixed(2)+' s':'Not measured';
      pre.append(description([['Requested history',duration(prebuffer.requested_ms)],
        ['Browser history retained',duration(prebuffer.browser?.available_ms)],
        ['Record click in timeline',duration(prebuffer.browser?.trigger_elapsed_ms)]]));
      const server=prebuffer.server;
      const workers=Array.isArray(server?.workers)?server.workers:[];
      if(workers.length)for(const [index,worker] of workers.entries()){
        const row=description([['Server worker '+(index+1),duration(worker.available_ms)]]);
        row.title=worker.worker_id||worker.id||'unknown';
        pre.append(row);
        if(worker.limitations?.length)pre.append(node('p',worker.limitations.map(text).join(' · '),'jr-callout'));
      }else pre.append(node('p','Server history: not measured.','jr-muted'));
      if(server?.missing_workers?.length)pre.append(node('p','Missing server workers: '+server.missing_workers.join(', '),'jr-callout'));
      for(const channel of [prebuffer.browser,server])if(channel?.limitations?.length)
        pre.append(node('p',channel.limitations.map(text).join(' · '),'jr-callout'));
      pre.append(node('p','Coverage can be shorter during warm-up, memory pressure or interruptions. Requirement results use the retained evidence.','jr-muted'));
      out.append(pre);
    }
    const coverage=section('Requirements');coverage.append(node('p','Results apply only to the actions and states observed in this recording.','jr-muted'));
    const checks=report.checks||report.requirements||report.results||[];
    const measured=checks.filter(c=>c.result==='pass' || c.result==='fail').length;
    coverage.append(node('p',measured+' of '+checks.length+' requirements measured · '+
      checks.filter(c=>c.result==='blocked').length+' need more evidence · '+
      checks.filter(c=>c.result==='not_run').length+' not exercised','jr-muted'));
    coverage.append(node('p','Scope: observed transcript; this report does not establish full journey acceptance.','jr-muted'));
    for(const [i,check] of checks.entries()){
      const d=node('details');d.dataset.key='check-'+(check.id||i);const summary=node('summary');summary.append(badge(check.status||check.result),node('span',check.title||check.name||check.id||'Requirement'));d.append(summary);
      d.append(description([['Expected',check.expected],['Observed',check.actual||check.observed||check.reason]]));
      if(check.next_action)d.append(description([['To measure',check.next_action]]));
      if(check.limitations?.length)d.append(node('p',check.limitations.join(' '),'jr-callout'));
      if(check.guideline||check.requirement)d.append(node('p',text(check.guideline||check.requirement),'jr-muted'));
      const refs=check.evidence||check.evidence_refs||[];
      if(refs.length){const refsBody=node('div');for(const ref of refs){const id=typeof ref==='object'?(ref.event_seq??ref.seq??ref.id):ref;refsBody.append(button('Evidence '+id,()=>showEvidence(id)),node('pre',text(ref)));}d.append(details('Evidence',refsBody,'evidence-'+(check.id||i)));}
      coverage.append(d);
    }
    if(!checks.length)coverage.append(node('p','No evaluated requirements yet. Capture more of the chat task to establish coverage.','jr-muted'));out.append(coverage);
    const marked=section('Captured issues');
    for(const [i,m] of markers.entries()){
      const fig=node('figure');fig.append(node('figcaption',time(m.created_at||m.timestamp)+' — '+(m.note||m.label||'Issue marker')));
      const artifact=m.artifact_id||m.screenshot?.artifact_id;
      if(artifact&&blobs.has(capture.id+'/'+artifact)){const img=node('img');img.alt=m.note||'User-captured chat at issue marker';img.src=blobs.get(capture.id+'/'+artifact);fig.append(img);}
      else if(artifact){const load=button('View screenshot',async()=>{load.disabled=true;try{const key=capture.id+'/'+artifact;let url=blobs.get(key);if(!url){const blob=await request('/'+encodeURIComponent(capture.id)+'/artifacts/'+encodeURIComponent(artifact),{blob:true});if(disposed)return;url=URL.createObjectURL(blob);blobs.set(key,url);}if(!fig.isConnected)return;const img=node('img');img.alt=m.note||'User-captured chat at issue marker';img.src=url;fig.append(img);load.remove();}catch(e){load.disabled=false;notice(e.message);}});fig.append(load);}
      if(m.event_seq!==undefined)fig.append(button('Nearby events',()=>showEvidence(m.event_seq)));
      marked.append(fig);
    }
    if(!markers.length)marked.append(node('p','No issue markers or screenshots attached.','jr-muted'));out.append(marked);
    const inv=state.detail.investigation||capture.investigation||{};out.append(investigation(inv));
    const meta=node('div');meta.append(description([['Sessions',capture.sessions||[]],['Channels',capture.channels||{}],['Rule set',report.rule_set_version||report.ruleset_version],['Build',capture.build||capture.app_revision],['Capture health',capture.health||{}]]));out.append(details('Capture details',meta,'capture-details'));
    const timeline=section('Evidence timeline','jr-timeline');timeline.append(node('p','Event details are preserved as text. Captured page HTML is never executed.','jr-muted'));const events=node('div');events.id='jr-events';timeline.append(events,button('Load more events',()=>loadEvents()));out.append(timeline);
    host.replaceChildren(out);for(const d of host.querySelectorAll('details[data-key]'))if(opened.has(d.dataset.key))d.open=true;renderEvents();
    if(focused){const el=root.querySelector('#'+focused);if(el){el.focus({preventScroll:true});if(selection!=null&&el.setSelectionRange)el.setSelectionRange(selection,selection);}}
  }
  function investigation(inv){
    const s=section('Investigation and plan');s.append(badge(inv.state||'not_started'));
    if(inv.session_id)s.append(node('p','Investigation session: '+inv.session_id,'jr-muted'));
    if(inv.error)s.append(node('p',text(inv.error),'jr-callout'));
    if(inv.summary)s.append(node('p',text(inv.summary)));
    const plan=inv.plan;
    if(!plan){s.append(node('p',active(inv.state)?'Next: investigation produces an evidence-backed plan.':'Investigate the evidence and relevant source without changing the app.','jr-muted'));const b=button('Investigate findings',()=>act('/investigate',{}));b.disabled=active(inv.state)||actionBusy;s.append(b);return s;}
    s.append(node('h3','Plan revision '+plan.revision),node('p',text(plan.approach||plan.summary)));
    for(const [label,value] of [['Proposed files',plan.files],['Steps',plan.steps],['Risks',plan.risks],['Verification',plan.verification]])if(value){const body=node('div');if(Array.isArray(value)){const list=node('ol');for(const v of value)list.append(node('li',text(v)));body.append(list);}else body.append(node('p',text(value)));s.append(details(label,body,'plan-'+label));}
    const comment=node('textarea');comment.id='jr-plan-comment';comment.placeholder='Optional review comment';comment.setAttribute('aria-label','Plan review comment');comment.value=drafts.get(state.selected)||'';comment.addEventListener('input',()=>{drafts.set(state.selected,comment.value);save();},{signal:life.signal});s.append(comment);
    // Approval binds a concrete revision AND a user-chosen execution agent.
    // Never silently pick the first agent; backend owns eligibility and access.
    const selection=agentChoices.has(state.selected)?agentChoices.get(state.selected):(inv.approval?.execution_agent_id||inv.execution_agent_id||'');
    const chooser=node('select');chooser.id='jr-execution-agent';chooser.setAttribute('aria-label','Execution agent');
    const placeholder=node('option',agentsLoaded?'Choose an execution agent':'Loading eligible agents…');placeholder.value='';chooser.append(placeholder);
    for(const agent of executionAgents){const option=node('option',agent.name||agent.id);option.value=agent.id;chooser.append(option);}
    chooser.value=selection;
    const eligible=!!selection&&executionAgents.some(agent=>agent.id===selection);
    chooser.disabled=actionBusy||active(inv.state)||!agentsLoaded;
    chooser.addEventListener('change',()=>{agentChoices.set(state.selected,chooser.value);save();lastDetail='';renderReport();},{signal:life.signal});
    const label=node('label','Execution agent');label.htmlFor=chooser.id;s.append(label,chooser);
    if(agentsError)s.append(node('p',agentsError,'jr-callout'),button('Retry agent list',()=>{agentsLoaded=false;agentsError='';wake();}));
    else if(agentsLoaded&&!executionAgents.length)s.append(node('p','No eligible execution agents are available. Configure a code-capable agent before approving execution.','jr-callout'));
    const approved=inv.approval?.decision==='approve'&&Number(inv.approval?.plan_revision ?? inv.approval?.revision)===Number(plan.revision)&&inv.approval?.execution_agent_id===selection&&eligible;
    s.append(node('p',approved?'Next: authorize execution of this approved revision.':'Next: review and approve this plan before further automation or changes.','jr-muted'));
    const actions=node('div',undefined,'jr-actions');const revision=plan.revision;
    for(const [label,path,payload,primary] of [['Approve plan','/approval',{plan_revision:revision,decision:'approve',execution_agent_id:selection},true],['Reject plan','/approval',{plan_revision:revision,decision:'reject'},false],['Execute approved plan','/execute',{plan_revision:revision,execution_agent_id:selection},true]]){
      const b=button(label,()=>act(path,{...payload,...(path==='/approval'?{comment:comment.value}:{})}),primary);b.disabled=actionBusy||active(inv.state)||!Number.isInteger(revision)||(path==='/execute'&&!approved)||(payload.decision==='approve'&&!eligible);actions.append(b);
    }
    s.append(actions);return s;
  }
  async function act(path,body){if(actionBusy)return;actionBusy=true;lastDetail='';renderReport();try{await request('/'+encodeURIComponent(state.selected)+path,{method:'POST',body:JSON.stringify(body)});notice('Saved. Refreshing authoritative review state…');}catch(e){notice(e.message);}finally{actionBusy=false;lastDetail='';wake();}}
  async function loadEvents(){const id=state.selected;if(!id)return;try{const data=await request('/'+encodeURIComponent(id)+'/events?after='+encodeURIComponent(state.cursor)+'&limit=100');if(disposed||id!==state.selected)return;const seen=new Set(state.events.map(e=>e.seq??e.event_seq??e.id));state.events.push(...(data.events||[]).filter(e=>!seen.has(e.seq??e.event_seq??e.id)));state.cursor=data.next_cursor??state.cursor;state.more=!!data.has_more;renderEvents();}catch(e){notice(e.message);}}
  function renderEvents(){const box=$('jr-events');if(!box)return;const existing=new Set(Array.from(box.children).map(e=>e.dataset.event));for(const [i,e] of state.events.entries()){const id=String(e.id??e.seq??e.event_seq??i);if(existing.has(id))continue;const d=details(time(e.timestamp||e.ts||e.created_at)+' · '+(e.kind||e.type||e.event||'Event'),node('pre',text(e)),'event-'+id);d.dataset.event=id;box.append(d);}}
  async function showEvidence(id){await loadEvents();let target=Array.from(root.querySelectorAll('[data-event]')).find(e=>e.dataset.event===String(id));if(!target){notice('Event '+id+' is outside the loaded timeline. Use Load more events to inspect earlier/later evidence.');target=$('jr-timeline');}if(target){if(target.tagName==='DETAILS')target.open=true;target.scrollIntoView({block:'nearest',behavior:'smooth'});}}
  function getSessions(){return state.captures.flatMap(c=>{const inv=c.investigation||{};return active(inv.state)&&inv.session_id?[inv.session_id]:[];});}
  function interval(){return state.captures.some(c=>active(c.state)||active(c.investigation?.state))?3000:15000;}
  function schedule(ms){clearTimeout(timer);if(!disposed&&document.visibilityState!=='hidden')timer=setTimeout(poll,ms);}
  function wake(){if(disposed)return;if(busy){again=true;return;}schedule(100);}
  async function poll(){
    if(disposed||document.visibilityState==='hidden')return;if(busy){again=true;return;}busy=true;
    try{const list=await request();if(disposed)return;state.captures=list.captures||[];captures();if(!state.selected&&state.captures.length){state.selected=state.captures[0].id;save();captures();}
      const id=state.selected;if(id){const result=await request('/'+encodeURIComponent(id));if(!disposed&&id===state.selected){state.detail=result;
        if((result.investigation||result.capture?.investigation)?.plan&&!agentsLoaded){
          try{const choices=await request('/api/v1/recordings/execution-agents');if(disposed)return;executionAgents=choices.agents||[];agentsError='';}
          catch(e){if(disposed)return;executionAgents=[];agentsError='Execution agents unavailable: '+e.message;}
          agentsLoaded=true;lastDetail='';
        }
        if(id===state.selected)renderReport();}}
      notice('Updated '+new Date().toLocaleTimeString()+' · '+state.captures.length+' recordings');
    }catch(e){if(!disposed&&e.name!=='AbortError')notice(e.message+' — existing evidence remains visible.');}
    finally{busy=false;if(again){again=false;schedule(100);}else schedule(interval());}
  }
  $('jr-search').value=state.query;$('jr-filter').value=state.filter;
  $('jr-search').addEventListener('input',e=>{state.query=e.target.value;save();captures();},{signal:life.signal});
  $('jr-filter').addEventListener('change',e=>{state.filter=e.target.value;save();captures();},{signal:life.signal});
  $('jr-refresh').addEventListener('click',wake,{signal:life.signal});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')clearTimeout(timer);else wake();},{signal:life.signal});
  // GenUILive session wake: the host owns subscription disposal on unmount.
  // Guard callbacks as well because older hosts return no unsubscribe function.
  const unsubscribe=api?.onSessionActivity?.(event=>{if(!disposed&&getSessions().includes(event?.session_id))wake();});
  poll();
  return ()=>{disposed=true;clearTimeout(timer);life.abort();if(typeof unsubscribe==='function')unsubscribe();for(const url of blobs.values())URL.revokeObjectURL(url);blobs.clear();};
});
