//! Agent-scoped model administration. Installation oversight never implies credential ownership.
use crate::{App, Api, ApiError};
use crate::access::{Principal, Relationship};
use crate::model_selection::Catalog;
use axum::{extract::{State, Path, Query}, Extension, Json, Router, routing::get, http::StatusCode};
use serde_json::{json, Value};
use std::collections::HashMap;

// MODEL-IDENTITY: reload account status before trusting the authenticated identity.
async fn principal(app: &App, identity: &Value) -> Result<Principal, ApiError> {
    let account = app.store.account_by_id(identity["_user_id"].as_str().unwrap_or("")).await?
        .ok_or_else(|| denied())?;
    let value = Principal::from_account(&account)?;
    if !value.active() { return Err(denied()); }
    Ok(value)
}
fn denied() -> ApiError { ApiError(StatusCode::FORBIDDEN, "Agent or model administration unavailable".into()) }
fn selected(params: &HashMap<String,String>) -> &str {
    params.get("agent_id").map(String::as_str).unwrap_or("shared_default")
}
// AGENT-AUTHORITY: resolve the requested tenant before opening model records.
async fn authority(app: &App, identity: &Value, agent: &str, write: bool) -> Result<Value, ApiError> {
    let caller = principal(app,identity).await?;
    let value = app.store.agent_authority(&caller.user_id,agent).await.map_err(|_|denied())?;
    if write {
        if value["can_manage"] != true { return Err(denied()); }
    } else if value["can_manage"] != true && !app.store.live_access(&caller.user_id,agent).await
        .is_ok_and(|access|access.admitted()) { return Err(denied()); }
    Ok(value)
}
// This projection contains only caller-owned records and grants for the selected agent.
// Unowned grants to other tenants are deliberately absent and cannot be overwritten.
fn scoped(catalog: &Catalog, caller: &str, agent: &str) -> Catalog {
    let mut view = catalog.clone();
    view.connections.retain(|_,c|c.owner_id == caller || c.allowed_agents.contains(agent));
    view.entries.retain(|_,e|e.owner_id == caller ||
        (e.allowed_agents.contains(agent) && view.connections.contains_key(&e.connection_id)));
    view.connections.retain(|id,c|c.owner_id == caller || view.entries.values().any(|e|e.connection_id == *id));
    for c in view.connections.values_mut().filter(|c|c.owner_id != caller) {
        c.allowed_agents.retain(|id|id == agent);
    }
    for e in view.entries.values_mut().filter(|e|e.owner_id != caller) {
        e.allowed_agents.retain(|id|id == agent);
    }
    view.platform_sources.retain(|id,_|view.entries.contains_key(id));
    view.groups.clear();
    view.assignments.clear();
    view.agent_models.retain(|id,_|id == agent);
    view
}
// MODEL-VISIBILITY: members receive only roster entries within their live tier ceiling.
async fn response(app: &App, identity: &Value, agent: &str, catalog: &Catalog) -> Api {
    let rights=authority(app,identity,agent,false).await?;
    let caller=principal(app,identity).await?;
    let mut view=scoped(catalog,&caller.user_id,agent);
    let access=app.store.live_access(&caller.user_id,agent).await.ok();
    let mut available=Vec::new();
    for entry in view.entries.values() {
        if !entry.allowed_agents.contains(agent)
            || !view.connections.get(&entry.connection_id).is_some_and(|c|!c.revoked && c.allowed_agents.contains(agent))
            || (rights["can_manage"]!=true && !catalog.agent_models.get(agent).is_some_and(|m|m.entry_ids.contains(&entry.id))) {continue;}
        let allowed=if let Some(pin)=catalog.platform_sources.get(&entry.id) {
            app.store.resolve_account_model_source(&caller.user_id,&crate::account_catalog::Source{
                origin:crate::account_catalog::Origin::Platform,
                entry_id:crate::account_catalog::platform_id(&pin.roster_id,&pin.entry.id)}).await.is_ok()
        } else {access.as_ref().is_some_and(|a|a.model_allowed(&entry.upstream_model))};
        if allowed {available.push(entry.id.clone());}
    }
    if rights["can_manage"] != true {
        view.entries.retain(|id,_|available.contains(id));
        view.connections.retain(|id,_|view.entries.values().any(|e|e.connection_id == *id));
        for c in view.connections.values_mut() { c.credential_ref.clear(); }
        for a in view.agent_models.values_mut() {
            a.entry_ids.retain(|id|available.contains(id));
            a.roles.retain(|_,r|available.contains(&r.entry_id));
            for r in a.roles.values_mut() {r.backup_entry_ids.retain(|id|available.contains(id));}
        }
    }
    let adoptions=app.store.agent_model_adoptions(&caller.user_id,agent).await?;
    Ok(Json(json!({"adoptions":adoptions,"catalog":view,"caller_id":caller.user_id,"can_manage":rights["can_manage"],
        "can_create_agent":caller.role != crate::access::Role::Guest,"agent_owner_id":rights["owner_id"],
        "available_entries":available,"supported_transports":["openai_subscription","deepseek","openai_api","openrouter","abacus","gemini","groq","together","mistral","fireworks","xai","perplexity","deepinfra"]})))
}
// MODEL-READ: the catalog projection excludes other tenants and private credentials.
async fn read(State(app): State<App>, Extension(identity): Extension<Value>,
    Query(params): Query<HashMap<String,String>>) -> Api {
    let agent=selected(&params);
    authority(&app,&identity,agent,false).await?;
    response(&app,&identity,agent,&app.store.model_catalog().await?).await
}
// MODEL-WRITE: serialize scoped merges; catalog and record revisions fence stale editors.
async fn save(State(app): State<App>, Extension(identity): Extension<Value>,
    Query(params): Query<HashMap<String,String>>, Json(input): Json<Catalog>) -> Api {
    let agent=selected(&params);
    let _guard=app.gate.lock().await;
    let rights=authority(&app,&identity,agent,true).await?;
    let caller=principal(&app,&identity).await?;
    let old=app.store.model_catalog().await?;
    if old.revision != input.revision {return Err(crate::conflict("Model catalog revision changed; reload before saving"));}
    let view=scoped(&old,&caller.user_id,agent);
    if input.platform_sources!=view.platform_sources {return Err(crate::bad("Platform provenance is server controlled"));}
    if input.entries.keys().chain(input.connections.keys()).any(|id|id.starts_with("platform:") && !view.entries.contains_key(id) && !view.connections.contains_key(id)) {return Err(denied());}
    let mut next=old.clone();
    // Shared records are read-only to recipients, even to installation administrators.
    macro_rules! merge {
        ($field:ident) => {
            for (id,record) in &view.$field {
                if !input.$field.contains_key(id) {return Err(crate::bad("Revoke or unlink records explicitly; deletion is not supported"));}
                if record.owner_id != caller.user_id && input.$field.get(id) != Some(record) {return Err(denied());}
            }
            for (id,record) in &input.$field {
                if let Some(previous)=old.$field.get(id) {
                    if !view.$field.contains_key(id) {return Err(denied());}
                    if previous.owner_id != caller.user_id {continue;}
                    if record.owner_id != previous.owner_id {return Err(denied());}
                } else if record.owner_id != caller.user_id {return Err(denied());}
                for grant in &record.allowed_agents {
                    app.store.for_agent(grant).await.map_err(|_|crate::bad("Grant agent does not exist"))?;
                }
                next.$field.insert(id.clone(),record.clone());
            }
        };
    }
    merge!(connections);
    merge!(entries);
    for (id,connection) in &mut next.connections {
        if let Some(previous)=old.connections.get(id) {
            if connection.credential_ref != previous.credential_ref || connection.transport != previous.transport {
                return Err(crate::bad("Credential references and provider transport are server controlled"));
            }
        } else {
            // Client-supplied references must never select another tenant's stored credential.
            connection.credential_ref=format!("provider_credential:{}",crate::uuid());
        }
    }
    for (id,entry) in &input.entries {
        if old.entries.get(id) != Some(entry) && entry.owner_id == caller.user_id {
            let connection=next.connections.get(&entry.connection_id).ok_or_else(||crate::bad("Connection missing"))?;
            if connection.owner_id != caller.user_id {return Err(denied());}
            if !entry.allowed_agents.is_subset(&connection.allowed_agents) {return Err(crate::bad("Entry grants exceed connection grants"));}
        }
    }
    // BYO-POLICY: the existing editor is not an alternate quota/permission lane.
    let tier=app.store.entitlement_tier(&caller.tier_id).await?
        .ok_or_else(||denied())?;
    let previous_count=old.entries.values().filter(|e|e.owner_id==caller.user_id).count();
    let next_count=next.entries.values().filter(|e|e.owner_id==caller.user_id).count();
    if !tier.allows_byo_growth(previous_count,next_count) {return Err(denied());}
    for (id,entry) in &next.entries {
        if entry.owner_id!=caller.user_id {continue;}
        let increased_use=old.entries.get(id).is_none_or(|previous|
            entry.upstream_model!=previous.upstream_model
            || entry.connection_id!=previous.connection_id
            || !entry.allowed_agents.is_subset(&previous.allowed_agents));
        if increased_use && !tier.allows_byo_model(&entry.upstream_model) {return Err(denied());}
    }
    if !input.assignments.is_empty() || !input.groups.is_empty()
        || input.agent_models.keys().any(|id|id != agent) {return Err(denied());}
    if let Some(models)=input.agent_models.get(agent) {
        if models.owner_id != rights["owner_id"].as_str().unwrap_or("") || models.agent_id != agent {return Err(denied());}
        let previous=old.agent_models.get(agent);
        let mut new_uses=std::collections::BTreeSet::new();
        for id in &models.entry_ids {
            if previous.is_none_or(|r|!r.entry_ids.contains(id)) {new_uses.insert(id);}
        }
        for (role,appointment) in &models.roles {
            if previous.and_then(|r|r.roles.get(role))!=Some(appointment) {
                new_uses.insert(&appointment.entry_id);
                new_uses.extend(&appointment.backup_entry_ids);
            }
        }
        for id in new_uses {
            if let Some(pin)=old.platform_sources.get(id) {
                app.store.resolve_account_model_source(&caller.user_id,&crate::account_catalog::Source{
                    origin:crate::account_catalog::Origin::Platform,
                    entry_id:crate::account_catalog::platform_id(&pin.roster_id,&pin.entry.id)}).await.map_err(|_|denied())?;
            } else {
                let entry=next.entries.get(id).ok_or_else(||crate::bad("Role entry missing"))?;
                if !tier.allows_byo_model(&entry.upstream_model) {return Err(denied());}
            }
        }
        next.agent_models.insert(agent.to_owned(),models.clone());
    } else if view.agent_models.contains_key(agent) {return Err(crate::bad("Clear roles explicitly"));}
    let saved=app.store.save_model_catalog(&next,input.revision).await
        .map_err(|_|crate::bad("Catalog update rejected: check roster, roles, grants and revisions"))?;
    response(&app,&identity,agent,&saved).await
}
// AGENT-DISCOVERY: list only agents the caller can use or explicitly administer.
async fn agents(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    use sqlx::Row;
    let caller=principal(&app,&identity).await?;
    let mut values=Vec::new();
    for id in app.store.agent_ids().await? {
        if authority(&app,&identity,&id,false).await.is_err() {continue;}
        let view=app.store.for_agent(&id).await?;
        let row=sqlx::query("SELECT name,description,status,revision FROM agents WHERE id=?")
            .bind(&id).fetch_one(&view.agent_pool).await.map_err(anyhow::Error::from)?;
        values.push(json!({"id":id,"name":row.get::<String,_>("name"),"description":row.get::<String,_>("description"),
            "status":row.get::<String,_>("status"),"revision":row.get::<i64,_>("revision")}));
    }
    // Even a caller with no memberships can create their first owned agent.
    Ok(Json(json!({"agents":values,"caller_id":caller.user_id,"can_create_agent":caller.role != crate::access::Role::Guest})))
}

