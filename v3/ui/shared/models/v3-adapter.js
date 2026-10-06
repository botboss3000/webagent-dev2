 'use strict';
// V3 boundary for the actual reference renderer. No v2 network fallback is allowed.
export const MISSING = 'Unavailable in v3';
const reject = detail => { throw Error(MISSING + ': ' + detail); };
const response = (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
export function createModelAdapter(request, agentId, onAssignment) {
  let data, catalog, account, destroyed=false;
  // Public GPT-6 choices are a preview until this connection returns its account catalog.
  const subscriptionPreviewModels=[
    {id:'gpt-6-astra',name:'GPT-6 Astra',text_capable:true},
    {id:'gpt-6.1-sol',name:'GPT-6.1 Sol',text_capable:true},
    {id:'gpt-6-sol',name:'GPT-6 Sol',text_capable:true},
    {id:'gpt-6-luna',name:'GPT-6 Luna',text_capable:true},
  ];
  const providerPresets={openai_subscription:{name:'ChatGPT Subscription',base_url:'codex://chatgpt'},deepseek:{name:'DeepSeek',base_url:'https://api.deepseek.com'},openai_api:{name:'OpenAI API',base_url:'https://api.openai.com/v1'},openrouter:{name:'OpenRouter',base_url:'https://openrouter.ai/api/v1'},abacus:{name:'Abacus RouteLLM',base_url:'https://routellm.abacus.ai/v1'},gemini:{name:'Gemini',base_url:'https://generativelanguage.googleapis.com/v1beta/openai'},groq:{name:'Groq',base_url:'https://api.groq.com/openai/v1'},together:{name:'Together',base_url:'https://api.together.xyz/v1'},mistral:{name:'Mistral',base_url:'https://api.mistral.ai/v1'},fireworks:{name:'Fireworks',base_url:'https://api.fireworks.ai/inference/v1'},xai:{name:'xAI',base_url:'https://api.x.ai/v1'},perplexity:{name:'Perplexity',base_url:'https://api.perplexity.ai'},deepinfra:{name:'DeepInfra',base_url:'https://api.deepinfra.com/v1/openai'}};
  async function accountChoices(){account=await request('/models/me/catalog');return account;}
  async function adopt(source,alias){
    const saved=await request('/models/agents/'+encodeURIComponent(agentId)+'/adoptions','POST',{
      client_request_id:crypto.randomUUID(),expected_roster_revision:catalog.agent_models?.[agentId]?.revision||0,source,alias});
    await read();return saved.entry_id;
  }
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
    return {entry_id:e.id,connection_id:c.id,catalog_source_id:data.adoptions?.[e.id]?.source_id||(catalog.platform_sources?.[e.id]?'':'byo:'+e.id),provider:c.transport,model:e.upstream_model,display_name:data.adoptions?.[e.id]?.alias||e.display_name,
      base_url:c.transport==='deepseek'?'https://api.deepseek.com':'codex://chatgpt',api_key:'',
      text_capable:e.capabilities.includes('text'),image_capable:e.capabilities.includes('image_in'),
      image_out_capable:e.capabilities.includes('image_out'),voice_capable:e.capabilities.includes('voice'),
      credential_configured:!!statuses.get(c.id)?.connected,catalog_origin:catalog.platform_sources?.[e.id]?'platform':'byo'};
  });
  async function save({providers,role_assignments={}}){
    if(destroyed)throw Error('Models page closed');
    if(!data.can_manage)throw Error('Agent model management is read-only');
    for(const p of providers){
      if(p.provider==='webagent_catalog'&&!catalog.entries[p.entry_id]){
        const choices=await accountChoices();const choice=[...(choices.available_platform_models||[]),...(choices.entries||[])].find(c=>c.id===p.entry_id);
        if(!choice)throw Error('Model choice is no longer available');
        const alias=p.display_name||choices.aliases?.[choice.id]||choice.display_name;
        p.entry_id=await adopt({origin:choice.origin,entry_id:choice.id},alias);
        const entry=catalog.entries[p.entry_id];p.provider=catalog.connections[entry.connection_id].transport;p.model=entry.upstream_model;p.display_name=alias;
        p.catalog_origin=choice.origin;p.api_key='';
      } else if(data.adoptions?.[p.entry_id]&&p.display_name!==data.adoptions[p.entry_id].alias){
        const previous=data.adoptions[p.entry_id];
        await adopt({origin:previous.origin,entry_id:previous.source_id},p.display_name||p.model);
      }
    }
    const next=structuredClone(catalog);
    const rowIds=[];
    const cfg=next.agent_models[agentId]??={agent_id:agentId,owner_id:data.agent_owner_id,entry_ids:[],roles:{},revision:1};
    for(const p of providers){
      if(p.alias_group_id || p.reference_cost_input!=null || p.reference_cost_output!=null)reject('alias groups and reference pricing');
      if(!Object.hasOwn(providerPresets,p.provider))reject('provider transport');
      let e=next.entries[p.entry_id];
      if(!e){
        const id=p.entry_id||'model-'+crypto.randomUUID(); const connectionId=p.connection_id||drafts.get(id)||'connection-'+crypto.randomUUID();
        if(p.connection_id){
          const connection=next.connections[connectionId];
          if(!connection||connection.owner_id!==data.caller_id||connection.transport!==p.provider||!connection.allowed_agents.includes(agentId)||connection.revoked)throw Error('Subscription connection is unavailable');
        }
        next.connections[connectionId]??={id:connectionId,owner_id:data.caller_id,transport:p.provider,credential_ref:'provider_credential:'+crypto.randomUUID(),allowed_agents:[agentId],revision:1,revoked:false};
        e=next.entries[id]={id,owner_id:data.caller_id,connection_id:connectionId,upstream_model:p.model,display_name:p.display_name||p.model,capabilities:['text'],allowed_agents:[agentId],revision:1};
        // Hydrate reference row identity only after the catalog write succeeds below.
      }else if(data.adoptions?.[e.id]||next.platform_sources?.[e.id]){
        if(p.api_key)throw Error('Adopted credentials cannot be replaced through the roster');
      }else if(e.owner_id===data.caller_id){
        e.display_name=p.display_name||e.upstream_model;
      }else if(p.display_name!==e.display_name)throw Error('Only the model owner can rename this connection');
      rowIds.push(e.id);
      if(p.api_key && (p.provider==='openai_subscription' || !/^[!-~]{8,512}$/.test(p.api_key)))throw Error('Invalid API key');
      if(p.api_key && next.connections[e.connection_id].owner_id!==data.caller_id)throw Error('Connection credentials are owner-only');
      if(!/^[!-~]{1,200}$/.test(p.model))throw Error('Invalid upstream model ID');
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
    providers.forEach((p,i)=>{p.entry_id=cfg.entry_ids[i];p.connection_id=catalog.entries[p.entry_id]?.connection_id||'';p.catalog_source_id=data.adoptions?.[p.entry_id]?.source_id||p.catalog_source_id||'';});
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
    loadPresets:async()=>({webagent_catalog:{name:'WebAgent Catalog / Available models',base_url:''},...providerPresets}),
    fetchModels:async({provider,api_key,entry_id})=>{
      if(provider==='openai_subscription'){
        const connectionId=catalog.entries[entry_id]?.connection_id||drafts.get(entry_id);
        if(!connectionId)return {models:subscriptionPreviewModels,preview:true};
        try{return await request('/models/connections/'+encodeURIComponent(connectionId)+'/models?agent_id='+encodeURIComponent(agentId));}
        catch(error){
          if(error.status===428)return {models:subscriptionPreviewModels,preview:true};
          return {models:[],error:error.message||'Subscription model catalog unavailable'};
        }
      }
      if(provider!=='webagent_catalog')return request('/models/discovery','POST',{provider,api_key});
      const choices=await accountChoices();
      return {models:[...(choices.available_platform_models||[]),...(choices.entries||[])].map(c=>({
        id:c.id,display_name:choices.aliases?.[c.id]||c.display_name,name:choices.aliases?.[c.id]||c.display_name,catalog_name:c.display_name,
        text_capable:c.capabilities.includes('text'),image_capable:c.capabilities.includes('image_in'),image_out_capable:c.capabilities.includes('image_out'),voice_capable:c.capabilities.includes('voice'),origin:c.origin}))};
    },
    fetchModelInfo:async()=>({}),fetchModelUsage:async()=>null,
    loadDirectives:async()=>({standard:'',premium:'',system:'',image_in:'',image_out:'',voice:'',custom:{}}),
    saveDirectives:async()=>reject('role prompt injections'),
    saveDefaultEntryId:async(id)=>{
      const choices=await accountChoices();let sourceId=id;
      if(id&&catalog.entries[id])sourceId=data.adoptions?.[id]?.source_id||(catalog.platform_sources?.[id]?id:'byo:'+id);
      if(sourceId?.startsWith('platform:')&&!choices.selected_platform_ids.includes(sourceId)){
        account=await request('/models/me/catalog','PUT',{revision:choices.revision,selected_platform_ids:[...choices.selected_platform_ids,sourceId],aliases:choices.aliases,default_entry_id:choices.default_entry_id});
      }
      account=await request('/models/me/catalog/default','PUT',{expected_revision:account.revision,default_entry_id:sourceId||null});return account;
    },
    listCredentials:async()=>reject('named credential library'),
    loadCredential:async()=>reject('named credential library'),
    saveCredential:async()=>reject('credential library validation/save'),
    deleteCredential:async()=>reject('credential library deletion'),
    resetSubscriptionCredentials:async()=>{
      if(destroyed)throw Error('Models page closed');
      const result=await request('/models/connections/subscription/reset?agent_id='+encodeURIComponent(agentId),'POST');
      for(const id of result.connection_ids||[])statuses.set(id,{connected:false});
      await read();
      return result.connection_ids||[];
    },
    subscriptionRequest:async(path,options={})=>{
      // The reference control keeps its V2 paths; only this bridge reaches V3.
      if(destroyed)throw Error('Models page closed');
      const url=new URL(path,location.origin);const entryId=url.searchParams.get('entry_id');
      if(url.pathname.endsWith('/login/start')){
        if(!data.can_manage)throw Error('Read-only agent');
        const pending=await request('/models/connections/authorization/pending?agent_id='+encodeURIComponent(agentId));
        if(pending.pending){
          const id=Object.values(catalog.entries).find(e=>e.connection_id===pending.connection_id)?.id
            || 'model-'+pending.connection_id;
          drafts.set(id,pending.connection_id);
          return response({...pending,entry_id:id});
        }
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
      if(url.pathname.endsWith('/login/reset')){
        const connectionId=catalog.entries[entryId]?.connection_id||drafts.get(entryId);
        if(!connectionId)return response({reset:true});
        const result=await request('/models/connections/'+encodeURIComponent(connectionId)+'/authorization/reset','POST');
        return response(result);
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
