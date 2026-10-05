//! Startup-only installation service administration. Browser middleware owns CSRF.
//! Agent administrators cannot manage these records and updates do not stop current work.
use crate::{App,Api,ApiError,services};
use axum::{extract::State,Extension,Json,Router,routing::get,http::StatusCode};
use serde::Deserialize;
use serde_json::{json,Value};

pub(crate) async fn read(State(app):State<App>,Extension(identity):Extension<Value>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let config=services::boot_orphan_resume_config(&app.store).await?;
    Ok(Json(json!({"service_id":services::BOOT_ORPHAN_RESUME,"enabled":config.enabled,
        "revision":config.revision,"readiness":"pending","applies":"next_loop_startup"})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Change {expected_revision:u64,enabled:bool}
pub(crate) async fn write(State(app):State<App>,Extension(identity):Extension<Value>,Json(body):Json<Change>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let actor=identity["_user_id"].as_str().ok_or_else(||ApiError(StatusCode::UNAUTHORIZED,"Sign in required".into()))?;
    let _guard=app.gate.lock().await;
    let config=services::save_boot_orphan_resume(&app.store,actor,body.enabled,body.expected_revision).await
        .map_err(|error| match error.to_string().as_str() {
            "Service revision changed" => ApiError(StatusCode::CONFLICT,"Service settings changed; reload before saving".into()),
            "Installation administrator required" => ApiError(StatusCode::FORBIDDEN,"Installation administrator required".into()),
            "Service revision overflow" => ApiError(StatusCode::BAD_REQUEST,"Invalid service revision".into()),
            _ => error.into(),
        })?;
    // This only chooses safe auto-resume behavior at a future loop startup.
    // It never quiesces active runs or disables mandatory recovery classification.
    Ok(Json(json!({"service_id":services::BOOT_ORPHAN_RESUME,"enabled":config.enabled,
        "revision":config.revision,"readiness":"pending","applies":"next_loop_startup"})))
}
/// Nest under the existing authenticated administrator router at /admin/services.
/// That browser-authenticated surface enforces CSRF on PUT before these handlers.
pub(crate) fn router()->Router<App> {
    Router::new().route("/boot_orphan_resume",get(read).put(write))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn admin_handlers_reject_members_and_stale_writes() {
        let (_root,app)=crate::runtime_tests::fixture_uninitialized().await;
        app.store.create_admin_account("admin","fixture-password-hash","fixture-generation").await.unwrap();
        let admin=json!({"_user_id":"admin"});
        let user=app.store.create_user_account("service-api-member","long-fixture-password-hash").await.unwrap();
        let member=json!({"_user_id":user["user_id"]});
        assert!(read(State(app.clone()),Extension(member.clone())).await.is_err());
        assert!(write(State(app.clone()),Extension(member),Json(Change {expected_revision:0,enabled:false})).await.is_err());
        let Json(initial)=read(State(app.clone()),Extension(admin.clone())).await.unwrap();
        assert_eq!(initial["enabled"],true);assert_eq!(initial["revision"],0);
        assert_eq!(initial["readiness"],"pending");
        let Json(saved)=write(State(app.clone()),Extension(admin.clone()),Json(Change {expected_revision:0,enabled:false})).await.unwrap();
        assert_eq!(saved["enabled"],false);assert_eq!(saved["revision"],1);
        assert_eq!(saved["readiness"],"pending");
        let error=write(State(app.clone()),Extension(admin),Json(Change {expected_revision:0,enabled:true})).await.unwrap_err();
        assert_eq!(error.0,StatusCode::CONFLICT);
        assert!(!services::startup_resume_enabled(&app.store).await.unwrap());
    }
    #[test]
    fn service_command_accepts_no_tenant_or_user_override() {
        for body in [json!({"expected_revision":0,"enabled":true,"agent_id":"foreign"}),
            json!({"expected_revision":0,"enabled":true,"user_id":"foreign"}),
            json!({"expected_revision":0,"enabled":true,"actor_id":"admin"})] {
            assert!(serde_json::from_value::<Change>(body).is_err());
        }
    }
}
