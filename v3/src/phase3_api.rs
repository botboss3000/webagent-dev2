//! Administrative account, entitlement, and membership APIs.
use crate::{access::{AccountStatus, Principal, Relationship, Role, Tier}, App, Api, ApiError};
use argon2::{password_hash::{PasswordHasher, SaltString}, Argon2};
use axum::{extract::{Path, Query, State}, http::StatusCode, routing::get, Extension, Json, Router};
use rand::rngs::OsRng;
use serde::Deserialize;
use serde_json::{json, Value};

fn invalid(message: &str) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, message.into())
}
fn stale(message: &str) -> ApiError {
    ApiError(StatusCode::CONFLICT, message.into())
}
pub(crate) async fn administrator(app: &App, identity: &Value) -> Result<(), ApiError> {
    let id = identity["_user_id"].as_str().unwrap_or("");
    let account = app.store.account_by_id(id).await?
        .ok_or_else(|| ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()))?;
    let principal = Principal::from_account(&account)
        .map_err(|_| ApiError(StatusCode::FORBIDDEN, "Administrator access required".into()))?;
    if !principal.administrator() {
        return Err(ApiError(StatusCode::FORBIDDEN, "Administrator access required".into()));
    }
    Ok(())
}
/// Called while holding App.gate after an access mutation.
async fn quiesce_user(app: &App, user_id: &str, agent_filter: Option<&str>) -> Result<(), ApiError> {
    let mut caller = app.clone();
    caller.store = app.store.for_user(user_id).await?;
    for view in caller.store.active_runs().await? {
        let id = view["id"].as_str().unwrap_or("");
        let mut run = caller.run(id).await?;
        let agent_id = run["agent_id"].as_str().unwrap_or("shared_default");
        if agent_filter.is_some_and(|filter| filter != agent_id) {
            continue;
        }
        if !matches!(crate::status(&run), "queued" | "running" | "waiting_approval") {
            continue;
        }
        if let Some(token) = app.active.lock().await.get(id) {
            token.cancel();
        }
        if run["uncertain"] == true || (run["pending"]["state"] == "executing"
            && !crate::tools::is_read(run["pending"]["name"].as_str().unwrap_or(""))) {
            run["status"] = json!("recovery");
            run["uncertain"] = json!(true);
            run["error"] = json!("Access changed during an uncertain tool effect; review required");
        } else {
            run["status"] = json!("stopped");
            run["error"] = json!("Access changed; review permissions before resuming");
        }
        caller.save_override(&mut run).await?;
    }
    Ok(())
}
pub(crate) async fn quiesce_agent(app: &App, agent_id: &str) -> Result<(), ApiError> {
    for user_id in app.store.all_user_ids().await? {
        quiesce_user(app, &user_id, Some(agent_id)).await?;
    }
    Ok(())
}
async fn quiesce_tier(app: &App, tier_id: &str) -> Result<(), ApiError> {
    for user_id in app.store.all_user_ids().await? {
        if app.store.account_by_id(&user_id).await?
            .is_some_and(|account| account["tier_id"] == tier_id) {
            quiesce_user(app, &user_id, None).await?;
        }
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NewAccount { username: String, password: String }
async fn accounts(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    administrator(&app, &identity).await?;
    Ok(Json(json!({"accounts": app.store.access_accounts().await?})))
}
async fn create_account(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Json(body): Json<NewAccount>,
) -> Api {
    administrator(&app, &identity).await?;
    if body.password.len() < 8 || body.password.len() > 1024 {
        return Err(invalid("Password must be 8-1024 characters"));
    }
    let password = body.password;
    let hash = tokio::task::spawn_blocking(move || {
        let salt = SaltString::generate(&mut OsRng);
        Argon2::default().hash_password(password.as_bytes(), &salt)
            .map(|value| value.to_string())
    }).await.map_err(|_| invalid("Password hashing unavailable"))?
        .map_err(|_| invalid("Password hashing failed"))?;
    let account = app.store.create_user_account(&body.username, &hash).await
        .map_err(|_| stale("Account creation rejected; check username and existing accounts"))?;
    Ok(Json(json!({"account":account})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AccountChange {
    expected_revision: u64,
    status: AccountStatus,
    role: Role,
    tier_id: String,
}
async fn change_account(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<AccountChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let account = app.store.update_account_access(&id, body.expected_revision,
        body.status, body.role, &body.tier_id).await
        .map_err(|_| stale("Account update rejected; reload the account and tier"))?;
    quiesce_user(&app, &id, None).await?;
    Ok(Json(json!({"account":account})))
}
async fn tiers(State(app): State<App>, Extension(identity): Extension<Value>) -> Api {
    administrator(&app, &identity).await?;
    Ok(Json(json!({"tiers":app.store.entitlement_tiers().await?})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TierChange { expected_revision: u64, tier: Tier }
async fn change_tier(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<TierChange>,
) -> Api {
    administrator(&app, &identity).await?;
    if id != body.tier.id {
        return Err(invalid("Tier ID does not match route"));
    }
    let _guard = app.gate.lock().await;
    let tier = app.store.save_entitlement_tier(body.tier, body.expected_revision).await
        .map_err(|_| stale("Tier update rejected; reload the tier and grants"))?;
    quiesce_tier(&app, &id).await?;
    Ok(Json(json!({"tier":tier})))
}
async fn membership(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path((user_id, agent_id)): Path<(String, String)>,
) -> Api {
    administrator(&app, &identity).await?;
    let value = app.store.agent_membership(&user_id, &agent_id).await
        .map_err(|_| invalid("Invalid membership identity"))?;
    Ok(Json(json!({"membership":value})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MembershipChange { expected_revision: u64, relationship: Relationship }
async fn change_membership(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path((user_id, agent_id)): Path<(String, String)>,
    Json(body): Json<MembershipChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let value = app.store.set_agent_membership(&user_id, &agent_id,
        body.relationship, body.expected_revision).await
        .map_err(|_| stale("Membership update rejected; reload the account and agent"))?;
    quiesce_user(&app, &user_id, Some(&agent_id)).await?;
    Ok(Json(json!({"membership":value})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolPermissionsChange {
    expected_revision: u64,
    tool_permissions: std::collections::BTreeMap<String, crate::access::Permission>,
}
async fn agent_tool_permissions(
    State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
) -> Api {
    administrator(&app, &identity).await?;
    let value = app.store.agent_tool_permissions(&id).await
        .map_err(|_| invalid("Agent permissions unavailable"))?;
    Ok(Json(value))
}
async fn change_agent_tool_permissions(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<ToolPermissionsChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let value = app.store.set_agent_tool_permissions(&id, body.expected_revision,
        body.tool_permissions).await
        .map_err(|_| stale("Agent permissions changed; reload before saving"))?;
    quiesce_agent(&app, &id).await?;
    Ok(Json(value))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolCallersChange {
    expected_revision: u64,
    tool_callers: std::collections::BTreeMap<String, Vec<Relationship>>,
}
async fn agent_tool_callers(
    State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
) -> Api {
    administrator(&app, &identity).await?;
    let value = app.store.agent_tool_callers(&id).await
        .map_err(|_| invalid("Agent caller restrictions unavailable"))?;
    Ok(Json(value))
}
async fn change_agent_tool_callers(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<ToolCallersChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let value = app.store.set_agent_tool_callers(&id, body.expected_revision,
        body.tool_callers).await
        .map_err(|_| stale("Agent caller restrictions changed; reload before saving"))?;
    quiesce_agent(&app, &id).await?;
    Ok(Json(value))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AbilityPermissionsChange {
    expected_revision: u64,
    ability_permissions: std::collections::BTreeMap<String, crate::access::Permission>,
}
async fn agent_ability_permissions(
    State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
) -> Api {
    administrator(&app, &identity).await?;
    let value = app.store.agent_ability_permissions(&id).await
        .map_err(|_| invalid("Agent ability permissions unavailable"))?;
    Ok(Json(value))
}
async fn change_agent_ability_permissions(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<AbilityPermissionsChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    let value = app.store.set_agent_ability_permissions(&id, body.expected_revision,
        body.ability_permissions).await
        .map_err(|_| stale("Agent ability permissions changed; reload before saving"))?;
    quiesce_agent(&app, &id).await?;
    Ok(Json(value))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuditArgs { limit: Option<u32> }
async fn access_audit(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Query(args): Query<AuditArgs>,
) -> Api {
    administrator(&app, &identity).await?;
    let events = app.store.access_audit(args.limit.unwrap_or(50)).await
        .map_err(|_| invalid("Audit limit must be 1-200"))?;
    Ok(Json(json!({"events":events})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentStatusChange { expected_revision: u64, status: String }
async fn agent_status(
    State(app): State<App>, Extension(identity): Extension<Value>, Path(id): Path<String>,
) -> Api {
    administrator(&app, &identity).await?;
    let status = app.store.agent_status(&id).await
        .map_err(|_| invalid("Agent status unavailable"))?;
    Ok(Json(status))
}
async fn change_agent_status(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<AgentStatusChange>,
) -> Api {
    administrator(&app, &identity).await?;
    if !matches!(body.status.as_str(), "active" | "disabled") {
        return Err(invalid("Agent status must be active or disabled"));
    }
    let _guard = app.gate.lock().await;
    let status = app.store.set_agent_status(&id, body.expected_revision, &body.status).await
        .map_err(|_| stale("Agent status changed; reload before saving"))?;
    quiesce_agent(&app, &id).await?;
    Ok(Json(status))
}
pub fn router() -> Router<App> {
    Router::new()
        .route("/accounts", get(accounts).post(create_account))
        .route("/accounts/{id}", axum::routing::put(change_account))
        .route("/tiers", get(tiers))
        .route("/tiers/{id}", axum::routing::put(change_tier))
        .route("/agents/{id}/tool-callers",
            get(agent_tool_callers).put(change_agent_tool_callers))
        .route("/memberships/{user_id}/{agent_id}",
            get(membership).put(change_membership))
        .route("/agents/{id}/tool-permissions",
            get(agent_tool_permissions).put(change_agent_tool_permissions))
        .route("/agents/{id}/ability-permissions",
            get(agent_ability_permissions).put(change_agent_ability_permissions))
        .route("/agents/{id}/status",
            get(agent_status).put(change_agent_status))
        .route("/audit", get(access_audit))
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_tests::fixture;

    #[tokio::test]
    async fn admin_api_creates_pending_account_and_requires_live_role() {
        let (_root, app) = fixture().await;
        let admin = json!({"_user_id":"admin"});
        let Json(created) = create_account(State(app.clone()), Extension(admin.clone()),
            Json(NewAccount { username:"new_user".into(),
                password:"test-password-only".into() })).await.unwrap();
        let id = created["account"]["user_id"].as_str().unwrap();
        assert_eq!(created["account"]["status"], "pending");
        let pending = json!({"_user_id":id});
        assert!(accounts(State(app.clone()), Extension(pending.clone())).await.is_err());
        let Json(updated) = change_account(State(app.clone()), Extension(admin.clone()),
            Path(id.into()), Json(AccountChange { expected_revision:1,
                status:AccountStatus::Active, role:Role::User,
                tier_id:"basic".into() })).await.unwrap();
        assert_eq!(updated["account"]["status"], "active");
        assert!(accounts(State(app.clone()), Extension(pending.clone())).await.is_err());
        let Json(promoted) = change_account(State(app.clone()), Extension(admin),
            Path(id.into()), Json(AccountChange { expected_revision:2,
                status:AccountStatus::Active, role:Role::Administrator,
                tier_id:"basic".into() })).await.unwrap();
        assert_eq!(promoted["account"]["role"], "administrator");
        assert!(accounts(State(app.clone()), Extension(pending)).await.is_ok());
    }
    #[tokio::test]
    async fn agent_status_api_disables_calls_and_rejects_stale_revision() {
        let (_root, app) = fixture().await;
        let admin = json!({"_user_id":"admin"});
        let Json(before) = agent_status(State(app.clone()), Extension(admin.clone()),
            Path("shared_default".into())).await.unwrap();
        let revision = before["revision"].as_u64().unwrap();
        let Json(disabled) = change_agent_status(State(app.clone()), Extension(admin.clone()),
            Path("shared_default".into()), Json(AgentStatusChange {
                expected_revision:revision, status:"disabled".into() })).await.unwrap();
        assert_eq!(disabled["status"], "disabled");
        assert!(!app.store.live_access("admin", "shared_default").await.unwrap().admitted());
        assert!(change_agent_status(State(app), Extension(admin),
            Path("shared_default".into()), Json(AgentStatusChange {
                expected_revision:revision, status:"active".into() })).await.is_err());
    }
}
