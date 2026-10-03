//! Administrator routes for people, tiers, and agent membership. When access
//! is reduced, active work is stopped or held for review so an old grant cannot
//! continue running in the separate loop process.
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
pub(crate) async fn quiesce_user(app: &App, user_id: &str, agent_filter: Option<&str>) -> Result<(), ApiError> {
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
            && !crate::tools::is_read(run["pending"]["name"].as_str().unwrap_or("")))
            || crate::parallel_batch::executing_write(&run) {
            run["status"] = json!("recovery");
            run["uncertain"] = json!(true);
            // Access revocation preserves reconciliation for every in-flight write.
            crate::parallel_batch::mark_uncertain_writes(&mut run);
            run["error"] = json!("Access changed during an uncertain tool effect; review required");
        } else {
            run["status"] = json!("stopped");
            run["error"] = json!("Access changed; review permissions before resuming");
        }
        caller.save_override(&mut run).await?;
    }
    let mut closure_error=None;
    for session in caller.store.list("session:").await? {
        let agent=session["agent_id"].as_str().unwrap_or("shared_default");
        if agent_filter.is_some_and(|filter|filter!=agent){continue;}
        if let Some(sid)=session["id"].as_str().filter(|sid|uuid::Uuid::parse_str(sid).is_ok()) {
            if let Err(error)=quiesce_session_resources(&caller,sid).await {closure_error=Some(error);}
        }
    }
    if let Some(error)=closure_error {return Err(error);}
    Ok(())
}

/// Close the caller-owned session's persistent resources after a durable restriction.
pub(crate) async fn quiesce_session_resources(app:&App,sid:&str)->Result<(),ApiError> {
    if uuid::Uuid::parse_str(sid).is_err() {return Err(invalid("Invalid session identifier"));}
    if app.store.get(&crate::key("session",sid)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND,"Session not found".into()));
    }
    let host=app.store.get(&format!("administrator_host:{sid}")).await?;
    let browser=app.store.get(&format!("administrator_browser:{sid}")).await?;
    let mut failed=false;
    if let Some(job)=host.as_ref() {
        let trusted=json!({"user_id":app.store.current_user_id(),"session_id":sid,
            "lease_until":crate::now()+30,"access_fingerprint":job["access_fingerprint"]});
        let result=tokio::time::timeout(std::time::Duration::from_secs(2),
            crate::tools::execute_trusted("administrator_host_revoke",&json!({}),sid,
                std::path::Path::new("/run/v3/build/build.sock"),Some(trusted),
                crate::CancellationToken::new())).await;
        failed |= !matches!(result,Ok(Ok(ref value)) if value["status"]=="success");
        if matches!(result,Ok(Ok(ref value)) if value["status"]=="success") {
            app.store.delete(&format!("administrator_host:{sid}")).await?;
        }
    }
    if let Some(browser)=browser.as_ref() {
        if let Some(token)=browser["cookie"].as_str() {
            app.store.delete(&format!("auth:{}",crate::digest(&json!(token)))).await?;
        }
        app.store.delete(&format!("administrator_browser:{sid}")).await?;
    }
    // Missing worker plus no authorization has no runnable persistent resources.
    // A live socket or known authorization must acknowledge bounded shutdown.
    if app.tool_socket.exists() || host.is_some() || browser.is_some() {
        let result=tokio::time::timeout(std::time::Duration::from_secs(10),
            crate::tools::execute_trusted("administrator_quiesce",&json!({}),sid,
                &app.tool_socket,None,crate::CancellationToken::new())).await;
        failed |= !matches!(result,Ok(Ok(ref value)) if value["status"]=="success");
    }
    app.store.event(sid,if failed {"administrator_quiesce_failed"} else {"administrator_quiesced"},
        &json!({"reason":"session_access_changed"})).await?;
    if failed {return Err(ApiError(StatusCode::SERVICE_UNAVAILABLE,
        "Session restriction saved; persistent resource shutdown could not be confirmed".into()));}
    Ok(())
}

