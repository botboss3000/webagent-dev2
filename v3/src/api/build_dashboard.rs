//! Read-only administrator projection of the protected build broker.
use super::*;

pub(crate) async fn list(State(app): State<App>) -> Api {
    const DASHBOARD_SESSION: &str = "00000000-0000-0000-0000-000000000000";
    let user_id = app.store.current_user_id();
    let trusted = json!({
        "user_id": user_id,
        "session_id": DASHBOARD_SESSION,
        "lease_until": now() + 30,
        "access_fingerprint": digest(&json!(["v3_build_dashboard", user_id])),
    });
    let response = tools::execute_trusted(
        "v3_build_dashboard",
        &json!({}),
        DASHBOARD_SESSION,
        std::path::Path::new("/run/v3/build/build.sock"),
        Some(trusted),
        CancellationToken::new(),
    ).await?;
    if response["status"] != "success" || !response["output"]["jobs"].is_array() {
        return Err(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "Build status is unavailable".into(),
        ));
    }
    Ok(Json(response["output"].clone()))
}
