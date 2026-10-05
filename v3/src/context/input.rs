//! Borrowed ingredients for a model call and selection of trusted temporary additions.
//! This module never combines instructions, budgets requests, compacts or writes history.
//! Producers must resolve authorization first; transcript/tool output is not a trusted injection API.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;
use crate::{context_compaction, provider};
/// Identifies the current turn and provider attempt for temporary-input selection.
#[derive(Debug, Clone, Copy)]
pub struct CallIdentity { pub turn: u64, pub call: u64 }

/// Borrowed call ingredients plus resolved model and budget policies.
/// This is not an assembled payload: history and schemas stay borrowed, and
/// assembly combines instructions only after contribution selection succeeds.
pub struct Request<'a> {
    pub history: &'a [Value],
    pub base: &'a str,
    pub guidance: &'a str,
    pub schemas: &'a [Value],
    /// Only native, trusted producers can supply instruction contributions.
    pub contributions: &'a [Contribution],
    pub identity: CallIdentity,
    pub selection: &'a provider::ProviderSelection,
    pub max_bytes: usize,
    pub compaction: &'a context_compaction::Config,
}


#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Lifetime { History, Run, Turn, Call }
/// Trusted native contribution. History entries must already be committed by their producer.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Contribution {
 pub id: String, pub source: String, pub lifetime: Lifetime,
 pub content: String, pub turn: u64, pub call: u64,
}
/// A decision retains a reference rather than copying private content.
pub struct SelectedContribution<'a> { pub item: &'a Contribution, pub disposition: &'static str }
/// Validate every contribution and select by lifetime exactly once. First active ID wins.
/// Return excluded decisions too, so the assembly report cannot disagree with actual inclusion.
pub fn select<'a>(items: &'a [Contribution], identity: CallIdentity) -> Result<Vec<SelectedContribution<'a>>> {
 let mut seen=HashSet::new();
 items.iter().map(|item| {
  if item.id.is_empty() || item.source.is_empty() || item.content.len()>65536 { bail!("Invalid context contribution"); }
  let active=match item.lifetime {
   Lifetime::History=>false, Lifetime::Run=>true,
   Lifetime::Turn=>item.turn==identity.turn,
   Lifetime::Call=>item.turn==identity.turn && item.call==identity.call,
  };
  let disposition=if item.lifetime==Lifetime::History {"already_in_history"}
   else if !active {"expired"} else if !seen.insert(&item.id) {"duplicate"} else {"included"};
  Ok(SelectedContribution{item,disposition})
 }).collect()
}
#[cfg(test)] mod tests {
 use super::*;
 #[test] fn selection_is_borrowed_and_does_not_reinject_history() {
  let c=Contribution{id:"a".into(),source:"native".into(),lifetime:Lifetime::Call,content:"private".into(),turn:1,call:2};
  let items=[c.clone(),c.clone(),Contribution{lifetime:Lifetime::History,..c.clone()},Contribution{call:3,id:"old".into(),..c}];
  let selected=select(&items,CallIdentity{turn:1,call:2}).unwrap();
  assert_eq!(selected.iter().map(|x|x.disposition).collect::<Vec<_>>(),vec!["included","duplicate","already_in_history","expired"]);
  assert!(std::ptr::eq(selected[0].item,&items[0]));
 }
 #[test] fn invalid_expired_input_is_rejected() {
  let c=Contribution{id:"".into(),source:"native".into(),lifetime:Lifetime::Call,content:"x".into(),turn:9,call:9};
  assert!(select(&[c],CallIdentity{turn:1,call:1}).is_err());
 }
}
