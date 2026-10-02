//! A checkpoint is a named stop in a run, such as admission, a tool request, or
//! final publication. Each has typed facts and a recorded decision. Optional
//! modules may advise at supported stops, but cannot bypass Rust's controls.
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
pub struct ModelSnapshot {
    pub input_digest: String,
    pub instructions_digest: String,
    pub schemas_digest: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreToolInput {
    pub name: String,
    pub arguments_digest: String,
    pub policy_version: u64,
    pub mode_revision: u64,
    pub visibility_revision: u64,
    pub ability_state_revision: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolResultInput {
    pub status: String,
    pub result_digest: String,
}
/// Gate-specific facts, without raw model, client, or tool payloads.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "checkpoint", content = "facts", rename_all = "snake_case")]
pub enum Input {
    Admission { request_digest: String },
    AttachmentPreparation { attachment_count: usize },
    ContextAssembly(ModelSnapshot),
    ModelDispatch(ModelSnapshot),
    ModelOutput { output_digest: String },
    PreTool(PreToolInput),
    ToolResult(ToolResultInput),
    Publication { text_digest: String },
}
impl Input {
    pub fn checkpoint(&self) -> Checkpoint {
        match self {
            Self::Admission { .. } => Checkpoint::Admission,
            Self::AttachmentPreparation { .. } => Checkpoint::AttachmentPreparation,
            Self::ContextAssembly(_) => Checkpoint::ContextAssembly,
            Self::ModelDispatch(_) => Checkpoint::ModelDispatch,
            Self::ModelOutput { .. } => Checkpoint::ModelOutput,
            Self::PreTool(_) => Checkpoint::PreTool,
            Self::ToolResult(_) => Checkpoint::ToolResult,
            Self::Publication { .. } => Checkpoint::Publication,
        }
    }
}
pub fn digest_input(input: &Input) -> Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(input)?)))
}

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
        let defaults = crate::loop_runtime::Config::default();
        let mut value = json!({"schema_version":1,"max_turns":defaults.workflow.max_turns,
            "max_attempts":defaults.workflow.max_attempts,"max_context_bytes":defaults.context.max_context_bytes,
            "summary_reserve_bytes":defaults.context.summary_reserve_bytes,
            "max_publication_bytes":defaults.workflow.max_publication_bytes,"modules":defaults.workflow.modules});
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
            if modules.is_empty() { continue; }
            if checkpoint != "publication" || modules.len() != 1
                || !matches!(modules[0].as_str(), "publication_size_guard_v1"
                    | "optional:publication_size_guard_v1") {
                bail!("Optional workflow module is not installed at this checkpoint");
            }
        }
        Ok(policy)
    }
    pub fn revision(&self) -> Result<String> {
        Ok(hex::encode(Sha256::digest(serde_json::to_vec(self)?)))
    }
}

/// Receipts are private run state, committed with the surrounding transition.
/// Include generation, policy revision and exact typed input identity.
pub fn record(run: &mut Value, policy: &Policy, input: &Input, decision: Decision) -> Result<()> {
    let checkpoint = input.checkpoint();
    let input_digest = digest_input(input)?;
    let name = serde_json::to_value(checkpoint)?.as_str().unwrap().to_owned();
    if run.get("workflow").is_none() { run["workflow"] = json!({"schema_version":1,"receipts":{}}); }
    run["workflow"]["receipts"][&name] = json!({
        "checkpoint": checkpoint, "generation":run["generation"],
        "policy_revision":policy.revision()?, "input_digest":input_digest,
        "turn":run["turns"], "call_index":run["call_index"], "result":decision
    });
    Ok(())
}

/// The tool result inherits the authorized dispatch identity, even after a
/// restart changes the run generation or effective policy.
pub fn record_tool_result(run: &mut Value, result: &Value) -> Result<()> {
    if let Some(previous) = run.pointer("/workflow/receipts/pre_tool").cloned() {
        let input = Input::ToolResult(ToolResultInput {
            status: result["status"].as_str().unwrap_or("unknown").to_owned(),
            result_digest: hex::encode(Sha256::digest(serde_json::to_vec(result)?)),
        });
        let mut receipt = previous;
        receipt["checkpoint"] = json!(Checkpoint::ToolResult);
        receipt["input_digest"] = json!(digest_input(&input)?);
        receipt["result"] = if result["status"] == "denied" {
            json!(Decision::Reject { reason: "Tool invocation denied".into() })
        } else {
            json!(Decision::Pass)
        };
        run["workflow"]["receipts"]["tool_result"] = receipt;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn pre_tool(name: &str) -> Input {
        Input::PreTool(PreToolInput { name:name.into(), arguments_digest:"digest".into(),
            policy_version:1, mode_revision:0, visibility_revision:0, ability_state_revision:0 })
    }
    #[test] fn defaults_and_tightening() {
        assert_eq!(Policy::resolve(None).unwrap().max_turns,50);
        assert_eq!(Policy::resolve(Some(&json!({"max_turns":2}))).unwrap().max_turns,2);
    }
    #[test] fn rejects_unsafe_or_unknown_configuration() {
        for value in [json!({"max_turns":51}),json!({"schema_version":2}),
            json!({"disable_authorization":true}),json!({"modules":{"pre_tool":["unknown"]}}),
            json!({"modules":{"pre_tool":["publication_size_guard_v1"]}}),
            json!({"modules":{"publication":["unknown"]}}),
            json!({"modules":{"publication":["publication_size_guard_v1","publication_size_guard_v1"]}}),
            json!({"modules":{"arbitrary_gate":[]}})] {
            assert!(Policy::resolve(Some(&value)).is_err());
        }
    }
    #[test] fn publication_module_modes_are_explicit_and_bounded() {
        for name in ["publication_size_guard_v1", "optional:publication_size_guard_v1"] {
            let policy=Policy::resolve(Some(&json!({"modules":{"publication":[name]}}))).unwrap();
            assert_eq!(policy.modules["publication"],vec![name]);
        }
    }
    #[test] fn receipts_bind_policy_generation_and_input() {
        let p=Policy::resolve(None).unwrap();
        let mut run=json!({"generation":"g1","turns":1,"call_index":0});
        record(&mut run,&p,&pre_tool("read"),Decision::Pass).unwrap();
        let first=run["workflow"]["receipts"]["pre_tool"].clone();
        run["generation"]=json!("g2");
        record(&mut run,&p,&pre_tool("write"),Decision::Pass).unwrap();
        assert_ne!(first["input_digest"],run["workflow"]["receipts"]["pre_tool"]["input_digest"]);
        assert_eq!(run["workflow"]["receipts"]["pre_tool"]["generation"],"g2");
    }
    #[test] fn gate_inputs_are_typed_and_cannot_be_mislabeled() {
        let input=Input::Publication { text_digest:"accepted".into() };
        assert_eq!(input.checkpoint(),Checkpoint::Publication);
        assert!(serde_json::from_value::<Input>(json!({
            "checkpoint":"publication","facts":{"text":"raw untyped content"}})).is_err());
    }
}
