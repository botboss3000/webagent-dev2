// Stable keys are the only reconciliation authority. No text or time matching.
export function reconcilePublicNodes(container, state, pending, createNode) {
  const nodes=new Map();
  for(const node of container.children) {
    const key=node.dataset?.publicId || (node.dataset?.clientMessageId && 'pending:'+node.dataset.clientMessageId);
    if(key) nodes.set(key,node);
  }
  const ordered=[],accepted=new Set();
  for(const item of state.items) {
    const clientId=item.kind==='user' && item.payload.client_message_id;
    if(clientId) accepted.add(clientId);
    if(item.deleted) continue;
    let node=nodes.get(item.id) || (clientId && nodes.get('pending:'+clientId));
    if(node && Number(node.dataset.publicRevision || 0)!==item.revision && node.dataset.publicId) {
      // Only registered editable components can change after acceptance.
      const replacement=createNode(item);
      if(!replacement) continue;
      node.replaceWith(replacement);node=replacement;
    }
    if(!node) node=createNode(item);
    if(!node) continue;
    node.dataset.publicId=item.id;
    node.dataset.publicRevision=String(item.revision);
    if(clientId) node.dataset.clientMessageId=clientId;
    node.classList.remove('pending','rejected');
    node.querySelector('[data-submission-status]')?.remove();
    ordered.push({node,position:item.position});
  }
  for(const entry of pending) {
    const clientId=entry.message.client_message_id;
    if(accepted.has(clientId)) continue;
    let node=nodes.get('pending:'+clientId);
    if(!node) node=createNode({id:clientId,kind:'user',payload:entry.message,
      created_at:new Date(entry.createdAt).toISOString(),pending:true});
    if(!node) continue;
    node.dataset.clientMessageId=clientId;
    node.classList.add('pending');
    // Keep optimistic input at the public boundary seen when it was queued.
    // New publications cannot leap over it while server acceptance is pending.
    const position=Number.isSafeInteger(entry.afterPosition) && entry.afterPosition>=0
      ? entry.afterPosition : Number.MAX_SAFE_INTEGER;
    const next=ordered.findIndex(row=>row.position>position);
    ordered.splice(next<0?ordered.length:next,0,{node,position});
  }
  const wanted=ordered.map(row=>row.node);
  const keep=new Set(wanted);
  for(const node of [...container.children]) {
    if((node.dataset?.publicId || node.dataset?.clientMessageId) && !keep.has(node)) node.remove();
  }
  // Activity groups and other authorized decorations live between public
  // nodes. Compare only public neighbors so a state refresh cannot shuttle a
  // message across its activity group and make the activity renderer move it
  // back on every delivery.
  const isPublic=node=>!!(node.dataset?.publicId || node.dataset?.clientMessageId);
  const nextPublic=after=>{
    let node=after ? after.nextSibling : container.firstChild;
    while(node && !isPublic(node)) node=node.nextSibling;
    return node;
  };
  let previous=null;
  for(const node of wanted) {
    const next=nextPublic(previous);
    if(node!==next) container.insertBefore(node,next);
    previous=node;
  }
  return wanted;
}
