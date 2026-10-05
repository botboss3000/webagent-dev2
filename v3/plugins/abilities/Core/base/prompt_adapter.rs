//! Read current-agent prompt configuration through the authenticated session.
//! Read grants reveal configuration; they never grant administration or editing.
use crate::{App,ability_controls,session_agent_id,session_mode,live_tool_decision};
use anyhow::{bail,Result};
use serde::Deserialize;
use serde_json::{json,Value};
use tokio_util::sync::CancellationToken;
const MAX_OUTPUT_BYTES:usize=64*1024;
pub fn owns(name:&str)->bool {name=="read_own_prompt"}
pub fn schemas()->Vec<Value> {
    vec![json!({"type":"function","name":"read_own_prompt",
        "description":"Read the current agent's ordered prompt slots and revisions; internal slots are omitted",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":false}})]
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PromptArgs {}
#[derive(Debug)]
struct ConfigurationError(&'static str);
impl std::fmt::Display for ConfigurationError {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str(self.0)}
}
impl std::error::Error for ConfigurationError {}
struct View {app:App,agent:String,manager:bool,fingerprint:String}
async fn view(app:&App,sid:&str,run_id:&str)->Result<View> {
    // The caller-owned session determines the agent. Never trust an app view
    // inherited from a previous API call or accept a model-supplied target.
    let agent=session_agent_id(app,sid).await?;
    let mut scoped=app.clone();
    scoped.store=app.store.for_agent(&agent).await?;
    let run=scoped.run(run_id).await?;
    if run["session_id"]!=sid || run["agent_id"].as_str().unwrap_or("shared_default")!=agent {
        bail!("Run does not belong to current session and agent");
    }
    let loaded=run["loaded_abilities"].clone();
    let (policy,mode_revision,visibility,state)=ability_controls(&scoped,sid).await?;
    let mode=session_mode(&scoped,sid).await?;
    let access=scoped.store.live_access(scoped.store.current_user_id(),&agent).await?;
    if !access.admitted() || live_tool_decision(&scoped,&access,"read_own_prompt",
        &policy,&visibility,&state,&loaded,mode["mode"]=="read_only")
        .permission==crate::access::Permission::Deny {
        bail!("Current-agent prompt read unavailable under current access");
    }
    let authority=scoped.store.agent_authority(scoped.store.current_user_id(),&agent).await?;
    let manager=authority["can_manage"].as_bool().unwrap_or(false);
    let fingerprint=crate::digest(&json!([agent,policy,mode_revision,visibility,state,mode,
        loaded,access.context_fingerprint(),authority,scoped.abilities.live_version()]));
    Ok(View {app:scoped,agent,manager,fingerprint})
}
fn render_slots(agent:&str,slots:Vec<Value>,manager:bool)->Result<Value> {
    let mut projected=Vec::new();
    for slot in slots {
        let name=slot["slot_name"].as_str().ok_or(ConfigurationError("Prompt slot name unavailable"))?;
        if name.starts_with("__") {continue;}
        let locked=slot["lock"].as_bool().ok_or(ConfigurationError("Prompt slot lock unavailable"))?;
        let revision=slot["revision"].as_u64().filter(|r|*r>0)
            .ok_or(ConfigurationError("Prompt slot revision unavailable"))?;
        let order=slot["order_index"].as_u64().ok_or(ConfigurationError("Prompt slot order unavailable"))?;
        let content=slot["content"].as_str().ok_or(ConfigurationError("Prompt slot content unavailable"))?;
        projected.push(json!({"slot_name":name,"order":order,"locked":locked,
            "editable":manager&&!locked,"revision":revision,"content":content}));
    }
    let result=json!({"status":"success","output":{"agent_id":agent,"slots":projected,
        "count":projected.len(),"source_kind":"agent_prompt_configuration","read_only":true}});
    if serde_json::to_vec(&result)?.len()>MAX_OUTPUT_BYTES {
        return Err(ConfigurationError("Prompt configuration exceeds the 64 KiB read limit; use bounded administrator slot reads").into());
    }
    Ok(result)
}
async fn work(app:&App,sid:&str,run_id:&str,name:&str,args:&Value)->Result<Value> {
    if !owns(name) {bail!("Unknown prompt read tool");}
    let _:PromptArgs=serde_json::from_value(args.clone())
        .map_err(|_|anyhow::anyhow!("read_own_prompt accepts only an empty object"))?;
    let current=view(app,sid,run_id).await?;
    let slots=current.app.store.list_prompt_slots().await?;
    let output=render_slots(&current.agent,slots,current.manager)?;
    if view(app,sid,run_id).await?.fingerprint!=current.fingerprint {
        bail!("Prompt read access changed during read");
    }
    Ok(output)
}
pub async fn execute(app:&App,sid:&str,run_id:&str,name:&str,args:&Value,cancel:CancellationToken)->Result<Value> {
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    let result=tokio::select! {
        _=cancel.cancelled()=>bail!("tool execution cancelled"),
        result=work(app,sid,run_id,name,args)=>result,
    };
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    Ok(match result {
        Ok(output)=>output,
        Err(error)=>json!({"status":"error","error_kind":
            if error.downcast_ref::<ConfigurationError>().is_some() {"prompt_configuration_invalid"}
            else {"prompt_read_rejected"},"output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn slot(name:&str,content:&str,locked:bool,order:u64)->Value {
        json!({"slot_name":name,"content":content,"lock":locked,"order_index":order,"revision":3})
    }
    #[test]
    fn prompt_projection_hides_internal_slots_preserves_revisions_and_limits_editability() {
        let slots=vec![slot("identity","locked content",true,0),
            slot("__skills__","INTERNAL SECRET",false,1),slot("style","short",false,10)];
        let owner=render_slots("agent-a",slots.clone(),true).unwrap();
        assert_eq!(owner["output"]["count"],2);
        assert_eq!(owner["output"]["slots"][0]["revision"],3);
        assert_eq!(owner["output"]["slots"][0]["locked"],true);
        assert_eq!(owner["output"]["slots"][0]["editable"],false);
        assert_eq!(owner["output"]["slots"][1]["editable"],true);
        assert_eq!(owner["output"]["slots"][1]["order"],10);
        assert!(!owner.to_string().contains("INTERNAL SECRET"));
        let member=render_slots("agent-a",slots,false).unwrap();
        assert!(member["output"]["slots"].as_array().unwrap().iter().all(|s|s["editable"]==false));
        assert_eq!(member["output"]["read_only"],true);
    }
    #[test]
    fn output_bound_is_serialized_bytes_and_never_truncates_slot_content() {
        let oversized=(0..4).map(|i|slot(&format!("large{i}"),&"\n".repeat(8192),false,i)).collect();
        let error=render_slots("agent-a",oversized,true).unwrap_err();
        assert!(error.downcast_ref::<ConfigurationError>().is_some());
        assert!(error.to_string().contains("64 KiB"));
        assert!(serde_json::from_value::<PromptArgs>(json!({"agent_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<PromptArgs>(json!({"user_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<PromptArgs>(Value::Null).is_err());
        assert!(serde_json::from_value::<PromptArgs>(json!({})).is_ok());
    }
    #[tokio::test]
    async fn session_agent_routing_isolated_from_passed_app_view_and_live_deny() {
        let (root,app)=crate::runtime_tests::fixture().await;
        // Isolated encrypted fixture only: create a second tenant and migrate
        // through the normal storage splitter, never read runtime database files.
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('prompt_tenant','Other','','openai_subscription','fixture','active',?,1,1,1)")
            .bind(app.store.seal(&json!({"owner_id":"admin","ability_revision":0})).unwrap())
            .execute(&app.store.agent_pool).await.unwrap();
        app.store.migrate_agent_storage(root.path()).await.unwrap();
        let other=app.store.for_agent("prompt_tenant").await.unwrap();
        app.store.upsert_prompt_slot("identity","DEFAULT PRIVATE",None,None,None,0).await.unwrap();
        other.upsert_prompt_slot("identity","TENANT PRIVATE",None,None,None,0).await.unwrap();
        app.store.put("session:tenant",&json!({"id":"tenant","agent_id":"prompt_tenant","title":"t","created_at":1})).await.unwrap();
        let agent=session_agent_id(&app,"tenant").await.unwrap();
        assert_eq!(agent,"prompt_tenant");
        let scoped=app.store.for_agent(&agent).await.unwrap();
        let output=render_slots(&agent,scoped.list_prompt_slots().await.unwrap(),true).unwrap();
        assert!(output.to_string().contains("TENANT PRIVATE"));
        assert!(!output.to_string().contains("DEFAULT PRIVATE"));
        app.store.put("run:read-run",&json!({"id":"read-run","session_id":"tenant",
            "agent_id":"prompt_tenant","loaded_abilities":["base"],"status":"executing"})).await.unwrap();
        // Newly installed Base tools have no implicit tier/level grant.
        let denied=execute(&app,"tenant","read-run","read_own_prompt",&json!({}),CancellationToken::new()).await.unwrap();
        assert_eq!(denied["status"],"error");
        assert!(!denied.to_string().contains("TENANT PRIVATE"));
        let malformed=execute(&app,"tenant","read-run","read_own_prompt",&json!({"agent_id":"shared_default"}),CancellationToken::new()).await.unwrap();
        assert_eq!(malformed["status"],"error");
        let cancel=CancellationToken::new();cancel.cancel();
        assert!(execute(&app,"tenant","read-run","read_own_prompt",&json!({}),cancel).await.is_err());
    }
}
