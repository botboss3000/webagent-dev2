use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::sync::OnceLock;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
};
use tokio_util::sync::CancellationToken;

pub fn is_read(name: &str) -> bool {
    matches!(name, "file_list" | "file_read" | "file_search")
}

pub fn schemas() -> Vec<Value> {
    static SCHEMAS: OnceLock<Vec<Value>> = OnceLock::new();
    SCHEMAS.get_or_init(|| [
        ("file_list", "List files in the session workspace", json!({"path":{"type":"string"}}), vec![]),
        ("file_read", "Read a workspace file", json!({"path":{"type":"string"}}), vec!["path"]),
        ("file_search", "Search workspace file content", json!({"path":{"type":"string"},"query":{"type":"string"}}), vec!["query"]),
        ("file_write", "Write a workspace file; requires approval", json!({"path":{"type":"string"},"content":{"type":"string"}}), vec!["path","content"]),
        ("shell", "Execute a shell command in the workspace; requires approval", json!({"command":{"type":"string"}}), vec!["command"]),
        ("python", "Execute Python in the workspace; requires approval", json!({"code":{"type":"string"}}), vec!["code"]),
    ].into_iter().map(|(name, description, properties, required)| json!({
        "type":"function", "name":name, "description":description,
        "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}
    })).collect()).clone()
}

pub async fn execute(
    name: &str,
    args: &Value,
    session: &str,
    cancel: CancellationToken,
) -> Result<Value> {
    if !matches!(
        name,
        "file_list" | "file_read" | "file_search" | "file_write" | "shell" | "python"
    ) {
        bail!("unknown tool");
    }
    uuid::Uuid::parse_str(session).context("invalid workspace identifier")?;
    if cancel.is_cancelled() {
        bail!("tool execution cancelled");
    }
    let socket =
        std::env::var("V3_TOOL_SOCKET").unwrap_or_else(|_| "/run/v3/worker/tools.sock".into());
    let mut stream = UnixStream::connect(socket)
        .await
        .context("tool worker unavailable")?;
    let request = json!({"id":uuid::Uuid::new_v4().to_string(),"name":name,
        "arguments":args,"workspace":session,"timeout_seconds":120});
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
            Ok(serde_json::from_str::<Value>(&response)?)
        } => result
    }
}
