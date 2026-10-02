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
    let available:Vec<_>=view.entries.values().filter(|e|e.allowed_agents.contains(agent)
        && view.connections.get(&e.connection_id).is_some_and(|c|!c.revoked && c.allowed_agents.contains(agent))
        && access.as_ref().is_some_and(|a|a.model_allowed(&e.upstream_model))
        && (rights["can_manage"] == true || catalog.agent_models.get(agent).is_some_and(|m|m.entry_ids.contains(&e.id))))
        .map(|e|e.id.clone()).collect();
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
    Ok(Json(json!({"catalog":view,"caller_id":caller.user_id,"can_manage":rights["can_manage"],
        "can_create_agent":caller.role != crate::access::Role::Guest,"agent_owner_id":rights["owner_id"],
        "available_entries":available,"supported_transports":["openai_subscription","deepseek"]})))
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
    if !input.assignments.is_empty() || !input.groups.is_empty()
        || input.agent_models.keys().any(|id|id != agent) {return Err(denied());}
    if let Some(models)=input.agent_models.get(agent) {
        if models.owner_id != rights["owner_id"].as_str().unwrap_or("") || models.agent_id != agent {return Err(denied());}
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
    let result=json!({"id":id,"name":body.name.trim(),"assignment_required":true});
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
// CREDENTIAL-AUTHORIZATION: only the connection owner starts a private device flow.
async fn connection_authorize(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    let caller=principal(&app,&identity).await?;
    let _guard=app.gate.lock().await;
    let mut catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get_mut(&id).ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
    if connection.owner_id != caller.user_id {return Err(denied());}
    if connection.revoked { return Err(crate::conflict("Connection is revoked")); }
    if connection.transport != crate::model_selection::Transport::OpenaiSubscription { return Err(crate::bad("This provider uses an API key, not device authorization")); }
    if app.login.read().await.as_ref().is_some_and(|v|v["status"]=="pending") {
        return Err(crate::conflict("Authorization already pending"));
    }
    // Server mints each credential namespace. Reconnect invalidates old pinned refs.
    let reference=format!("provider_credential:{}",crate::uuid());
    connection.credential_ref=reference.clone();
    app.store.save_model_catalog(&catalog,catalog.revision).await?;
    crate::provider_start_for(app.clone(),reference,Some(id)).await
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
    if connection.revoked { return Err(crate::conflict("Connection is revoked")); }
    if connection.transport != crate::model_selection::Transport::Deepseek { return Err(crate::bad("This connection does not use a DeepSeek API key")); }
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

/// Nest only under the authenticated browser API (including its CSRF middleware).
pub fn router() -> Router<App> {
    Router::new().route("/catalog", get(read).put(save))
        .route("/agents", get(agents).post(create_agent))
        .route("/agents/{id}/members", get(members).put(member_set))
        .route("/connections/{id}", get(connection_status))
        .route("/connections/{id}/api-key", axum::routing::put(connection_key))
        .route("/connections/{id}/authorization", axum::routing::post(connection_authorize))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_tests::{fixture, selection_fixture};
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
