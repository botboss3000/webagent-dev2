// Private journal projection: no public reducer or persistent browser cache.
export function mergeActivity(previous, page, sessionId) {
  if (!Array.isArray(page.events) || !Array.isArray(page.segments)
      || !Number.isSafeInteger(page.cursor) || page.cursor < previous.cursor)
    throw new Error('Invalid activity page');
  const events = new Map(previous.events.map(row => [row.seq, row]));
  for (const row of page.events) {
    if (row.session_id !== sessionId || !Number.isSafeInteger(row.seq)
        || row.seq <= 0 || typeof row.run_id !== 'string')
      throw new Error('Activity identity mismatch');
    const old = events.get(row.seq);
    if (old && JSON.stringify(old) !== JSON.stringify(row))
      throw new Error('Activity revision conflict');
    events.set(row.seq, row);
  }
  if (page.events.some(row => row.seq > page.cursor)) throw new Error('Invalid activity cursor');
  return {events: [...events.values()].sort((a,b) => a.seq-b.seq),
    segments: page.segments, cursor: page.cursor};
}

export function activityRows(events) {
  const rows=[], tools=new Map(), messages=new Map();
  for (const event of events) {
    const p=event.payload || {}, kind=event.kind;
    if (kind === 'tool_call' || kind === 'tool_result') {
      const key=p.tool_call_id || event.event_id;
      let row=tools.get(key);
      if (!row) {
        row={id:'tool:'+key, category:'tools', label:p.tool || 'Tool', events:[]};
        tools.set(key,row); rows.push(row);
      }
      row.events.push(event);
      row.label=(p.tool || row.label.split(' · ')[0])+' · '+
        (kind==='tool_call' ? 'running' : p.error ? 'error' : 'done');
    } else if (kind === 'agent_step_end' || kind === 'response') {
      const key=p.asst_id || event.event_id;
      let row=messages.get(key);
      if (!row) {
        row={id:'message:'+key,category:kind==='response'?'responses':'updates',
          label:kind==='response'?'Response':'Update',events:[]};
        messages.set(key,row);rows.push(row);
      }
      row.events.push(event);row.text=p.content || '';
      if (kind==='response') {row.category='responses';row.label='Response';}
    } else if (kind === 'mode_change' || kind === 'execution_mode') {
      rows.push({id:event.event_id,category:'modes',label:'Mode change',events:[event]});
    } else if (kind === 'compaction_completed' || kind === 'compaction_result'
        || (kind==='pipeline' && p.step==='compaction_end')) {
      rows.push({id:event.event_id,category:'compactions',label:'Compaction',events:[event]});
    }
  }
  return rows;
}

export function activityGroups(activity, runs) {
  const owns=(segment,event)=>event.run_id===segment.run_id && segment.first_seq != null
    && event.seq>=segment.first_seq && event.seq<=segment.last_seq;
  const covered=new Set(activity.events.filter(e=>activity.segments.some(s=>owns(s,e))).map(e=>e.seq));
  const lastByRun=new Map(activity.segments.map(s=>[s.run_id,s.id]));
  const terminal=new Set(['completed','failed','cancelled']);
  return activity.segments.map(segment => {
    // Late/stale events remain available in raw diagnostics, without extending
    // the semantic counters of an already closed execution segment.
    const events=activity.events.filter(e=>owns(segment,e) ||
      (!covered.has(e.seq) && e.run_id===segment.run_id && lastByRun.get(e.run_id)===segment.id));
    const rows=activityRows(events);
    const counts={tools:0,updates:0,responses:0,modes:0,compactions:0};
    for (const row of rows) counts[row.category]++;
    const run=runs.find(r=>r.id===segment.run_id);
    const finalOutcome=lastByRun.get(segment.run_id)===segment.id && terminal.has(run?.state);
    return {...segment,events,rows,counts,kind:run?.kind,
      state:segment.closed && !finalOutcome?'finished':run?.state || 'queued'};
  });
}

// Serial pagination, identity fencing, and polling are independent of the public
// chat transport. Private records live only until this session is disconnected.
export function watchActivity({sessionId, request, onChange, onError, schedule=setTimeout, cancel=clearTimeout}) {
  let state={events:[],segments:[],cursor:0}, timer=null, stopped=false;
  const controller=new AbortController();
  async function poll() {
    try {
      let more=true;
      while (!stopped && more) {
        const response=await request(state.cursor,controller.signal);
        if(stopped) return;
        if (!response.ok) {
          const error=new Error('Activity unavailable ('+response.status+')');
          error.status=response.status;throw error;
        }
        const page=await response.json();
        if(stopped) return;
        const previous=state.cursor;
        state=mergeActivity(state,page,sessionId);
        more=page.has_more===true;
        if(more && state.cursor===previous) throw new Error('Activity cursor did not advance');
        onChange(state,more);
      }
    } catch(error) {
      if(stopped) return;
      onError(error);
      if([401,403,404].includes(error.status)) {stop();return;}
    }
    if(!stopped) timer=schedule(poll,2000);
  }
  function stop() {
    stopped=true;controller.abort();if(timer!==null) cancel(timer);
    state={events:[],segments:[],cursor:0};
  }
  void poll();
  return stop;
}
