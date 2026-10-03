//! A model can change only the authenticated run's own session mode.
//! Elevating read_only to write always needs a fresh human approval.
use crate::{api, session_agent_id, App};
use anyhow::{bail, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name: &str) -> bool { name == "set_execution_mode" }
pub fn schemas() -> Vec<Value> {
    vec![json!({"type":"function","name":"set_execution_mode",
        "description":"Change this session between read_only and write at a revision checkpoint. Moving to write requires human approval.",
        "parameters":{"type":"object","properties":{
            "mode":{"type":"string","enum":["read_only","write"]},
            "expected_revision":{"type":"integer","minimum":0},
            "reason":{"type":"string","maxLength":500}},
            "required":["mode","expected_revision"],"additionalProperties":false}})]
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args { mode:String, expected_revision:u64, #[serde(default)] reason:Option<String> }
pub async fn execute(app:&App,sid:&str,run_id:&str,generation:&str,pending_id:&str,name:&str,args:&Value,
    cancel:CancellationToken)->Result<Value> {
    if !owns(name) || cancel.is_cancelled() {bail!("Mode change unavailable");}
    let result:Result<Value>=async {
        let query:Args=serde_json::from_value(args.clone())?;
        if !matches!(query.mode.as_str(),"read_only"|"write")
            || query.reason.as_ref().is_some_and(|r|r.len()>500) {
            bail!("Invalid mode change");
        }
        let agent=session_agent_id(app,sid).await?;
        let mut scoped=app.clone();
        scoped.store=app.store.for_agent(&agent).await?;
        let response=api::change_session_mode(&scoped,sid,&query.mode,
            Some(query.expected_revision),Some(api::ModeCall{run_id,generation,pending_id})).await
            .map_err(|_|anyhow::anyhow!("Session mode revision or access changed"))?.0;
        Ok(json!({"status":"success","output":response}))
    }.await;
    Ok(match result {
        Ok(value)=>value,
        Err(error)=>json!({"status":"error","error_kind":"mode_change_rejected",
            "output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_cannot_select_another_session() {
        assert_eq!(schemas()[0]["name"],"set_execution_mode");
        assert!(serde_json::from_value::<Args>(json!({"mode":"write",
            "expected_revision":0,"session_id":"other"})).is_err());
    }
    #[tokio::test]
    async fn mode_receipt_and_upgrade_approval_are_run_fenced() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let mut tier=app.store.entitlement_tier("operator").await.unwrap().unwrap();
        tier.ability_permissions.insert("base".into(),crate::access::Permission::Auto);
        let tier_revision=tier.revision;
        app.store.save_entitlement_tier(tier,tier_revision).await.unwrap();
        let before=app.store.live_access("admin","shared_default").await.unwrap();
        let mut level=before.capability_level;
        level.ability_permissions.insert("base".into(),crate::access::Permission::Auto);
        app.store.save_capability_level("shared_default",level,0).await.unwrap();
        let sid=crate::uuid();
        app.store.put(&format!("session:{sid}"),&json!({"id":sid,
            "agent_id":"shared_default","title":"Mode test","created_at":crate::now()})).await.unwrap();
        app.store.put(&format!("session_ability_state:{sid}"),
            &json!({"revision":1,"active":["base"],"suppressed":[]})).await.unwrap();
        let (policy,mode_revision,visibility,state)=crate::ability_controls(&app,&sid).await.unwrap();
        let access=app.store.live_access("admin","shared_default").await.unwrap();
        let rid=crate::uuid();let pending_id=crate::uuid();
        let mut run=json!({"id":rid,"session_id":sid,"agent_id":"shared_default",
            "status":"running","phase":"tools","generation":"generation-one",
            "loaded_abilities":["base"],"pending":{"id":pending_id,
                "name":"set_execution_mode","state":"executing",
                "policy_version":policy["version"],"mode_revision":mode_revision,
                "visibility_revision":visibility["revision"],
                "ability_state_revision":state["revision"],
                "access_revision":access.revision,"human_approved":false}});
        app.store.put(&format!("run:{rid}"),&run).await.unwrap();
        let call=api::ModeCall{run_id:&rid,generation:"generation-one",pending_id:&pending_id};
        let first=api::change_session_mode(&app,&sid,"read_only",Some(0),Some(call)).await.unwrap().0;
        assert_eq!(first["revision"],1);
        assert_eq!(api::change_session_mode(&app,&sid,"read_only",Some(0),Some(call)).await.unwrap().0,first);
        let forged=api::ModeCall{run_id:&rid,generation:"stale",pending_id:&pending_id};
        assert!(api::change_session_mode(&app,&sid,"read_only",Some(0),Some(forged)).await.is_err());
        let next_id=crate::uuid();
        run["pending"]["id"]=json!(next_id);
        run["pending"]["mode_revision"]=json!(1);
        app.store.put(&format!("run:{rid}"),&run).await.unwrap();
        let next=api::ModeCall{run_id:&rid,generation:"generation-one",pending_id:&next_id};
        assert!(api::change_session_mode(&app,&sid,"write",Some(1),Some(next)).await.is_err());
        run["pending"]["human_approved"]=json!(true);
        app.store.put(&format!("run:{rid}"),&run).await.unwrap();
        assert_eq!(api::change_session_mode(&app,&sid,"write",Some(1),Some(next)).await.unwrap().0["revision"],2);
        assert!(api::change_session_mode(&app,&sid,"write",Some(1),Some(next)).await.is_ok());
    }
}
