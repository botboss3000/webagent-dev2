//! The v3 executable has three roles: API (accepts work), loop (executes it),
//! and health (checks that the loop is alive). Start with docs/code-tour.md
//! for a plain-language path from a message to its single published reply.
#[path = "access/mod.rs"] mod access;
#[path = "access/policy.rs"] mod access_policy;
#[path = "storage/access.rs"] mod access_store;
#[path = "storage/agent.rs"] mod agent_storage;
#[path = "api/admin_access.rs"] mod phase3_api;
#[path = "api/user.rs"] mod user_api;
#[path = "context/accounting.rs"] mod context_accounting;
#[path = "runtime_config.rs"] mod loop_runtime;
#[path = "gates/mod.rs"] mod gate_registry;
#[path = "gates/controls.rs"] mod gate_controls;
#[path = "agent_loop/tools/batch.rs"] mod parallel_batch;
mod abilities;
#[path = "api/browser_login.rs"] mod browser_login;
#[path = "api/accounts.rs"] mod accounts;
#[path = "api/registration.rs"] mod registration;
#[path = "storage/cache.rs"] mod cache;
#[path = "context/compaction.rs"] mod context_compaction;
mod attachments;
#[path = "context/memory.rs"] mod memory;
#[path = "context/assembly.rs"] mod context_assembly;
#[path = "context/input.rs"] mod context_input;
mod workflow;
#[path = "agent_loop/publication.rs"] mod publication;
#[path = "api/progress.rs"] mod task_progress;
#[path = "gates/protocol.rs"] mod gate_protocol;
mod promotion;
#[path = "models/provider/mod.rs"] mod provider;
#[path = "models/selection.rs"] mod model_selection;
#[path = "api/model_admin.rs"] mod model_selection_api;
#[cfg(test)]
mod runtime_tests;
#[path = "storage/mod.rs"] mod store;
#[path = "agent_loop/tools/mod.rs"] mod tools;
#[path = "api/ui_pages.rs"] mod ui_pages;
use anyhow::{anyhow, Result};
use argon2::{
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Argon2,
};
use axum::{
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{
        sse::{Event, KeepAlive},
        IntoResponse, Response, Sse,
    },
    routing::{get, post, put},
    Json, Router,
};
use fs2::FileExt;
use rand::rngs::OsRng;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    convert::Infallible,
    fs::{File, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use store::Store;
use tokio::sync::{Mutex, RwLock, Semaphore};
use tokio_util::sync::CancellationToken;
use tower_http::services::{ServeDir, ServeFile};
use uuid::Uuid;

// Shared state stays at crate root so API and loop modules use one authority.
include!("app.rs");
mod api;
use api::*;
mod agent_loop;
use agent_loop::*;
// Choose one role for this process. The API serves requests; the loop owns
// recovery and execution; health only checks a recent loop heartbeat.
#[tokio::main]
async fn main() -> Result<()> { run_process().await }
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_tool_policy_is_denied() {
        assert_eq!(policy_decision(&policy_default(), "unknown"), "deny");
    }
    #[test]
    fn policy_defaults_gate_all_side_effects() {
        let p = policy_default();
        for name in [
            "file_write",
            "file_edit",
            "prompt_slot_write",
            "workspace_seed",
            "source_patch",
            "shell",
            "python",
        ] {
            assert_eq!(policy_decision(&p, name), "require_approval");
        }
        assert_eq!(policy_decision(&p, "file_read"), "allow");
        assert_eq!(policy_decision(&p, "prompt_slot_read"), "allow");
        assert_eq!(policy_decision(&p, "promotion_propose"), "allow");
    }
    #[tokio::test]
    async fn existing_policy_gains_approval_for_new_edit_tool_once() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        let mut policy = policy_default();
        policy["rules"].as_object_mut().unwrap().remove("file_edit");
        policy["rules"]
            .as_object_mut()
            .unwrap()
            .remove("workspace_seed");
        store.put("policy", &policy).await.unwrap();
        migrate_tool_policy_defaults(&store).await.unwrap();
        migrate_tool_policy_defaults(&store).await.unwrap();
        let migrated = store.get("policy").await.unwrap().unwrap();
        assert_eq!(migrated["rules"]["file_edit"], "require_approval");
        assert_eq!(migrated["rules"]["workspace_seed"], "require_approval");
        assert_eq!(migrated["version"], 2);
    }
    // DISPATCH-DIGEST-PARITY: streaming must retain the historical buffered
    // JSON fingerprint, including Unicode, escaping, nested data and empty arrays.
    #[test]
    fn streaming_digest_matches_buffered_json() {
        for value in [json!([]), json!({"text":"é\n\"quoted\"","nested":[null,true,42]}),
            json!({"output":"x".repeat(65536)})] {
            assert_eq!(digest(&value), hex::encode(Sha256::digest(serde_json::to_vec(&value).unwrap())));
        }
    }
    #[test]
    fn arguments_digest_changes_with_action() {
        assert_ne!(
            digest(&json!({"command":"echo hello"})),
            digest(&json!({"command":"rm file"}))
        );
    }
    #[test]
    fn consumed_tool_result_advances_once() {
        let mut r = json!({"input":[],"pending":{"call_id":"c"},"call_index":0});
        append_tool(&mut r, &json!({"output":"done"})).unwrap();
        assert_eq!(r["call_index"], 1);
        assert!(r["pending"].is_null());
        assert_eq!(r["input"][0]["call_id"], "c");
    }
}

#[cfg(test)] mod context_summary_tests {
 use super::*;
 // CONTEXT-SUMMARY-REGRESSION: neither private fields nor malformed counts escape.
 #[test] fn summary_is_numeric_and_allowlisted() {
  let mut run=json!({"context_assembly_report":{"version":1,"scope":"logical_call_envelope","envelope_bytes":100,"max_bytes":200,"history_json_bytes":50,"instruction_json_bytes":30,"schemas_json_bytes":20,"turn":1,"call":2,"private":"SECRET"}});
  assert!(!public_context_summary(&run).to_string().contains("SECRET"));
  assert_eq!(public_context_summary(&run)["envelope_bytes"],100);
  run["context_assembly_report"]["call"]=json!("SECRET");assert!(public_context_summary(&run).is_null());
 }
}
