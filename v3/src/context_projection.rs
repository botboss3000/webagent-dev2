//! Model-call projection: overlays never mutate the durable input array.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Lifetime { History, Run, Turn, Call }
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Contribution {
    pub id: String,
    pub source: String,
    pub lifetime: Lifetime,
    pub content: String,
    pub turn: u64,
    pub call: u64,
}
pub struct Projection { pub input: Vec<Value>, pub instructions: String }
/// Only trusted context producers may call this; not an agent supplied instruction API.
/// History contributions must be committed by their producer, never implicitly here.
pub fn assemble(history: &[Value], base: &str, guidance: &str,
    contributions: &[Contribution], turn: u64, call: u64) -> Result<Projection> {
    let mut instructions = format!("{base}{guidance}");
    let mut ids=HashSet::new();
    for item in contributions {
        if item.id.is_empty() || item.source.is_empty() || item.content.len()>65536 {
            bail!("Invalid context contribution");
        }
        let active = match item.lifetime {
            Lifetime::History => false,
            Lifetime::Run => true,
            Lifetime::Turn => item.turn==turn,
            Lifetime::Call => item.turn==turn && item.call==call,
        };
        if active && ids.insert(&item.id) {
            instructions.push_str("\n"); instructions.push_str(&item.content);
        }
    }
    Ok(Projection {input:history.to_vec(),instructions})
}

/// Budget the complete serialized request, not just conversation history.
/// This is a byte ceiling, not a claim of provider token accounting.
pub fn history_budget(base: &str, guidance: &str, schemas: &[Value], max: usize) -> Result<usize> {
    let overhead=serde_json::to_vec(&serde_json::json!({"instructions":format!("{base}{guidance}"),
        "tools":schemas,"input":[]}))?.len();
    max.checked_sub(overhead).filter(|remaining|*remaining>2)
        .ok_or_else(||anyhow::anyhow!("Instructions and schemas exceed context budget"))
}
#[cfg(test)]
mod tests {
 use super::*;
 use serde_json::json;
 #[test] fn instructions_and_schemas_consume_the_budget() {
    assert!(history_budget("large instruction","",&[],8).is_err());
    let small=history_budget("a","",&[],1024).unwrap();
    let large=history_budget("a","",&[json!({"name":"tool","description":"large schema"})],1024).unwrap();
    assert!(large<small);
 }
 #[test] fn lifetimes_do_not_contaminate_history() {
    let history=vec![json!({"role":"user","content":"hello"})];
    let c=Contribution{id:"x".into(),source:"trusted_test".into(),lifetime:Lifetime::Call,
        content:"temporary".into(),turn:1,call:2};
    let a=assemble(&history,"base","",&[c.clone(),c.clone()],1,2).unwrap();
    assert_eq!(a.instructions,"base\ntemporary"); assert_eq!(a.input,history);
    assert_eq!(assemble(&history,"base","",&[c],1,3).unwrap().instructions,"base");
 }
 #[test] fn history_is_not_reinjected() {
    let c=Contribution{id:"x".into(),source:"test".into(),lifetime:Lifetime::History,
        content:"already stored".into(),turn:0,call:0};
    assert_eq!(assemble(&[],"base","",&[c],0,0).unwrap().instructions,"base");
 }
}
