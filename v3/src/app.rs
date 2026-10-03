// Shared application state and cross-boundary helpers used by API and loop.
const DEFAULT_AGENT_MODEL: &str = "gpt-6-luna";

#[derive(Clone)]
struct App {
    runtime: Arc<RwLock<loop_runtime::Config>>,
    runtime_path: PathBuf,
    compaction_config_path: PathBuf,
    store: Store,
    abilities: Arc<abilities::Catalog>,
    gate: Arc<Mutex<()>>,
    provider: provider::Provider,
    refresh: Arc<Mutex<()>>,
    login: Arc<RwLock<Option<Value>>>,
    active: Arc<Mutex<HashMap<String, CancellationToken>>>,
    slots: Arc<Semaphore>,
    ready: Arc<AtomicBool>,
    stop: CancellationToken,
    login_attempts: Arc<Mutex<Vec<u64>>>,
    control_dir: PathBuf,
    tool_socket: PathBuf,
    promotion_socket: PathBuf,
    secure: bool,
    chat_ui_config_path: PathBuf,
    message_api_token_hash: Option<[u8; 32]>,
    browser_login_bridge: Option<browser_login::Bridge>,
}
#[derive(Debug)]
struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
impl From<anyhow::Error> for ApiError {
    fn from(error: anyhow::Error) -> Self {
        if error.downcast_ref::<crate::store::session::RecycledSession>().is_some() {
            return conflict("Session is recycled; restore it first");
        }
        tracing::error!("Request storage or execution operation failed");
        Self(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Operation failed; check development logs".into(),
        )
    }
}
type Api = std::result::Result<Json<Value>, ApiError>;
fn bad(s: &str) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, s.into())
}
fn conflict(s: &str) -> ApiError {
    ApiError(StatusCode::CONFLICT, s.into())
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}
fn uuid() -> String {
    Uuid::new_v4().to_string()
}
// DISPATCH-DIGEST-STREAM: hash the exact JSON representation without allocating
// a temporary JSON tree or serialized history-sized byte buffer.
fn digest<T: serde::Serialize + ?Sized>(value: &T) -> String {
    struct HashWriter(Sha256);
    impl std::io::Write for HashWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            use sha2::Digest;
            self.0.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    let mut writer = HashWriter(Sha256::new());
    serde_json::to_writer(&mut writer, value).expect("JSON digest serialization");
    hex::encode(writer.0.finalize())
}
fn key(prefix: &str, id: &str) -> String {
    format!("{prefix}:{id}")
}
fn terminal(status: &str) -> bool {
    matches!(status, "completed" | "cancelled" | "failed")
}
fn status(run: &Value) -> &str {
    run["status"].as_str().unwrap_or("failed")
}
// CONTEXT-PUBLIC-SUMMARY: publish measured numeric totals only, never arbitrary
// persisted report fields, overlays, schema descriptions or transcript content.
fn public_context_summary(run: &Value) -> Value {
    let report = &run["context_assembly_report"];
    if report["version"] != 1 || report["scope"] != "logical_call_envelope" { return Value::Null; }
    let fields = ["envelope_bytes", "max_bytes", "history_json_bytes",
        "instruction_json_bytes", "schemas_json_bytes", "turn", "call"];
    let mut summary=serde_json::Map::new();
    for field in fields {
        let Some(value)=report[field].as_u64() else {return Value::Null;};
        summary.insert(field.into(),json!(value));
    }
    summary.insert("accounting".into(),json!("serialized_json_bytes_not_provider_tokens"));
    Value::Object(summary)
}
fn public_run(run: &Value) -> Value {
    json!({"id":run["id"],"session_id":run["session_id"],"status":run["status"],"error":run["error"],
        "pending":run["pending"],"uncertain":run["uncertain"],"turns":run["turns"],
        "attachments":run["attachments"].as_array().cloned().unwrap_or_default(),
        "context_summary":public_context_summary(run)})
}
fn run_diagnostics(run: &Value) -> Value {
    const CHECKPOINTS: [&str; 8] = ["admission", "attachment_preparation", "context_assembly",
        "model_dispatch", "model_output", "pre_tool", "tool_result", "publication"];
    let checkpoints = CHECKPOINTS.iter().filter_map(|name| {
        let receipt = run.pointer(&format!("/workflow/receipts/{name}"))?;
        let decision = receipt["result"]["decision"].as_str()?;
        if !matches!(decision, "pass" | "reject") { return None; }
        let reason_code = if decision == "reject" {
            match receipt["result"]["reason"].as_str() {
                Some("Publication exceeds configured limit") => "publication_limit",
                Some("Tool invocation denied") |
                Some("Tool invocation denied by policy or invalid arguments") => "tool_denied",
                _ => "rejected",
            }
        } else { "" };
        Some(json!({"checkpoint":name,"decision":decision,"reason_code":reason_code,
            "policy_revision":receipt["policy_revision"],"turn":receipt["turn"],
            "call_index":receipt["call_index"]}))
    }).collect::<Vec<_>>();
    json!({"run_id":run["id"],"session_id":run["session_id"],"status":run["status"],
        "uncertain":run["uncertain"],"checkpoints":checkpoints,
        "context_summary":public_context_summary(run)})
}
async fn run_diagnostics_api(
    State(app): State<App>,
    Path((sid, rid)): Path<(String, String)>,
) -> Api {
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &sid)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let run = app.run(&rid).await
        .map_err(|_| ApiError(StatusCode::NOT_FOUND, "Run not found".into()))?;
    if run["session_id"] != sid {
        return Err(ApiError(StatusCode::NOT_FOUND, "Run not found".into()));
    }
    Ok(Json(run_diagnostics(&run)))
}
// Session access is checked again against the durable run before projecting
// progress. The projection contains no model, tool, or user message content.
async fn run_progress_api(
    State(app): State<App>,
    Path((sid, rid)): Path<(String, String)>,
) -> Api {
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &sid)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let run = app.run(&rid).await
        .map_err(|_| ApiError(StatusCode::NOT_FOUND, "Run not found".into()))?;
    if run["session_id"] != sid {
        return Err(ApiError(StatusCode::NOT_FOUND, "Run not found".into()));
    }
    Ok(Json(task_progress::project(&run)))
}
fn required<'a>(body: &'a Value, name: &str, max: usize) -> std::result::Result<&'a str, ApiError> {
    let s = body[name]
        .as_str()
        .ok_or_else(|| bad(&format!("{name} is required")))?;
    if s.is_empty() || s.len() > max {
        return Err(bad(&format!("{name} has invalid length")));
    }
    Ok(s)
}
async fn assembled_instructions(store: &Store) -> Result<String> {
    assembled_agent_instructions(store, "shared_default").await
}
async fn assembled_agent_instructions(store: &Store, agent_id: &str) -> Result<String> {
    context_assembly::pinned_instructions(provider::DEFAULT_INSTRUCTIONS,
        &store.list_agent_prompt_slots(agent_id).await?)
}
/// Advance a pinned cursor only for a typed, safe pre-response failure.
fn model_failure(run: &mut Value, error: &anyhow::Error) -> Result<()> {
    use model_selection::{FailureKind, FailoverState, NextAction, SelectionPlan};
    let failure = error.downcast_ref::<provider::ProviderFailure>();
    let kind = failure.map(|e| e.kind).unwrap_or(FailureKind::Protocol);
    let mut state: FailoverState = run.get("failover_state").filter(|v| !v.is_null())
        .map(|v| serde_json::from_value(v.clone())).transpose()?.unwrap_or_default();
    state.response_started |= failure.is_some_and(|e| e.response_started);
    state.uncertain_tool_effect |= run["uncertain"] == true;
    let action = if let Some(value) = run.get("selection_plan").filter(|v| !v.is_null()) {
        let plan: SelectionPlan = serde_json::from_value(value.clone())?;
        let action = state.failed(&plan, kind);
        if action == NextAction::Backup {
            run["provider_selection"] = provider::ProviderSelection {
                provider: plan.candidates[state.candidate_index].transport.provider_id(),
                model: plan.candidates[state.candidate_index].upstream_model.clone(),
            }.value();
            run["attempt"] = json!(0);
        }
        action
    } else if kind == FailureKind::Transient && !state.response_started
        && !state.uncertain_tool_effect && state.retries < 1 {
        state.retries += 1;
        NextAction::Retry
    } else { NextAction::Terminal };
    run["failover_state"] = serde_json::to_value(state)?;
    run["status"] = json!(match action { NextAction::Retry | NextAction::Backup => "queued", NextAction::Terminal => "failed" });
    run["error"] = json!(match action {
        NextAction::Retry => format!("Transient provider failure; bounded retry: {error}"),
        NextAction::Backup => format!("Primary/backup provider failed; trying the next configured connection: {error}"),
        NextAction::Terminal => format!("Provider request failed; no safe retry or eligible backup remains: {error}"),
    });
    Ok(())
}
// Compaction has its own pinned System cursor. Failover must never rewrite the chat role.
fn system_dispatch_run(run: &Value) -> Result<Value> {
    let plan:model_selection::SelectionPlan=serde_json::from_value(run["system_selection_plan"].clone())
        .map_err(|_|anyhow!("System model required for context compaction; configure it before starting a new run"))?;
    if plan.role != model_selection::ModelRole::System {return Err(anyhow!("Invalid System role plan"));}
    let state:model_selection::FailoverState=serde_json::from_value(run["system_failover_state"].clone())?;
    let candidate=plan.candidates.get(state.candidate_index).ok_or_else(||anyhow!("System candidates exhausted"))?;
    Ok(json!({"agent_id":run["agent_id"],"selection_plan":plan,"failover_state":state,
        "uncertain":run["uncertain"],"provider_selection":provider::ProviderSelection {
            provider:candidate.transport.provider_id(),model:candidate.upstream_model.clone()}.value()}))
}
fn system_model_failure(run: &mut Value, error: &anyhow::Error) -> Result<()> {
    let mut system=system_dispatch_run(run)?;
    model_failure(&mut system,error)?;
    run["system_failover_state"]=system["failover_state"].clone();
    run["status"]=system["status"].clone();
    run["error"]=system["error"].clone();
    Ok(())
}
// System requests use their own model ceiling, including instructions and JSON framing.
fn system_summary_input(context: &loop_runtime::Context, selection: &provider::ProviderSelection,
    prompt: String, max_bytes: usize) -> Result<Vec<Value>> {
    #[derive(serde::Serialize)]
    struct Envelope<'a> { instructions: &'a str, tools: [Value;0], input: &'a [Value] }
    let input=vec![json!({"role":"user","content":prompt})];
    let envelope=Envelope {instructions:provider::DEFAULT_INSTRUCTIONS,tools:[],input:&input};
    if context_accounting::json_size(&envelope)? > context.ceiling(selection)?.min(max_bytes) {
        return Err(anyhow!("System model context limit is too small for this compaction source"));
    }
    Ok(input)
}
#[cfg(test)]
mod system_budget_tests {
    use super::*;
    // Different role profiles must not let a large Standard budget authorize a smaller System request.
    #[test]
    fn compaction_uses_system_profile_and_counts_escaped_envelope() {
        let mut context=loop_runtime::Config::default().context;
        let standard=provider::ProviderSelection {provider:provider::ProviderId::OpenaiSubscription,model:"standard".into()};
        let system=provider::ProviderSelection {provider:provider::ProviderId::OpenaiSubscription,model:"system".into()};
        context.model_profiles.insert("openai_subscription/system".into(),
            loop_runtime::ModelBudget {context_tokens:512,output_reserve_tokens:64,bytes_per_token:1});
        let prompt="\"\\\n".repeat(200);
        assert!(system_summary_input(&context,&standard,prompt.clone(),262144).is_ok());
        assert!(system_summary_input(&context,&system,prompt,262144).is_err());
        assert!(system_summary_input(&context,&standard,"small".into(),8).is_err());
    }
}
fn policy_default() -> Value {
    let mut policy=json!({"version":1,"rules":{"list_abilities":"allow","load_ability":"allow","list_skills":"allow","load_skill":"allow","get_time":"allow","get_date":"allow","calculate":"allow","read_attachment":"allow","search_this_session":"allow","read_own_prompt":"allow","web_search":"allow","get_weather":"allow","maps_geocode":"allow","file_list":"allow","file_read":"allow","file_search":"allow","state_inspect":"allow","prompt_slot_read":"allow","prompt_slot_write":"require_approval","file_write":"require_approval","file_edit":"require_approval","workspace_seed":"require_approval","source_review":"allow","source_patch":"require_approval","promotion_propose":"allow","shell":"require_approval","python":"require_approval"}});
    for schema in tools::administrator::schemas() {
        let name=schema["name"].as_str().unwrap();
        policy["rules"][name]=json!(if tools::is_read(name){"allow"}else{"require_approval"});
    }
    for schema in tools::core_mode::schemas().into_iter()
        .chain(tools::core_search::schemas()).chain(tools::core_compaction::schemas())
        .chain(tools::core_recall::schemas()).chain(tools::core_self_edit::schemas())
        .chain(tools::core_task::schemas()) {
        let name=schema["name"].as_str().unwrap();
        policy["rules"][name]=json!(if tools::is_read(name){"allow"}else{"require_approval"});
    }
    policy
}
fn policy_decision<'a>(policy: &'a Value, name: &str) -> &'a str {
    policy["rules"][name].as_str().unwrap_or("deny")
}
fn live_tool_decision(app:&App, access:&access_policy::LiveAccess, name:&str,
    policy:&Value, visibility:&Value, state:&Value, loaded:&Value,
    read_only:bool) -> access::ToolDecision {
    let installation=access::Permission::from_policy(policy_decision(policy,name))
        .unwrap_or(access::Permission::Deny);
    let (available,visible)=app.abilities.tool_gate(name,visibility,state,loaded);
    let ability_ids=app.abilities.tool_abilities(name,visibility,state,loaded);
    let caution=if !tools::is_read(name) && ability_ids.iter().any(|id| {
        let key=match id.as_str(){"terminal_control"=>"terminal_caution","codebase_admin"=>"destructive_caution",_=>return false};
        visibility["settings"][id][key]=="cautious"
    }) {access::Permission::Ask}else{access::Permission::Auto};
    access.tool_decision_for_abilities(name,&ability_ids,installation.meet(caution),
        available,visible,read_only,tools::is_read(name))
}
fn filter_ability_entry(app:&App, access:&access_policy::LiveAccess, entry:&mut Value,
    policy:&Value, visibility:&Value, state:&Value, loaded:&Value,
    read_only:bool) -> bool {
    let id=entry["id"].as_str().unwrap_or("");
    if access.ability_permission(id)==access::Permission::Deny {return false;}
    let Some(tools)=entry["tools"].as_array_mut() else {return false;};
    tools.retain(|tool| {
        let name=tool["name"].as_str().unwrap_or("");
        let installation=access::Permission::from_policy(policy_decision(policy,name))
            .unwrap_or(access::Permission::Deny);
        let (available,_)=app.abilities.tool_gate(name,visibility,state,loaded);
        let ability_ids=app.abilities.tool_abilities(name,visibility,state,loaded);
        access.tool_decision_for_abilities(name,&ability_ids,installation,
            available,true,read_only,tools::is_read(name))
            .permission!=access::Permission::Deny
    });
    !tools.is_empty()
}
fn default_session_mode() -> Value {
    json!({"mode":"write","revision":0})
}
async fn agent_abilities(app: &App) -> Result<Value> {
    app.store.agent_abilities().await
}
async fn session_agent_id(app: &App, sid: &str) -> Result<String> {
    let session = app.store.get(&key("session", sid)).await?
        .ok_or_else(|| anyhow!("Session not found"))?;
    Ok(session["agent_id"].as_str().unwrap_or("shared_default").to_owned())
}
async fn session_agent_abilities(app: &App, sid: &str) -> Result<Value> {
    let agent_id=session_agent_id(app,sid).await?;
    app.store.for_agent(&agent_id).await?.agent_abilities_for(&agent_id).await
}
fn default_session_abilities() -> Value {
    json!({"revision":0,"active":[],"suppressed":[]})
}
async fn session_ability_state(app: &App, sid: &str) -> Result<Value> {
    let value = app
        .store
        .get(&key("session_ability_state", sid))
        .await?
        .unwrap_or_else(default_session_abilities);
    if value["revision"].as_u64().is_none()
        || !value["active"]
            .as_array()
            .is_some_and(|a| a.iter().all(Value::is_string))
        || !value["suppressed"]
            .as_array()
            .is_some_and(|a| a.iter().all(Value::is_string))
    {
        return Err(anyhow!("Invalid session ability state"));
    }
    Ok(value)
}
async fn ability_controls(app: &App, sid: &str) -> Result<(Value, u64, Value, Value)> {
    let (policy, mode_revision) = effective_policy(app, sid).await?;
    let visibility = session_agent_abilities(app, sid).await?;
    let state = session_ability_state(app, sid).await?;
    Ok((policy, mode_revision, visibility, state))
}
fn invalidate_waiting_run(mut run: Value) -> Value {
    run["pending"]["state"] = json!("requested");
    run["pending"]["decision"] = Value::Null;
    run["status"] = json!("queued");
    run["updated_at"] = json!(now());
    run
}
async fn session_mode(app: &App, sid: &str) -> Result<Value> {
    let value = app
        .store
        .get(&key("session_mode", sid))
        .await?
        .unwrap_or_else(default_session_mode);
    if !matches!(value["mode"].as_str(), Some("write" | "read_only"))
        || value["revision"].as_u64().is_none()
    {
        return Err(anyhow!("Invalid session capability mode"));
    }
    Ok(value)
}
async fn effective_policy(app: &App, sid: &str) -> Result<(Value, u64)> {
    let mut policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    let mode = session_mode(app, sid).await?;
    if mode["mode"] == "read_only" {
        for schema in tools::schemas() {
            let name = schema["name"].as_str().unwrap_or("");
            if !tools::is_read(name) {
                policy["rules"][name] = json!("deny");
            }
        }
    }
    Ok((policy, mode["revision"].as_u64().unwrap()))
}
fn marker(app: &App, shutoff: bool) -> Result<()> {
    let lock = File::open(app.control_dir.join("dispatch.lock"))?;
    lock.lock_exclusive()?;
    let result = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(app.control_dir.join("shutoff"))?;
        f.write_all(if shutoff { b"1" } else { b"0" })?;
        f.sync_all()?;
        Ok(())
    })();
    FileExt::unlock(&lock)?;
    result
}
impl App {
    async fn run(&self, id: &str) -> Result<Value> {
        self.store
            .get(&key("run", id))
            .await?
            .ok_or_else(|| anyhow!("Run not found"))
    }
    async fn shutoff(&self) -> Result<bool> {
        Ok(self
            .store
            .get("control")
            .await?
            .unwrap_or(json!({"shutoff":true}))["shutoff"]
            .as_bool()
            .unwrap_or(true))
    }
    async fn save(&self, run: &mut Value) -> Result<()> {
        run["updated_at"] = json!(now());
        let public=public_run(run);
        self.store
            .save_run(run, "run_status", &public)
            .await
    }
    async fn save_override(&self, run:&mut Value) -> Result<()> {
        run["updated_at"]=json!(now());
        let public=public_run(run);
        self.store.save_run_override(run,"run_status",&public).await
    }
    async fn selection(&self) -> Result<provider::ProviderSelection> {
        let value = self.store.agent_model().await?;
        provider::ProviderSelection::parse(&value, DEFAULT_AGENT_MODEL)
    }
    async fn run_candidate(&self, run: &Value) -> Result<Option<model_selection::Candidate>> {
        // Recovery cannot invent a role for an old run that never pinned one.
        let Some(value) = run.get("selection_plan").filter(|v| !v.is_null()) else {
            return Err(anyhow!("Legacy run has no pinned role; start a new message after configuring Standard"));
        };
        let plan: model_selection::SelectionPlan = serde_json::from_value(value.clone())?;
        let state: model_selection::FailoverState = serde_json::from_value(run["failover_state"].clone())?;
        if !matches!(plan.version,1|2) || plan.agent_id != run["agent_id"].as_str().unwrap_or("") {
            return Err(anyhow!("Invalid pinned model plan identity"));
        }
        let catalog = self.store.model_catalog().await?;
        // Membership, account ceiling and roster revocation remain live even for recovered plans.
        let candidate = plan.candidates.get(state.candidate_index).ok_or_else(||anyhow!("Candidate missing"))?;
        let access=self.store.live_access(self.store.current_user_id(),&plan.agent_id).await?;
        if !access.model_allowed(&candidate.upstream_model)
            || catalog.agent_models.get(&plan.agent_id).is_some_and(|m|!m.entry_ids.contains(&candidate.entry_id)) {
            return Err(anyhow!("Pinned model is no longer available to this caller"));
        }
        Ok(Some(plan.authorize_candidate(state.candidate_index, &catalog.entries, &catalog.connections)?.clone()))
    }
    async fn tokens_for(&self, selection: &provider::ProviderSelection, run: &Value) -> Result<Value> {
        let candidate = self.run_candidate(run).await?;
        if let Some(candidate) = &candidate {
            if selection.model != candidate.upstream_model || selection.provider != candidate.transport.provider_id() {
                return Err(anyhow!("Pinned model selection mismatch"));
            }
        }
        let reference = candidate.as_ref().map(|c| c.credential_ref.as_str()).unwrap_or("provider_tokens");
        match selection.provider {
            provider::ProviderId::Deepseek => {
                if candidate.is_none() { return Err(anyhow!("DeepSeek requires an explicit connection")); }
                let tokens = self.store.get(reference).await?.ok_or_else(|| provider::typed_failure(model_selection::FailureKind::InvalidCredentials, false, "Save a DeepSeek API key for this connection"))?;
                if tokens["api_key"].as_str().is_none_or(|s| s.is_empty()) { return Err(provider::typed_failure(model_selection::FailureKind::InvalidCredentials, false, "DeepSeek API key unavailable")); }
                Ok(tokens)
            }
            provider::ProviderId::OpenaiSubscription => self.tokens_at(reference).await
                .map_err(|_| provider::typed_failure(model_selection::FailureKind::InvalidCredentials,
                    false, "Assigned provider authorization unavailable; reconnect this connection")),
        }
    }
    async fn tokens_at(&self, reference: &str) -> Result<Value> {
        let _lock = self.refresh.lock().await;
        let mut tokens = self
            .store
            .get(reference)
            .await?
            .ok_or_else(|| anyhow!("Connect your ChatGPT subscription first"))?;
        if tokens["expires_at"].as_f64().unwrap_or(0.) < now() as f64 + 60. {
            tokens = self.provider.refresh(&tokens).await?;
            self.store.put(reference, &tokens).await?;
        }
        Ok(tokens)
    }
}
// Initialize the selected process role against the shared v3 state root.
async fn run_process() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("webagent_v3=info".parse()?),
        )
        .init();
    let root = PathBuf::from(std::env::var("V3_STATE_ROOT").unwrap_or_else(|_| "/state".into()));
    let role=std::env::var("V3_PROCESS_ROLE").unwrap_or_else(|_|"api".into());
    if role=="health" {
        let url=format!("sqlite://{}?mode=ro",root.join("db/app.db").display());
        let pool=sqlx::SqlitePool::connect(&url).await?;
        let age:Option<i64>=sqlx::query_scalar(
            "SELECT CAST(strftime('%s','now') AS INTEGER)-updated_at FROM loop_heartbeat WHERE singleton=1 AND protocol_version=1")
            .fetch_optional(&pool).await?;
        pool.close().await;
        return if age.is_some_and(|age|age>=0 && age<=10) {Ok(())}
            else {Err(anyhow!("V3 loop heartbeat is stale"))};
    }
    if !matches!(role.as_str(),"api"|"loop") {
        return Err(anyhow!("Invalid v3 process role"));
    }
    std::fs::create_dir_all(&root)?;
    let lock=if role=="loop" {
        let lock=OpenOptions::new().create(true).truncate(false).write(true)
            .open(root.join("runtime.lock"))?;
        lock.try_lock_exclusive()
            .map_err(|_|anyhow!("Another v3 loop owns this state"))?;
        Some(lock)
    } else {None};
    let store = Store::open(&root).await?;
    store.migrate_agent_storage(&root).await?;
    let (runtime, runtime_path) = loop_runtime::Config::initialize(&root, &store).await?;
    // One data-root authority, reread at call boundaries. Legacy JSON is only
    // a first-start migration source; existing invalid data is never overwritten.
    let compaction_config_path = context_compaction::Config::initialize(&root)?;
    store.configure_cache(runtime.cache.document_budget_bytes, runtime.cache.context_budget_bytes);
    // The temporary development bootstrap credential had no generation. Retire it
    // once so this installation follows the same first-run setup flow as v2.
    if store
        .get("admin")
        .await?
        .is_some_and(|v| v["generation"].is_null())
    {
        store.delete("admin").await?;
    }
    for (k, v) in [
        ("policy", policy_default()),
        ("settings", json!({"model":DEFAULT_AGENT_MODEL})),
        ("control", json!({"shutoff":false})),
    ] {
        if store.get(k).await?.is_none() {
            store.put(k, &v).await?;
        }
    }
    migrate_default_agent_model(&store).await?;
    migrate_provider_selection(&store).await?;
    // Catalog roles become authoritative once; later clears never restore legacy defaults.
    model_selection_api::migrate_legacy_default(&store).await?;
    migrate_tool_policy_defaults(&store).await?;
    let abilities_root = std::env::var("V3_ABILITIES_ROOT")
        .unwrap_or_else(|_| "/opt/v3/plugins/abilities".to_owned());
    let catalog = abilities::Catalog::load(&PathBuf::from(abilities_root))?;
    store.ensure_agent_connections(&catalog.ids()).await?;
    let max_active_runs = runtime.scheduler.max_active_runs;
    let app = App {
        runtime: Arc::new(RwLock::new(runtime)), runtime_path, compaction_config_path,
        browser_login_bridge: browser_login::Bridge::configure(
            &std::env::var("V3_BROWSER_LOGIN_TOKEN").unwrap_or_default(),
            &std::env::var("V3_BROWSER_LOGIN_CALLER_ID").unwrap_or_default(),
        )?,
        message_api_token_hash: message_api_token_hash(&std::env::var("V3_MESSAGE_API_TOKEN").unwrap_or_default())?,
        store,
        abilities: Arc::new(catalog),
        gate: Arc::new(Mutex::new(())),
        provider: provider::Provider::new(),
        refresh: Arc::new(Mutex::new(())),
        login: Arc::new(RwLock::new(None)),
        active: Arc::new(Mutex::new(HashMap::new())),
        slots: Arc::new(Semaphore::new(max_active_runs)),
        ready: Arc::new(AtomicBool::new(false)),
        stop: CancellationToken::new(),
        login_attempts: Arc::new(Mutex::new(Vec::new())),
        control_dir: PathBuf::from(
            std::env::var("V3_CONTROL_ROOT").unwrap_or_else(|_| "/run/v3/control".into()),
        ),
        tool_socket: PathBuf::from(
            std::env::var("V3_TOOL_SOCKET").unwrap_or_else(|_| "/run/v3/worker/tools.sock".into()),
        ),
        promotion_socket: PathBuf::from(
            std::env::var("V3_PROMOTE_SOCKET")
                .unwrap_or_else(|_| "/run/v3/promote/promote.sock".into()),
        ),
        secure: std::env::var("V3_SECURE_COOKIE").unwrap_or_else(|_| "true".into()) != "false",
        chat_ui_config_path: PathBuf::from(
            std::env::var("V3_CHAT_UI_CONFIG_PATH")
                .unwrap_or_else(|_| "/state/config/chat.json".into()),
        ),
    };
    if role=="loop" {
        marker(&app, app.shutoff().await?)?;
        recover(&app, true).await?;
        app.ready.store(true, Ordering::Release);
        let scheduler=tokio::spawn(scheduler(app.clone()));
        let mut signal=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {_=signal.recv()=>{},_=tokio::signal::ctrl_c()=>{}}
        app.ready.store(false, Ordering::Release);
        let _=scheduler.await;
        let deadline=tokio::time::Instant::now()+Duration::from_secs(12);
        while !app.active.lock().await.is_empty() && tokio::time::Instant::now()<deadline {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let _=marker(&app,true);
        app.stop.cancel();
        for token in app.active.lock().await.values() {token.cancel();}
        let _=app.store.clear_loop_heartbeat().await;
        tokio::time::sleep(Duration::from_secs(1)).await;
        app.store.pool.close().await;
        app.store.agent_pool.close().await;
        app.store.app_pool.close().await;
        drop(lock);
        return Ok(());
    }
    recover_promotions(&app).await?;
    app.ready.store(true, Ordering::Release);
    let listener = tokio::net::TcpListener::bind(
        std::env::var("V3_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into()),
    )
    .await?;
    tracing::info!("WebAgent v3 API ready");
    let administrator_watch=tokio::spawn(administrator_services::watch(app.clone()));
    let shutdown = app.clone();
    axum::serve(listener, router(app.clone()))
        .with_graceful_shutdown(async move {
            let mut signal =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("signal handler");
            tokio::select! {_=signal.recv()=>{},_=tokio::signal::ctrl_c()=>{}}
            shutdown.ready.store(false, Ordering::Release);
        })
        .await?;
    app.stop.cancel();
    let _=administrator_watch.await;
    app.store.pool.close().await;
    app.store.agent_pool.close().await;
    app.store.app_pool.close().await;
    Ok(())
}