// IDENTITY-WRITE: identity and named execution modes; tenant authority and revision fence edits.
async fn identity_get(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    use sqlx::Row;
    let rights = authority(&app, &identity, &id, false).await?;
    let store = app.store.for_agent(&id).await?;
    let row = sqlx::query("SELECT name,description,revision FROM agents WHERE id=?")
        .bind(&id).fetch_one(&store.agent_pool).await.map_err(anyhow::Error::from)?;
    let config=crate::execution_modes::config(&store,&id).await?;
    let settings=crate::gate_controls::agent_visibility(&store,&id).await?;
    let closing=crate::gate_controls::agent_closing(&store,&id).await?;
    let closing_protocol=crate::gate_registry::closing_config()?;
    let fixed=crate::gate_registry::visibility_config()?.guidance;
    Ok(Json(json!({"closing_message":closing,"closing_protocol":{"flags":closing_protocol.flags,"notification_max_words":closing_protocol.notification_max_words},"message_visibility":{"enabled":settings.enabled,"guidance":settings.guidance,"format":settings.format,"fixed_protocol":fixed},"execution_modes":config["execution_modes"],"default_execution_mode":config["default_execution_mode"],"id":id,"name":row.get::<String,_>("name"),
        "description":row.get::<String,_>("description"),"revision":row.get::<i64,_>("revision"),
        "can_manage":rights["can_manage"]})))
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityUpdate { revision: i64, name: Option<String>, description: Option<String>, execution_modes: Option<Value>, default_execution_mode: Option<String>, message_visibility: Option<crate::gate_controls::AgentVisibility>, closing_message: Option<crate::gate_controls::AgentClosing> }
async fn identity_put(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>, Json(body): Json<IdentityUpdate>) -> Api {
    if body.revision < 0 || (body.name.is_none() && body.description.is_none() && body.execution_modes.is_none() && body.default_execution_mode.is_none() && body.message_visibility.is_none() && body.closing_message.is_none())
        || body.name.as_ref().is_some_and(|s| s.trim().is_empty() || s.len()>200)
        || body.description.as_ref().is_some_and(|s| s.len()>2000) {
        return Err(ApiError(StatusCode::BAD_REQUEST,"Name must contain 1–200 bytes; description at most 2000 bytes".into()));
    }
    let _guard = app.gate.lock().await;
    authority(&app, &identity, &id, true).await?;
    let store = app.store.for_agent(&id).await?;
    let blob:Vec<u8>=sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?").bind(&id).fetch_one(&store.agent_pool).await.map_err(anyhow::Error::from)?;
    let mut metadata=store.unseal(&blob)?;
    let modes=crate::execution_modes::normalize(body.execution_modes.as_ref().unwrap_or(&metadata["execution_modes"]))
        .map_err(|e|crate::bad(&e.to_string()))?;
    let requested=body.default_execution_mode.as_deref().unwrap_or(metadata["default_execution_mode"].as_str().unwrap_or("ask"));
    let default=if modes.as_array().unwrap().iter().any(|m|m["id"]==requested) {requested.to_owned()}
        else if body.default_execution_mode.is_some() {return Err(crate::bad("Unknown default mode"));} else {"ask".to_owned()};
    metadata["execution_modes"]=modes; metadata["default_execution_mode"]=json!(default);
    if let Some(settings)=body.message_visibility {
        settings.validate().map_err(|e|crate::bad(&e.to_string()))?;
        metadata["message_visibility"]=serde_json::to_value(settings).map_err(anyhow::Error::from)?;
    }
    if let Some(settings)=body.closing_message {
        settings.validate().map_err(|e|crate::bad(&e.to_string()))?;
        metadata["closing_message"]=serde_json::to_value(settings).map_err(anyhow::Error::from)?;
    }
    let changed = sqlx::query("UPDATE agents SET name=COALESCE(?,name),description=COALESCE(?,description),metadata=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?")
        .bind(body.name.as_deref().map(str::trim)).bind(body.description.as_deref()).bind(store.seal(&metadata)?)
        .bind(crate::now() as i64).bind(&id).bind(body.revision)
        .execute(&store.agent_pool).await.map_err(anyhow::Error::from)?;
    if changed.rows_affected()!=1 {return Err(ApiError(StatusCode::CONFLICT,"Identity changed; reload before saving".into()));}
    identity_get(State(app.clone()),Extension(identity),Path(id)).await
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct NewAgent { client_request_id: String, name: String, description: Option<String> }
// AGENT-CREATION: the caller owns the new directory record; retries preserve that identity.
async fn create_agent(State(app): State<App>, Extension(identity): Extension<Value>, Json(body): Json<NewAgent>) -> Api {
    if principal(&app,&identity).await?.role == crate::access::Role::Guest { return Err(denied()); }
    if body.client_request_id.is_empty() || body.client_request_id.len()>128 || body.name.trim().is_empty()
        || body.name.len()>200 || body.description.as_ref().is_some_and(|s| s.len()>2000) {
        return Err(crate::bad("Invalid agent fields"));
    }
    let owner=identity["_user_id"].as_str().unwrap_or("");
    let receipt=format!("receipt:model-agent:{owner}:{}",body.client_request_id);
    let request=json!({"name":body.name,"description":body.description});
    let _guard=app.gate.lock().await;
    if let Some(saved)=app.store.get(&receipt).await? {
        if saved["digest"]!=crate::digest(&request) { return Err(crate::conflict("Request identifier reused with different input")); }
        return Ok(Json(saved["result"].clone()));
    }
    // The agent row and receipt live in separate encrypted stores. A stable
    // request-derived ID and embedded digest make crash-window retries safe.
    let id=format!("agent-{}", &crate::digest(&json!({"owner":owner,"request_id":body.client_request_id}))[..32]);
    let metadata=app.store.seal(&json!({"owner_id":owner,"ability_modes":{"codebase_admin":"off"},
        "skill_modes":{},"tool_modes":{},"ability_revision":0,"creation_digest":crate::digest(&request)}))?;
    let agent_pool=crate::agent_storage::open_agent_pool(&app.store.root,&id).await?;
    let existing: Option<Vec<u8>> = sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
        .bind(&id).fetch_optional(&agent_pool).await.map_err(anyhow::Error::from)?;
    if let Some(blob)=existing {
        let old=app.store.unseal(&blob)?;
        if old["creation_digest"]!=crate::digest(&request) || old["owner_id"]!=owner {
            agent_pool.close().await;
            return Err(crate::conflict("Request identifier reused with different input"));
        }
    } else {
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,?,'openai_subscription','','active',?,1,?,?)")
            .bind(&id).bind(body.name.trim()).bind(body.description.as_deref().unwrap_or(""))
            .bind(metadata).bind(crate::now() as i64).bind(crate::now() as i64)
            .execute(&agent_pool).await.map_err(anyhow::Error::from)?;
    }
    agent_pool.close().await;
    app.store.register_agent(&id,owner).await?;
    match app.store.agent_membership(owner,&id).await? {
        None => { app.store.set_agent_membership(owner,&id,Relationship::Owner,0).await?; }
        Some(value) if value["relationship"]=="owner" => {}
        Some(_) => return Err(crate::conflict("Agent ownership changed")),
    }
    let seeded=app.store.seed_new_agent_default(owner,&id).await?;
    let result=json!({"id":id,"name":body.name.trim(),"assignment_required":!seeded});
    app.store.put(&receipt,&json!({"digest":crate::digest(&request),"result":result})).await?;
    Ok(Json(result))
}

// CONNECTION-VISIBILITY: status follows tenant grants and members' effective model availability.
async fn connection_status(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
    Query(params): Query<HashMap<String,String>>) -> Api {
    let caller=principal(&app,&identity).await?;
    let agent=selected(&params);
    let rights=authority(&app,&identity,agent,false).await?;
    let catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get(&id).ok_or_else(|| ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
    if connection.owner_id != caller.user_id && (!connection.allowed_agents.contains(agent)
        || !catalog.entries.values().any(|e|e.connection_id==id && e.allowed_agents.contains(agent))) {return Err(denied());}
    if rights["can_manage"] != true {
        let access=app.store.live_access(&caller.user_id,agent).await.map_err(|_|denied())?;
        let visible=!connection.revoked && connection.allowed_agents.contains(agent)
            && catalog.agent_models.get(agent).is_some_and(|models|models.entry_ids.iter().any(|entry_id|
                catalog.entries.get(entry_id).is_some_and(|entry|entry.connection_id == id
                    && entry.allowed_agents.contains(agent) && access.model_allowed(&entry.upstream_model))));
        if !visible {return Err(denied());}
    }
    let login=app.login.read().await;
    let authorization=login.as_ref().filter(|v| connection.owner_id == caller.user_id && v["connection_id"]==id).map(|v|json!({
        "status":v["status"],"verification_url":v["verification_url"],"user_code":v["user_code"]}));
    Ok(Json(json!({"id":id,"revoked":connection.revoked,
        "connected":!connection.revoked && app.store.get(&connection.credential_ref).await?.is_some(),"authorization":authorization})))
}
// Reattach the owner's pending device code after a page reload. Draft
// connections have no roster entry yet, so connection status alone cannot
// recover their verification details.
async fn pending_subscription_authorization(State(app): State<App>, Extension(identity): Extension<Value>,
    Query(params): Query<HashMap<String,String>>) -> Api {
    let caller=principal(&app,&identity).await?;
    let agent=selected(&params);
    authority(&app,&identity,agent,true).await?;
    let login=app.login.read().await;
    let Some(pending)=login.as_ref().filter(|v|v["status"]=="pending") else {
        return Ok(Json(json!({"pending":false})));
    };
    let Some(id)=pending["connection_id"].as_str() else {
        return Ok(Json(json!({"pending":false})));
    };
    let catalog=app.store.model_catalog().await?;
    let visible=catalog.connections.get(id).is_some_and(|c|c.owner_id==caller.user_id
        && c.allowed_agents.contains(agent) && !c.revoked
        && c.transport==crate::model_selection::Transport::OpenaiSubscription);
    if !visible { return Ok(Json(json!({"pending":false}))); }
    Ok(Json(json!({"pending":true,"connection_id":id,
        "verification_url":pending["verification_url"],"user_code":pending["user_code"],
        "expires_at":pending["expires_at"]})))
}

// Subscription discovery is scoped to the selected owned connection.
async fn subscription_models(State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Query(params): Query<HashMap<String,String>>) -> Api {
    let caller=principal(&app,&identity).await?;
    let agent=selected(&params);
    authority(&app,&identity,agent,true).await?;
    let catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get(&id).filter(|c|c.owner_id==caller.user_id
        && c.allowed_agents.contains(agent) && !c.revoked
        && c.transport==crate::model_selection::Transport::OpenaiSubscription)
        .ok_or_else(denied)?;
    let tokens=app.tokens_at(&connection.credential_ref).await
        .map_err(|_|ApiError(StatusCode::PRECONDITION_REQUIRED,"Connect ChatGPT to list models".into()))?;
    Ok(Json(app.provider.subscription_models(&tokens).await
        .map_err(|_|ApiError(StatusCode::BAD_GATEWAY,"Subscription model catalog unavailable".into()))?))
}

async fn byo_credential_permission(app:&App, caller:&Principal) -> Result<(),ApiError> {
    let tier=app.store.entitlement_tier(&caller.tier_id).await?.ok_or_else(||denied())?;
    if tier.model_policy.as_ref().is_some_and(|p|!p.allow_byo) {return Err(denied());}
    Ok(())
}
// CREDENTIAL-AUTHORIZATION: only the connection owner starts a private device flow.
async fn connection_authorize(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    let caller=principal(&app,&identity).await?;
    let _guard=app.gate.lock().await;
    let mut catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get_mut(&id).ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
    if connection.owner_id != caller.user_id {return Err(denied());}
    byo_credential_permission(&app,&caller).await?;
    if connection.revoked { return Err(crate::conflict("Connection is revoked")); }
    if connection.transport != crate::model_selection::Transport::OpenaiSubscription { return Err(crate::bad("This provider uses an API key, not device authorization")); }
    if let Some(pending) = app.login.read().await.as_ref().filter(|v|v["status"]=="pending") {
        if pending["connection_id"] == id {
            return Ok(Json(json!({"verification_url":pending["verification_url"],"user_code":pending["user_code"]})));
        }
        return Err(crate::conflict("Authorization is pending for another connection. Reset it before retrying"));
    }
    // A failed device-start request must not disconnect the credential already
    // attached to this connection. The gate keeps polling from publishing until
    // the new reference has been committed.
    let reference=format!("provider_credential:{}",crate::uuid());
    let started=crate::provider_start_for(app.clone(),reference.clone(),Some(id.clone())).await?;
    catalog.connections.get_mut(&id).expect("connection checked above").credential_ref=reference;
    if let Err(error)=app.store.save_model_catalog(&catalog,catalog.revision).await {
        let mut login=app.login.write().await;
        if login.as_ref().is_some_and(|v|v["connection_id"]==id) { *login=None; }
        return Err(error.into());
    }
    Ok(started)
}

// The connection owner may cancel a pending device flow without revoking a
// previously connected account. The polling task observes the cleared login.
async fn connection_authorization_reset(State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>) -> Api {
    let caller=principal(&app,&identity).await?;
    let catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get(&id).ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
    if connection.owner_id != caller.user_id {return Err(denied());}
    let mut login=app.login.write().await;
    if login.as_ref().is_some_and(|v|v["connection_id"]==id) {
        *login=None;
    }
    Ok(Json(json!({"reset":true})))
}

// The agent's owner may clear every owned subscription credential represented
// by its roster. Reset also cancels an owned draft flow for this agent, which
// may no longer have a visible row after a page reload.
async fn subscription_credentials_reset(State(app): State<App>, Extension(identity): Extension<Value>,
    Query(params): Query<HashMap<String,String>>) -> Api {
    let caller=principal(&app,&identity).await?;
    let agent=selected(&params);
    authority(&app,&identity,agent,true).await?;
    let _guard=app.gate.lock().await;
    let mut catalog=app.store.model_catalog().await?;
    let mut ids=std::collections::BTreeSet::new();
    if let Some(roster)=catalog.agent_models.get(agent) {
        for entry_id in &roster.entry_ids {
            if let Some(entry)=catalog.entries.get(entry_id) { ids.insert(entry.connection_id.clone()); }
        }
    }
    let pending_id=app.login.read().await.as_ref()
        .and_then(|v|v["connection_id"].as_str().map(str::to_owned));
    let cancel_owned_pending=pending_id.as_ref().is_some_and(|id|
        catalog.connections.get(id).is_some_and(|c|c.owner_id==caller.user_id
            && c.transport==crate::model_selection::Transport::OpenaiSubscription));
    if let Some(id)=pending_id.as_ref() {
        if catalog.connections.get(id).is_some_and(|c|c.allowed_agents.contains(agent)) {
            ids.insert(id.clone());
        }
    }
    ids.retain(|id|catalog.connections.get(id).is_some_and(|c|
        c.owner_id==caller.user_id && c.allowed_agents.contains(agent) && !c.revoked
        && c.transport==crate::model_selection::Transport::OpenaiSubscription));
    for id in &ids {
        catalog.connections.get_mut(id).expect("connection checked above").credential_ref=
            format!("provider_credential:{}",crate::uuid());
    }
    if !ids.is_empty() { app.store.save_model_catalog(&catalog,catalog.revision).await?; }
    let mut login=app.login.write().await;
    if cancel_owned_pending { *login=None; }
    Ok(Json(json!({"reset":true,"connection_ids":ids})))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ApiKey { api_key: String }
// CREDENTIAL-ROTATION: only the owner can replace a secret and invalidate pinned references.
async fn connection_key(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>, Json(body): Json<ApiKey>) -> Api {
    let caller=principal(&app,&identity).await?;
    if body.api_key.len() < 8 || body.api_key.len() > 512 || !body.api_key.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(crate::bad("Invalid API key: use 8–512 printable non-space characters"));
    }
    let _guard = app.gate.lock().await;
    let mut catalog = app.store.model_catalog().await?;
    let connection = catalog.connections.get_mut(&id).ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Connection not found".into()))?;
    if connection.owner_id != caller.user_id {return Err(denied());}
    byo_credential_permission(&app,&caller).await?;
    if connection.revoked { return Err(crate::conflict("Connection is revoked")); }
    if !connection.transport.uses_api_key() { return Err(crate::bad("This connection does not use an API key")); }
    let reference = format!("provider_credential:{}", crate::uuid());
    // Store the secret only in the encrypted credential plane. No key in catalog/events.
    app.store.put(&reference, &json!({"api_key":body.api_key})).await?;
    connection.credential_ref = reference;
    app.store.save_model_catalog(&catalog, catalog.revision).await?;
    Ok(Json(json!({"saved":true})))
}


// Default-agent compatibility is a one-time, explicit catalog migration. No run
// silently falls back to the old model setting after this boundary.
pub(crate) async fn migrate_legacy_default(store: &crate::store::Store) -> anyhow::Result<()> {
    use crate::model_selection::*;
    const MARKER:&str="migration:model_roles_default_v1";
    if store.get(MARKER).await?.is_some() {return Ok(());}
    let mut catalog=store.model_catalog().await?;
    if !catalog.agent_models.contains_key("shared_default") && store.get("provider_tokens").await?.is_some() {
        let selection=crate::provider::ProviderSelection::parse(&store.agent_model().await?,crate::DEFAULT_AGENT_MODEL)?;
        if selection.provider != crate::provider::ProviderId::OpenaiSubscription {
            anyhow::bail!("Legacy default provider requires explicit model configuration");
        }
        let grants=std::collections::BTreeSet::from(["shared_default".to_owned()]);
        let id="legacy-default-subscription".to_owned();
        if catalog.connections.contains_key(&id) || catalog.entries.contains_key(&id) {anyhow::bail!("Legacy migration identity conflict");}
        catalog.connections.insert(id.clone(),Connection {id:id.clone(),owner_id:"admin".into(),
            transport:Transport::OpenaiSubscription,credential_ref:"provider_tokens".into(),
            allowed_agents:grants.clone(),revision:1,revoked:false});
        catalog.entries.insert(id.clone(),ModelEntry {id:id.clone(),owner_id:"admin".into(),
            connection_id:id.clone(),upstream_model:selection.model,display_name:"Existing default subscription".into(),
            capabilities:std::collections::BTreeSet::from(["text".into()]),allowed_agents:grants,revision:1});
        catalog.agent_models.insert("shared_default".into(),AgentModels {agent_id:"shared_default".into(),owner_id:"admin".into(),
            entry_ids:vec![id.clone()],roles:std::collections::BTreeMap::from([(ModelRole::Standard,
                RoleAssignment {entry_id:id,backup_entry_ids:vec![]})]),revision:1});
        store.save_model_catalog(&catalog,catalog.revision).await?;
    } else if !catalog.agent_models.is_empty() {
        // Persist projected old primary assignments without changing their ordered backups.
        store.save_model_catalog(&catalog,catalog.revision).await?;
    }
    store.put(MARKER,&json!({"complete":true})).await?;
    Ok(())
}

// TENANT-ROSTER: membership identities are visible only to this agent's administrators.
async fn members(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    let rights=authority(&app,&identity,&id,true).await?;
    Ok(Json(json!({"members":app.store.agent_memberships(&id).await?,"authority":rights})))
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct MemberUpdate {
    user_id:String, relationship:Relationship,
    #[serde(default)] agent_administrator:bool,
    revision:u64,
}
// TENANT-ASSIGNMENT: storage rechecks actor authority and immutable ownership atomically.
async fn member_set(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
    Json(body):Json<MemberUpdate>) -> Api {
    let _guard=app.gate.lock().await;
    authority(&app,&identity,&id,true).await?;
    let result=app.store.assign_agent_membership(identity["_user_id"].as_str().unwrap_or(""),
        &body.user_id,&id,body.relationship,body.agent_administrator,body.revision).await
        .map_err(|_|crate::conflict("Membership update rejected: check authority, account and revision"))?;
    // MEMBERSHIP-REVOCATION: stop active work now, preserving uncertain effects for review.
    crate::phase3_api::quiesce_user(&app,&body.user_id,Some(&id)).await?;
    Ok(Json(json!({"membership":result})))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Discovery { provider:Option<crate::provider::ProviderId>, api_key:Option<String>, connection_id:Option<String> }
async fn discover(State(app):State<App>,Extension(identity):Extension<Value>,Json(input):Json<Discovery>)->Api {
    let caller=principal(&app,&identity).await?;
    if !caller.administrator(){byo_credential_permission(&app,&caller).await?;}
    let (provider,tokens)=if let Some(id)=input.connection_id {
        if input.provider.is_some()||input.api_key.is_some(){return Err(crate::bad("Use one discovery credential source"));}
        let catalog=app.store.model_catalog().await?;
        let connection=catalog.connections.get(&id).filter(|c|c.owner_id==caller.user_id&&!c.revoked&&c.transport.uses_api_key())
            .ok_or_else(denied)?;
        (connection.transport.provider_id(),app.store.get(&connection.credential_ref).await?.ok_or_else(denied)?)
    } else {
        let provider=input.provider.ok_or_else(||crate::bad("Provider required"))?;
        let key=input.api_key.ok_or_else(||crate::bad("API key required"))?;
        if !key.bytes().all(|b|b.is_ascii_graphic())||!(8..=512).contains(&key.len())||provider.chat_endpoint().is_none(){return Err(crate::bad("Invalid discovery credential or provider"));}
        (provider,json!({"api_key":key}))
    };
    Ok(Json(app.provider.discover_models(provider,&tokens,tokio_util::sync::CancellationToken::new()).await
        .map_err(|_|ApiError(StatusCode::BAD_GATEWAY,"Provider discovery unavailable; check provider and credential".into()))?))
}

/// Nest only under the authenticated browser API (including its CSRF middleware).
pub fn router() -> Router<App> {
    Router::new().route("/catalog", get(read).put(save))
        .route("/discovery", axum::routing::post(discover))
        .route("/agents", get(agents).post(create_agent))
        .route("/agents/{id}/identity", get(identity_get).put(identity_put))
        .route("/agents/{id}/members", get(members).put(member_set))
        .route("/connections/{id}", get(connection_status))
        .route("/connections/authorization/pending", get(pending_subscription_authorization))
        .route("/connections/{id}/models", get(subscription_models))
        .route("/connections/{id}/api-key", axum::routing::put(connection_key))
        .route("/connections/{id}/authorization", axum::routing::post(connection_authorize))
        .route("/connections/{id}/authorization/reset", axum::routing::post(connection_authorization_reset))
        .route("/connections/subscription/reset", axum::routing::post(subscription_credentials_reset))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_tests::{fixture, selection_fixture};
    #[tokio::test]
    async fn authorization_reset_is_owner_scoped_and_cancels_only_its_flow() {
        let (_root, app) = fixture().await;
        selection_fixture(&app).await;
        let admin = json!({"_user_id":"admin"});
        let outsider = json!({"_user_id":"other"});
        *app.login.write().await = Some(json!({"connection_id":"one","status":"pending","device_auth_id":"fixture"}));
        assert!(connection_authorization_reset(State(app.clone()),Extension(outsider),
            Path("one".into())).await.is_err());
        assert_eq!(app.login.read().await.as_ref().unwrap()["status"],"pending");
        connection_authorization_reset(State(app.clone()),Extension(admin.clone()),
            Path("two".into())).await.unwrap();
        assert_eq!(app.login.read().await.as_ref().unwrap()["status"],"pending");
        connection_authorization_reset(State(app.clone()),Extension(admin.clone()),
            Path("one".into())).await.unwrap();
        assert!(app.login.read().await.is_none());
        connection_authorization_reset(State(app.clone()),Extension(admin),
            Path("one".into())).await.unwrap();
        assert!(app.login.read().await.is_none());
    }
    #[tokio::test]
    async fn pending_authorization_reuses_its_code_without_rotating_credentials() {
        let (_root,app)=fixture().await;
        selection_fixture(&app).await;
        let identity=json!({"_user_id":"admin"});
        let before=app.store.model_catalog().await.unwrap().connections["one"].credential_ref.clone();
        *app.login.write().await=Some(json!({"connection_id":"one","status":"pending",
            "device_auth_id":"fixture","verification_url":"https://example.invalid/verify","user_code":"FIXTURE"}));
        let Json(reused)=connection_authorize(State(app.clone()),Extension(identity.clone()),Path("one".into())).await.unwrap();
        assert_eq!(reused["user_code"],"FIXTURE");
        let Json(pending)=pending_subscription_authorization(State(app.clone()),Extension(identity.clone()),
            Query(HashMap::new())).await.unwrap();
        assert_eq!(pending["pending"],true);
        assert_eq!(pending["connection_id"],"one");
        assert_eq!(pending["user_code"],"FIXTURE");
        assert_eq!(app.store.model_catalog().await.unwrap().connections["one"].credential_ref,before);
        app.login.write().await.as_mut().unwrap()["connection_id"]=json!("two");
        assert_eq!(connection_authorize(State(app.clone()),Extension(identity),Path("one".into()))
            .await.unwrap_err().0,StatusCode::CONFLICT);
        assert_eq!(app.store.model_catalog().await.unwrap().connections["one"].credential_ref,before);
    }
    #[tokio::test]
    async fn subscription_reset_detaches_agent_credentials_and_cancels_own_pending_flow() {
        let (_root,app)=fixture().await;
        selection_fixture(&app).await;
        let admin=json!({"_user_id":"admin"});
        let other=json!({"_user_id":"other"});
        let before=app.store.model_catalog().await.unwrap();
        *app.login.write().await=Some(json!({"connection_id":"two","status":"pending","device_auth_id":"fixture"}));
        assert!(subscription_credentials_reset(State(app.clone()),Extension(other),
            Query(HashMap::new())).await.is_err());
        assert_eq!(app.store.model_catalog().await.unwrap().connections["one"].credential_ref,
            before.connections["one"].credential_ref);
        let Json(result)=subscription_credentials_reset(State(app.clone()),Extension(admin.clone()),
            Query(HashMap::new())).await.unwrap();
        assert_eq!(result["connection_ids"],json!(["one","two"]));
        let after=app.store.model_catalog().await.unwrap();
        for id in ["one","two"] {
            assert_ne!(after.connections[id].credential_ref,before.connections[id].credential_ref);
            let Json(status)=connection_status(State(app.clone()),Extension(admin.clone()),
                Path(id.into()),Query(HashMap::new())).await.unwrap();
            assert_eq!(status["connected"],false);
        }
        assert!(app.login.read().await.is_none());
        assert_eq!(after.agent_models["shared_default"].entry_ids,before.agent_models["shared_default"].entry_ids);
    }
    #[tokio::test]
    async fn visibility_preferences_preserve_fixed_protocol_and_revision_fence() {
        let (_root,app)=fixture().await;let identity=json!({"_user_id":"admin"});
        let Json(before)=identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();
        assert_eq!(before["message_visibility"]["enabled"],true);
        let patch=|revision,guidance:&str|IdentityUpdate{revision,name:None,description:None,execution_modes:None,default_execution_mode:None,message_visibility:Some(crate::gate_controls::AgentVisibility{enabled:false,guidance:guidance.into(),format:"Use short lines".into()}),closing_message:None};
        let revision=before["revision"].as_i64().unwrap();
        let Json(saved)=identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(patch(revision,"Keep progress concise"))).await.unwrap();
        assert_eq!(saved["message_visibility"]["fixed_protocol"],before["message_visibility"]["fixed_protocol"]);
        assert_eq!(saved["message_visibility"]["format"],"Use short lines");
        assert_eq!(saved["message_visibility"]["enabled"],false);
        let settings=crate::gate_controls::agent_visibility(&app.store,"shared_default").await.unwrap();assert!(!settings.enabled);assert_eq!(settings.guidance,"Keep progress concise");assert_eq!(settings.format,"Use short lines");
        let Json(reloaded)=identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();assert_eq!(reloaded,saved);
        assert_eq!(identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(patch(revision,"stale"))).await.unwrap_err().0,StatusCode::CONFLICT);
        let outsider=tenant_user(&app,"visibility-outsider").await;
        assert!(identity_put(State(app.clone()),Extension(json!({"_user_id":outsider})),Path("shared_default".into()),Json(patch(revision+1,"forbidden"))).await.is_err());
        assert!(identity_put(State(app.clone()),Extension(identity),Path("shared_default".into()),Json(patch(revision+1,&"x".repeat(4097)))).await.is_err());
    }
    #[tokio::test]
    async fn notification_guidance_roundtrips_with_identity_revision() {
        let (_root,app)=fixture().await;
        let identity=json!({"_user_id":"admin"});
        let Json(before)=identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();
        assert_eq!(before["closing_message"]["notification_guidance"],"");
        let revision=before["revision"].as_i64().unwrap();
        let update=|revision,guidance:&str|IdentityUpdate{revision,name:None,description:None,
            execution_modes:None,default_execution_mode:None,message_visibility:None,
            closing_message:Some(crate::gate_controls::AgentClosing{
                notification_guidance:guidance.into()})};
        let Json(saved)=identity_put(State(app.clone()),Extension(identity.clone()),
            Path("shared_default".into()),Json(update(revision,"Name the next step"))).await.unwrap();
        assert_eq!(saved["closing_message"]["notification_guidance"],"Name the next step");
        let Json(reloaded)=identity_get(State(app.clone()),Extension(identity.clone()),
            Path("shared_default".into())).await.unwrap();
        assert_eq!(reloaded,saved);
        assert_eq!(identity_put(State(app.clone()),Extension(identity.clone()),
            Path("shared_default".into()),Json(update(revision,"stale"))).await.unwrap_err().0,StatusCode::CONFLICT);
        assert!(identity_put(State(app.clone()),Extension(identity),
            Path("shared_default".into()),Json(update(saved["revision"].as_i64().unwrap(),&"x".repeat(4097)))).await.is_err());
    }
    #[tokio::test]
    async fn identity_modes_persist_default_and_enforce_live_boundary() {
        let (_root,app)=fixture().await;
        let identity=json!({"_user_id":"admin"});
        let Json(before)=identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();
        let Json(saved)=identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(IdentityUpdate{
            revision:before["revision"].as_i64().unwrap(),name:None,description:None,
            execution_modes:Some(json!([{"id":"review","label":"Review","permission_policy":"read_only","prompt":"Inspect only"}])),
            default_execution_mode:Some("review".into()),message_visibility:None,closing_message:None})).await.unwrap();
        let Json(reloaded)=identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();
        assert_eq!(saved,reloaded);
        let Json(session)=crate::api::create_session(State(app.clone()),Json(json!({"client_request_id":crate::uuid()}))).await.unwrap();
        let sid=session["id"].as_str().unwrap();
        let mode=crate::session_mode(&app,sid).await.unwrap();
        assert_eq!(mode["execution_mode"],"review"); assert_eq!(mode["mode"],"read_only");
        assert_eq!(mode["definition"]["prompt"],"Inspect only");
        let mut modes=saved["execution_modes"].clone();
        modes.as_array_mut().unwrap().last_mut().unwrap()["permission_policy"]=json!("write");
        identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(IdentityUpdate{
            revision:saved["revision"].as_i64().unwrap(),name:None,description:None,execution_modes:Some(modes),default_execution_mode:None,message_visibility:None,closing_message:None})).await.unwrap();
        assert_eq!(crate::session_mode(&app,sid).await.unwrap()["mode"],"read_only");
        let Json(next)=crate::api::change_session_mode(&app,sid,"review",Some(0),None).await.unwrap();
        assert_eq!(next["mode"],"write");
        assert!(crate::api::change_session_mode(&app,sid,"unknown",None,None).await.is_err());
    }
    #[tokio::test]
    async fn byo_disabled_blocks_credential_writes_without_mutating_catalog() {
        let (_root,app)=fixture().await;
        let catalog=selection_fixture(&app).await;
        let mut tier=app.store.entitlement_tier("basic").await.unwrap().unwrap();
        let revision=tier.revision;
        tier.model_policy=Some(crate::access::ModelPolicy{roster_id:None,
            allowed_entry_ids:Default::default(),allow_byo:false,max_byo_entries:0});
        app.store.save_entitlement_tier(tier,revision).await.unwrap();
        let user=tenant_user(&app,"byo-disabled").await;
        let account=app.store.account_by_id(&user).await.unwrap().unwrap();
        app.store.update_account_access(&user,account["access_revision"].as_u64().unwrap(),
            crate::access::AccountStatus::Active,crate::access::Role::User,"basic").await.unwrap();
        // Ownership is immutable; use a fresh ordinary-account connection instead.
        let mut connection=catalog.connections["one"].clone();
        connection.id="ordinary-byo".into();connection.owner_id=user.clone();connection.revision=1;
        let mut next=catalog.clone();next.connections.insert(connection.id.clone(),connection);
        let catalog=app.store.save_model_catalog(&next,next.revision).await.unwrap();
        let identity=json!({"_user_id":user});
        let caller=principal(&app,&identity).await.unwrap();
        assert!(byo_credential_permission(&app,&caller).await.is_err());
        assert!(connection_authorize(State(app.clone()),Extension(identity.clone()),
            Path("ordinary-byo".into())).await.is_err());
        assert!(connection_key(State(app.clone()),Extension(identity),
            Path("ordinary-byo".into()),Json(ApiKey{api_key:"fixture-no-network-key".into()})).await.is_err());
        assert_eq!(app.store.model_catalog().await.unwrap(),catalog);
    }
    #[tokio::test]
    async fn identity_is_scoped_validated_and_revision_fenced() {
        let (_root, app) = fixture().await;
        let identity = json!({"_user_id":"admin"});
        let Json(before) = identity_get(State(app.clone()),Extension(identity.clone()),Path("shared_default".into())).await.unwrap();
        let revision = before["revision"].as_i64().unwrap();
        let Json(saved) = identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(IdentityUpdate{revision,name:Some("  Renamed  ".into()),description:Some("Description".into()),execution_modes:None,default_execution_mode:None,message_visibility:None,closing_message:None})).await.unwrap();
        assert_eq!(saved["name"],"Renamed"); assert_eq!(saved["description"],"Description");
        assert!(identity_put(State(app.clone()),Extension(identity.clone()),Path("shared_default".into()),Json(IdentityUpdate{revision,name:Some("Stale".into()),description:None,execution_modes:None,default_execution_mode:None,message_visibility:None,closing_message:None})).await.is_err());
        assert!(identity_put(State(app.clone()),Extension(identity),Path("shared_default".into()),Json(IdentityUpdate{revision:revision+1,name:Some(" ".into()),description:None,execution_modes:None,default_execution_mode:None,message_visibility:None,closing_message:None})).await.is_err());
        let outsider = tenant_user(&app,"identity-outsider").await;
        assert!(identity_put(State(app.clone()),Extension(json!({"_user_id":outsider})),Path("shared_default".into()),Json(IdentityUpdate{revision:revision+1,name:Some("Forbidden".into()),description:None,execution_modes:None,default_execution_mode:None,message_visibility:None,closing_message:None})).await.is_err());
    }
    #[tokio::test]
    async fn deepseek_keys_are_validated_encrypted_scoped_and_not_returned() {
        let (_root, app) = fixture().await;
        let mut catalog = selection_fixture(&app).await;
        catalog.connections.get_mut("one").unwrap().transport = crate::model_selection::Transport::Deepseek;
        app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
        let identity=json!({"_user_id":"admin"});
        let result=connection_key(State(app.clone()),Extension(identity.clone()),Path("one".into()),Json(ApiKey{api_key:"bad key".into()})).await;
        assert!(result.is_err());
        assert!(connection_key(State(app.clone()),Extension(identity.clone()),Path("two".into()),Json(ApiKey{api_key:"test-key-no-network".into()})).await.is_err());
        assert!(connection_authorize(State(app.clone()),Extension(identity.clone()),Path("one".into())).await.is_err());
        let Json(result)=connection_key(State(app.clone()),Extension(identity.clone()),Path("one".into()),Json(ApiKey{api_key:"test-key-no-network".into()})).await.unwrap();
        assert_eq!(result,json!({"saved":true}));
        let catalog=app.store.model_catalog().await.unwrap();
        let reference=&catalog.connections["one"].credential_ref;
        assert_eq!(app.store.get(reference).await.unwrap().unwrap()["api_key"],"test-key-no-network");
        let Json(status)=connection_status(State(app.clone()),Extension(identity.clone()),Path("one".into()),Query(HashMap::new())).await.unwrap();
        assert_eq!(status["connected"],true);assert!(!status.to_string().contains("test-key"));
        assert!(!serde_json::to_string(&catalog).unwrap().contains("test-key"));
        let mut catalog=catalog;catalog.connections.get_mut("one").unwrap().revoked=true;
        app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
        assert!(connection_key(State(app.clone()),Extension(identity),Path("one".into()),Json(ApiKey{api_key:"test-key-no-network".into()})).await.is_err());
    }

    // TENANT-API-FIXTURE: use real encrypted stores; no network or live account data.
    #[tokio::test]
    async fn discovery_rejects_forged_credentials_and_endpoint_overrides_without_network() {
        let (_root,app)=fixture().await;
        assert!(serde_json::from_value::<Discovery>(json!({"provider":"openrouter","api_key":"secret-key","base_url":"http://127.0.0.1"})).is_err());
        let identity=json!({"_user_id":"admin"});
        assert!(discover(State(app.clone()),Extension(identity.clone()),Json(Discovery{
            provider:None,api_key:None,connection_id:Some("not-owned".into())})).await.is_err());
        assert!(discover(State(app),Extension(identity),Json(Discovery{
            provider:Some(crate::provider::ProviderId::Openrouter),api_key:Some("secret-key".into()),connection_id:Some("ambiguous".into())})).await.is_err());
    }
    #[tokio::test]
    async fn new_agent_uses_account_default_without_reseeding_existing_rosters() {
        let (_root,app)=fixture().await;
        let catalog=app.store.model_catalog().await.unwrap();
        let entry=catalog.entries.values().find(|e|e.owner_id=="admin").unwrap();
        let id=entry.id.clone();
        let mut preferences=app.store.account_model_preferences("admin").await.unwrap();
        preferences.default_entry_id=Some(crate::account_catalog::byo_id(&id));
        app.store.save_account_model_preferences("admin",&preferences).await.unwrap();
        let identity=json!({"_user_id":"admin"});
        let input=||NewAgent{client_request_id:"seeded-creation".into(),name:"Seeded".into(),description:None};
        let Json(created)=create_agent(State(app.clone()),Extension(identity.clone()),Json(input())).await.unwrap();
        assert_eq!(created["assignment_required"],false);
        let agent=created["id"].as_str().unwrap();
        let mut catalog=app.store.model_catalog().await.unwrap();
        assert_eq!(catalog.agent_models[agent].roles[&crate::model_selection::ModelRole::Standard].entry_id,id);
        let Json(retry)=create_agent(State(app.clone()),Extension(identity),Json(input())).await.unwrap();
        assert_eq!(retry,created);
        assert_eq!(app.store.model_catalog().await.unwrap(),catalog);
        catalog.agent_models.get_mut(agent).unwrap().roles.clear();
        let cleared=app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
        assert!(!app.store.seed_new_agent_default("admin",agent).await.unwrap());
        assert_eq!(app.store.model_catalog().await.unwrap(),cleared);
    }
    async fn tenant_user(app: &App, name: &str) -> String {
        let account=app.store.create_user_account(name,"long-hash-placeholder").await.unwrap();
        let id=account["user_id"].as_str().unwrap();
        app.store.update_account_access(id,1,crate::access::AccountStatus::Active,
            crate::access::Role::User,"operator").await.unwrap();
        id.to_owned()
    }
    fn agent_query(agent: &str) -> Query<HashMap<String,String>> {
        Query(HashMap::from([("agent_id".into(),agent.into())]))
    }
    // TENANT-API-PRIVACY: grants, roster, tier and administrator checks compose independently.
    #[tokio::test]
    async fn tenant_members_cannot_edit_or_read_private_model_connections() {
        let (_root,app)=fixture().await;
        let mut catalog=selection_fixture(&app).await;
        let owner=tenant_user(&app,"tenant-owner").await;
        let member=tenant_user(&app,"tenant-member").await;
        let outsider=tenant_user(&app,"outsider").await;
        let identity=json!({"_user_id":owner});
        let Json(created)=create_agent(State(app.clone()),Extension(identity.clone()),Json(NewAgent {
            client_request_id:"tenant-api".into(),name:"Tenant".into(),description:None})).await.unwrap();
        let agent=created["id"].as_str().unwrap();
        // Both connections are granted; only one is placed on the tenant roster.
        for id in ["one","two"] {
            catalog.connections.get_mut(id).unwrap().allowed_agents.insert(agent.into());
            catalog.entries.get_mut(id).unwrap().allowed_agents.insert(agent.into());
        }
        let mut roster=catalog.agent_models["agent-a"].clone();
        roster.agent_id=agent.into();roster.owner_id=owner.clone();roster.revision=1;
        roster.entry_ids=vec!["one".into()];
        roster.roles=std::collections::BTreeMap::from([(crate::model_selection::ModelRole::Standard,
            crate::model_selection::RoleAssignment {entry_id:"one".into(),backup_entry_ids:vec![]})]);
        catalog.agent_models.insert(agent.into(),roster);
        let mut private=catalog.connections["one"].clone();
        private.id="private".into();private.allowed_agents=std::collections::BTreeSet::from(["agent-a".into()]);
        catalog.connections.insert("private".into(),private);
        catalog=app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
        app.store.assign_agent_membership(&owner,&member,agent,Relationship::Member,false,0).await.unwrap();
        let member_identity=json!({"_user_id":member});
        let Json(view)=read(State(app.clone()),Extension(member_identity.clone()),agent_query(agent)).await.unwrap();
        assert!(view["catalog"]["connections"].get("private").is_none());
        assert!(view["catalog"]["connections"].get("two").is_none());
        assert_eq!(view["catalog"]["connections"]["one"]["credential_ref"],"");
        assert_eq!(view["available_entries"],json!(["one"]));
        assert!(matches!(save(State(app.clone()),Extension(member_identity.clone()),agent_query(agent),
            Json(scoped(&catalog,&member,agent))).await,Err(ApiError(StatusCode::FORBIDDEN,_))));
        assert!(matches!(read(State(app.clone()),Extension(json!({"_user_id":outsider})),agent_query(agent)).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        assert!(matches!(members(State(app.clone()),Extension(member_identity.clone()),Path(agent.into())).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        assert!(connection_status(State(app.clone()),Extension(member_identity.clone()),Path("one".into()),agent_query(agent)).await.is_ok());
        assert!(connection_status(State(app.clone()),Extension(member_identity.clone()),Path("two".into()),agent_query(agent)).await.is_err());
        assert!(connection_status(State(app.clone()),Extension(identity.clone()),Path("private".into()),agent_query(agent)).await.is_err());
        // The tenant owner may manage shared entries, but cannot replace their owner's credentials.
        assert!(matches!(connection_key(State(app.clone()),Extension(identity.clone()),Path("one".into()),
            Json(ApiKey {api_key:"valid-fixture-secret".into()})).await,Err(ApiError(StatusCode::FORBIDDEN,_))));
        assert!(matches!(connection_authorize(State(app.clone()),Extension(identity),Path("one".into())).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        let revision=app.store.account_by_id(&member).await.unwrap().unwrap()["access_revision"].as_u64().unwrap();
        app.store.update_account_access(&member,revision,crate::access::AccountStatus::Active,crate::access::Role::User,"basic").await.unwrap();
        assert!(connection_status(State(app.clone()),Extension(member_identity.clone()),Path("one".into()),agent_query(agent)).await.is_err());
        let Json(view)=read(State(app),Extension(member_identity),agent_query(agent)).await.unwrap();
        assert_eq!(view["available_entries"],json!([]));
    }
    // TENANT-API-MUTATION: scoped editors cannot forge shared references or overwrite newer revisions.
    #[tokio::test]
    async fn scoped_catalog_rejects_reference_forgery_and_stale_revisions() {
        let (_root,app)=fixture().await;
        let catalog=selection_fixture(&app).await;
        let manager=tenant_user(&app,"delegated-manager").await;
        app.store.assign_agent_membership("admin",&manager,"agent-a",Relationship::Member,true,0).await.unwrap();
        let identity=json!({"_user_id":manager});
        let mut forged=scoped(&catalog,&manager,"agent-a");
        forged.connections.get_mut("one").unwrap().credential_ref="credential:two".into();
        assert!(matches!(save(State(app.clone()),Extension(identity.clone()),agent_query("agent-a"),Json(forged)).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        let original=scoped(&catalog,&manager,"agent-a");
        let mut forged=original.clone();
        forged.entries.get_mut("one").unwrap().connection_id="two".into();
        assert!(matches!(save(State(app.clone()),Extension(identity.clone()),agent_query("agent-a"),Json(forged)).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        // A valid scoped round trip preserves grants that this recipient cannot see.
        let _ = save(State(app.clone()),Extension(identity.clone()),agent_query("agent-a"),Json(original.clone())).await.unwrap();
        let persisted=app.store.model_catalog().await.unwrap();
        assert!(persisted.connections["one"].allowed_agents.contains("shared_default"));
        assert!(matches!(save(State(app.clone()),Extension(identity),agent_query("agent-a"),Json(original)).await,
            Err(ApiError(StatusCode::CONFLICT,_))));
        let mut forged=scoped(&persisted,"admin","agent-a");
        forged.connections.get_mut("one").unwrap().credential_ref="credential:two".into();
        assert!(matches!(save(State(app),Extension(json!({"_user_id":"admin"})),agent_query("agent-a"),Json(forged)).await,
            Err(ApiError(StatusCode::BAD_REQUEST,_))));
    }
}
