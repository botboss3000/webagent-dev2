//! Route each person's chat request to that person's storage. This is the
//! boundary that keeps one user's conversations out of another user's view.
use crate::{App, InspectArgs, SessionAbilityBody, SessionModeBody};
use axum::{extract::{Path, Query, State}, http::HeaderMap, response::{IntoResponse, Response},
    routing::{get, post}, Extension, Json, Router};
use serde_json::Value;
use std::collections::HashMap;

async fn session_app(mut app: App, session_id: &str) -> Result<App, crate::ApiError> {
    let session = app.store.get(&crate::key("session", session_id)).await?
        .ok_or_else(|| crate::ApiError(axum::http::StatusCode::NOT_FOUND,
            "Session not found".into()))?;
    let agent_id = session["agent_id"].as_str().unwrap_or("shared_default");
    app.store = app.store.for_agent(agent_id).await
        .map_err(|_| crate::ApiError(axum::http::StatusCode::NOT_FOUND,
            "Agent not found".into()))?;
    let user_id = app.store.current_user_id().to_owned();
    let access = app.store.live_access(&user_id, agent_id).await
        .map_err(|_| crate::ApiError(axum::http::StatusCode::NOT_FOUND,
            "Agent not found".into()))?;
    if !access.admitted() {
        return Err(crate::ApiError(axum::http::StatusCode::FORBIDDEN,
            "Agent access unavailable".into()));
    }
    Ok(app)
}
async fn run_app(app: App, run_id: &str) -> Result<App, crate::ApiError> {
    let run = app.run(run_id).await
        .map_err(|_| crate::ApiError(axum::http::StatusCode::NOT_FOUND,
            "Run not found".into()))?;
    let session_id = run["session_id"].as_str()
        .ok_or_else(|| crate::ApiError(axum::http::StatusCode::NOT_FOUND,
            "Run not found".into()))?;
    session_app(app, session_id).await
}
trait SessionPath { fn session_id(&self) -> &str; }
impl SessionPath for String { fn session_id(&self) -> &str { self } }
impl SessionPath for (String, String) { fn session_id(&self) -> &str { &self.0 } }
macro_rules! json_arg {
    ($name:ident => $inner:ident) => {
        async fn $name(Extension(app): Extension<App>, Json(body): Json<Value>) -> Response {
            crate::$inner(State(app), Json(body)).await.into_response()
        }
    };
}
macro_rules! path_arg {
    ($name:ident => $inner:ident, $path:ty) => {
        async fn $name(Extension(app): Extension<App>, Path(path): Path<$path>) -> Response {
            let app = match session_app(app, path.session_id()).await {
                Ok(app) => app, Err(error) => return error.into_response(),
            };
            crate::$inner(State(app), Path(path)).await.into_response()
        }
    };
}
macro_rules! path_json {
    ($name:ident => $inner:ident, $path:ty, $body:ty) => {
        async fn $name(Extension(app): Extension<App>, Path(path): Path<$path>,
            Json(body): Json<$body>) -> Response {
            let app = match session_app(app, path.session_id()).await {
                Ok(app) => app, Err(error) => return error.into_response(),
            };
            crate::$inner(State(app), Path(path), Json(body)).await.into_response()
        }
    };
}
async fn sessions(Extension(app): Extension<App>, Query(args): Query<crate::SessionListArgs>) -> Response {
    crate::sessions(State(app),Query(args)).await.into_response()
}
json_arg!(create_session => create_session);
path_arg!(snapshot => snapshot, String);
path_json!(session_read => session_read_api, String, crate::SessionReadBody);
path_json!(session_recycle => recycle_session_api, String, crate::store::session::SessionLifecycle);
path_json!(session_restore => restore_session_api, String, crate::store::session::SessionLifecycle);
path_json!(session_patch => patch_session_api, String, crate::store::session::SessionPatch);
path_arg!(diagnostics => run_diagnostics_api, (String, String));
path_arg!(progress => run_progress_api, (String, String));
path_arg!(mode_get => session_mode_get, String);
path_json!(mode_set => session_mode_set, String, SessionModeBody);
path_arg!(abilities => session_abilities, String);
path_json!(ability_set => session_ability_set, (String, String), SessionAbilityBody);
path_json!(send => send, String, Value);
path_arg!(attachment => attachment_get, (String, String));
path_arg!(memory_list => memory_list, String);
path_json!(memory_save => memory_save, String, Value);
path_arg!(memory_delete => memory_delete, (String, String));
path_arg!(source_review => source_review, String);
path_arg!(promotions => promotions, String);
path_arg!(promotion_review => promotion_review, (String, String));
path_arg!(promotion_receipt => promotion_receipt, (String, String));
path_arg!(promotion_reject => promotion_reject, (String, String));

