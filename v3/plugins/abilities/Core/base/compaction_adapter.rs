//! A model can request a fold, but only the loop may summarize and replace
//! context. This adapter checks a prospective completed exchange and returns a
//! control marker; the loop saves the request with the tool result.
use crate::{App, context_compaction, session_agent_id};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name:&str)->bool { name=="compact_context" }
pub fn schemas()->Vec<Value> {
    vec![json!({"type":"function","name":"compact_context",
        "description":"Request a safe summary of older current-session context at the next model boundary. Original history stays encrypted for recall_compacted.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":false}})]
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}

pub async fn execute(app:&App,sid:&str,run_id:&str,name:&str,args:&Value,
    cancel:CancellationToken)->Result<Value> {
    if !owns(name) || cancel.is_cancelled() { bail!("Context request unavailable"); }
    let result:Result<Value>=async {
        let _:Empty=serde_json::from_value(args.clone())
            .context("compact_context accepts only an empty object")?;
        let agent=session_agent_id(app,sid).await?;
        let mut scoped=app.clone();
        scoped.store=app.store.for_agent(&agent).await?;
        let run=scoped.run(run_id).await?;
        if run["session_id"]!=sid || run["agent_id"].as_str().unwrap_or("shared_default")!=agent
            || run["status"]!="running" || run["phase"]!="tools"
            || run["pending"]["name"]!="compact_context"
            || run["pending"]["state"]!="executing" {
            bail!("Context request is not the current executing call");
        }
        let call_id=run["pending"]["call_id"].as_str()
            .filter(|id|!id.is_empty()&&id.len()<=128)
            .context("Context request call ID unavailable")?;
        let config=context_compaction::Config::read(&scoped.compaction_config_path)?;
        let mut prospective=run["input"].as_array().context("Run context unavailable")?.clone();
        prospective.push(json!({"type":"function_call_output","call_id":call_id,
            "output":"{\"status\":\"success\"}"}));
        let bound=config.summary_reserve_bytes+2*1024*1024;
        let plan=context_compaction::plan_manual(&prospective,bound,&config)
            .context("No safe completed context boundary is available yet")?;
        let plan=plan.context("No earlier context is available to compact")?;
        if serde_json::to_vec(&plan.source)?.len()>config.max_source_bytes {
            bail!("Older context exceeds the configured summary-source limit");
        }
        Ok(json!({"status":"success","control":"manual_compaction_requested",
            "output":{"scheduled":true,"source_items":plan.source.len(),
                "boundary":"next_model_call"}}))
    }.await;
    if cancel.is_cancelled() { bail!("Context request cancelled"); }
    Ok(match result {
        Ok(value)=>value,
        Err(error)=>json!({"status":"error","error_kind":"compaction_request_rejected",
            "output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_has_no_target_selector() {
        assert_eq!(schemas()[0]["name"],"compact_context");
        assert!(serde_json::from_value::<Empty>(json!({"session_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<Empty>(json!({})).is_ok());
    }
}
