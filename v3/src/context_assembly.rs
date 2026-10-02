//! Pure assembly of a model-call envelope beside the runtime loop.
//! Producers resolve authorization before calling this module. Credentials,
//! dispatch, approvals and durable history writes remain with the runtime.
use anyhow::{bail, Result};
use serde::Serialize;
use serde_json::{json, Value};
use crate::{context_compaction, context_input, provider};
use crate::context_input::{Request, CallIdentity};
use crate::context_accounting::HistoryReport;

/// Provider-neutral envelope using v3's existing Responses-shaped history.
/// This is deliberately NOT the final provider-specific HTTP request body.
pub struct Envelope<'a> {
    pub input: &'a [Value],
    pub instructions: String,
    pub schemas: &'a [Value],
    pub report: Report,
}
pub enum Prepared<'a> { Compact(context_compaction::Plan), Ready(Envelope<'a>) }

#[derive(Debug, Serialize)]
pub struct Overlay {
    pub id: String,
    pub source: String,
    pub lifetime: context_input::Lifetime,
    pub disposition: &'static str,
    pub content_bytes: usize,
}
#[derive(Debug, Serialize)]
pub struct Schema { pub name: String, pub bytes: usize, pub bootstrap: bool }
#[derive(Debug, Serialize)]
pub struct Report {
    pub version: u32,
    pub scope: &'static str,
    pub accounting: &'static str,
    pub provider: String,
    pub model: String,
    pub transport: &'static str,
    pub turn: u64,
    pub call: u64,
    pub max_bytes: usize,
    pub history_budget_bytes: usize,
    pub summary_reserve_bytes: usize,
    pub envelope_bytes: usize,
    pub instruction_json_bytes: usize,
    pub base_content_bytes: usize,
    pub guidance_content_bytes: usize,
    pub schemas_json_bytes: usize,
    pub history_json_bytes: usize,
    pub framing_bytes: usize,
    pub history: HistoryReport,
    pub overlays: Vec<Overlay>,
    pub schemas: Vec<Schema>,
}
fn size<T: Serialize + ?Sized>(value: &T) -> Result<usize> { crate::context_accounting::json_size(value) }

/// Keep existing base/slot ordering and limits when pinning a run's prompt.
pub fn pinned_instructions(base: &str, slots: &[Value]) -> Result<String> {
    let mut instructions = base.to_owned();
    for slot in slots {
        if let Some(content) = slot["content"].as_str().filter(|s| !s.trim().is_empty()) {
            instructions.push_str("\n\n");
            instructions.push_str(content.trim());
        }
    }
    if instructions.len() > 96 * 1024 { bail!("Agent instructions exceed provider limit"); }
    Ok(instructions)
}

