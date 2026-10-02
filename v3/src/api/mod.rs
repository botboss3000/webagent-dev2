//! HTTP admission, administration, and routing. Rust checks authority before a run is queued.
use super::*;
mod auth;
mod admin;
mod sessions;
mod promotions;
mod chat;
pub(crate) use auth::*;
pub(crate) use admin::*;
pub(crate) use sessions::*;
pub(crate) use promotions::*;
pub(crate) use chat::*;
pub(super) fn router(app: App) -> Router {
    let messaging = user_api::messaging_router()
        .route_layer(middleware::from_fn_with_state(app.clone(), message_api_auth));
    let admin_api = Router::new()
        .route("/status", get(overview))
        .route("/settings", put(settings))
        .nest("/admin/access", phase3_api::router())
        .route("/admin/access/registration", get(registration::access_mode).put(registration::set_mode))
        .route("/workflow/policy", get(workflow_policy_get).put(workflow_policy_set))
        .route("/agents/{id}/model", get(agent_model).put(agent_model_set))
        .route("/agents/{id}/abilities", get(agent_ability_catalog))
        .route("/agents/{id}/context-composition", get(agent_context_composition))
        .route("/agents/{id}/abilities/{ability_id}/visibility", put(agent_ability_visibility_set))
        .route("/agents/{id}/abilities/{ability_id}/skill-visibility",
            put(agent_ability_skill_visibility_set))
        .route("/agents/{id}/abilities/{ability_id}/tools/{tool_name}/mode",
            put(agent_tool_mode_set))
        .route("/agents/{id}/slots", get(prompt_slots_api))
        .route("/agents/{id}/slots/{name}",
            get(prompt_slot_api).put(prompt_slot_put_api).delete(prompt_slot_delete_api))
        .route("/policy", put(policy_set))
        .route("/abilities", get(ability_catalog))
        .route("/provider/login", post(provider_start))
        .route("/provider/logout", post(provider_logout))
        .route("/control", put(control))
        .route_layer(middleware::from_fn_with_state(app.clone(), admin_auth));
    // Model handlers enforce tenant authority themselves; all retain browser auth and CSRF.
    let api = Router::new()
        .nest("/models", model_selection_api::router())
        .route("/auth/me", get(me))
        .route("/auth/ui-config", get(chat_ui_config))
        .route("/auth/profile", put(update_profile))
        .route("/auth/session-policy", get(session_policy).put(update_session_policy))
        .route("/auth/sessions", get(list_auth_sessions))
        .route("/auth/sessions/{id}", axum::routing::delete(revoke_auth_session))
        .route("/auth/change-password", post(change_password))
        .route("/auth/logout", post(logout))
        .merge(user_api::router())
        .merge(admin_api)
        .route_layer(middleware::from_fn_with_state(app.clone(), auth));
    let ui = std::env::var("V3_UI_ROOT").unwrap_or_else(|_| "/opt/v3/ui".into());
    let shared = std::env::var("V3_SHARED_UI_ROOT").unwrap_or_else(|_| "/opt/v3/shared-ui".into());
    Router::new().route("/api/v3/pages/catalog",get(ui_pages::catalog))
        .route("/api/v3/auth/login",post(login))
        .route("/api/v3/auth/register",post(registration::register))
        .route("/api/v3/auth/guest-login",post(registration::guest_login))
        .route("/api/v3/auth/access-mode",get(registration::access_mode))
        .route("/api/v3/diagnostics/browser-login",post(browser_login::login))
        .route("/api/v3/auth/setup-status",get(setup_status))
        .route("/api/v3/auth/setup-admin",post(setup_admin))
        .nest("/api/v3",api)
        .nest("/api/v3/admin/messaging",messaging)
        .route("/health/live",get(||async{Json(json!({"status":"alive"}))}))
        .route("/health/ready",get(|State(app):State<App>|async move{
            let healthy=app.ready.load(Ordering::Acquire)
                && sqlx::query("SELECT 1").execute(&app.store.app_pool).await.is_ok()
                && sqlx::query("SELECT 1").execute(&app.store.pool).await.is_ok()
                && sqlx::query("SELECT 1").execute(&app.store.agent_pool).await.is_ok()
                && app.store.loop_fresh(10).await.unwrap_or(false);
            (if healthy{StatusCode::OK}else{StatusCode::SERVICE_UNAVAILABLE},Json(json!({"ready":healthy})))
        }))
        .route_service("/setup.html",ServeFile::new(format!("{ui}/setup.html")))
        .route_service("/app",ServeFile::new(format!("{ui}/index.html")))
        .route_service("/",ServeFile::new(format!("{ui}/index.html")))
        .nest_service("/v3-ui",ServeDir::new(ui)).nest_service("/ui",ServeDir::new(shared))
        .layer(DefaultBodyLimit::max(2*1024*1024))
        .layer(middleware::from_fn(|request:Request,next:Next|async move{
            let mut response=next.run(request).await;
            response.headers_mut().insert("cache-control",HeaderValue::from_static("no-store"));
            response.headers_mut().insert("x-content-type-options",HeaderValue::from_static("nosniff"));
            response.headers_mut().insert("referrer-policy",HeaderValue::from_static("same-origin"));
            response.headers_mut().insert("content-security-policy",HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"));
            response
        })).with_state(app)
}
