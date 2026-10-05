//! HTTP handlers for admin operations.
use super::*;
pub(crate) async fn overview(State(app): State<App>) -> Api {
    // Installation status must not reveal another owner\'s device authorization code.
    let login=app.login.read().await.clone().filter(|v|v["connection_id"].is_null()).map(|v|json!({"verification_url":v["verification_url"],"user_code":v["user_code"],"status":v["status"]}));
    let mut runs = app.store.active_runs().await?;
    runs.extend(app.store.recent_runs(None).await?);
    let runs = runs.iter().map(public_run).collect::<Vec<_>>();
    Ok(Json(
        json!({"shutoff":app.shutoff().await?,"provider":{"connected":app.store.get("provider_tokens").await?.is_some(),"login":login},
        "model":app.store.agent_model().await?["model"],"policy":app.store.get("policy").await?,"runs":runs}),
    ))
}
pub(crate) fn requested_selection(body: &Value) -> std::result::Result<provider::ProviderSelection, ApiError> {
    required(body, "model", 128)?;
    provider::ProviderSelection::parse(body, DEFAULT_AGENT_MODEL)
        .map_err(|_| bad("Invalid model or unsupported provider"))
}
pub(crate) async fn settings(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let selection = requested_selection(&body)?;
    let _guard = app.gate.lock().await;
    let value = selection.value();
    app.store.save_agent_model(&value).await?;
    Ok(Json(value))
}
pub(crate) async fn admin_agent_view(app:&App,id:&str) -> std::result::Result<App,ApiError> {
    let mut scoped=app.clone();
    scoped.store=app.store.for_agent(id).await
        .map_err(|_|ApiError(StatusCode::NOT_FOUND,"Agent not found".into()))?;
    let exists:Option<String>=sqlx::query_scalar("SELECT id FROM agents WHERE id=?")
        .bind(id).fetch_optional(&scoped.store.agent_pool).await.map_err(anyhow::Error::from)?;
    if exists.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND,"Agent not found".into()));
    }
    Ok(scoped)
}
// Model-only persisted settings retain the original subscription default.
pub(crate) async fn agent_model(State(app): State<App>, Path(id): Path<String>) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let mut value = app.selection().await?.value();
    value["id"] = json!(id);
    Ok(Json(value))
}
pub(crate) async fn agent_model_set(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let selection = requested_selection(&body)?;
    let _guard = app.gate.lock().await;
    let value = selection.value();
    app.store.save_agent_model(&value).await?;
    let mut result = value;
    result["id"] = json!(id);
    Ok(Json(result))
}
// The original v3 seed used gpt-5.4. Move that exact old default once,
// while leaving any other explicitly selected model untouched.
pub(crate) async fn migrate_default_agent_model(store: &Store) -> Result<()> {
    const MARKER: &str = "migration:default_model_gpt_6_luna";
    if store.get(MARKER).await?.is_some() {
        return Ok(());
    }
    let mut value = store.agent_model().await?;
    if value["model"] == "gpt-5.4" {
        value["model"] = json!(DEFAULT_AGENT_MODEL);
        store.save_agent_model(&value).await?;
    }
    store.put(MARKER, &json!({"complete":true})).await?;
    Ok(())
}
pub(crate) async fn migrate_provider_selection(store: &Store) -> Result<()> {
    let value = store.agent_model().await?;
    let selection = provider::ProviderSelection::parse(&value, DEFAULT_AGENT_MODEL)?;
    let normalized = selection.value();
    if value != normalized {
        store.save_agent_model(&normalized).await?;
    }
    Ok(())
}

