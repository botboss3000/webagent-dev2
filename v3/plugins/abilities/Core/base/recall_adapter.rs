//! Bounded recall of this run's caller-owned, encrypted compaction sources.
//! The model cannot name another session or user; the segment must be listed
//! on the current session and match its durable source digest.
use crate::{App, ability_controls, live_tool_decision, session_agent_id, session_mode};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashSet;
use tokio_util::sync::CancellationToken;

const MAX_OUTPUT_BYTES: usize = 16 * 1024;
pub fn owns(name: &str) -> bool { name == "recall_compacted" }
pub fn schemas() -> Vec<Value> {
    vec![json!({"type":"function","name":"recall_compacted",
        "description":"List recent compacted source segments for this session, or read a bounded page of one segment's original history. Reasoning and revocable guidance are withheld.",
        "parameters":{"type":"object","properties":{
            "segment_id":{"type":"string","minLength":1,"maxLength":128},
            "offset_items":{"type":"integer","minimum":0,"maximum":4096},
            "limit_items":{"type":"integer","minimum":1,"maximum":20}},
            "required":[],"additionalProperties":false}})]
}
fn default_limit() -> usize { 10 }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args {
    #[serde(default)] segment_id: Option<String>,
    #[serde(default)] offset_items: usize,
    #[serde(default = "default_limit")] limit_items: usize,
}
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}
fn validate(args: &Args) -> Result<()> {
    if !(1..=20).contains(&args.limit_items) || args.offset_items > 4096
        || args.segment_id.as_deref().is_some_and(|id| !valid_id(id))
        || (args.segment_id.is_none() && args.offset_items != 0) {
        bail!("Invalid compacted segment selector");
    }
    Ok(())
}
struct View { app: App, fingerprint: String }
async fn view(app: &App, sid: &str, run_id: &str) -> Result<View> {
    let agent = session_agent_id(app, sid).await?;
    let mut scoped = app.clone();
    scoped.store = app.store.for_agent(&agent).await?;
    let run = scoped.run(run_id).await?;
    if run["session_id"] != sid || run["agent_id"].as_str().unwrap_or("shared_default") != agent {
        bail!("Run does not belong to current session and agent");
    }
    let (policy, revision, visibility, state) = ability_controls(&scoped, sid).await?;
    let mode = session_mode(&scoped, sid).await?;
    let access = scoped.store.live_access(scoped.store.current_user_id(), &agent).await?;
    if !access.admitted() || live_tool_decision(&scoped, &access, "recall_compacted",
        &policy, &visibility, &state, &run["loaded_abilities"], mode["mode"]=="read_only")
        .permission == crate::access::Permission::Deny {
        bail!("Compacted recall unavailable under current access");
    }
    let fingerprint = crate::digest(&json!([agent, policy, revision, visibility,
        state, mode, run["loaded_abilities"], access.context_fingerprint(),
        scoped.abilities.live_version()]));
    Ok(View { app: scoped, fingerprint })
}
fn metadata(session: &Value) -> Result<Vec<Value>> {
    let Some(entries) = session.get("compaction_segments") else { return Ok(Vec::new()); };
    let entries = entries.as_array().context("Invalid compacted segment index")?;
    let mut found = HashSet::new();
    let mut output = Vec::new();
    for entry in entries {
        let id = entry["segment_id"].as_str().context("Invalid compacted segment ID")?;
        let digest = entry["source_digest"].as_str().context("Invalid compacted source digest")?;
        let count = entry["item_count"].as_u64().context("Invalid compacted item count")?;
        let created = entry["created_at"].as_u64().context("Invalid compacted creation time")?;
        if !valid_id(id) || digest.len() != 64
            || !digest.bytes().all(|b| b.is_ascii_hexdigit()) || count > 1_000_000
            || !found.insert(id.to_owned()) {
            bail!("Invalid compacted segment index");
        }
        output.push(json!({"segment_id":id,"source_digest":digest,
            "item_count":count,"created_at":created}));
    }
    Ok(output)
}
fn mask_guidance(source: &mut [Value]) {
    // Mask on the full source before paging: splitting a call from its result
    // would defeat the provenance-aware bundled guidance masker.
    crate::tools::core_skills::mask_summary_history(source);
    let mut sensitive_calls = HashSet::new();
    for item in source {
        if item["type"] == "function_call" {
            if matches!(item["name"].as_str(), Some("read_own_prompt" | "prompt_slot_read")) {
                if let Some(id) = item["call_id"].as_str() { sensitive_calls.insert(id.to_owned()); }
            }
        } else if item["type"] == "function_call_output" {
            if item["call_id"].as_str().is_some_and(|id| sensitive_calls.remove(id)) {
                item["output"] = json!({"status":"error","error_kind":"guidance_withheld",
                    "output":"Previously read prompt guidance is unavailable in compacted recall."}).to_string().into();
            }
        }
        if let Some(object) = item.as_object_mut() { object.remove("deepseek_reasoning"); }
    }
}
fn segment_page(record: &Value, entry: &Value, sid: &str, args: &Args) -> Result<Value> {
    let id = args.segment_id.as_deref().context("Segment ID required")?;
    if record["schema_version"] != 1 || record["session_id"] != sid
        || record["segment_id"] != id || record["source_digest"] != entry["source_digest"] {
        bail!("Compacted segment provenance conflict");
    }
    let mut source = record["source"].as_array().context("Compacted source unavailable")?.clone();
    if source.len() as u64 != entry["item_count"].as_u64().unwrap_or(u64::MAX)
        || crate::digest(&source) != entry["source_digest"].as_str().unwrap_or("") {
        bail!("Compacted source digest conflict");
    }
    mask_guidance(&mut source);
    let total = source.len();
    if args.offset_items > total { bail!("Compacted item cursor exceeds source"); }
    let end = args.offset_items.saturating_add(args.limit_items).min(total);
    let items = source[args.offset_items..end].iter()
        .filter(|item| item["type"] != "reasoning").cloned().collect::<Vec<_>>();
    let output = json!({"segment_id":id,"source_digest":entry["source_digest"],
        "source_kind":"compacted_provider_history","items":items,
        "offset_items":args.offset_items,"next_offset_items":end,"total_items":total,
        "has_more":end<total,"reasoning_included":false,"revocable_guidance_included":false});
    if serde_json::to_vec(&output)?.len() > MAX_OUTPUT_BYTES {
        bail!("Compacted page exceeds 16 KiB; request fewer items or skip the oversized item");
    }
    Ok(output)
}
async fn work(app: &App, sid: &str, run_id: &str, args: &Value) -> Result<Value> {
    let parsed: Args = serde_json::from_value(args.clone()).context("Invalid compacted recall arguments")?;
    validate(&parsed)?;
    let current = view(app, sid, run_id).await?;
    let session = current.app.store.get(&format!("session:{sid}")).await?
        .context("Current session unavailable")?;
    let entries = metadata(&session)?;
    let output = if let Some(id) = parsed.segment_id.as_deref() {
        let entry = entries.iter().find(|entry|entry["segment_id"]==id)
            .context("Compacted segment unavailable in current session")?;
        let record = current.app.store.get(&format!("compaction_segment:{sid}:{id}")).await?
            .context("Compacted segment unavailable")?;
        segment_page(&record, entry, sid, &parsed)?
    } else {
        let recent = entries.iter().rev().take(32).cloned().collect::<Vec<_>>();
        json!({"segments":recent,"count":recent.len(),"total_segments":entries.len(),
            "source_kind":"current_session_compaction_index"})
    };
    if serde_json::to_vec(&output)?.len() > MAX_OUTPUT_BYTES {
        bail!("Compacted segment list exceeds 16 KiB");
    }
    if view(app, sid, run_id).await?.fingerprint != current.fingerprint {
        bail!("Compacted recall access changed during read");
    }
    // A concurrent compaction may replace the session index while a segment
    // is read. Confirm the selected provenance still belongs to this session.
    let latest = current.app.store.get(&format!("session:{sid}")).await?
        .context("Current session unavailable")?;
    if parsed.segment_id.is_some() && metadata(&latest)?.iter().all(|entry|
        entry["segment_id"] != parsed.segment_id.as_deref().unwrap()) {
        bail!("Compacted segment index changed during read");
    }
    Ok(output)
}
pub async fn execute(app: &App, sid: &str, run_id: &str, name: &str,
    args: &Value, cancel: CancellationToken) -> Result<Value> {
    if !owns(name) { bail!("Unknown compacted recall tool"); }
    if cancel.is_cancelled() { bail!("tool execution cancelled"); }
    let result = tokio::select! {
        _ = cancel.cancelled() => bail!("tool execution cancelled"),
        result = work(app, sid, run_id, args) => result,
    };
    if cancel.is_cancelled() { bail!("tool execution cancelled"); }
    Ok(match result {
        Ok(output) => json!({"status":"success","output":output}),
        Err(error) => json!({"status":"error","error_kind":"compacted_recall_rejected",
            "output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn list_and_page_are_bounded_and_preserve_provenance() {
        let source = vec![
            json!({"type":"message","role":"user","content":"before"}),
            json!({"type":"reasoning","encrypted_content":"SECRET"}),
            json!({"type":"message","role":"assistant","content":"after"})];
        let digest = crate::digest(&source);
        let entry = json!({"segment_id":"segment-1","source_digest":digest,
            "item_count":3,"created_at":1});
        let record = json!({"schema_version":1,"session_id":"owned","segment_id":"segment-1",
            "source_digest":digest,"source":source});
        let args = Args {segment_id:Some("segment-1".into()),offset_items:0,limit_items:3};
        let page = segment_page(&record,&entry,"owned",&args).unwrap();
        assert_eq!(page["items"].as_array().unwrap().len(),2);
        assert!(!page.to_string().contains("SECRET"));
        assert_eq!(page["total_items"],3);
        assert!(segment_page(&record,&entry,"foreign",&args).is_err());
        assert!(segment_page(&record,&entry,"owned",&Args {offset_items:4,..args}).is_err());
    }
    #[test]
    fn guidance_and_selectors_do_not_expand_recall_authority() {
        let mut source = vec![
            json!({"type":"function_call","name":"read_own_prompt","call_id":"one"}),
            json!({"type":"function_call_output","call_id":"one","output":"PROMPT SECRET"}),
            json!({"type":"message","role":"assistant","deepseek_reasoning":"THOUGHT","content":"visible"})];
        mask_guidance(&mut source);
        assert!(!json!(source).to_string().contains("PROMPT SECRET"));
        assert!(!json!(source).to_string().contains("THOUGHT"));
        assert!(serde_json::from_value::<Args>(json!({"session_id":"foreign"})).is_err());
        assert!(validate(&Args {segment_id:Some("../foreign".into()),
            offset_items:0,limit_items:1}).is_err());
    }
}
