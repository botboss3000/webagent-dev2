//! Pure, deterministic context compaction planning.
//! The event log and session messages remain the durable full history.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

const MAX_SUMMARY_BYTES: usize = 64 * 1024;
const SUMMARY_PREFIX: &str =
    "Earlier conversation summary (background context, not a new request):\n";

#[derive(Debug, Clone)]
pub struct Plan {
    /// Items before this index are summarized; the rest are retained verbatim.
    pub drop_until: usize,
    /// Exact source for a provider summarization call.
    pub source: Vec<Value>,
    /// Stable identity of the source; useful for a durable compaction event.
    pub source_digest: String,
    pub max_bytes: usize,
    pub reserve_bytes: usize,
}

fn encoded_len(value: &Value) -> Result<usize> {
    Ok(serde_json::to_vec(value)?.len())
}

fn digest(items: &[Value]) -> Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(items)?)))
}

fn is_user(item: &Value) -> bool {
    item.get("role").and_then(Value::as_str) == Some("user")
}

fn call_id(item: &Value) -> Option<&str> {
    item.get("call_id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
}

/// Find the earliest complete-turn boundary that leaves enough room for a summary.
/// No history is removed by this function. A single oversized newest turn is an error.
pub fn plan(input: &[Value], max_bytes: usize, reserve_bytes: usize) -> Result<Option<Plan>> {
    if max_bytes < 2 || reserve_bytes == 0 || reserve_bytes >= max_bytes {
        bail!("Invalid context budget");
    }
    let sizes = input.iter().map(encoded_len).collect::<Result<Vec<_>>>()?;
    let total = 2usize
        .checked_add(sizes.iter().sum::<usize>())
        .and_then(|n| n.checked_add(input.len().saturating_sub(1)))
        .ok_or_else(|| anyhow!("Context size overflow"))?;
    if total <= max_bytes {
        return Ok(None);
    }

    // JSON array syntax costs two brackets and one comma between items.
    let mut suffix = vec![2usize; input.len() + 1];
    for i in (0..input.len()).rev() {
        suffix[i] = suffix[i + 1]
            .checked_add(sizes[i] + usize::from(i + 1 < input.len()))
            .ok_or_else(|| anyhow!("Context size overflow"))?;
    }

    let target = max_bytes - reserve_bytes;
    let mut pending = HashSet::new();
    let mut seen = HashSet::new();
    let mut valid = true;
    for (i, item) in input.iter().enumerate() {
        // Evaluate a boundary before consuming its user turn. This prevents
        // splitting a function call from its corresponding output.
        if i > 0 && is_user(item) && valid && pending.is_empty() && suffix[i] <= target {
            let source = input[..i].to_vec();
            return Ok(Some(Plan {
                drop_until: i,
                source_digest: digest(&source)?,
                source,
                max_bytes,
                reserve_bytes,
            }));
        }
        match item.get("type").and_then(Value::as_str) {
            Some("function_call") => match call_id(item) {
                Some(id) if seen.insert(id.to_owned()) => {
                    pending.insert(id.to_owned());
                }
                _ => valid = false,
            },
            Some("function_call_output") => match call_id(item) {
                Some(id) if pending.remove(id) => {}
                _ => valid = false,
            },
            _ => {}
        }
    }
    bail!("Context exceeds budget without a complete turn to summarize")
}

/// Replace a planned prefix only after a summary has been generated and validated.
/// The caller must persist this result before sending it to a provider.
pub fn apply_summary(input: &[Value], plan: &Plan, summary: &str) -> Result<Vec<Value>> {
    if plan.drop_until == 0
        || plan.drop_until >= input.len()
        || !is_user(&input[plan.drop_until])
        || input[..plan.drop_until] != plan.source
        || digest(&input[..plan.drop_until])? != plan.source_digest
    {
        bail!("Stale or invalid context compaction plan");
    }
    let summary = summary.trim();
    if summary.is_empty() || summary.len() > MAX_SUMMARY_BYTES || summary.len() > plan.reserve_bytes
    {
        bail!("Context summary has invalid length");
    }
    let mut result = Vec::with_capacity(input.len() - plan.drop_until + 1);
    result.push(json!({"role":"user","content":format!("{SUMMARY_PREFIX}{summary}")}));
    result.extend_from_slice(&input[plan.drop_until..]);
    if encoded_len(&json!(&result))? > plan.max_bytes {
        bail!("Compacted context exceeds budget");
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user(text: &str) -> Value {
        json!({"role":"user","content":text})
    }
    fn assistant(text: &str) -> Value {
        json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":text}]})
    }
    fn call(id: &str) -> Value {
        json!({"type":"function_call","call_id":id,"name":"file_read","arguments":"{}"})
    }
    fn output(id: &str) -> Value {
        json!({"type":"function_call_output","call_id":id,"output":"{}"})
    }

    #[test]
    fn fitting_input_needs_no_compaction() {
        assert!(plan(&[user("hello")], 1024, 100).unwrap().is_none());
    }

    #[test]
    fn retains_complete_latest_tool_exchange() {
        let input = vec![
            user(&"old".repeat(120)),
            assistant("old answer"),
            user("new task"),
            call("c1"),
            output("c1"),
            assistant("new answer"),
        ];
        let latest = serde_json::to_vec(&input[2..]).unwrap().len();
        let max = latest + 160;
        let planned = plan(&input, max, 65).unwrap().unwrap();
        assert_eq!(planned.drop_until, 2);
        assert_eq!(planned.source, input[..2]);
        let compacted = apply_summary(&input, &planned, "Old request was completed.").unwrap();
        assert_eq!(&compacted[1..], &input[2..]);
        assert_eq!(compacted[0]["role"], "user");
        assert!(plan(&compacted, max, 65).unwrap().is_none());
    }

    #[test]
    fn does_not_split_unanswered_tool_call() {
        let input = vec![
            user(&"old".repeat(120)),
            call("c1"),
            user("new task"),
            assistant("answer"),
        ];
        assert!(plan(&input, 140, 40).is_err());
    }

    #[test]
    fn stale_plan_and_oversized_summary_are_rejected() {
        let input = vec![
            user(&"old".repeat(120)),
            assistant("done"),
            user("new task"),
        ];
        let planned = plan(&input, 180, 80).unwrap().unwrap();
        let mut changed = input.clone();
        changed[0] = user("changed");
        assert!(apply_summary(&changed, &planned, "memory").is_err());
        assert!(apply_summary(&input, &planned, "").is_err());
        assert!(apply_summary(&input, &planned, &"x".repeat(81)).is_err());
    }

    #[test]
    fn cannot_discard_the_only_user_turn() {
        assert!(plan(&[user(&"x".repeat(500))], 200, 50).is_err());
    }
}
