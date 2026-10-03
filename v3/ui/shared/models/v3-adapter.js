 'use strict';
// V3 boundary for the actual reference renderer. No v2 network fallback is allowed.
export const MISSING = 'NEED BACKEND CONTRACT';
const reject = detail => { throw Error(MISSING + ': ' + detail); };
const response = (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
export function createModelAdapter(request, agentId, onAssignment) {
  let data, catalog, destroyed=false;
  const statuses=new Map();
  let queue=Promise.resolve();
  const drafts=new Map();
  const scope='/models/catalog?agent_id='+encodeURIComponent(agentId);
  async function read(){
    if(!agentId) throw Error('Select an authorized agent first');
    data=await request(scope);catalog=data.catalog;return data;
  }
  const rows=()=> (catalog.agent_models?.[agentId]?.entry_ids||[]).map(id=>catalog.entries[id]).filter(Boolean).map(e=>{
    const c=catalog.connections[e.connection_id];
    return {entry_id:e.id,provider:c.transport,model:e.upstream_model,display_name:e.display_name,
      base_url:c.transport==='deepseek'?'https://api.deepseek.com':'codex://chatgpt',api_key:'',
      text_capable:e.capabilities.includes('text'),image_capable:e.capabilities.includes('image_in'),
      image_out_capable:e.capabilities.includes('image_out'),voice_capable:e.capabilities.includes('voice'),
      credential_configured:!!statuses.get(c.id)?.connected,catalog_origin:'byo'};
  });
  async function save({providers,role_assignments={}}){
    if(destroyed)throw Error('Models page closed');
    if(!data.can_manage)throw Error('Agent model management is read-only');
    const next=structuredClone(catalog);
    const rowIds=[];
    const cfg=next.agent_models[agentId]??={agent_id:agentId,owner_id:data.agent_owner_id,entry_ids:[],roles:{},revision:1};
    for(const p of providers){
      if(p.alias_group_id || p.reference_cost_input!=null || p.reference_cost_output!=null)reject('alias groups and reference pricing');
      if(!['deepseek','openai_subscription'].includes(p.provider))reject('provider transport');
      let e=next.entries[p.entry_id];
      if(!e){
        const id=p.entry_id||'model-'+crypto.randomUUID(); const connectionId=drafts.get(id)||'connection-'+crypto.randomUUID();
        next.connections[connectionId]??={id:connectionId,owner_id:data.caller_id,transport:p.provider,credential_ref:'provider_credential:'+crypto.randomUUID(),allowed_agents:[agentId],revision:1,revoked:false};
        e=next.entries[id]={id,owner_id:data.caller_id,connection_id:connectionId,upstream_model:p.model,display_name:p.display_name||p.model,capabilities:['text'],allowed_agents:[agentId],revision:1};
        // Hydrate reference row identity only after the catalog write succeeds below.
      }else if(e.owner_id===data.caller_id){
        e.display_name=p.display_name||e.upstream_model;
      }else if(p.display_name!==e.display_name)throw Error('Only the model owner can rename this connection');
      rowIds.push(e.id);
      if(p.api_key && (p.provider!=='deepseek' || !/^[!-~]{8,512}$/.test(p.api_key)))throw Error('Invalid DeepSeek API key');
      if(p.api_key && next.connections[e.connection_id].owner_id!==data.caller_id)throw Error('Connection credentials are owner-only');
      if(!/^[A-Za-z0-9._-]{1,128}$/.test(p.model))throw Error('Invalid upstream model ID');
    }
    // Keep records when a row is removed; roster removal must not revoke a shared credential.
    cfg.entry_ids=rowIds;
    const roles={};
    for(const [role,id] of Object.entries(role_assignments)){
      if(!id)continue;
      if(!cfg.entry_ids.includes(id))throw Error('Role is outside this agent roster');
      const old=cfg.roles[role];
      // V2's scalar appointment has no per-role backup editor; retain existing v3 backups.
      roles[role]={entry_id:id,backup_entry_ids:(old?.backup_entry_ids||[]).filter(x=>cfg.entry_ids.includes(x)&&x!==id)};
    }
    cfg.roles=roles;
    const saved=await request(scope,'PUT',next);data=saved;catalog=saved.catalog;
    providers.forEach((p,i)=>{p.entry_id=cfg.entry_ids[i];});
    await onAssignment?.(agentId,catalog.agent_models?.[agentId]);
    for(const p of providers){
      if(p.api_key){
        const e=catalog.entries[p.entry_id];const c=catalog.connections[e.connection_id];
        if(c.owner_id!==data.caller_id)throw Error('Connection credentials are owner-only');
        await request('/models/connections/'+encodeURIComponent(c.id)+'/api-key','PUT',{api_key:p.api_key});
        p.api_key='';
      }
    }
    // Credential writes advance revisions separately; refresh before the next roster write.
    await read();return {providers};
  }
  const adapter={
    loadConfig:async()=>{await read();await Promise.all((catalog.agent_models?.[agentId]?.entry_ids||[]).map(async id=>{const c=catalog.connections[catalog.entries[id]?.connection_id];if(c)try{statuses.set(c.id,await request('/models/connections/'+encodeURIComponent(c.id)+'?agent_id='+encodeURIComponent(agentId)));}catch{statuses.delete(c.id);}}));return {provider:'webagent_catalog',roster:rows(),role_assignments:Object.fromEntries(Object.entries(catalog.agent_models?.[agentId]?.roles||{}).map(([r,a])=>[r,a.entry_id]))};},
    saveRoster:payload=>{const work=()=>save(payload);const result=queue.then(work,work);queue=result.catch(()=>{});return result;},
    loadPresets:async()=>({webagent_catalog:{name:'WebAgent Catalog',base_url:''},openai_subscription:{name:'ChatGPT Subscription',base_url:'codex://chatgpt'},deepseek:{name:'DeepSeek',base_url:'https://api.deepseek.com'}}),
    fetchModels:async()=>({models:[],error:MISSING+': model discovery / entitled catalog. Enter an upstream model ID manually for supported providers.'}),
    fetchModelInfo:async()=>({}),fetchModelUsage:async()=>null,
    loadDirectives:async()=>({standard:'',premium:'',system:'',image_in:'',image_out:'',voice:'',custom:{}}),
    saveDirectives:async()=>reject('role prompt injections'),
    saveDefaultEntryId:async()=>reject('account default for new agents'),
    listCredentials:async()=>reject('named credential library'),
    loadCredential:async()=>reject('named credential library'),
    saveCredential:async()=>reject('credential library validation/save'),
    deleteCredential:async()=>reject('credential library deletion'),
    subscriptionRequest:async(path,options={})=>{
      if(destroyed)throw Error('Models page closed');
      const url=new URL(path,location.origin);const entryId=url.searchParams.get('entry_id');
      if(url.pathname.endsWith('/login/start')){
        if(!data.can_manage)throw Error('Read-only agent');
        let connectionId=entryId&&(catalog.entries[entryId]?.connection_id||drafts.get(entryId));
        let id=entryId;
        if(!connectionId){
          id='model-'+crypto.randomUUID();connectionId='connection-'+crypto.randomUUID();
          const next=structuredClone(catalog);
          next.connections[connectionId]={id:connectionId,owner_id:data.caller_id,transport:'openai_subscription',credential_ref:'provider_credential:'+crypto.randomUUID(),allowed_agents:[agentId],revision:1,revoked:false};
          const saved=await request(scope,'PUT',next);data=saved;catalog=saved.catalog;drafts.set(id,connectionId);
        }
        if(catalog.connections[connectionId]?.owner_id!==data.caller_id)throw Error('Connection authorization is owner-only');
        const auth=await request('/models/connections/'+encodeURIComponent(connectionId)+'/authorization','POST');await read();
        return response({...auth,entry_id:id});
      }
      if(url.pathname.endsWith('/auth/status')){
        const connectionId=catalog.entries[entryId]?.connection_id||drafts.get(entryId);
        if(!connectionId)return response({chatgpt_authorized:false});
        const state=await request('/models/connections/'+encodeURIComponent(connectionId)+'?agent_id='+encodeURIComponent(agentId));
        return response({chatgpt_authorized:state.connected,account:{},authorization:state.authorization});
      }
      return response({detail:MISSING+': '+url.pathname},501);
    }
  };
  return {adapter,ready:read,state:()=>data,destroy:()=>{destroyed=true;},blockedFetch:async()=>response({detail:MISSING},501)};
}