pub(crate) async fn migrate_tool_policy_defaults(store: &Store) -> Result<()> {
    let mut policy = store.get("policy").await?.unwrap_or_else(policy_default);
    let defaults = policy_default();
    let mut changed = false;
    for (name, decision) in defaults["rules"].as_object().unwrap() {
        if policy["rules"][name].is_null() {
            policy["rules"][name] = decision.clone();
            changed = true;
        }
    }
    if changed {
        policy["version"] = json!(policy["version"].as_u64().unwrap_or(0) + 1);
        store.put("policy", &policy).await?;
    }
    Ok(())
}
pub(crate) async fn ability_catalog(State(app): State<App>) -> Api {
    let policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    let visibility = agent_abilities(&app).await?;
    Ok(Json(json!({"catalog_version":app.abilities.version(),
        "visibility_revision":visibility["revision"],
        "abilities":app.abilities.catalog_with(&policy, &visibility, &Value::Null)})))
}
pub(crate) async fn agent_ability_catalog(State(app): State<App>, Path(id): Path<String>) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let visibility = agent_abilities(&app).await?;
    let policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    Ok(Json(json!({"catalog_version":app.abilities.version(),
        "revision":visibility["revision"],"visibility":visibility["visibility"],
        "skill_visibility":visibility["skill_visibility"],"tool_modes":visibility["tool_modes"],
        "abilities":app.abilities.configuration_catalog(&policy, &visibility)})))
}
// Same fresh-agent Prompt / Skills / Tools composition as the v2 Abilities
// overview. V3 has no provider tokenizer yet, so the numbers are explicit
// character-based estimates over the actual persisted slots and sent schemas.
pub(crate) fn estimated_tokens(text: &str) -> usize {
    text.chars().count().div_ceil(4)
}
pub(crate) async fn agent_context_composition(State(app): State<App>, Path(id): Path<String>) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    let slots = app.store.list_prompt_slots().await?;
    let visibility = agent_abilities(&app).await?;
    let policy = app.store.get("policy").await?.unwrap_or_else(policy_default);
    let mut prompt_sources = vec![json!({"label":"Base instructions",
        "tokens":estimated_tokens(provider::DEFAULT_INSTRUCTIONS)})];
    for slot in slots {
        let content = slot["content"].as_str().unwrap_or("").trim();
        if !content.is_empty() {
            prompt_sources.push(json!({"label":slot["slot_name"],
                "tokens":estimated_tokens(content)}));
        }
    }
    let guidance = app.abilities.visible_guidance(&policy, &visibility, &Value::Null);
    let skill_sources = if guidance.is_empty() { Vec::new() } else {
        vec![json!({"label":"Visible ability guidance","tokens":estimated_tokens(&guidance)})]
    };
    let tool_sources = app.abilities.schemas_with(
        &policy, &json!([]), &visibility, &Value::Null)
        .into_iter().map(|schema| {
            let name = schema["name"].as_str().unwrap_or("Tool");
            json!({"label":name,"tokens":estimated_tokens(&schema.to_string())})
        }).collect::<Vec<_>>();
    let sum = |sources: &Vec<Value>| -> usize {
        sources.iter().map(|source| source["tokens"].as_u64().unwrap_or(0) as usize).sum()
    };
    let prompt_tokens = sum(&prompt_sources);
    let skill_tokens = sum(&skill_sources);
    let tool_tokens = sum(&tool_sources);
    Ok(Json(json!({
        "tokens":prompt_tokens + skill_tokens + tool_tokens,
        "estimated":true,
        "breakdown":{"system_prompt":{"tokens":prompt_tokens},
            "skills":{"tokens":skill_tokens},"tools":{"tokens":tool_tokens}},
        "sources":{"system_prompt":prompt_sources,"skills":skill_sources,
            "tools":tool_sources}
    })))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct AbilityVisibilityBody {
    pub(crate) visibility: String,
    pub(crate) expected_revision: u64,
}
pub(crate) async fn agent_ability_visibility_set(
    State(app): State<App>,
    Path((id, ability_id)): Path<(String, String)>,
    Json(body): Json<AbilityVisibilityBody>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    if !app.abilities.known(&ability_id) {
        return Err(bad("Unknown ability ID"));
    }
    if !matches!(body.visibility.as_str(), "off" | "visible" | "discoverable") {
        return Err(bad("Invalid ability visibility"));
    }
    let _guard = app.gate.lock().await;
    let mut value = agent_abilities(&app).await?;
    if value["revision"] != body.expected_revision {
        return Err(conflict("Agent ability revision changed"));
    }
    let old = value["visibility"][&ability_id]
        .as_str()
        .unwrap_or("discoverable");
    if old == body.visibility {
        return Ok(Json(value));
    }
    value["revision"] = json!(body.expected_revision + 1);
    value["visibility"][&ability_id] = json!(body.visibility);
    app.store.save_agent_abilities(&value, body.expected_revision).await?;
    phase3_api::quiesce_agent(&app,&id).await?;
    Ok(Json(value))
}
pub(crate) async fn agent_ability_skill_visibility_set(
    State(app): State<App>,
    Path((id, ability_id)): Path<(String, String)>,
    Json(body): Json<AbilityVisibilityBody>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    if !app.abilities.known(&ability_id) {
        return Err(bad("Unknown ability ID"));
    }
    if !matches!(body.visibility.as_str(), "off" | "visible" | "discoverable") {
        return Err(bad("Invalid skill visibility"));
    }
    let _guard = app.gate.lock().await;
    let mut value = agent_abilities(&app).await?;
    if value["revision"] != body.expected_revision {
        return Err(conflict("Agent ability revision changed"));
    }
    let old = value["skill_visibility"][&ability_id].as_str().unwrap_or("visible");
    if old == body.visibility {
        return Ok(Json(value));
    }
    value["revision"] = json!(body.expected_revision + 1);
    value["skill_visibility"][&ability_id] = json!(body.visibility);
    app.store.save_agent_abilities(&value, body.expected_revision).await?;
    phase3_api::quiesce_agent(&app,&id).await?;
    Ok(Json(value))
}
pub(crate) async fn agent_tool_mode_set(
    State(app): State<App>,
    Path((id, ability_id, tool_name)): Path<(String, String, String)>,
    Json(body): Json<AbilityVisibilityBody>,
) -> Api {
    let app=admin_agent_view(&app,&id).await?;
    if !app.abilities.has_tool(&ability_id, &tool_name) {
        return Err(bad("Unknown ability tool"));
    }
    if !matches!(body.visibility.as_str(), "off" | "visible" | "discoverable") {
        return Err(bad("Invalid tool mode"));
    }
    let _guard = app.gate.lock().await;
    let mut value = agent_abilities(&app).await?;
    if value["revision"] != body.expected_revision {
        return Err(conflict("Agent ability revision changed"));
    }
    if value["tool_modes"][&tool_name] == body.visibility {
        return Ok(Json(value));
    }
    value["revision"] = json!(body.expected_revision + 1);
    value["tool_modes"][&tool_name] = json!(body.visibility);
    app.store.save_agent_abilities(&value, body.expected_revision).await?;
    phase3_api::quiesce_agent(&app,&id).await?;
    Ok(Json(value))
}
pub(crate) async fn policy_set(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let rules = body["rules"]
        .as_object()
        .ok_or_else(|| bad("rules required"))?;
    // Discovery is dispatched by Rust rather than tools::execute, but its
    // installation rules are still required by the same live permission check.
    let names: std::collections::BTreeSet<String> = tools::schemas().iter()
        .filter_map(|schema| schema["name"].as_str().map(str::to_owned))
        .chain(["list_abilities", "load_ability"].into_iter().map(str::to_owned))
        .collect();
    if rules.len() != names.len() {
        return Err(bad("Specify every tool policy"));
    }
    for name in &names {
        if !matches!(
            rules
                .get(name)
                .and_then(Value::as_str),
            Some("allow" | "deny" | "require_approval")
        ) {
            return Err(bad("Invalid policy rule"));
        }
    }
    let _guard = app.gate.lock().await;
    let old = app.store.get("policy").await?.unwrap();
    let value = json!({"version":old["version"].as_u64().unwrap()+1,"rules":rules});
    app.store.put("policy", &value).await?;
    app.store
        .event("", "policy_changed", &json!({"version":value["version"]}))
        .await?;
    for user_id in app.store.all_user_ids().await? {phase3_api::quiesce_user(&app,&user_id,None).await?;}
    Ok(Json(value))
}
#[cfg(test)]
mod policy_tests {
    use super::*;

