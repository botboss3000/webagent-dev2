//! HTTP handlers for sessions operations.
use super::*;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SessionSearchArgs {
    q: String, status: Option<String>, limit: Option<usize>, continuation: Option<String>,
}
pub(crate) async fn session_search_api(State(app): State<App>, Query(args): Query<SessionSearchArgs>) -> Api {
    let mut page = app.store.search_private_sessions(&args.q, args.status.as_deref().unwrap_or("active"), args.limit.unwrap_or(20), args.continuation.as_deref()).await
        .map_err(|error| if error.downcast_ref::<crate::store::search::InvalidSearch>().is_some() { bad("Invalid or expired search parameters/continuation") } else { ApiError::from(error) })?;
    let mut results = Vec::new();
    let mut admitted = std::collections::HashMap::new();
    for hit in page["results"].as_array().unwrap() {
        let agent = hit["agent_id"].as_str().unwrap_or("shared_default");
        let allowed = if let Some(allowed) = admitted.get(agent) { *allowed } else {
            let allowed = app.store.live_access(app.store.current_user_id(), agent).await.map(|a| a.admitted()).unwrap_or(false);
            admitted.insert(agent.to_owned(), allowed); allowed
        };
        if allowed { results.push(hit.clone()); }
    }
    page["results"] = json!(results);
    page["server_time"] = json!(now());
    Ok(Json(page))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SessionListArgs { pub status: Option<String> }
pub(crate) async fn sessions(State(app): State<App>, Query(args): Query<SessionListArgs>) -> Api {
    let recycled = match args.status.as_deref().unwrap_or("active") {
        "active" => false, "recycled" => true, _ => return Err(bad("Invalid session status")),
    };
    let list = app.store.ordered_sessions_status(recycled).await?;
    Ok(Json(json!({"sessions":list,"server_time":now()})))
}
pub(crate) async fn patch_session_api(State(app): State<App>, Path(id): Path<String>,
    Json(body): Json<crate::store::session::SessionPatch>) -> Api {
    use crate::store::session::SessionPatchOutcome;
    match app.store.patch_session(&id, &body).await? {
        SessionPatchOutcome::Saved(value) => Ok(Json(value)),
        SessionPatchOutcome::NotFound => Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into())),
        SessionPatchOutcome::Conflict(message) => Err(conflict(message)),
        SessionPatchOutcome::Invalid(message) => Err(bad(message)),
    }
}
pub(crate) async fn create_session(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let request_id = required(&body, "client_request_id", 128)?;
    let title = body["title"].as_str().unwrap_or("New chat");
    if title.len() > 200 {
        return Err(bad("Title too long"));
    }
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:session:{request_id}");
    if let Some(receipt) = app.store.get(&receipt_key).await? {
        if receipt["digest"] != digest(&body) {
            return Err(conflict("Request identifier reused with different input"));
        }
        return Ok(Json(receipt["result"].clone()));
    }
    let agent_id = body.get("agent_id").map(|v| v.as_str().ok_or_else(|| bad("Invalid agent ID")))
        .transpose()?.unwrap_or("shared_default");
    let access=app.store.live_access(app.store.current_user_id(),agent_id).await
        .map_err(|_|ApiError(StatusCode::FORBIDDEN,"Agent unavailable".into()))?;
    if !access.admitted() { return Err(ApiError(StatusCode::FORBIDDEN,"Agent unavailable".into())); }
    if app.store.list("session:").await?.len() >= access.max_sessions() as usize {
        return Err(conflict("Session limit reached"));
    }
    let agent_store=app.store.for_agent(agent_id).await
        .map_err(|_|bad("Agent not found"))?;
    let exists: Option<String> = sqlx::query_scalar("SELECT id FROM agents WHERE id=?")
        .bind(agent_id).fetch_optional(&agent_store.agent_pool).await.map_err(anyhow::Error::from)?;
    if exists.is_none() { return Err(bad("Agent not found")); }
    let id = uuid();
    let value = json!({"id":id,"agent_id":agent_id,"title":title,"checkpoint_input":[],"checkpoint_through":0,"created_at":now()});
    let result = json!({"id":id,"agent_id":agent_id,"title":title});
    app.store
        .atomic_docs_event(
            vec![
                (key("session", &id), value),
                (key("session_mode", &id), {
                    let config=crate::execution_modes::config(&app.store,agent_id).await?;
                    let chat=crate::execution_modes::chat_preferences(&app.chat_ui_config_path).await?;
                    let default=chat.default_mode.as_deref().unwrap_or(config["default_execution_mode"].as_str().unwrap());
                    let entry=crate::execution_modes::resolve(&config,default).map_err(|_|bad("Unknown chat default execution mode"))?;
                    json!({"mode":entry["permission_policy"],"execution_mode":entry["id"],"revision":0})
                }),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            &id,
            "session_created",
            &result,
        )
        .await?;
    Ok(Json(result))
}
pub(crate) async fn snapshot(State(app): State<App>, Path(id): Path<String>) -> Api {
    let _guard = app.gate.lock().await;
    let _session = app
        .store
        .get(&key("session", &id))
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Session not found".into()))?;
    let (mut messages, last_seq) = app.store.chat_snapshot(&id).await?;
    uploads::message_files(&app,&mut messages).await?;
    let usage=crate::usage::public(&app.store.get(&format!("session_usage:{id}")).await?.unwrap_or(Value::Null));
    let runs = public_runs(&app,app.store.recent_runs(Some(&id)).await?).await?;
    let components = app.store.base_chat_components(&id).await?;
    Ok(Json(
        json!({"usage":usage,"session":app.store.session_read_summary(&id).await?,
        "messages":messages,"runs":runs,"components":components,"last_seq":last_seq}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InspectArgs {
    resource: String,
    after: Option<i64>,
    limit: Option<i64>,
}
pub(crate) fn validate_inspect_args(args: &InspectArgs) -> std::result::Result<(), ApiError> {
    match args.resource.as_str() {
        "catalog" | "session" if args.after.is_none() && args.limit.is_none() => Ok(()),
        "chat" if args.after.unwrap_or(0) >= 0 && (1..=50).contains(&args.limit.unwrap_or(20)) => {
            Ok(())
        }
        "runs" if args.after.is_none() && (1..=50).contains(&args.limit.unwrap_or(20)) => Ok(()),
        _ => Err(bad("Invalid state inspection resource or bounds")),
    }
}
pub(crate) fn preview(value: &Value, max_chars: usize) -> Value {
    let raw = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    let length = raw.chars().count();
    let text: String = raw.chars().take(max_chars).collect();
    json!({"text":text,"truncated":length > max_chars})
}
pub(crate) async fn inspect_state(app: &App, sid: &str, args: &InspectArgs) -> Result<Value> {
    match args.resource.as_str() {
        "catalog" => {
            app.store.session_summary(sid).await?;
            Ok(json!({"resources":[
                {"name":"session","description":"Current session title, creation time and chat cursor"},
                {"name":"chat","description":"Paginated current-session message previews","after":"nonnegative chat sequence","limit":"1-50"},
                {"name":"runs","description":"Recent current-session run status summaries","limit":"1-50"}
            ]}))
        }
        "session" => app.store.session_summary(sid).await,
        "chat" => {
            let mut page = app
                .store
                .chat_page(sid, args.after.unwrap_or(0), args.limit.unwrap_or(20))
                .await?;
            if let Some(messages) = page["messages"].as_array_mut() {
                for message in messages {
                    let content = preview(&message["content"], 512);
                    message.as_object_mut().unwrap().remove("content");
                    message["content_preview"] = content;
                }
            }
            Ok(page)
        }
        "runs" => {
            let mut runs = app
                .store
                .run_summaries(sid, args.limit.unwrap_or(20))
                .await?;
            for run in &mut runs {
                if !run["error"].is_null() {
                    let error = preview(&run["error"], 512);
                    run["error"] = error;
                }
            }
            Ok(json!({"runs":runs}))
        }
        _ => Err(anyhow!("Invalid state inspection resource")),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptReadArgs {
    action: String,
    slot_name: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptWriteArgs {
    action: String,
    slot_name: String,
    expected_revision: i64,
    content: Option<String>,
    order_index: Option<i64>,
    lock: Option<bool>,
    merge_mode: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptSlotPutBody {
    content: String,
    expected_revision: i64,
    order_index: Option<i64>,
    lock: Option<bool>,
    merge_mode: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptSlotDeleteBody {
    expected_revision: i64,
}
pub(crate) fn valid_slot_name(name: &str) -> bool {
    !name.is_empty() && name == name.trim() && name.len() <= 128 && name != "__skills__"
}
pub(crate) fn valid_prompt_read(args: &PromptReadArgs) -> bool {
    match args.action.as_str() {
        "list" => args.slot_name.is_none(),
        "get" => args.slot_name.as_deref().is_some_and(valid_slot_name),
        _ => false,
    }
}
pub(crate) fn valid_prompt_write(args: &PromptWriteArgs) -> bool {
    if !valid_slot_name(&args.slot_name)
        || args.expected_revision < 0
        || args
            .order_index
            .is_some_and(|order| !(0..=1_000_000).contains(&order))
        || args
            .merge_mode
            .as_deref()
            .is_some_and(|mode| !matches!(mode, "replace" | "append"))
    {
        return false;
    }
    match args.action.as_str() {
        "put" => args
            .content
            .as_ref()
            .is_some_and(|content| content.len() <= 16 * 1024),
        "delete" => {
            args.expected_revision > 0
                && args.content.is_none()
                && args.order_index.is_none()
                && args.lock.is_none()
                && args.merge_mode.is_none()
        }
        _ => false,
    }
}
pub(crate) fn prompt_slot_error(error: anyhow::Error) -> ApiError {
    let message = error.to_string();
    if message == "Prompt slot revision mismatch" || message == "Prompt slot not found" {
        conflict("Prompt slot changed; reload it and retry")
    } else {
        error.into()
    }
}
pub(crate) async fn execute_prompt_slot(app: &App, name: &str, args: &Value) -> Result<Value> {
    // A tool grant controls use; editing agent configuration also requires tenant authority.
    if name=="prompt_slot_write" && !app.store.agent_authority(app.store.current_user_id(),app.store.current_agent_id()).await?
        ["can_manage"].as_bool().unwrap_or(false) {
        return Ok(json!({"status":"denied","output":"Agent administration required to edit prompts"}));
    }
    if name == "prompt_slot_read" {
        let Ok(query) = serde_json::from_value::<PromptReadArgs>(args.clone()) else {
            return Ok(json!({"status":"denied","output":"Invalid prompt slot read arguments"}));
        };
        if !valid_prompt_read(&query) {
            return Ok(json!({"status":"denied","output":"Invalid prompt slot read arguments"}));
        }
        let output = if query.action == "list" {
            json!({"slots":app.store.list_prompt_slots().await?})
        } else {
            json!({"slot":app.store.get_prompt_slot(query.slot_name.as_deref().unwrap()).await?})
        };
        Ok(json!({"status":"success","output":output}))
    } else {
        let Ok(command) = serde_json::from_value::<PromptWriteArgs>(args.clone()) else {
            return Ok(json!({"status":"denied","output":"Invalid prompt slot write arguments"}));
        };
        if !valid_prompt_write(&command) {
            return Ok(json!({"status":"denied","output":"Invalid prompt slot write arguments"}));
        }
        let result = if command.action == "put" {
            app.store
                .upsert_prompt_slot(
                    &command.slot_name,
                    command.content.as_deref().unwrap(),
                    command.order_index,
                    command.lock,
                    command.merge_mode.as_deref(),
                    command.expected_revision,
                )
                .await
                .map(|slot| json!({"slot":slot}))
        } else {
            app.store
                .delete_prompt_slot(&command.slot_name, command.expected_revision)
                .await
                .map(|deleted| json!({"deleted":deleted,"slot_name":command.slot_name}))
        };
        match result {
            Ok(output) => Ok(json!({"status":"success","output":output})),
            Err(error)
                if matches!(
                    error.to_string().as_str(),
                    "Prompt slot revision mismatch" | "Prompt slot not found"
                ) =>
            {
                Ok(
                    json!({"status":"denied","output":"Prompt slot changed; read current revision and retry"}),
                )
            }
            Err(error) => Err(error),
        }
    }
}
pub(crate) async fn prompt_slots_api(State(app): State<App>, Path(id): Path<String>) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    Ok(Json(json!({"slots":app.store.list_prompt_slots().await?})))
}
pub(crate) async fn prompt_slot_api(State(app): State<App>, Path((id, name)): Path<(String, String)>) -> Api {
    if !valid_slot_name(&name) {
        return Err(ApiError(
            StatusCode::NOT_FOUND,
            "Prompt slot not found".into(),
        ));
    }
    let app=admin_agent_view(&app,&id).await?;
    let slot = app
        .store
        .get_prompt_slot(&name)
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Prompt slot not found".into()))?;
    Ok(Json(json!({"slot":slot})))
}
pub(crate) async fn prompt_slot_put_api(
    State(app): State<App>,
    Path((id, name)): Path<(String, String)>,
    Json(body): Json<PromptSlotPutBody>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let command = PromptWriteArgs {
        action: "put".into(),
        slot_name: name,
        content: Some(body.content),
        expected_revision: body.expected_revision,
        order_index: body.order_index,
        lock: body.lock,
        merge_mode: body.merge_mode,
    };
    if !valid_prompt_write(&command) {
        return Err(bad("Invalid prompt slot or revision"));
    }
    let slot = app
        .store
        .upsert_prompt_slot(
            &command.slot_name,
            command.content.as_deref().unwrap(),
            command.order_index,
            command.lock,
            command.merge_mode.as_deref(),
            command.expected_revision,
        )
        .await
        .map_err(prompt_slot_error)?;
    Ok(Json(json!({"slot":slot})))
}
pub(crate) async fn prompt_slot_delete_api(
    State(app): State<App>,
    Path((id, name)): Path<(String, String)>,
    Json(body): Json<PromptSlotDeleteBody>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let command = PromptWriteArgs {
        action: "delete".into(),
        slot_name: name,
        content: None,
        expected_revision: body.expected_revision,
        order_index: None,
        lock: None,
        merge_mode: None,
    };
    if !valid_prompt_write(&command) {
        return Err(bad("Invalid prompt slot or revision"));
    }
    let deleted = app
        .store
        .delete_prompt_slot(&command.slot_name, command.expected_revision)
        .await
        .map_err(prompt_slot_error)?;
    Ok(Json(json!({"deleted":deleted})))
}
pub(crate) async fn state_inspect_api(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(args): Query<InspectArgs>,
) -> Api {
    validate_inspect_args(&args)?;
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    if policy_decision(&policy, "state_inspect") != "allow" {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "State inspection is unavailable under current policy".into(),
        ));
    }
    Ok(Json(inspect_state(&app, &id, &args).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SessionAbilityBody {
    pub(crate) state: String,
    pub(crate) expected_revision: u64,
}
pub(crate) async fn session_ability_set(
    State(app): State<App>,
    Path((id, ability_id)): Path<(String, String)>,
    Json(body): Json<SessionAbilityBody>,
) -> Api {
    if !app.abilities.known(&ability_id) {
        return Err(bad("Unknown ability ID"));
    }
    if !matches!(body.state.as_str(), "active" | "suppressed" | "default") {
        return Err(bad("Invalid session ability state"));
    }
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let mut state = session_ability_state(&app, &id).await?;
    if state["revision"] != body.expected_revision {
        return Err(conflict("Session ability revision changed"));
    }
    let old_active = state["active"].as_array().unwrap();
    let old_suppressed = state["suppressed"].as_array().unwrap();
    let desired = body.state.as_str();
    let unchanged = match desired {
        "active" => {
            old_active
                .iter()
                .any(|v| v.as_str() == Some(ability_id.as_str()))
                && !old_suppressed
                    .iter()
                    .any(|v| v.as_str() == Some(ability_id.as_str()))
        }
        "suppressed" => {
            old_suppressed
                .iter()
                .any(|v| v.as_str() == Some(ability_id.as_str()))
                && !old_active
                    .iter()
                    .any(|v| v.as_str() == Some(ability_id.as_str()))
        }
        _ => {
            !old_active
                .iter()
                .any(|v| v.as_str() == Some(ability_id.as_str()))
                && !old_suppressed
                    .iter()
                    .any(|v| v.as_str() == Some(ability_id.as_str()))
        }
    };
    if unchanged {
        if desired=="suppressed" {phase3_api::quiesce_session_resources(&app,&id).await?;}
        return Ok(Json(state));
    }
    state["active"]
        .as_array_mut()
        .unwrap()
        .retain(|v| v.as_str() != Some(ability_id.as_str()));
    state["suppressed"]
        .as_array_mut()
        .unwrap()
        .retain(|v| v.as_str() != Some(ability_id.as_str()));
    if desired != "default" {
        state[if desired == "active" {
            "active"
        } else {
            "suppressed"
        }]
        .as_array_mut()
        .unwrap()
        .push(json!(ability_id));
    }
    state["revision"] = json!(state["revision"].as_u64().unwrap() + 1);
    let mut docs = vec![(key("session_ability_state", &id), state.clone())];
    for view in app.store.active_runs().await? {
        if view["session_id"] != id || view["status"] != "waiting_approval" {
            continue;
        }
        let run = invalidate_waiting_run(app.run(view["id"].as_str().unwrap()).await?);
        docs.push((key("run", run["id"].as_str().unwrap()), run));
    }
    app.store
        .atomic_docs_event(docs, &id, "session_abilities_changed", &state)
        .await?;
    phase3_api::quiesce_session_resources(&app,&id).await?;
    Ok(Json(state))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SessionModeBody {
    pub(crate) mode: String,
    #[serde(default)] pub(crate) expected_revision: Option<u64>,
    #[serde(default)] pub(crate) confirm_write: bool,
}
pub(crate) async fn session_mode_get(State(app): State<App>, Path(id): Path<String>) -> Api {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    Ok(Json(session_mode(&app, &id).await?))
}
pub(crate) async fn session_mode_set(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(body): Json<SessionModeBody>,
) -> Api {
    change_session_mode_authorized(&app,&id,&body.mode,body.expected_revision,None,body.confirm_write).await
}
#[derive(Clone, Copy)]
pub(crate) struct ModeCall<'a> { pub run_id: &'a str, pub generation: &'a str, pub pending_id: &'a str }


pub(crate) async fn change_session_mode(
    app: &App, id: &str, mode: &str, expected_revision: Option<u64>, tool: Option<ModeCall<'_>>,
) -> Api {
    change_session_mode_authorized(app,id,mode,expected_revision,tool,true).await
}
async fn change_session_mode_authorized(
    app: &App, id: &str, mode: &str, expected_revision: Option<u64>, tool: Option<ModeCall<'_>>,
    human_confirmed: bool,
) -> Api {
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let current = session_mode(&app, &id).await?;
    let agent=session_agent_id(app,id).await?;
    let config=crate::execution_modes::config(&app.store,&agent).await?;
    let named=if matches!(mode,"write"|"read_only") {None} else {
        Some(crate::execution_modes::resolve(&config,mode).map_err(|_|bad("Unknown execution mode"))?)
    };
    let target_policy=named.as_ref().map(|m|m["permission_policy"].as_str().unwrap()).unwrap_or(mode);
    // A chat.json setting may waive only the browser confirmation for Auto.
    // Revision fencing and model-call authorization remain mandatory.
    let confirm_auto=if tool.is_none() && named.as_ref().is_some_and(|entry|entry["id"]=="auto") {
        crate::execution_modes::chat_preferences(&app.chat_ui_config_path).await?.confirm_auto_mode
    } else { true };
    if tool.is_none() && current["mode"]=="read_only" && target_policy=="write"
        && (expected_revision.is_none() || (confirm_auto && !human_confirmed)) {
        return Err(ApiError(StatusCode::FORBIDDEN,"Confirm write-capable mode with the current revision".into()));
    }
    let receipt = tool.map(|call| {
        let key = format!("receipt:session_mode:{id}:{}",call.pending_id);
        let hash = digest(&json!([id,mode,expected_revision,call.run_id,call.generation,call.pending_id]));
        (key,hash)
    });
    if let Some((receipt_key,hash)) = &receipt {
        if let Some(saved) = app.store.get(receipt_key).await? {
            if saved["request_hash"]!=*hash { return Err(conflict("Mode call receipt mismatch")); }
            if mode=="read_only" {phase3_api::quiesce_session_resources(app,id).await?;}
            return Ok(Json(saved["result"].clone()));
        }
    }
    if let Some(call) = tool {
        if expected_revision.is_none()
            || uuid::Uuid::parse_str(call.run_id).is_err()
            || uuid::Uuid::parse_str(call.pending_id).is_err()
            || call.generation.is_empty() {
            return Err(bad("Invalid mode call identity or revision"));
        }
        let agent = session_agent_id(app,id).await?;
        let run = app.run(call.run_id).await?;
        let (policy,mode_revision,visibility,state)=ability_controls(app,id).await?;
        let access=app.store.live_access(app.store.current_user_id(),&agent).await?;
        if run["session_id"]!=id || run["agent_id"]!=agent
            || run["generation"]!=call.generation
            || run["status"]!="running" || run["phase"]!="tools"
            || run["pending"]["id"]!=call.pending_id
            || run["pending"]["name"]!="set_execution_mode"
            || run["pending"]["state"]!="executing"
            || run["pending"]["policy_version"]!=policy["version"]
            || run["pending"]["mode_revision"]!=json!(mode_revision)
            || run["pending"]["visibility_revision"]!=visibility["revision"]
            || run["pending"]["ability_state_revision"]!=state["revision"]
            || run["pending"]["access_revision"]!=json!(access.revision.clone())
            || !access.admitted()
            || live_tool_decision(app,&access,"set_execution_mode",&policy,&visibility,
                &state,&run["loaded_abilities"],current["mode"]=="read_only")
                .permission==access::Permission::Deny {
            return Err(conflict("Mode call authorization changed"));
        }
        if current["mode"]=="read_only" && target_policy=="write"
            && run["pending"]["human_approved"]!=true {
            return Err(ApiError(StatusCode::FORBIDDEN,"Write mode requires approval".into()));
        }
    }
    if expected_revision.is_some_and(|revision| current["revision"].as_u64()!=Some(revision)) {
        return Err(conflict("Session mode revision changed"));
    }
    if current["mode"] == mode && named.is_none() && current["execution_mode"].is_null() {
        if mode=="read_only" {phase3_api::quiesce_session_resources(&app,&id).await?;}
        return Ok(Json(current));
    }
    let mut next = json!({"mode":target_policy,"revision":current["revision"].as_u64().unwrap()+1});
    if let Some(entry)=named {next["execution_mode"]=entry["id"].clone();}
    let mut docs = vec![(key("session_mode", &id), next.clone())];
    if let Some((receipt_key,hash)) = receipt {
        docs.push((receipt_key,json!({"request_hash":hash,"result":next})));
    }

    for view in app.store.active_runs().await? {
        if view["session_id"] != id || view["status"] != "waiting_approval" {
            continue;
        }
        let mut run = app.run(view["id"].as_str().unwrap()).await?;
        run["pending"]["state"] = json!("requested");
        run["pending"]["decision"] = Value::Null;
        run["status"] = json!("queued");
        run["updated_at"] = json!(now());
        docs.push((key("run", run["id"].as_str().unwrap()), run));
    }
    app.store
        .atomic_docs_event(docs, &id, "session_mode_changed", &next)
        .await?;
    phase3_api::quiesce_session_resources(&app,&id).await?;
    Ok(Json(next))
}
pub(crate) async fn session_abilities(State(app): State<App>, Path(id): Path<String>) -> Api {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let (policy, _) = effective_policy(&app, &id).await?;
    let visibility = session_agent_abilities(&app, &id).await?;
    let state = session_ability_state(&app, &id).await?;
    let mode=session_mode(&app,&id).await?;
    let agent_id=session_agent_id(&app,&id).await?;
    let access=app.store.live_access(app.store.current_user_id(),&agent_id).await?;
    let mut abilities=app.abilities.catalog_with(&policy,&visibility,&state);
    abilities.retain_mut(|entry|filter_ability_entry(&app,&access,entry,
        &policy,&visibility,&state,&json!([]),mode["mode"]=="read_only"));
    Ok(Json(json!({"mode":mode,
        "catalog_version":app.abilities.version(),
        "visibility_revision":visibility["revision"],"state":state,
        "abilities":abilities})))
}
pub(crate) async fn source_review(State(app): State<App>, Path(id): Path<String>) -> Api {
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    if policy_decision(&policy, "source_review") != "allow" {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "Source review is unavailable under current policy".into(),
        ));
    }
    if app.shutoff().await? {
        return Err(conflict("Global shutoff is active"));
    }
    drop(_guard);
    let result = tools::execute(
        "source_review",
        &json!({}),
        &id,
        &app.tool_socket,
        CancellationToken::new(),
    )
    .await?;
    Ok(Json(result))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SessionReadBody { pub through_seq: u64 }
pub(crate) async fn session_read_api(State(app): State<App>, Path(id): Path<String>, Json(body): Json<SessionReadBody>) -> Api {
    use crate::store::session::SessionPatchOutcome;
    match app.store.acknowledge_session_read(&id, body.through_seq).await? {
        SessionPatchOutcome::Saved(value) => Ok(Json(value)),
        SessionPatchOutcome::NotFound => Err(ApiError(StatusCode::NOT_FOUND,"Session not found".into())),
        SessionPatchOutcome::Invalid(message) => Err(bad(message)),
        SessionPatchOutcome::Conflict(message) => Err(conflict(message)),
    }
}

pub(crate) async fn recycle_session_api(State(app): State<App>, Path(id): Path<String>, Json(body): Json<crate::store::session::SessionLifecycle>) -> Api {
    lifecycle_session_api(app,id,body,false).await
}
pub(crate) async fn restore_session_api(State(app): State<App>, Path(id): Path<String>, Json(body): Json<crate::store::session::SessionLifecycle>) -> Api {
    lifecycle_session_api(app,id,body,true).await
}
async fn lifecycle_session_api(app: App, id: String, body: crate::store::session::SessionLifecycle, restore: bool) -> Api {
    use crate::store::session::SessionPatchOutcome;
    match app.store.lifecycle_session(&id,&body,restore).await? {
        SessionPatchOutcome::Saved(value) => Ok(Json(value)),
        SessionPatchOutcome::NotFound => Err(ApiError(StatusCode::NOT_FOUND,"Session not found".into())),
        SessionPatchOutcome::Conflict(message) => Err(conflict(message)),
        SessionPatchOutcome::Invalid(message) => Err(bad(message)),
    }
}
