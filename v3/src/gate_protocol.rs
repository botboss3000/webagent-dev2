//! Speak to the optional Python checker over a small, versioned local message.
//! It receives reply metadata, not the reply text. Rust still makes the final
//! decision and is the only process that can publish an assistant message.
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{path::Path, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    time::timeout,
};

pub const MODULE_ID: &str = "publication_size_guard_v1";
pub const SOCKET_PATH: &str = "/run/v3/gate/gate.sock";
const PROTOCOL_VERSION: u32 = 1;
const MAX_FRAME: usize = 16 * 1024;
const GATE_UID: u32 = 1102;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GateOutcome {
    Pass,
    Reject { reason_code: String },
    Skipped { reason_code: String },
}

#[derive(Debug, Clone)]
pub struct PublicationGateInput {
    pub run_id: String,
    pub generation: String,
    pub policy_revision: String,
    pub input_digest: String,
    pub turn: u64,
    pub call_index: u64,
    pub text_bytes: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ErrorPolicy { Mandatory, Optional }

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct Request<'a> {
    protocol_version: u32,
    request_id: String,
    module_id: &'static str,
    checkpoint: &'static str,
    run_id: &'a str,
    generation: &'a str,
    policy_revision: &'a str,
    input_digest: &'a str,
    turn: u64,
    call_index: u64,
    facts: Facts,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct Facts { text_bytes: usize }

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Response {
    protocol_version: u32,
    request_id: String,
    module_id: String,
    decision: Decision,
    reason_code: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Decision { Pass, Reject }

/// The installed module is opt-in. An explicit rejection always rejects;
/// optional mode skips only transport/protocol failures and records that skip.
pub async fn evaluate_publication(
    socket: &Path,
    policy: &crate::workflow::Policy,
    input: &PublicationGateInput,
) -> Result<GateOutcome> {
    let Some(modules) = policy.modules.get("publication") else {
        return Ok(GateOutcome::Pass);
    };
    let Some(configured) = modules.first() else {
        return Ok(GateOutcome::Pass);
    };
    let error_policy = match configured.as_str() {
        MODULE_ID => ErrorPolicy::Mandatory,
        "optional:publication_size_guard_v1" => ErrorPolicy::Optional,
        _ => bail!("Uninstalled publication module"),
    };
    if modules.len() != 1 {
        bail!("Only one publication module is installed");
    }
    match timeout(
        Duration::from_secs(1),
        exchange(socket, input, GATE_UID),
    ).await {
        Ok(Ok(outcome)) => Ok(outcome),
        Ok(Err(_)) | Err(_) => match error_policy {
            ErrorPolicy::Mandatory => Ok(GateOutcome::Reject {
                reason_code: "module_unavailable".into(),
            }),
            ErrorPolicy::Optional => Ok(GateOutcome::Skipped {
                reason_code: "module_unavailable".into(),
            }),
        },
    }
}

async fn exchange(
    socket: &Path,
    input: &PublicationGateInput,
    expected_uid: u32,
) -> Result<GateOutcome> {
    let mut stream = UnixStream::connect(socket).await.context("gate unavailable")?;
    if stream.peer_cred()?.uid() != expected_uid {
        bail!("gate peer identity mismatch");
    }
    let identity = serde_json::to_vec(&(
        &input.run_id, &input.generation, &input.policy_revision,
        &input.input_digest, input.turn, input.call_index,
    ))?;
    let request_id = hex::encode(Sha256::digest(identity));
    let request = Request {
        protocol_version: PROTOCOL_VERSION,
        request_id: request_id.clone(),
        module_id: MODULE_ID,
        checkpoint: "publication",
        run_id: &input.run_id,
        generation: &input.generation,
        policy_revision: &input.policy_revision,
        input_digest: &input.input_digest,
        turn: input.turn,
        call_index: input.call_index,
        facts: Facts { text_bytes: input.text_bytes },
    };
    let mut encoded = serde_json::to_vec(&request)?;
    if encoded.len() >= MAX_FRAME {
        bail!("gate request exceeds limit");
    }
    encoded.push(b'\n');
    stream.write_all(&encoded).await?;
    let mut frame = Vec::new();
    BufReader::new(stream.take((MAX_FRAME + 1) as u64))
        .read_until(b'\n', &mut frame).await?;
    if frame.len() > MAX_FRAME || frame.last() != Some(&b'\n') {
        bail!("invalid gate frame");
    }
    let response: Response = serde_json::from_slice(&frame)?;
    if response.protocol_version != PROTOCOL_VERSION
        || response.request_id != request_id
        || response.module_id != MODULE_ID {
        bail!("gate response identity mismatch");
    }
    match (response.decision, response.reason_code) {
        (Decision::Pass, None) => Ok(GateOutcome::Pass),
        (Decision::Reject, Some(reason)) if reason == "publication_size_exceeded" =>
            Ok(GateOutcome::Reject { reason_code: reason }),
        _ => bail!("invalid gate decision"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::MetadataExt;
    use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, net::UnixListener};

    fn input() -> PublicationGateInput {
        PublicationGateInput {
            run_id: "run".into(), generation: "gen".into(),
            policy_revision: "policy".into(), input_digest: "digest".into(),
            turn: 1, call_index: 0, text_bytes: 12,
        }
    }

    async fn mock(response: impl FnOnce(&serde_json::Value) -> serde_json::Value + Send + 'static)
        -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("gate.sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let mut line = String::new();
            let mut reader = BufReader::new(socket);
            reader.read_line(&mut line).await.unwrap();
            let request: serde_json::Value = serde_json::from_str(&line).unwrap();
            let body = response(&request);
            reader.get_mut().write_all(format!("{body}\n").as_bytes()).await.unwrap();
        });
        (dir, path)
    }

    #[tokio::test]
    async fn accepted_response_binds_request_identity() {
        let (_dir, path) = mock(|r| json!({"protocol_version":1,
            "request_id":r["request_id"],"module_id":MODULE_ID,"decision":"pass",
            "reason_code":null})).await;
        let peer_uid = std::fs::metadata(&path).unwrap().uid();
        assert_eq!(exchange(&path, &input(), peer_uid).await.unwrap(),
            GateOutcome::Pass);
    }

    #[tokio::test]
    async fn explicit_reject_is_never_skipped() {
        let (_dir, path) = mock(|r| json!({"protocol_version":1,
            "request_id":r["request_id"],"module_id":MODULE_ID,"decision":"reject",
            "reason_code":"publication_size_exceeded"})).await;
        let peer_uid = std::fs::metadata(&path).unwrap().uid();
        assert_eq!(exchange(&path, &input(), peer_uid).await.unwrap(),
            GateOutcome::Reject { reason_code:"publication_size_exceeded".into() });
    }

    #[tokio::test]
    async fn unavailable_gate_obeys_configured_error_policy() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("missing.sock");
        let mandatory = crate::workflow::Policy::resolve(Some(
            &json!({"modules":{"publication":[MODULE_ID]}}))).unwrap();
        let optional = crate::workflow::Policy::resolve(Some(
            &json!({"modules":{"publication":["optional:publication_size_guard_v1"]}}))).unwrap();
        assert_eq!(evaluate_publication(&missing, &mandatory, &input()).await.unwrap(),
            GateOutcome::Reject { reason_code:"module_unavailable".into() });
        assert_eq!(evaluate_publication(&missing, &optional, &input()).await.unwrap(),
            GateOutcome::Skipped { reason_code:"module_unavailable".into() });
    }

    #[tokio::test]
    async fn wrong_identity_or_peer_is_rejected() {
        let (_dir, path) = mock(|_| json!({"protocol_version":1,
            "request_id":"wrong","module_id":MODULE_ID,"decision":"pass",
            "reason_code":null})).await;
        let peer_uid = std::fs::metadata(&path).unwrap().uid();
        assert!(exchange(&path, &input(), peer_uid).await.is_err());
        let (_dir, path) = mock(|r| json!({"protocol_version":1,
            "request_id":r["request_id"],"module_id":MODULE_ID,"decision":"pass",
            "reason_code":null})).await;
        assert!(exchange(&path, &input(), u32::MAX).await.is_err());
    }
}
