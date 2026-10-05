//! Self-edit tools act only on the authenticated run's current agent.
use crate::{App, ability_controls, session_agent_id, session_mode, live_tool_decision};
use anyhow::{bail, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name: &str) -> bool {
    matches!(name, "edit_own_prompt" | "save_own_skill" | "remove_own_skill")
}
pub fn schemas() -> Vec<Value> {
    vec![
        json!({"type":"function","name":"edit_own_prompt",
            "description":"Edit one unlocked section of this agent's prompt; read_own_prompt first. Takes effect on later runs.",
            "parameters":{"type":"object","properties":{
                "slot_name":{"type":"string","minLength":1,"maxLength":128},
                "content":{"type":"string","minLength":1,"maxLength":16384},
                "mode":{"type":"string","enum":["replace","append"]},
                "expected_revision":{"type":"integer","minimum":0}},
                "required":["slot_name","content","expected_revision"],"additionalProperties":false}}),
        json!({"type":"function","name":"save_own_skill",
            "description":"Create or revise this agent's selectable reusable skill. List or load it first; use revision 0 to create.",
            "parameters":{"type":"object","properties":{
                "name":{"type":"string","minLength":1,"maxLength":64,"pattern":"^[a-z0-9_]+$"},
                "description":{"type":"string","minLength":1,"maxLength":256},
                "instructions":{"type":"string","minLength":1,"maxLength":16384},
                "mode":{"type":"string","enum":["selectable"]},
                "expected_revision":{"type":"integer","minimum":0}},
                "required":["name","expected_revision"],"additionalProperties":false}}),
        json!({"type":"function","name":"remove_own_skill",
            "description":"Remove this agent's self-authored skill by name and revision; bundled ability guidance cannot be removed.",
            "parameters":{"type":"object","properties":{
                "name":{"type":"string","minLength":1,"maxLength":64,"pattern":"^[a-z0-9_]+$"},
                "expected_revision":{"type":"integer","minimum":1}},
                "required":["name","expected_revision"],"additionalProperties":false}}),
    ]
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PromptArgs {
    slot_name: String, content: String, expected_revision: i64,
    #[serde(default="replace")] mode: String,
}
fn replace() -> String { "replace".into() }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SaveArgs {
    name: String, expected_revision: i64,
    #[serde(default)] description: Option<String>,
    #[serde(default)] instructions: Option<String>,
    #[serde(default)] mode: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoveArgs { name: String, expected_revision: i64 }
struct View { app: App, fingerprint: String }
async fn view(app: &App, sid: &str, run_id: &str, name: &str) -> Result<View> {
    // The user-owned session fixes the agent, and the run fixes the call.
    let agent = session_agent_id(app,sid).await?;
    let mut scoped = app.clone();
    scoped.store = app.store.for_agent(&agent).await?;
    let run = scoped.run(run_id).await?;
    if run["session_id"] != sid || run["agent_id"].as_str().unwrap_or("shared_default") != agent {
        bail!("Run does not belong to current session and agent");
    }
    let (policy,mode_revision,visibility,state) = ability_controls(&scoped,sid).await?;
    let mode = session_mode(&scoped,sid).await?;
    let access = scoped.store.live_access(scoped.store.current_user_id(),&agent).await?;
    if !access.admitted() || live_tool_decision(&scoped,&access,name,&policy,&visibility,
        &state,&run["loaded_abilities"],mode["mode"]=="read_only")
        .permission == crate::access::Permission::Deny {
        bail!("Self-edit tool unavailable under current access");
    }
    let fingerprint = crate::digest(&json!([agent,policy,mode_revision,visibility,state,
        mode,run["loaded_abilities"],access.context_fingerprint(),scoped.abilities.live_version()]));
    Ok(View { app: scoped, fingerprint })
}
async fn work(app: &App, sid: &str, run_id: &str, name: &str,
    args: &Value, pending: &str) -> Result<Value> {
    if !owns(name) { bail!("Unknown self-edit tool"); }
    let current = view(app,sid,run_id,name).await?;
    // Reject an access change before the transactional write; the normal
    // dispatcher also rechecks policy/approval at its safe checkpoint.
    if view(app,sid,run_id,name).await?.fingerprint != current.fingerprint {
        bail!("Self-edit access changed before write");
    }
    // Bind the idempotency receipt to the authenticated actor and run.
    let scope = crate::digest(&json!([sid,run_id,current.app.store.current_user_id(),
        current.app.store.current_agent_id()]));
    match name {
        "edit_own_prompt" => {
            let query: PromptArgs = serde_json::from_value(args.clone())?;
            current.app.store.self_edit_prompt_slot(&query.slot_name,&query.content,
                &query.mode,query.expected_revision,pending,&scope).await
        }
        "save_own_skill" => {
            let query: SaveArgs = serde_json::from_value(args.clone())?;
            current.app.store.mutate_own_skill(&query.name,query.description.as_deref(),
                query.instructions.as_deref(),query.mode.as_deref(),
                query.expected_revision,false,pending,&scope).await
        }
        "remove_own_skill" => {
            let query: RemoveArgs = serde_json::from_value(args.clone())?;
            current.app.store.mutate_own_skill(&query.name,None,None,None,
                query.expected_revision,true,pending,&scope).await
        }
        _ => unreachable!(),
    }
}
pub async fn execute(app: &App, sid: &str, run_id: &str, name: &str,
    args: &Value, pending: &str, cancel: CancellationToken) -> Result<Value> {
    if cancel.is_cancelled() { bail!("tool execution cancelled"); }
    let result = tokio::select! {
        _ = cancel.cancelled() => bail!("tool execution cancelled"),
        result = work(app,sid,run_id,name,args,pending) => result,
    };
    // Once storage commits, its receipt makes recovery idempotent. Cancellation
    // after commit cannot undo the edit; the caller must reconcile the receipt.
    Ok(match result {
        Ok(value) => value,
        Err(error) => json!({"status":"error","error_kind":"self_edit_rejected",
            "output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selectors_cannot_target_other_agents_or_skip_revisions() {
        assert!(serde_json::from_value::<PromptArgs>(json!({"slot_name":"style",
            "content":"brief","expected_revision":1,"agent_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<SaveArgs>(json!({"name":"how_to",
            "expected_revision":0,"user_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<RemoveArgs>(json!({"name":"how_to",
            "expected_revision":1,"ability_id":"foreign"})).is_err());
        assert_eq!(schemas().len(),3);
    }
}
