//! One accepted terminal publication, committed atomically with the completed run.
use anyhow::{bail, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use crate::{store::Store,workflow::{self,Decision,Input,Policy}};
pub async fn terminal(store: &Store, run: &mut Value, output: &[Value],
    text: &str, policy: &Policy) -> Result<()> {
    if text.trim().is_empty() || text.len()>policy.max_publication_bytes {
        bail!("Invalid final publication length");
    }
    // Do not expose a completed status or accepted receipt before durable commit.
    // On storage failure the caller retains its original recoverable run state.
    let mut published = run.clone();
    workflow::record(&mut published,policy,&Input::Publication {
        text_digest:hex::encode(Sha256::digest(text.as_bytes())) },Decision::Pass)?;
    published["status"]=json!("completed");
    store.save_run(&mut published,"model_output",&json!({"output":output,"terminal_text":text})).await?;
    *run = published;
    Ok(())
}
