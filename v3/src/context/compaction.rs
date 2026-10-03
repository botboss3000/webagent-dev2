//! Pure, deterministic context compaction planning.
//! The event log and session messages remain the durable full history.
//! COMPACTION: policy comes from <state root>/config/context-compaction.json; context_assembly.rs
//! supplies the available history budget, and main.rs owns model dispatch and saving.
//! See README.md, Context compaction, for the single-file deployment contract.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

/// The external JSON file is the sole configuration authority.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u32,
    pub trigger_tokens: usize,
    pub tail_tokens: usize,
    pub summary_tokens: usize,
    pub characters_per_token: usize,
    pub summary_reserve_bytes: usize,
    pub max_source_bytes: usize,
    pub max_summary_bytes: usize,
    pub summary_prefix: String,
    pub active_summary_prefix: String,
    pub summary_prompt: String,
}
impl Config {
    // COMPACTION CONFIG: deploy.sh points both processes at the mounted config directory.
    // This path supplies first-start migration only; runtime reads the data-root file.
    pub fn configured_path() -> std::path::PathBuf {
        std::env::var_os("V3_CONTEXT_COMPACTION_CONFIG_PATH")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("config/context-compaction.json"))
    }
    pub fn configured() -> Result<Self> {
        Self::read(&Self::configured_path())
    }
    /// Data-root JSON is authoritative. Import the validated legacy/operator file
    /// only on first startup; never replace an existing (even invalid) data file.
    /// Atomic no-clobber publication prevents partial reads during concurrent startup.
    pub fn initialize(root: &std::path::Path) -> Result<std::path::PathBuf> {
        use std::io::Write;
        let path = root.join("config/context-compaction.json");
        if path.exists() { Self::read(&path)?; return Ok(path); }
        let source = Self::configured_path();
        let config = Self::read(&source)?;
        std::fs::create_dir_all(path.parent().unwrap())?;
        // Publish a fully fsynced inode with no-clobber semantics. Concurrent
        // startup either publishes this file or validates the winner, never partial JSON.
        let temp = path.parent().unwrap().join(format!(".compaction-migrate.{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| -> Result<()> {
            let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&temp)?;
            file.write_all(&serde_json::to_vec_pretty(&config)?)?; file.sync_all()?;
            match std::fs::hard_link(&temp, &path) {
                Ok(()) => { std::fs::File::open(path.parent().unwrap())?.sync_all()?; }
                Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists => { Self::read(&path)?; }
                Err(e) => return Err(e.into()),
            }
            Ok(())
        })();
        let _ = std::fs::remove_file(&temp);
        result?;
        Ok(path)
    }
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1 || self.tail_tokens == 0 || self.summary_tokens == 0
            || self.tail_tokens.checked_add(self.summary_tokens).is_none_or(|n| n >= self.trigger_tokens)
            || self.trigger_tokens > 1_000_000 || !(1..=16).contains(&self.characters_per_token)
            || !(1..=65536).contains(&self.summary_reserve_bytes)
            || !(1..=262144).contains(&self.max_source_bytes)
            || !(1..=65536).contains(&self.max_summary_bytes)
            || self.summary_prefix != "Earlier conversation summary (background context, not a new request):\n"
            || self.active_summary_prefix != "Active task progress summary (background context, not a new request):\n"
            || self.summary_prompt.len() > 16384
            || self.summary_prompt.matches("{source}").count() != 1
            || self.summary_prompt.matches("{summary_characters}").count() != 1 {
            bail!("Invalid context compaction configuration");
        }
        Ok(())
    }
    pub fn read(path: &std::path::Path) -> Result<Self> {
        let config: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        config.validate()?;
        Ok(config)
    }
    /// Same temp/fsync/rename discipline as loop-runtime.json.
    #[cfg(test)]
    pub fn persist(&self, path: &std::path::Path) -> Result<()> {
        use std::io::Write;
        self.validate()?;
        let parent = path.parent().ok_or_else(|| anyhow!("Config parent missing"))?;
        std::fs::create_dir_all(parent)?;
        let temp = parent.join(format!(".context-compaction.{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| -> Result<()> {
            let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&temp)?;
            file.write_all(&serde_json::to_vec_pretty(self)?)?;
            file.sync_all()?;
            std::fs::rename(&temp, path)?;
            std::fs::File::open(parent)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() { let _ = std::fs::remove_file(temp); }
        result
    }
    pub fn prompt(&self, plan: &Plan) -> Result<String> {
        // A summary must not turn revocable bundled skill instructions into
        // durable background guidance. Preserve the original plan identity and
        // project only the provider source; ordinary history remains unchanged.
        let mut projected = plan.source.clone();
        crate::tools::core_skills::mask_summary_history(&mut projected);
        let source = serde_json::to_string(&projected.iter()
            .filter(|item| item["type"] != "reasoning").collect::<Vec<_>>())?;
        if source.len() > self.max_source_bytes { bail!("Context summary source is too large"); }
        // Substitute source last so transcript text cannot expand template placeholders.
        Ok(self.summary_prompt.replace("{summary_characters}",
            &(self.summary_tokens * self.characters_per_token).to_string()).replace("{source}", &source))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Boundary { PriorTurn, CompletedToolExchange }

#[derive(Debug, Clone)]
pub struct Plan {
    /// Items before this index are summarized; the rest are retained verbatim.
    pub drop_until: usize,
    pub boundary: Boundary,
    /// Exact original prefix. The summary prompt masks native bundled guidance.
    pub source: Vec<Value>,
    /// Stable identity of the source; useful for a durable compaction event.
    pub source_digest: String,
    pub max_bytes: usize,
    pub reserve_bytes: usize,
    // COMPACTION SNAPSHOT: apply_summary uses the planning settings even if the
    // external JSON changes while main.rs is awaiting the normal agent model.
    config: Config,
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

/// Prefer an earlier completed user turn. If one long task fills the context,
/// a completed tool exchange is also safe: no tool call is left without its
/// output on the summarized side. Planning never removes durable history.
#[cfg(test)]
fn plan(input: &[Value], max_bytes: usize, reserve_bytes: usize) -> Result<Option<Plan>> {
    let mut config = Config::configured()?;
    config.summary_reserve_bytes = reserve_bytes;
    plan_with_config(input, max_bytes, &config, false)
}

/// COMPACTION POLICY: history targets are estimated characters/tokens, not the
/// serialized request ceiling supplied by context_assembly.rs. Either can trigger a fold.
pub fn plan_rolling(input: &[Value], max_bytes: usize, config: &Config) -> Result<Option<Plan>> {
    config.validate()?;
    plan_with_config(input, max_bytes, config, true)
}

// The live rolling planner and the test-only legacy planner share this
// measurement path, so release builds must include it.
fn plan_with_config(input: &[Value], max_bytes: usize, config: &Config, rolling: bool) -> Result<Option<Plan>> {
    let reserve_bytes = config.summary_reserve_bytes;
    if max_bytes < 2 || reserve_bytes == 0 || reserve_bytes >= max_bytes {
        bail!("Invalid context budget");
    }
    let sizes = input.iter().map(encoded_len).collect::<Result<Vec<_>>>()?;
    let characters = input.iter().map(|item| serde_json::to_string(item).map(|text| text.chars().count())).collect::<std::result::Result<Vec<_>, _>>()?;
    plan_measured(input, max_bytes, config, rolling, &sizes, &characters)
}

/// Consume append-only accounting without serializing unchanged history. Full source
/// copying and hashing occur only after a safe compaction boundary is selected.
pub fn plan_cached(input: &[Value], max_bytes: usize, config: &Config, sizes: &[usize], characters: &[usize]) -> Result<Option<Plan>> {
    config.validate()?;
    plan_measured(input, max_bytes, config, true, sizes, characters)
}
fn plan_measured(input: &[Value], max_bytes: usize, config: &Config, rolling: bool, sizes: &[usize], characters: &[usize]) -> Result<Option<Plan>> {
    let reserve_bytes = config.summary_reserve_bytes;
    if sizes.len()!=input.len() || characters.len()!=input.len() || reserve_bytes>=max_bytes { bail!("Invalid compaction measurements or budget"); }
    let total = 2usize
        .checked_add(sizes.iter().sum::<usize>())
        .and_then(|n| n.checked_add(input.len().saturating_sub(1)))
        .ok_or_else(|| anyhow!("Context size overflow"))?;
    let history_triggered = rolling && characters.iter().sum::<usize>()
        > config.trigger_tokens * config.characters_per_token;
    if total <= max_bytes && !history_triggered {
        return Ok(None);
    }

    // JSON array syntax costs two brackets and one comma between items.
    let mut suffix = vec![2usize; input.len() + 1];
    for i in (0..input.len()).rev() {
        suffix[i] = suffix[i + 1]
            .checked_add(sizes[i] + usize::from(i + 1 < input.len()))
            .ok_or_else(|| anyhow!("Context size overflow"))?;
    }

    // The tail target never raises the hard serialized-request ceiling. Choose
    // the earliest safe boundary satisfying both, retaining as much as possible.
    let target = max_bytes - reserve_bytes;
    let mut tail_chars = vec![0usize; input.len() + 1];
    for i in (0..input.len()).rev() { tail_chars[i] = tail_chars[i + 1] + characters[i]; }
    // Never summarize only the previous checkpoint: a rolling fold must also
    // age real history, otherwise an oversized checkpoint could retrigger forever.
    let checkpoint = input.first().and_then(|v| v["content"].as_str()).is_some_and(|text|
        text.starts_with(&config.summary_prefix) || text.starts_with(&config.active_summary_prefix));
    let fits = |index: usize| suffix[index] <= target && (!rolling ||
        tail_chars[index] <= config.tail_tokens * config.characters_per_token)
        && !(checkpoint && index == 1);
    let mut pending = HashSet::new();
    let mut seen = HashSet::new();
    let mut valid = true;
    let mut active_boundary = None;
    for (i, item) in input.iter().enumerate() {
        // Evaluate a boundary before consuming its user turn. This prevents
        // splitting a function call from its corresponding output.
        if i > 0 && is_user(item) && valid && pending.is_empty() && fits(i) {
            let source = input[..i].to_vec();
            return Ok(Some(Plan {
                drop_until: i,
                boundary: Boundary::PriorTurn,
                source_digest: digest(&source)?,
                source,
                max_bytes,
                reserve_bytes, config: config.clone(),
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
                Some(id) if pending.remove(id) => {
                    // Keep later exchanges verbatim while summarizing completed work.
                    if valid && pending.is_empty() && fits(i + 1)
                        && active_boundary.is_none() {
                        active_boundary = Some(i + 1);
                    }
                }
                _ => valid = false,
            },
            _ => {}
        }
    }
    if valid && pending.is_empty() {
        if let Some(drop_until) = active_boundary {
            let source = input[..drop_until].to_vec();
            return Ok(Some(Plan {
                drop_until, boundary: Boundary::CompletedToolExchange,
                source_digest: digest(&source)?, source, max_bytes, reserve_bytes, config: config.clone(),
            }));
        }
    }
    bail!("Context exceeds budget without a completed turn or tool exchange to summarize")
}

/// Replace a planned prefix only after a summary has been generated and validated.
/// The caller must persist this result before sending it to a provider.
pub fn apply_summary(input: &[Value], plan: &Plan, summary: &str) -> Result<Vec<Value>> {
    let valid_boundary = match plan.boundary {
        Boundary::PriorTurn => plan.drop_until < input.len() && is_user(&input[plan.drop_until]),
        Boundary::CompletedToolExchange => input.get(plan.drop_until.saturating_sub(1))
            .is_some_and(|item| item["type"] == "function_call_output"),
    };
    if plan.drop_until == 0
        || plan.drop_until > input.len()
        || !valid_boundary
        || input[..plan.drop_until] != plan.source
        || digest(&input[..plan.drop_until])? != plan.source_digest
    {
        bail!("Stale or invalid context compaction plan");
    }
    let summary = summary.trim();
    if summary.is_empty() || summary.len() > plan.config.max_summary_bytes || summary.len() > plan.reserve_bytes
    {
        bail!("Context summary has invalid length");
    }
    let mut result = Vec::with_capacity(input.len() - plan.drop_until + 1);
    let prefix = match plan.boundary {
        Boundary::PriorTurn => &plan.config.summary_prefix,
        Boundary::CompletedToolExchange => &plan.config.active_summary_prefix,
    };
    result.push(json!({"role":"user","content":format!("{prefix}{summary}")}));
    // COMPACTION CHECKPOINT: one summary replaces the entire aged prefix (including
    // its previous checkpoint); the retained suffix remains verbatim for main.rs to save.
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
    fn summary_projection_masks_native_skill_evidence_without_changing_plan_identity() {
        let config=rolling_config();
        let guide="REVOCABLE_BUNDLED_INSTRUCTIONS";
        let native=json!({"status":"success","output":{"ability_id":"base","kind":"ability",
            "skill_id":"base","bytes":guide.len(),"text":guide,
            "sha256":hex::encode(Sha256::digest(guide.as_bytes())),
            "source_kind":"untrusted_bundled_guidance","activates":false}});
        let input=vec![user(&"old conversation ".repeat(100)),
            json!({"type":"function_call","call_id":"skill","name":"load_skill",
                "arguments":json!({"ability_id":"base","kind":"ability","skill_id":"base"}).to_string()}),
            json!({"type":"function_call_output","call_id":"skill","output":native.to_string()}),
            json!({"type":"function_call","call_id":"ordinary","name":"web_search","arguments":"{}"}),
            json!({"type":"function_call_output","call_id":"ordinary",
                "output":json!({"status":"success","output":{"text":"ORDINARY_RESULT_MUST_REMAIN",
                    "source_kind":"untrusted_bundled_guidance"}}).to_string()}),
            user("latest")];
        let original=input.clone();
        let planned=plan_rolling(&input,8192,&config).unwrap().unwrap();
        let source=planned.source.clone();
        let identity=planned.source_digest.clone();
        assert!(serde_json::to_string(&source).unwrap().contains(guide));
        let prompt=config.prompt(&planned).unwrap();
        assert!(!prompt.contains(guide));
        assert!(prompt.contains("bundled_skill_withdrawn"));
        assert!(prompt.contains("ORDINARY_RESULT_MUST_REMAIN"));
        assert!(prompt.contains("old conversation"));
        assert_eq!(planned.source,source);
        assert_eq!(planned.source_digest,identity);
        assert_eq!(digest(&planned.source).unwrap(),identity);
        assert_eq!(input,original);
        // apply_summary still validates the original history/digest and leaves
        // the ordinary tail untouched. No source is rewritten by prompt projection.
        let compacted=apply_summary(&input,&planned,"Earlier work completed.").unwrap();
        assert_eq!(&compacted[1..],&input[planned.drop_until..]);
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

    #[test]
    fn compacts_completed_work_within_one_user_task() {
        let input = vec![
            user("Investigate the issue and finish the repair"),
            call("first"),
            json!({"type":"function_call_output","call_id":"first","output":"x".repeat(2000)}),
            call("second"),
            output("second"),
        ];
        let planned = plan(&input, 700, 250).unwrap().unwrap();
        assert_eq!(planned.boundary, Boundary::CompletedToolExchange);
        assert_eq!(planned.drop_until, 3);
        let compacted = apply_summary(&input, &planned,
            "The issue is under investigation. First read completed; repair remains pending.").unwrap();
        assert_eq!(&compacted[1..], &input[3..]);
        assert!(compacted[0]["content"].as_str().unwrap().contains("Active task progress"));
        assert!(plan(&compacted, 700, 250).unwrap().is_none());
    }

    #[test]
    fn active_compaction_rejects_mismatched_tool_results() {
        let input = vec![user("Do work"), call("first"),
            json!({"type":"function_call_output","call_id":"first","output":"x".repeat(2000)}),
            call("second"),
            json!({"type":"function_call_output","call_id":"unknown","output":"bad"})];
        assert!(plan(&input, 700, 250).is_err());
    }
    fn rolling_config() -> Config {
        let mut c = Config::configured().unwrap();
        c.trigger_tokens = 150;
        c.tail_tokens = 40;
        c.summary_tokens = 20;
        c.summary_reserve_bytes = 300;
        c
    }
    #[test]
    fn rolling_trigger_is_independent_of_byte_ceiling_and_keeps_tail() {
        let c = rolling_config();
        let input = vec![user(&"old".repeat(250)), assistant("done"), user("latest")];
        assert!(plan(&input, 4096, 300).unwrap().is_none());
        let p = plan_rolling(&input, 4096, &c).unwrap().unwrap();
        assert_eq!(p.drop_until, 2);
        let result = apply_summary(&input, &p, "remember original goal").unwrap();
        assert_eq!(&result[1..], &input[2..]);
        let mut raised = c.clone(); raised.trigger_tokens = 500;
        assert!(plan_rolling(&input, 4096, &raised).unwrap().is_none());
    }
    #[test]
    fn second_fold_includes_previous_checkpoint_without_stacking_summaries() {
        let c = rolling_config();
        let input = vec![user(&"old".repeat(250)), assistant("done"), user("latest")];
        let p = plan_rolling(&input, 4096, &c).unwrap().unwrap();
        let mut first = apply_summary(&input, &p, "checkpoint one").unwrap();
        first.extend([assistant(&"aged".repeat(250)), user("newest")]);
        let p2 = plan_rolling(&first, 4096, &c).unwrap().unwrap();
        assert_eq!(p2.source[0], first[0]);
        assert!(p2.drop_until > 1);
        let second = apply_summary(&first, &p2, "checkpoint two").unwrap();
        assert_eq!(second.len(), 2);
        assert_eq!(second[1], user("newest"));
        assert!(!second[0].to_string().contains("checkpoint one"));
    }
    #[test]
    fn single_file_is_required_live_and_rejects_invalid_or_provider_fields() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("context-compaction.json");
        assert!(Config::read(&path).is_err());
        assert!(!path.exists());
        let mut config = Config::configured().unwrap();
        config.persist(&path).unwrap();
        config.summary_reserve_bytes = 20000;
        config.persist(&path).unwrap();
        assert_eq!(Config::read(&path).unwrap().summary_reserve_bytes, 20000);
        let mut value = serde_json::to_value(config).unwrap();
        value["provider"] = json!("not permitted");
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        assert!(Config::read(&path).is_err());
        std::fs::write(&path, "{").unwrap();
        assert!(Config::read(&path).is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "{");
    }
    #[test]
    fn snapshot_prompt_and_validation_limits_are_stable() {
        let mut c = rolling_config();
        let input = vec![user(&"old".repeat(250)), user("latest")];
        let p = plan_rolling(&input, 4096, &c).unwrap().unwrap();
        let prompt = c.prompt(&p).unwrap();
        assert!(prompt.contains("under 80 characters"));
        assert!(prompt.contains("Conversation JSON:"));
        c.summary_reserve_bytes = 1;
        // The plan captures its own immutable settings, not this later edit.
        assert!(apply_summary(&input, &p, "remember goal").is_ok());
        assert!(apply_summary(&input, &p, "").is_err());
        assert!(apply_summary(&input, &p, &"x".repeat(301)).is_err());
        c.max_source_bytes = 1;
        assert!(c.prompt(&p).is_err());
    }
    #[test]
    fn completed_exchange_remains_an_available_rolling_boundary() {
        let c = rolling_config();
        let input = vec![user(&"task".repeat(250)), call("first"), output("first"),
            call("last"), output("last")];
        let p = plan_rolling(&input, 4096, &c).unwrap().unwrap();
        assert_eq!(p.boundary, Boundary::CompletedToolExchange);
        let result = apply_summary(&input, &p, "completed first exchange").unwrap();
        assert_eq!(&result[1..], &input[p.drop_until..]);
    }

    #[test]
    fn data_authority_wins_and_invalid_existing_is_preserved() {
        let root = tempfile::tempdir().unwrap();
        let path = Config::initialize(root.path()).unwrap();
        assert_eq!(path, root.path().join("config/context-compaction.json"));
        let mut config = Config::read(&path).unwrap();
        config.summary_reserve_bytes = 20000;
        config.persist(&path).unwrap();
        Config::initialize(root.path()).unwrap();
        assert_eq!(Config::read(&path).unwrap().summary_reserve_bytes, 20000);
        std::fs::write(&path, "{").unwrap();
        assert!(Config::initialize(root.path()).is_err());
        assert_eq!(std::fs::read_to_string(path).unwrap(), "{");
    }
    #[test]
    fn cached_planner_matches_reference_with_unicode_and_append() {
        let mut config = rolling_config();
        config.tail_tokens = 100;
        let mut history = vec![user(&"é🌍".repeat(600)), assistant("done"), user("latest")];
        let mut state = crate::context_accounting::ContextAccounting::default();
        for _ in 0..3 {
            state.sync(&history).unwrap();
            let (sizes, characters) = state.measurements();
            let cached = plan_cached(&history, 4096, &config, sizes, characters).unwrap().unwrap();
            let reference = plan_rolling(&history, 4096, &config).unwrap().unwrap();
            assert_eq!(cached.drop_until, reference.drop_until);
            assert_eq!(cached.source_digest, reference.source_digest);
            let measured = state.measured_items;
            state.sync(&history).unwrap();
            assert_eq!(state.measured_items, measured);
            history.push(assistant("next"));
        }
    }

}