pub(crate) async fn quiesce_agent(app: &App, agent_id: &str) -> Result<(), ApiError> {
    let mut closure_error=None;
    for user_id in app.store.all_user_ids().await? {
        if let Err(error)=quiesce_user(app,&user_id,Some(agent_id)).await {closure_error=Some(error);}
    }
    if let Some(error)=closure_error {return Err(error);}
    Ok(())
}
async fn quiesce_tier(app: &App, tier_id: &str) -> Result<(), ApiError> {
    let mut closure_error=None;
    for user_id in app.store.all_user_ids().await? {
        if app.store.account_by_id(&user_id).await?
            .is_some_and(|account| account["tier_id"] == tier_id) {
            if let Err(error)=quiesce_user(app,&user_id,None).await {closure_error=Some(error);}
        }
    }
    if let Some(error)=closure_error {return Err(error);}
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
    if identity["_user_id"]==id && (body.status!=AccountStatus::Active || body.role!=Role::Administrator) {
        return Err(invalid("You cannot restrict or demote your own administrator account"));
    }
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
struct MembershipChange { expected_revision: u64, relationship: Relationship,
    #[serde(default)] agent_administrator: bool }
async fn change_membership(
    State(app): State<App>, Extension(identity): Extension<Value>,
    Path((user_id, agent_id)): Path<(String, String)>,
    Json(body): Json<MembershipChange>,
) -> Api {
    administrator(&app, &identity).await?;
    let _guard = app.gate.lock().await;
    // LEGACY-MEMBERSHIP: installation oversight uses the same owner and revision checks as tenant APIs.
    let value = app.store.assign_agent_membership(identity["_user_id"].as_str().unwrap_or(""),
        &user_id, &agent_id, body.relationship, body.agent_administrator, body.expected_revision).await
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
async fn page_policy(State(app):State<App>,Extension(identity):Extension<Value>)->Api {
    administrator(&app,&identity).await?;
    let policy=app.store.read_access_policy()?;
    Ok(Json(json!({"pages":policy.get("pages").cloned().unwrap_or_else(||json!({})),"revision":policy["revision"].as_u64().unwrap_or(0)})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PagePolicyChange {expected_revision:u64,pages:Value}
async fn change_page_policy(State(app):State<App>,Extension(identity):Extension<Value>,Json(body):Json<PagePolicyChange>)->Api {
    administrator(&app,&identity).await?;
    crate::registration::validate_pages(&body.pages).map_err(|_|invalid("Invalid page audience policy"))?;
    let _guard=app.gate.lock().await;
    let policy=app.store.save_page_policy(body.pages,body.expected_revision).map_err(|_|stale("Policy changed; reload before saving"))?;
    for id in app.store.all_user_ids().await? {quiesce_user(&app,&id,None).await?;}
    app.store.event("","page_policy_changed",&json!({"revision":policy["revision"]})).await?;
    Ok(Json(policy))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileChange { username:String, display_name:String }
async fn change_profile(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(body):Json<ProfileChange>)->Api {
    administrator(&app,&identity).await?;
    let _guard=app.gate.lock().await;
    let profile=app.store.update_user_profile(&id,&body.username,&body.display_name).await.map_err(|_|invalid("Profile update rejected"))?;
    Ok(Json(profile))
}
async fn account_sessions(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    administrator(&app,&identity).await?;
    let account=app.store.account_by_id(&id).await?.ok_or_else(||invalid("Account not found"))?;
    let sessions=app.store.auth_sessions(&id,account["generation"].as_str().unwrap_or(""),crate::now()).await?;
    let public:Vec<Value>=sessions.into_iter().map(|mut session|{session.as_object_mut().unwrap().remove("key");session}).collect();
    Ok(Json(json!({"sessions":public})))
}
async fn revoke_account_session(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,session_id)):Path<(String,String)>)->Api {
    administrator(&app,&identity).await?;
    let _guard=app.gate.lock().await;
    let account=app.store.account_by_id(&id).await?.ok_or_else(||invalid("Account not found"))?;
    let sessions=app.store.auth_sessions(&id,account["generation"].as_str().unwrap_or(""),crate::now()).await?;
    let session=sessions.iter().find(|s|s["id"]==session_id).ok_or_else(||invalid("Sign-in session not found"))?;
    app.store.delete(session["key"].as_str().unwrap()).await?;
    Ok(Json(json!({"revoked":true})))
}
pub fn router() -> Router<App> {
    Router::new()
        .route("/page-policy",get(page_policy).put(change_page_policy))
        .route("/accounts", get(accounts).post(create_account))
        .route("/accounts/{id}", axum::routing::put(change_account))
        .route("/accounts/{id}/profile",axum::routing::put(change_profile))
        .route("/accounts/{id}/sessions",get(account_sessions))
        .route("/accounts/{id}/sessions/{session_id}",axum::routing::delete(revoke_account_session))
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

    #[tokio::test]
    async fn session_resource_quiesce_withdraws_only_own_browser_and_requires_worker_ack() {
        use tokio::io::{AsyncBufReadExt,AsyncWriteExt,BufReader};
        let (_root,app)=fixture().await;
        let sid=crate::uuid();let other=crate::uuid();let cookie=crate::uuid();let other_cookie=crate::uuid();
        for (id,token) in [(&sid,&cookie),(&other,&other_cookie)] {
            app.store.put(&crate::key("session",id),&json!({"id":id,"agent_id":"shared_default"})).await.unwrap();
            app.store.put(&format!("administrator_browser:{id}"),&json!({"cookie":token,"session_id":id})).await.unwrap();
            app.store.put(&format!("auth:{}",crate::digest(&json!(token))),&json!({"user_id":"admin"})).await.unwrap();
        }
        app.store.put(&format!("auth:{}",crate::digest(&json!(cookie))),&json!({"user_id":"admin"})).await.unwrap();
        let listener=tokio::net::UnixListener::bind(&app.tool_socket).unwrap();
        let expected=sid.clone();
        let worker=tokio::spawn(async move {
            let (stream,_)=listener.accept().await.unwrap();
            let mut reader=BufReader::new(stream);let mut line=String::new();
            reader.read_line(&mut line).await.unwrap();let request:Value=serde_json::from_str(&line).unwrap();
            assert_eq!(request["name"],"administrator_quiesce");assert_eq!(request["workspace"],expected);
            let response=format!("{}\n",json!({"id":request["id"],"status":"success"}));
            reader.get_mut().write_all(response.as_bytes()).await.unwrap();
        });
        quiesce_session_resources(&app,&sid).await.unwrap();worker.await.unwrap();
        assert!(app.store.get(&format!("administrator_browser:{sid}")).await.unwrap().is_none());
        assert!(app.store.get(&format!("auth:{}",crate::digest(&json!(cookie)))).await.unwrap().is_none());
        assert!(app.store.get(&format!("administrator_browser:{other}")).await.unwrap().is_some());
        assert!(app.store.get(&format!("auth:{}",crate::digest(&json!(other_cookie)))).await.unwrap().is_some());
    }
    #[tokio::test]
    async fn failed_shutdown_keeps_restriction_durable_and_withdraws_authentication() {
        let (_root,app)=fixture().await;let sid=crate::uuid();let cookie=crate::uuid();
        app.store.put(&crate::key("session",&sid),&json!({"id":sid,"agent_id":"shared_default"})).await.unwrap();
        app.store.put(&format!("administrator_browser:{sid}"),&json!({"cookie":cookie})).await.unwrap();
        let auth=format!("auth:{}",crate::digest(&json!(cookie)));
        app.store.put(&auth,&json!({"user_id":"admin"})).await.unwrap();
        let result=crate::api::session_mode_set(State(app.clone()),Path(sid.clone()),
            Json(crate::api::SessionModeBody {mode:"read_only".into()})).await;
        assert!(matches!(result,Err(ApiError(StatusCode::SERVICE_UNAVAILABLE,_))));
        assert_eq!(crate::session_mode(&app,&sid).await.unwrap()["mode"],"read_only");
        assert!(app.store.get(&auth).await.unwrap().is_none());
        assert!(app.store.get(&format!("administrator_browser:{sid}")).await.unwrap().is_none());
        // A caller retry can confirm no-worker shutdown without widening the saved mode.
        let _ = crate::api::session_mode_set(State(app.clone()),Path(sid.clone()),
            Json(crate::api::SessionModeBody {mode:"read_only".into()})).await.unwrap();
        assert!(quiesce_session_resources(&app,&crate::uuid()).await.is_err());
    }
}