    #[tokio::test]
    async fn policy_roundtrip_includes_discovery_and_preserves_restrictions() {
        let (_root, app) = crate::runtime_tests::fixture().await;
        let mut body = policy_default();
        body["rules"]["file_write"] = json!("allow");
        body["rules"]["shell"] = json!("require_approval");
        body["rules"]["python"] = json!("deny");
        body["rules"]["load_ability"] = json!("require_approval");
        let saved = policy_set(State(app.clone()), Json(body.clone())).await.unwrap().0;
        assert_eq!(saved["rules"], body["rules"]);
        assert_eq!(saved["version"], 2);
        assert_eq!(app.store.get("policy").await.unwrap().unwrap(), saved);
        let access = app.store.live_access("admin", "shared_default").await.unwrap();
        for (name, expected) in [("file_write", access::Permission::Auto),
            ("shell", access::Permission::Ask), ("python", access::Permission::Deny)] {
            let permission = access::Permission::from_policy(policy_decision(&saved, name)).unwrap();
            assert_eq!(access.tool_decision(name, permission, true, true, false, false).permission, expected);
        }
        assert_eq!(access.tool_decision("file_write", access::Permission::Auto,
            true, true, true, false).permission, access::Permission::Deny);

        for name in ["file_read", "list_abilities", "load_ability"] {
            let mut missing = body.clone();
            missing["rules"].as_object_mut().unwrap().remove(name);
            assert!(policy_set(State(app.clone()), Json(missing)).await.is_err());
        }
        let mut unknown = body.clone();
        unknown["rules"].as_object_mut().unwrap().remove("load_ability");
        unknown["rules"]["unknown_tool"] = json!("allow");
        assert!(policy_set(State(app.clone()), Json(unknown)).await.is_err());
        let mut invalid = body;
        invalid["rules"]["list_abilities"] = json!("invalid");
        assert!(policy_set(State(app.clone()), Json(invalid)).await.is_err());
        assert_eq!(app.store.get("policy").await.unwrap().unwrap(), saved);
    }
}

