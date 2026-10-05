//! HTTP admission, administration, and routing. Rust checks authority before a run is queued.
use super::*;
mod auth;
mod admin;
mod sessions;
mod promotions;
mod chat;
mod chat_controls;
pub(crate) mod media_lane;
pub(crate) mod uploads;
mod voice;
#[cfg(test)]mod composer_tests;
mod ability_admin;
mod service_admin;
mod administrator;
mod model_catalog;
mod account_models;
pub(crate) mod repository;
pub(crate) use auth::*;
pub(crate) use admin::*;
pub(crate) use sessions::*;
pub(crate) use promotions::*;
pub(crate) use chat::*;
pub(crate) use chat_controls::*;
pub(crate) use voice::*;
pub(crate) use uploads::{upload,upload_delete,upload_content,upload_describe};
pub(super) fn router(app: App) -> Router {
    let messaging = user_api::messaging_router()
        .route_layer(middleware::from_fn_with_state(app.clone(), message_api_auth));
    let admin_api = Router::new()
        .merge(diagnostics::router())
        .route("/status", get(overview))
        .route("/settings", put(settings))
        .nest("/admin/access", phase3_api::router())
        .nest("/admin/services", service_admin::router())
        .nest("/admin/models/rosters", model_catalog::router())
        .route("/admin/access/registration", get(registration::access_mode).put(registration::set_mode))
        .route("/workflow/policy", get(workflow_policy_get).put(workflow_policy_set))
        .route("/agents/{id}/model", get(agent_model).put(agent_model_set))
        .route("/policy", put(policy_set))
        .route("/abilities", get(ability_catalog))
        .route("/provider/login", post(provider_start))
        .route("/provider/logout", post(provider_logout))
        .route("/control", put(control))
        .route_layer(middleware::from_fn_with_state(app.clone(), admin_auth));
    // Model handlers enforce tenant authority themselves; all retain browser auth and CSRF.
    let api = Router::new()
        .route("/sessions/{sid}/administrator/artifacts/{id}",get(administrator::artifact))
        .route("/sessions/{sid}/administrator/notices",get(administrator::notices))
        .nest("/models", model_selection_api::router())
        .route("/models/me/catalog",get(account_models::read).put(account_models::save))
        .route("/models/me/catalog/default",put(account_models::set_default))
        .route("/models/agents/{id}/adoptions",post(account_models::adopt))
        .route("/agents/{id}/context-composition",get(ability_admin::composition))
        .route("/agents/{id}/slots",get(ability_admin::slots))
        .route("/agents/{id}/slots/{name}",get(ability_admin::slot).put(ability_admin::slot_put).delete(ability_admin::slot_delete))
        .route("/agents/{id}/repository-connections",get(repository::read))
        .route("/agents/{id}/repository-connections/{connection}",put(repository::put))
        .route("/agents/{id}/abilities",get(ability_admin::read))
        .route("/agents/{id}/abilities/{ability}/settings",put(ability_admin::settings_set))
        .route("/agents/{id}/abilities/effective",get(ability_admin::preview))
        .route("/agents/{id}/ability-members",get(ability_admin::members))
        .route("/agents/{id}/ability-permissions",put(ability_admin::permissions_set))
        .route("/agents/{id}/capability-levels/{level}",put(ability_admin::level_set))
        .route("/agents/{id}/members/{user}/capability-level",put(ability_admin::member_set))
        .route("/agents/{id}/abilities/{ability}/visibility",put(ability_admin::visibility_set))
        .route("/agents/{id}/abilities/{ability}/skill-visibility",put(ability_admin::skill_set))
        .route("/agents/{id}/abilities/{ability}/skill-content",get(ability_admin::skill_content))
        .route("/agents/{id}/abilities/{ability}/skills/{skill}/content",get(ability_admin::skill_named_content))
        .route("/agents/{id}/abilities/{ability}/skills/{skill}/visibility",put(ability_admin::skill_named_set))
        .route("/agents/{id}/abilities/{ability}/tools/{tool}/skill-content",get(ability_admin::tool_skill_content))
        .route("/agents/{id}/abilities/{ability}/tools/{tool}/mode",put(ability_admin::tool_set))
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
        .layer(middleware::from_fn(diagnostics::http_errors))
        .layer(middleware::from_fn(|request:Request,next:Next|async move{
            let mut response=next.run(request).await;
            response.headers_mut().insert("cache-control",HeaderValue::from_static("no-store"));
            response.headers_mut().insert("x-content-type-options",HeaderValue::from_static("nosniff"));
            response.headers_mut().insert("referrer-policy",HeaderValue::from_static("same-origin"));
            response.headers_mut().insert("content-security-policy",HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"));
            response
        })).with_state(app)
}
