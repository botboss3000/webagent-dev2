use anyhow::{bail, Context, Result};
// Single and parallel turns share the same Rust dispatch for native and worker tools.
#[path = "../../../plugins/abilities/Web/web_access/adapter.rs"]
pub(crate) mod web_access;
#[path = "../../../plugins/abilities/Core/base/adapter.rs"]
pub(crate) mod core_utilities;
#[path = "../../../plugins/abilities/Core/base/session_adapter.rs"]
pub(crate) mod core_session;
#[path = "../../../plugins/abilities/Core/base/skills_adapter.rs"]
pub(crate) mod core_skills;
#[path = "../../../plugins/abilities/Core/base/prompt_adapter.rs"]
pub(crate) mod core_prompt;
#[path = "../../../plugins/abilities/Core/base/search_adapter.rs"]
pub(crate) mod core_search;
#[path = "../../../plugins/abilities/Core/base/self_edit_adapter.rs"]
pub(crate) mod core_self_edit;
#[path = "../../../plugins/abilities/Core/base/task_adapter.rs"]
pub(crate) mod core_task;
#[path = "../../../plugins/abilities/Core/base/mode_adapter.rs"]
pub(crate) mod core_mode;
#[path = "../../../plugins/abilities/Core/base/compaction_adapter.rs"]
pub(crate) mod core_compaction;
#[path = "../../../plugins/abilities/Core/base/recall_adapter.rs"]
pub(crate) mod core_recall;
mod dispatch;
pub(crate) mod administrator;
pub(crate) use dispatch::call;
use serde_json::{json, Value};
use std::sync::OnceLock;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
};
use tokio_util::sync::CancellationToken;

pub fn is_read(name: &str) -> bool {
    matches!(
        name,
        "get_time"
            | "get_date"
            | "calculate"
            | "read_attachment"
            | "search_this_session"
            | "list_skills"
            | "load_skill"
            | "read_own_prompt"
            | "session_search"
            | "similar_sessions"
            | "get_task_checkpoint"
            | "compact_context"
            | "recall_compacted"
            | "web_search"
            | "get_weather"
            | "maps_geocode"
            | "file_list"
            | "file_read"
            | "file_search"
            | "source_review"
            | "state_inspect"
            | "prompt_slot_read"
            | "set_execution_mode"
            | "code_index_lookup"
            | "code_index_coverage"
            | "code_index_trace"
            | "code_index_progress"
            | "terminal_read"
            | "terminal_wait"
            | "terminal_list"
            | "v3_workspace_status"
            | "v3_workspace_receipt"
            | "v3_build_status"
            | "v3_worker_job"
            | "read_diagnostics"
            | "dev_browser_url"
            | "get_tunnel_url"
            | "repository_access"
    )
}