// Tenant connections are the sole credential authority after the catalog migration.
// Retire the global route so installation oversight cannot bypass connection ownership.
pub(crate) async fn provider_start(State(_app): State<App>) -> Api {
    Err(conflict("Use the selected agent's Models page to authorize a connection"))
}
/// A device authorization belongs to one connection. Never copy subscription tokens.
pub(crate) async fn provider_start_for(app: App, credential_ref: String, connection_id: Option<String>) -> Api {
    let mut login = app.login.write().await;
    if login.as_ref().is_some_and(|v| v["status"] == "pending") {
        return Err(conflict("Authorization already pending"));
    }
    let mut device = app.provider.device_start().await?;
    device["status"] = json!("pending");
    device["connection_id"] = json!(connection_id);
    let expires_at = device["expires_at"].as_u64().unwrap_or_else(|| now() + 900);
    let answer =
        json!({"verification_url":device["verification_url"],"user_code":device["user_code"],"expires_at":expires_at});
    *login = Some(device.clone());
    drop(login);
    tokio::spawn(async move {
        let expires = expires_at;
        let interval = device["interval"].as_u64().unwrap_or(5).max(5);
        loop {
            tokio::select! {_=app.stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(interval))=>{}}
            let current = app.login.read().await.clone();
            if current.as_ref().is_none_or(|v| {
                v["device_auth_id"] != device["device_auth_id"] || v["status"] != "pending"
            }) {
                break;
            }
            if now() > expires {
                if let Some(v) = app.login.write().await.as_mut() {
                    v["status"] = json!("expired");
                }
                break;
            }
            match app.provider.device_poll(&device).await {
                Ok(Some(tokens)) => {
                    // Serialize with catalog edits: revocation or replacing a connection
                    // during OAuth cannot authorize the changed record after the fact.
                    let _catalog_guard = app.gate.lock().await;
                    let allowed = match &connection_id {
                        Some(id) => app.store.model_catalog().await.ok().and_then(|c| c.connections.get(id).cloned())
                            .is_some_and(|c| !c.revoked && c.credential_ref == credential_ref),
                        None => true,
                    };
                    let _guard = app.refresh.lock().await;
                    let mut login = app.login.write().await;
                    if login.as_ref().is_some_and(|v| {
                        v["device_auth_id"] == device["device_auth_id"] && v["status"] == "pending"
                    }) {
                        if allowed && app.store.put(&credential_ref, &tokens).await.is_ok() {
                            if let Some(v) = login.as_mut() {
                                v["status"] = json!("connected");
                            }
                        } else if let Some(v) = login.as_mut() {
                            v["status"] = json!("failed");
                        }
                    }
                    break;
                }
                Ok(None) => {}
                Err(_) => {
                    let mut login = app.login.write().await;
                    if let Some(v) = login.as_mut() {
                        if v["device_auth_id"] == device["device_auth_id"] {
                            v["status"] = json!("failed");
                        }
                    }
                    break;
                }
            }
        }
    });
    Ok(Json(answer))
}
pub(crate) async fn provider_logout(State(_app): State<App>) -> Api {
    // Revocation belongs to the owner-scoped catalog, never a global token deletion.
    Err(conflict("Use the selected agent's Models page to revoke a connection"))
}

#[cfg(test)]
mod legacy_credential_tests {
    use super::*;
    // Installation-only legacy routes cannot change credentials behind an explicit role plan.
    #[tokio::test]
    async fn legacy_provider_routes_cannot_bypass_connection_ownership() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let before=app.store.get("provider_tokens").await.unwrap();
        *app.login.write().await=Some(json!({"connection_id":"tenant-connection","status":"pending"}));
        assert!(matches!(provider_start(State(app.clone())).await,Err(ApiError(StatusCode::CONFLICT,_))));
        assert!(matches!(provider_logout(State(app.clone())).await,Err(ApiError(StatusCode::CONFLICT,_))));
        assert_eq!(app.store.get("provider_tokens").await.unwrap(),before);
        assert_eq!(app.login.read().await.as_ref().unwrap()["connection_id"],"tenant-connection");
    }
}
