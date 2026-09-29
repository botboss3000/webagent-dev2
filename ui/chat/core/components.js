// Owner-only session resources. Never render raw tool results or cache them
// with the participant-visible transcript.
export function watchComponents({read, apply, clear, current, onError=()=>{},
  schedule=setTimeout, cancel=clearTimeout, interval=2000}) {
  let stopped=false, timer=null, controller=null, version=0;
  async function refresh() {
    if(stopped || !current()) return;
    const requestVersion=++version;
    if(timer!==null) cancel(timer);
    controller?.abort();
    const requestController=new AbortController();
    controller=requestController;
    try {
      const response=await read(requestController.signal);
      if(stopped || requestVersion!==version || !current()) return;
      if(!response.ok) {
        if([401,403,404].includes(response.status)) {
          stop();clear();onError('Elements are unavailable for this session.');return;
        }
        throw new Error('Could not refresh elements');
      }
      const snapshot=await response.json();
      if(stopped || requestVersion!==version || !current()) return;
      if(!Array.isArray(snapshot.components)) throw new Error('Invalid elements snapshot');
      apply(snapshot);onError('');
    } catch(error) {
      if(!stopped && requestVersion===version && current() && error.name!=='AbortError')
        onError('Elements could not refresh. Retrying…');
    } finally {
      if(!stopped && requestVersion===version && current())
        timer=schedule(refresh,interval);
    }
  }
  function stop() {
    stopped=true;version++;
    if(timer!==null) cancel(timer);
    controller?.abort();
  }
  return {refresh,stop};
}

// Unchanged resource bodies must keep their DOM nodes, focus and draft inputs.
// Timestamps may advance without changing the visible component.
export function reconcileComponents(previous, rows, {render, remove, focused=()=>false}) {
  const desired=new Map(rows.map(row=>[row.id,row]));
  const signature=row=>JSON.stringify([row.type,row.title,row.data,row.lifecycle]);
  for(const [id] of previous) if(!desired.has(id)) {remove(id);previous.delete(id);}
  for(const [id,row] of desired) {
    const old=previous.get(id);
    if(old && signature(old)===signature(row)) {previous.set(id,row);continue;}
    // Do not replace an editor while the user is typing; retry next snapshot.
    if(old && focused(id)) continue;
    render(row);previous.set(id,row);
  }
}