pub fn schemas() -> Vec<Value> {
    static SCHEMAS: OnceLock<Vec<Value>> = OnceLock::new();
    SCHEMAS.get_or_init(|| { let mut schemas: Vec<Value> = [
        ("file_list", "List files in the session workspace", json!({"path":{"type":"string"}}), vec![]),
        ("file_read", "Read a bounded page of workspace file lines", json!({"path":{"type":"string"},"offset":{"type":"integer","minimum":1,"maximum":10000000},"limit":{"type":"integer","minimum":1,"maximum":2000}}), vec!["path"]),
        ("file_search", "Search workspace files by literal or regex query; outline comments in one file", json!({"path":{"type":"string"},"query":{"type":"string"},"regex":{"type":"boolean"},"file_glob":{"type":"string"},"comments_only":{"type":"boolean"}}), vec![]),
        ("file_write", "Write a workspace file; requires approval", json!({"path":{"type":"string"},"content":{"type":"string"}}), vec!["path","content"]),
        ("file_edit", "Replace exactly one matching span in an existing workspace file; requires approval", json!({"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"},"expected_sha256":{"type":"string"}}), vec!["path","old_text","new_text"]),
        ("workspace_seed", "Copy the audited WebAgent v3 source snapshot into this session workspace and create a Git baseline; requires approval", json!({}), vec![]),
        ("source_review", "Review tracked and new changes in the seeded v3 source without modifying its Git index", json!({}), vec![]),
        ("prompt_slot_read", "List or read the current agent's ordered instruction slots", json!({"action":{"type":"string","enum":["list","get"]},"slot_name":{"type":"string"}}), vec!["action"]),
        ("prompt_slot_write", "Create, update or delete a current-agent instruction slot; requires approval", json!({"action":{"type":"string","enum":["put","delete"]},"slot_name":{"type":"string"},"content":{"type":"string"},"expected_revision":{"type":"integer","minimum":0},"order_index":{"type":"integer"},"lock":{"type":"boolean"},"merge_mode":{"type":"string","enum":["replace","append"]}}), vec!["action","slot_name","expected_revision"]),
        ("state_inspect", "Read bounded public metadata for the current session; catalog, session, chat, or runs", json!({"resource":{"type":"string","enum":["catalog","session","chat","runs"]},"after":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":50}}), vec!["resource"]),
        ("source_patch", "Apply a checked text patch to the seeded v3 source; requires approval", json!({"patch":{"type":"string"},"expected_review_sha256":{"type":"string"}}), vec!["patch","expected_review_sha256"]),
        ("promotion_propose", "Submit the current reviewed v3 source patch for administrator approval; does not change live source", json!({"expected_review_sha256":{"type":"string"},"summary":{"type":"string"}}), vec!["expected_review_sha256","summary"]),
        ("shell", "Execute a shell command in the workspace; requires approval", json!({"command":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":600}}), vec!["command"]),
        ("python", "Execute Python in the workspace; requires approval", json!({"code":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":600}}), vec!["code"]),
    ].into_iter().map(|(name, description, properties, required)| json!({
        "type":"function", "name":name, "description":description,
        "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}
    })).collect();
        schemas.extend(administrator::schemas());
        schemas.extend(web_access::schemas());
        schemas.extend(core_utilities::schemas());
        schemas.extend(core_session::schemas());
        schemas.extend(core_skills::schemas());
        schemas.extend(core_prompt::schemas());
        schemas.extend(core_search::schemas());
        schemas.extend(core_self_edit::schemas());
        schemas.extend(core_task::schemas());
        schemas.extend(core_compaction::schemas());
        schemas.extend(core_recall::schemas());
        schemas.extend(core_mode::schemas());
        schemas
    }).clone()
}

pub async fn execute(
    name: &str,
    args: &Value,
    session: &str,
    socket: &std::path::Path,
    cancel: CancellationToken,
) -> Result<Value> {
    if !matches!(
        name,
        "file_list"
            | "file_read"
            | "file_search"
            | "file_write"
            | "file_edit"
            | "workspace_seed"
            | "source_review"
            | "source_patch"
            | "shell"
            | "python"
            | "code_index_scan"
            | "code_index_lookup"
            | "code_index_coverage"
            | "code_index_trace"
            | "code_index_progress"
            | "git_tool"
            | "resolve_conflict"
            | "commit_and_push"
            | "terminal_open"
            | "terminal_read"
            | "terminal_send"
            | "terminal_wait"
            | "terminal_list"
            | "terminal_close"
            | "v3_workspace_status"
            | "v3_workspace_action"
            | "v3_cargo_task"
    ) {
        bail!("unknown tool");
    }
    execute_trusted(name,args,session,socket,None,cancel).await
}

// Only Rust supplies trusted browser/host metadata; it is not a model schema.
pub(crate) async fn execute_trusted(
    name:&str,args:&Value,session:&str,socket:&std::path::Path,
    trusted:Option<Value>,cancel:CancellationToken,
)->Result<Value> {
    uuid::Uuid::parse_str(session).context("invalid workspace identifier")?;
    if cancel.is_cancelled() {
        bail!("tool execution cancelled");
    }
    let mut stream = UnixStream::connect(socket)
        .await
        .context("tool worker unavailable")?;
    let timeout = args["timeout_seconds"]
        .as_u64()
        .unwrap_or(120)
        .clamp(1, 600);
    let request_id = uuid::Uuid::new_v4().to_string();
    let request = json!({"id":request_id,"name":name,
        "arguments":args,"workspace":session,"timeout_seconds":timeout,"trusted":trusted});
    let mut encoded = serde_json::to_vec(&request)?;
    if encoded.len() > 1024 * 1024 {
        bail!("tool input exceeds limit");
    }
    encoded.push(b'\n');
    tokio::select! {
        _ = cancel.cancelled() => bail!("tool execution cancelled"),
        result = async {
            stream.write_all(&encoded).await?;
            let mut reader = BufReader::new(stream).take(2 * 1024 * 1024);
            let mut response = String::new();
            reader.read_line(&mut response).await?;
            if response.is_empty() { bail!("tool worker disconnected"); }
            let value = serde_json::from_str::<Value>(&response)?;
            if value["id"].as_str() != Some(request_id.as_str()) {
                bail!("tool response identifier mismatch");
            }
            Ok(value)
        } => result
    }
}