/// Select inputs once, combine instructions, then validate the exact logical envelope.
/// A summary is only planned here; the loop must generate, validate and persist it.
pub fn prepare<'a>(request: Request<'a>) -> Result<Prepared<'a>> {
    prepare_incremental(request, &mut crate::context_accounting::ContextAccounting::default())
}
pub fn prepare_incremental<'a>(request: Request<'a>, state: &mut crate::context_accounting::ContextAccounting) -> Result<Prepared<'a>> {
    state.sync(request.history)?;
    // Input selects once; assembly alone combines the selected ingredients in stable order.
    let selected = context_input::select(request.contributions, request.identity)?;
    let mut instructions = format!("{}{}", request.base, request.guidance);
    for decision in &selected {
        if decision.disposition == "included" { instructions.push('\n'); instructions.push_str(&decision.item.content); }
    }
    if instructions.is_empty() || instructions.len() > 96 * 1024 {
        bail!("Invalid assembled provider instructions");
    }
    // Keep the existing logical-envelope budget semantics, without allocating JSON buffers.
    let instruction_json_bytes = size(&instructions)?;
    let schemas_json_bytes = size(request.schemas)?;
    let framing = b"{\"instructions\":,\"tools\":,\"input\":}".len();
    // Empty history contributes two brackets. No transcript/schema copies are needed.
    let overhead = framing + instruction_json_bytes + schemas_json_bytes + 2;
    let history_budget = request.max_bytes.checked_sub(overhead).filter(|remaining| *remaining > 2)
        .ok_or_else(|| anyhow::anyhow!("Instructions and schemas exceed context budget"))?;
    // Assembly owns the envelope ceiling; only the compaction module owns folding policy.
    let reserve = request.compaction.summary_reserve_bytes;
    if history_budget <= reserve { bail!("Instructions and tool schemas exceed context budget; review required"); }
    let (sizes, characters) = state.measurements();
    let plan = context_compaction::plan_cached(request.history, history_budget, request.compaction, sizes, characters)?;
    if let Some(plan) = plan { return Ok(Prepared::Compact(plan)); }
    let history_json_bytes = state.bytes();
    let envelope_bytes = framing + instruction_json_bytes + schemas_json_bytes + history_json_bytes;
    if envelope_bytes > request.max_bytes { bail!("Assembled context exceeds budget"); }
    let selection = request.selection.value();
    let report = Report {
        version: 1, scope: "logical_call_envelope", accounting: "serialized_json_bytes_not_provider_tokens",
        provider: selection["provider"].as_str().unwrap_or("").into(),
        model: request.selection.model.clone(),
        transport: match request.selection.provider {
            provider::ProviderId::OpenaiSubscription => "responses",
            provider::ProviderId::Deepseek => "chat_completions",
        },
        turn: request.identity.turn, call: request.identity.call,
        max_bytes: request.max_bytes, history_budget_bytes: history_budget,
        summary_reserve_bytes: reserve, envelope_bytes,
        instruction_json_bytes, base_content_bytes: request.base.len(),
        guidance_content_bytes: request.guidance.len(), schemas_json_bytes, history_json_bytes,
        framing_bytes: envelope_bytes - instruction_json_bytes - schemas_json_bytes - history_json_bytes,
        history: state.report(),
        overlays: selected.iter().map(|decision| Overlay { id: decision.item.id.clone(), source: decision.item.source.clone(), lifetime: decision.item.lifetime, disposition: decision.disposition, content_bytes: decision.item.content.len() }).collect(),
        schemas: request.schemas.iter().map(|schema| Ok(Schema {
            name: schema["name"].as_str().unwrap_or("").into(), bytes: size(schema)?,
            bootstrap: matches!(schema["name"].as_str(), Some("list_abilities" | "load_ability")),
        })).collect::<Result<Vec<_>>>()?,
    };
    Ok(Prepared::Ready(Envelope { input: request.history, instructions: instructions,
        schemas: request.schemas, report }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::context_input::{Contribution, Lifetime};
    fn selection() -> provider::ProviderSelection {
        provider::ProviderSelection::parse(&json!({"provider":"deepseek","model":"deepseek-chat"}), "unused").unwrap()
    }
    fn test_config(reserve: usize) -> context_compaction::Config {
        let mut c = context_compaction::Config::configured().unwrap();
        c.summary_reserve_bytes=reserve; c
    }
    #[test]
    fn preserves_projection_and_reports_without_content() {
        let history = vec![json!({"role":"user","content":"private transcript"}),
            json!({"type":"function_call","call_id":"c","name":"read","arguments":"private arguments"}),
            json!({"type":"function_call_output","call_id":"c","output":"private output"})];
        let c = Contribution { id: "mode".into(), source: "native_mode".into(),
            lifetime: Lifetime::Call, content: "private overlay".into(), turn: 1, call: 2 };
        let contributions = [c.clone(), c.clone(), Contribution { call: 1, id: "old".into(), ..c }];
        let schemas = [json!({"name":"load_ability","parameters":{"description":"private schema"}})];
        let selection = selection();
        let config = test_config(100);
        let Prepared::Ready(envelope) = prepare(Request { history: &history, base: "private base",
            guidance: " private skill", schemas: &schemas, contributions: &contributions,
            identity: CallIdentity { turn: 1, call: 2 }, selection: &selection,
            max_bytes: 4096, compaction: &config }).unwrap() else { panic!("unexpected compaction") };
        assert_eq!(envelope.instructions, "private base private skill\nprivate overlay");
        assert_eq!(envelope.input, history);
        assert_eq!(envelope.schemas, schemas);
        let report = serde_json::to_value(&envelope.report).unwrap();
        assert!(!report.to_string().contains("private"));
        assert_eq!(report["transport"], "chat_completions");
        assert_eq!(report["overlays"][1]["disposition"], "duplicate");
        assert_eq!(report["overlays"][2]["disposition"], "expired");
        assert_eq!(envelope.report.envelope_bytes, envelope.report.instruction_json_bytes +
            envelope.report.schemas_json_bytes + envelope.report.history_json_bytes + envelope.report.framing_bytes);
        assert_eq!(envelope.report.history.argument_content_bytes, "private arguments".len());
        assert_eq!(envelope.report.history.result_content_bytes, "private output".len());
        assert!(envelope.report.schemas[0].bootstrap);
    }
    #[test]
    fn compacts_complete_turns_and_accounts_for_overlays() {
        let history = vec![json!({"role":"user","content":"x".repeat(2000)}),
            json!({"role":"assistant","content":"done"}), json!({"role":"user","content":"latest"})];
        let selection = selection();
        let Prepared::Compact(plan) = prepare(Request { history: &history, base: "base", guidance: "",
            schemas: &[], contributions: &[], identity: CallIdentity { turn: 1, call: 1 },
            selection: &selection, max_bytes: 600, compaction: &test_config(100) }).unwrap()
            else { panic!("expected compaction") };
        assert_eq!(plan.drop_until, 2);
        let overlay = Contribution { id: "huge".into(), source: "native".into(), lifetime: Lifetime::Run,
            content: "x".repeat(600), turn: 0, call: 0 };
        assert!(prepare(Request { history: &history, base: "base", guidance: "", schemas: &[],
            contributions: &[overlay], identity: CallIdentity { turn: 1, call: 1 },
            selection: &selection, max_bytes: 600, compaction: &test_config(100) }).is_err());
    }
    #[test]
    fn pinned_prompt_keeps_order_and_ignores_empty_slots() {
        assert_eq!(pinned_instructions("base", &[json!({"content":" a "}),
            json!({"content":" "}), json!({"content":"b"})]).unwrap(), "base\n\na\n\nb");
    }
    #[test]
    fn json_policy_triggers_below_request_ceiling() {
        let mut config = context_compaction::Config::configured().unwrap();
        config.trigger_tokens = 150; config.tail_tokens = 40; config.summary_tokens = 20;
        config.summary_reserve_bytes = 300;
        let history = vec![json!({"role":"user","content":"x".repeat(1000)}),
            json!({"role":"assistant","content":"done"}), json!({"role":"user","content":"new"})];
        let selection = selection();
        let Prepared::Compact(plan) = prepare(Request { history: &history, base: "base", guidance: "",
            schemas: &[], contributions: &[], identity: CallIdentity {turn:1,call:1},
            selection: &selection, max_bytes:4096, compaction:&config }).unwrap() else { panic!("expected history trigger") };
        assert_eq!(plan.reserve_bytes, 300);
        assert_eq!(plan.drop_until, 2);
    }

}
