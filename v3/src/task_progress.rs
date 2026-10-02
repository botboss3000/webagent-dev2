//! A small, content-free view of an existing run. It reports only facts already
//! saved by the Rust loop; it does not estimate completion or infer user goals.
use serde_json::{json, Value};

const CHECKPOINTS: [&str; 8] = [
    "admission", "attachment_preparation", "context_assembly", "model_dispatch",
    "model_output", "pre_tool", "tool_result", "publication",
];

fn known_status(value: &Value) -> Option<&str> {
    match value.as_str()? {
        "queued" => Some("queued"),
        "running" => Some("running"),
        "waiting_approval" => Some("waiting_approval"),
        "paused" => Some("paused"),
        "stopped" => Some("stopped"),
        "recovery" => Some("recovery"),
        "completed" => Some("completed"),
        "cancelled" => Some("cancelled"),
        "failed" => Some("failed"),
        _ => None,
    }
}

fn known_phase(value: &Value) -> Option<&str> {
    match value.as_str()? {
        "model" => Some("model"),
        "tools" => Some("tools"),
        _ => None,
    }
}

fn pending_state(run: &Value) -> Option<&str> {
    // A pending tool is private execution state. Its name and arguments never
    // enter this public projection, even while approval is requested.
    match run.pointer("/pending/state")?.as_str()? {
        "requested" => Some("requested"),
        "waiting" => Some("waiting"),
        "executing" => Some("executing"),
        _ => None,
    }
}

fn checkpoint_decisions(run: &Value) -> Vec<Value> {
    // Receipts are one slot per checkpoint, not an append-only timeline.
    // Fixed presentation order cannot be read as execution chronology.
    CHECKPOINTS.iter().filter_map(|name| {
        let receipt = run.pointer(&format!("/workflow/receipts/{name}"))?;
        let turn = receipt["turn"].as_u64()?;
        let call_index = receipt["call_index"].as_u64()?;
        let decision = receipt.pointer("/result/decision")?.as_str()?;
        if !matches!(decision, "pass" | "reject") { return None; }
        Some(json!({"checkpoint": name, "decision": decision,
            "turn": turn, "call_index": call_index}))
    }).collect()
}

fn compaction_count(run: &Value) -> Option<u64> {
    // Older runs without this counter have not compacted. A corrupt or
    // implausibly large stored value is unknown, never a made-up capped count.
    match run.get("compactions") {
        None => Some(0),
        Some(value) => value.as_u64().filter(|count| *count <= 1_000_000),
    }
}

/// The owning session and run are checked by the API before this projection.
/// Unknown stored values become null rather than being echoed to a caller.
pub(crate) fn project(run: &Value) -> Value {
    let status = known_status(&run["status"]);
    let phase = known_phase(&run["phase"]);
    let pending = pending_state(run);
    let waiting = status == Some("waiting_approval") && pending == Some("waiting");
    json!({
        "schema_version": 1,
        "run_id": run["id"],
        "session_id": run["session_id"],
        "status": status,
        "phase": phase,
        "turn": run["turns"].as_u64(),
        "pending_approval": waiting,
        "pending_tool_state": pending,
        "uncertain_effect": run["uncertain"] == true,
        "compaction_count": compaction_count(run),
        "checkpoint_decisions": checkpoint_decisions(run),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_only_saved_state_and_bounded_receipts() {
        let run = json!({
            "id":"run-1", "session_id":"session-1", "status":"waiting_approval",
            "phase":"tools", "turns":2, "uncertain":false, "compactions":1,
            "pending":{"state":"waiting","name":"file_write",
                "arguments":{"text":"private sentinel"}},
            "workflow":{"receipts":{
                "admission":{"turn":0,"call_index":0,"result":{"decision":"pass"}},
                "model_output":{"turn":2,"call_index":0,"result":{"decision":"pass"}},
                "pre_tool":{"turn":2,"call_index":0,"result":{
                    "decision":"reject","reason":"private sentinel"}}
            }}
        });
        let value = project(&run);
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["status"], "waiting_approval");
        assert_eq!(value["pending_approval"], true);
        assert_eq!(value["compaction_count"], 1);
        let receipts = value["checkpoint_decisions"].as_array().unwrap();
        assert_eq!(receipts.len(), 3);
        let pre_tool = receipts.iter().find(|r| r["checkpoint"] == "pre_tool").unwrap();
        assert_eq!(pre_tool["decision"], "reject");
        assert_eq!(pre_tool["turn"], 2);
        assert!(!value.to_string().contains("private sentinel"));
        assert!(!value.to_string().contains("file_write"));
    }

    #[test]
    fn no_receipt_and_unknown_fields_do_not_invent_progress() {
        let run = json!({"id":"run-2","session_id":"session-1",
            "status":"future-status","phase":"future-phase","turns":0,
            "pending":{"state":"unexpected","name":"private sentinel"}});
        let value = project(&run);
        assert!(value["status"].is_null());
        assert!(value["phase"].is_null());
        assert!(value["checkpoint_decisions"].as_array().unwrap().is_empty());
        assert_eq!(value["pending_approval"], false);
        assert!(value["pending_tool_state"].is_null());
        assert_eq!(value["compaction_count"], 0);
        assert!(project(&json!({"compactions":"invalid"}))["compaction_count"].is_null());
        assert!(project(&json!({"compactions":1_000_001}))["compaction_count"].is_null());
    }
}
