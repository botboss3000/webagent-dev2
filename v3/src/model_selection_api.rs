//! Administrator-only model catalog, agent creation, and scoped authorization.
//! Runtime plans remain pinned while live grants/revocation are enforced at dispatch.
use crate::{App, Api, ApiError};
use crate::model_selection::Catalog;
use axum::{extract::{State, Path}, Extension, Json, Router, routing::get, http::StatusCode};
use serde_json::{json, Value};

async fn require_admin(app: &App, identity: &Value) -> Result<(), ApiError> {
    let user = identity["_user_id"].as_str().unwrap_or("");
    let account = app.store.account_by_id(user).await?;
    let profile = app.store.user_profile(user).await?;
    if account.as_ref().is_none_or(|account| account["is_approved"] != true)
        || profile.as_ref().is_none_or(|profile| profile["is_admin"] != true) {
        return Err(ApiError(StatusCode::FORBIDDEN, "Administrator access required".into()));
    }
    Ok(())
}

async fn read(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    require_admin(&app, &identity).await?;
    let catalog = app.store.model_catalog().await?;
    Ok(Json(json!({"catalog":catalog,"caller_id":identity["_user_id"],"supported_transports":["openai_subscription"],
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
        let exists: Option<String> = sqlx::query_scalar("SELECT id FROM agents WHERE id=?")
            .bind(id).fetch_optional(&app.store.agent_pool).await.map_err(anyhow::Error::from)?;
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
    let rows = sqlx::query("SELECT id,name,description,status,revision FROM agents ORDER BY created_at,id")
        .fetch_all(&app.store.agent_pool).await.map_err(anyhow::Error::from)?;
    let values = rows.iter().map(|r| json!({"id":r.get::<String,_>("id"),"name":r.get::<String,_>("name"),
        "description":r.get::<String,_>("description"),"status":r.get::<String,_>("status"),"revision":r.get::<i64,_>("revision")})).collect::<Vec<_>>();
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
    let existing: Option<Vec<u8>> = sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
        .bind(&id).fetch_optional(&app.store.agent_pool).await.map_err(anyhow::Error::from)?;
    if let Some(blob)=existing {
        if app.store.unseal(&blob)?["creation_digest"]!=crate::digest(&request) { return Err(crate::conflict("Request identifier reused with different input")); }
    } else {
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,?,'openai_subscription','','active',?,1,?,?)")
            .bind(&id).bind(body.name.trim()).bind(body.description.as_deref().unwrap_or(""))
            .bind(metadata).bind(crate::now() as i64).bind(crate::now() as i64)
            .execute(&app.store.agent_pool).await.map_err(anyhow::Error::from)?;
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
    if app.login.read().await.as_ref().is_some_and(|v|v["status"]=="pending") {
        return Err(crate::conflict("Authorization already pending"));
    }
    // Server mints each credential namespace. Reconnect invalidates old pinned refs.
    let reference=format!("provider_credential:{}",crate::uuid());
    connection.credential_ref=reference.clone();
    app.store.save_model_catalog(&catalog,catalog.revision).await?;
    crate::provider_start_for(app.clone(),reference,Some(id)).await
}

/// Nest only under the authenticated browser API (including its CSRF middleware).
pub fn router() -> Router<App> {
    Router::new().route("/catalog", get(read).put(save))
        .route("/agents", get(agents).post(create_agent))
        .route("/connections/{id}", get(connection_status))
        .route("/connections/{id}/authorization", axum::routing::post(connection_authorize))
}