async fn state_inspect(Extension(app): Extension<App>, Path(id): Path<String>,
    Query(args): Query<InspectArgs>) -> Response {
    let app = match session_app(app, &id).await {
        Ok(app) => app, Err(error) => return error.into_response(),
    };
    crate::state_inspect_api(State(app), Path(id), Query(args)).await.into_response()
}
async fn events(Extension(app): Extension<App>, Path(id): Path<String>,
    headers: HeaderMap, Extension(auth_doc): Extension<Value>,
    Query(params): Query<HashMap<String, String>>) -> Response {
    let app = match session_app(app, &id).await {
        Ok(app) => app, Err(error) => return error.into_response(),
    };
    crate::events(State(app), Path(id), headers, Extension(auth_doc), Query(params))
        .await.into_response()
}
async fn promote(Extension(app): Extension<App>, Extension(identity): Extension<Value>,
    Path(id): Path<String>, Json(body): Json<Value>) -> Response {
    if let Err(error) = crate::phase3_api::administrator(&app, &identity).await {
        return error.into_response();
    }
    let app = match session_app(app, &id).await {
        Ok(app) => app, Err(error) => return error.into_response(),
    };
    crate::promote(State(app), Path(id), Json(body)).await.into_response()
}
async fn command(Extension(app): Extension<App>, Path((id, action)): Path<(String, String)>,
    Json(body): Json<Value>) -> Response {
    let app = match run_app(app, &id).await {
        Ok(app) => app, Err(error) => return error.into_response(),
    };
    crate::command(State(app), Path((id, action)), Json(body)).await.into_response()
}
async fn approval(Extension(app): Extension<App>, Path(id): Path<String>,
    Json(body): Json<Value>) -> Response {
    let run_id=match app.store.active_runs().await {
        Ok(runs)=>runs.into_iter().find(|run|run["pending"]["id"]==id)
            .and_then(|run|run["id"].as_str().map(str::to_owned)),
        Err(_)=>None,
    };
    let Some(run_id)=run_id else {return crate::ApiError(axum::http::StatusCode::NOT_FOUND,"Approval not found".into()).into_response();};
    let app = match run_app(app, &run_id).await {
        Ok(app) => app, Err(error) => return error.into_response(),
    };
    crate::approval(State(app), Path(id), Json(body)).await.into_response()
}

// Only caller-owned run summaries; never provider credentials or admin policy.
async fn user_status(Extension(app): Extension<App>) -> crate::Api {
    let mut runs=app.store.active_runs().await?;
    runs.extend(app.store.recent_runs(None).await?);
    Ok(Json(serde_json::json!({"shutoff":app.shutoff().await?,
        "runs":runs.iter().map(crate::public_run).collect::<Vec<_>>()})))
}

pub(crate) fn router() -> Router<App> {
    Router::new()
        .route("/user/status", get(user_status))
        .route("/sessions", get(sessions).post(create_session))
        .route("/sessions/{sid}/runs/{rid}/diagnostics", get(diagnostics))
        .route("/sessions/{sid}/runs/{rid}/progress", get(progress))
        .route("/sessions/{id}", get(snapshot).patch(session_patch).delete(session_recycle))
        .route("/sessions/{id}/restore", post(session_restore))
        .route("/sessions/{id}/read", axum::routing::put(session_read))
        .route("/sessions/{id}/mode", get(mode_get).put(mode_set))
        .route("/sessions/{id}/abilities", get(abilities))
        .route("/sessions/{id}/abilities/{ability_id}", axum::routing::put(ability_set))
        .route("/sessions/{id}/state-inspect", get(state_inspect))
        .route("/sessions/{id}/source-review", get(source_review))
        .route("/sessions/{id}/promotions", get(promotions).post(promote))
        .route("/sessions/{id}/promotions/{request_id}/review", get(promotion_review))
        .route("/sessions/{id}/promotions/{request_id}", get(promotion_receipt))
        .route("/sessions/{id}/promotions/{request_id}/reject", post(promotion_reject))
        .route("/sessions/{id}/messages", post(send))
        .route("/sessions/{id}/attachments/{aid}", get(attachment))
        .route("/sessions/{id}/memories", get(memory_list).post(memory_save))
        .route("/sessions/{id}/memories/{mid}", axum::routing::delete(memory_delete))
        .route("/sessions/{id}/events", get(events))
        .route("/runs/{id}/{action}", post(command))
        .route("/approvals/{id}/decision", post(approval))
}
pub(crate) fn messaging_router() -> Router<App> {
    Router::new()
        .route("/sessions", post(create_session))
        .route("/sessions/{id}", get(snapshot))
        .route("/sessions/{id}/messages", post(send))
}
