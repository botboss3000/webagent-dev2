//! Administrator-only model catalog, agent creation, and scoped authorization.
//! Runtime plans remain pinned while live grants/revocation are enforced at dispatch.
use crate::{App, Api, ApiError};
use crate::access::{Principal, Relationship};
use crate::model_selection::Catalog;
use axum::{extract::{State, Path}, Extension, Json, Router, routing::get, http::StatusCode};
use serde_json::{json, Value};

async fn require_admin(app: &App, identity: &Value) -> Result<(), ApiError> {
    let user = identity["_user_id"].as_str().unwrap_or("");
    let account = app.store.account_by_id(user).await?;
    if account.as_ref().and_then(|account| Principal::from_account(account).ok())
        .is_none_or(|principal| !principal.administrator()) {
        return Err(ApiError(StatusCode::FORBIDDEN, "Administrator access required".into()));
    }
    Ok(())
}

async fn read(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    require_admin(&app, &identity).await?;
    let catalog = app.store.model_catalog().await?;
    Ok(Json(json!({"catalog":catalog,"caller_id":identity["_user_id"],"supported_transports":["openai_subscription","deepseek"],
        "execution_status":"implemented_pending_live_validation"})))
}

async fn save(State(app): State<App>, Extension(identity): Extension<Value>, Json(catalog): Json<Catalog>) -> Api {
    require_admin(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let old = app.store.model_catalog().await?;
    if old.revision != catalog.revision {
        return Err(ApiError(StatusCode::CONFLICT, "Model catalog revision changed; reload before saving".into()));
    }
    let owner = identity["_user_id"].as_str().unwrap_or("");
    // Full user-owned sharing management is deferred. Do not allow this admin
    // endpoint to mint records that pretend to be owned by another account.
    macro_rules! new_owners {
        ($field:ident) => {
            for (id, record) in &catalog.$field {
                if !old.$field.contains_key(id) && record.owner_id != owner {
                    return Err(ApiError(StatusCode::FORBIDDEN, "New records must belong to the caller".into()));
                }
            }
        };
    }
    new_owners!(connections); new_owners!(entries); new_owners!(groups); new_owners!(assignments);
    for id in catalog.assignments.keys() {
        let view = app.store.for_agent(id).await
            .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "Assigned agent does not exist".into()))?;
        let exists: Option<String> = sqlx::query_scalar("SELECT id FROM agents WHERE id=?")
            .bind(id).fetch_optional(&view.agent_pool).await.map_err(anyhow::Error::from)?;
        if exists.is_none() {
            return Err(ApiError(StatusCode::BAD_REQUEST, "Assigned agent does not exist".into()));
        }
    }
    let saved = app.store.save_model_catalog(&catalog, catalog.revision).await
        .map_err(|_| ApiError(StatusCode::BAD_REQUEST, "Catalog update rejected: check references, grants, ownership and record revisions".into()))?;
    Ok(Json(json!({"catalog":saved,"execution_status":"implemented_pending_live_validation"})))
}


async fn agents(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    use sqlx::Row;
    require_admin(&app, &identity).await?;
    let mut values = Vec::new();
    for id in app.store.agent_ids().await? {
        let view=app.store.for_agent(&id).await?;
        let r=sqlx::query("SELECT id,name,description,status,revision FROM agents WHERE id=?")
            .bind(&id).fetch_one(&view.agent_pool).await.map_err(anyhow::Error::from)?;
        values.push(json!({"id":r.get::<String,_>("id"),"name":r.get::<String,_>("name"),
            "description":r.get::<String,_>("description"),"status":r.get::<String,_>("status"),
            "revision":r.get::<i64,_>("revision")}));
    }
    Ok(Json(json!({"agents":values})))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct NewAgent { client_request_id: String, name: String, description: Option<String> }
async fn create_agent(State(app): State<App>, Extension(identity): Extension<Value>, Json(body): Json<NewAgent>) -> Api {
    require_admin(&app, &identity).await?;
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

async fn connection_status(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    require_admin(&app,&identity).await?;
    let catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get(&id).ok_or_else(|| ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
    let login=app.login.read().await;
    let authorization=login.as_ref().filter(|v| v["connection_id"]==id).map(|v|json!({
        "status":v["status"],"verification_url":v["verification_url"],"user_code":v["user_code"]}));
    Ok(Json(json!({"id":id,"revoked":connection.revoked,
        "connected":!connection.revoked && app.store.get(&connection.credential_ref).await?.is_some(),"authorization":authorization})))
}
async fn connection_authorize(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>) -> Api {
    require_admin(&app,&identity).await?;
    let _guard=app.gate.lock().await;
    let mut catalog=app.store.model_catalog().await?;
    let connection=catalog.connections.get_mut(&id).ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Connection not found".into()))?;
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
async fn connection_key(State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>, Json(body): Json<ApiKey>) -> Api {
    require_admin(&app, &identity).await?;
    if body.api_key.len() < 8 || body.api_key.len() > 512 || !body.api_key.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(crate::bad("Invalid API key: use 8–512 printable non-space characters"));
    }
    let _guard = app.gate.lock().await;
    let mut catalog = app.store.model_catalog().await?;
    let connection = catalog.connections.get_mut(&id).ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Connection not found".into()))?;
    if connection.revoked { return Err(crate::conflict("Connection is revoked")); }
    if connection.transport != crate::model_selection::Transport::Deepseek { return Err(crate::bad("This connection does not use a DeepSeek API key")); }
    let reference = format!("provider_credential:{}", crate::uuid());
    // Store the secret only in the encrypted credential plane. No key in catalog/events.
    app.store.put(&reference, &json!({"api_key":body.api_key})).await?;
    connection.credential_ref = reference;
    app.store.save_model_catalog(&catalog, catalog.revision).await?;
    Ok(Json(json!({"saved":true})))
}

/// Nest only under the authenticated browser API (including its CSRF middleware).
pub fn router() -> Router<App> {
    Router::new().route("/catalog", get(read).put(save))
        .route("/agents", get(agents).post(create_agent))
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
        let Json(status)=connection_status(State(app.clone()),Extension(identity.clone()),Path("one".into())).await.unwrap();
        assert_eq!(status["connected"],true);assert!(!status.to_string().contains("test-key"));
        assert!(!serde_json::to_string(&catalog).unwrap().contains("test-key"));
        let mut catalog=catalog;catalog.connections.get_mut("one").unwrap().revoked=true;
        app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
        assert!(connection_key(State(app.clone()),Extension(identity),Path("one".into()),Json(ApiKey{api_key:"test-key-no-network".into()})).await.is_err());
    }
}
