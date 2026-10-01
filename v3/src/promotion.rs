use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
};
use tokio_util::sync::CancellationToken;

const MAX_PATCH: usize = 1024 * 1024;

pub async fn apply(socket: &Path, patch: &str, cancel: CancellationToken) -> Result<Value> {
    if patch.is_empty() || patch.len() > MAX_PATCH {
        bail!("Invalid promotion patch length");
    }
    let id = uuid::Uuid::new_v4().to_string();
    let patch_sha256 = hex::encode(Sha256::digest(patch.as_bytes()));
    let request = json!({"id":id,"patch":patch,"patch_sha256":patch_sha256});
    let mut encoded = serde_json::to_vec(&request)?;
    if encoded.len() > 3 * MAX_PATCH {
        bail!("Promotion request exceeds limit");
    }
    encoded.push(b'\n');
    let mut stream = UnixStream::connect(socket)
        .await
        .context("Promotion broker unavailable")?;
    let exchange = async {
        stream.write_all(&encoded).await?;
        let mut reader = BufReader::new(stream).take((3 * MAX_PATCH) as u64);
        let mut line = String::new();
        reader.read_line(&mut line).await?;
        if line.is_empty() {
            bail!("Promotion broker disconnected");
        }
        let reply: Value = serde_json::from_str(&line)?;
        if reply["id"].as_str() != Some(id.as_str()) {
            bail!("Promotion broker response identifier mismatch");
        }
        if !matches!(
            reply["status"].as_str(),
            Some("success" | "denied" | "uncertain")
        ) {
            bail!("Invalid promotion broker status");
        }
        if reply["status"] == "success" && reply["patch_sha256"] != patch_sha256 {
            bail!("Promotion broker patch digest mismatch");
        }
        Ok(reply)
    };
    tokio::select! {
        _ = cancel.cancelled() => bail!("Promotion cancelled"),
        result = tokio::time::timeout(Duration::from_secs(90), exchange) =>
            result.context("Promotion broker timed out")?,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;
    use tokio::net::UnixListener;

    #[tokio::test]
    async fn checks_broker_reply_identifier_and_digest() {
        let root = tempdir().unwrap();
        let path = root.path().join("promote.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let broker = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            let reply = json!({"id":request["id"],"status":"success",
                "patch_sha256":request["patch_sha256"],"paths":["src/main.rs"]});
            reader
                .get_mut()
                .write_all(format!("{reply}\n").as_bytes())
                .await
                .unwrap();
        });
        let reply = apply(&path, "patch", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(reply["status"], "success");
        broker.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_wrong_broker_reply() {
        let root = tempdir().unwrap();
        let path = root.path().join("promote.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let broker = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            reader
                .get_mut()
                .write_all(b"{\"id\":\"wrong\",\"status\":\"success\"}\n")
                .await
                .unwrap();
        });
        assert!(apply(&path, "patch", CancellationToken::new())
            .await
            .is_err());
        broker.await.unwrap();
    }
}
