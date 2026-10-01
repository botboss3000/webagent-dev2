//! Fixed checkpoint contracts. Configuration cannot reorder or bypass native controls.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Checkpoint {
    Admission, AttachmentPreparation, ContextAssembly, ModelDispatch,
    ModelOutput, PreTool, ToolResult, Publication,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case")]
pub enum Decision { Pass, Reject { reason: String } }

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub schema_version: u32,
    pub max_turns: u64,
    pub max_attempts: u64,
    pub max_context_bytes: usize,
    pub summary_reserve_bytes: usize,
    pub max_publication_bytes: usize,
    pub modules: BTreeMap<String, Vec<String>>,
}
impl Policy {
    pub fn resolve(overrides: Option<&Value>) -> Result<Self> {
        let mut value: Value = serde_json::from_str(include_str!("workflow.defaults.json"))?;
        if let Some(overrides) = overrides {
            let fields = overrides.as_object().ok_or_else(|| anyhow::anyhow!("Policy must be an object"))?;
            for (key, field) in fields { value[key] = field.clone(); }
        }
        let policy: Self = serde_json::from_value(value)?;
        if policy.schema_version != 1 || !(1..=50).contains(&policy.max_turns)
            || !(1..=3).contains(&policy.max_attempts)
            || !(1024..=262144).contains(&policy.max_context_bytes)
            || policy.summary_reserve_bytes == 0
            || policy.summary_reserve_bytes >= policy.max_context_bytes
            || !(1..=262144).contains(&policy.max_publication_bytes) {
            bail!("Invalid workflow policy or operator ceiling exceeded");
        }
        for (checkpoint, modules) in &policy.modules {
            let _: Checkpoint = serde_json::from_value(json!(checkpoint))?;
            // No executable optional modules shipped in Phase 1. Never silently
            // accept an unknown configured module or move it to another gate.
            if !modules.is_empty() { bail!("Optional workflow module is not installed"); }
        }
        Ok(policy)
    }
    pub fn revision(&self) -> Result<String> {
        Ok(hex::encode(Sha256::digest(serde_json::to_vec(self)?)))
    }
}

/// Receipts are private run state, committed with the surrounding transition.
/// Include generation, policy revision and exact input identity for recovery/audit.
pub fn record(run: &mut Value, checkpoint: Checkpoint, policy: &Policy,
              input: &Value, decision: Decision) -> Result<()> {
    let input_digest = hex::encode(Sha256::digest(serde_json::to_vec(input)?));
    let name = serde_json::to_value(checkpoint)?.as_str().unwrap().to_owned();
    if run.get("workflow").is_none() { run["workflow"] = json!({"schema_version":1,"receipts":{}}); }
    run["workflow"]["receipts"][&name] = json!({
        "checkpoint": checkpoint, "generation":run["generation"],
        "policy_revision":policy.revision()?, "input_digest":input_digest,
        "turn":run["turns"], "call_index":run["call_index"], "result":decision
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn defaults_and_tightening() {
        assert_eq!(Policy::resolve(None).unwrap().max_turns,50);
        assert_eq!(Policy::resolve(Some(&json!({"max_turns":2}))).unwrap().max_turns,2);
    }
    #[test] fn rejects_unsafe_or_unknown_configuration() {
        for value in [json!({"max_turns":51}),json!({"schema_version":2}),
            json!({"disable_authorization":true}),json!({"modules":{"pre_tool":["unknown"]}}),
            json!({"modules":{"arbitrary_gate":[]}})] {
            assert!(Policy::resolve(Some(&value)).is_err());
        }
    }
    #[test] fn receipts_bind_policy_generation_and_input() {
        let p=Policy::resolve(None).unwrap();
        let mut run=json!({"generation":"g1","turns":1,"call_index":0});
        record(&mut run,Checkpoint::PreTool,&p,&json!({"tool":"read"}),Decision::Pass).unwrap();
        let first=run["workflow"]["receipts"]["pre_tool"].clone();
        run["generation"]=json!("g2");
        record(&mut run,Checkpoint::PreTool,&p,&json!({"tool":"write"}),Decision::Pass).unwrap();
        assert_ne!(first["input_digest"],run["workflow"]["receipts"]["pre_tool"]["input_digest"]);
        assert_eq!(run["workflow"]["receipts"]["pre_tool"]["generation"],"g2");
    }
}
