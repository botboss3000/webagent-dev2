//! Opt-in trusted diagnostics bridge. Never accepts a target identity or origin.
use super::*;

#[derive(Clone)]
pub(crate) struct Bridge {
    token_hash: [u8; 32],
    caller_id: String,
}
impl Bridge {
    pub(crate) fn configure(token: &str, caller_id: &str) -> Result<Option<Self>> {
        if token.is_empty() && caller_id.is_empty() {
            return Ok(None);
        }
        if caller_id.is_empty() || caller_id.len() > 128 || caller_id.chars().any(char::is_control) {
            return Err(anyhow!("V3_BROWSER_LOGIN_CALLER_ID must identify the trusted diagnostics administrator"));
        }
        let token_hash = message_api_token_hash(token)?
            .ok_or_else(|| anyhow!("V3_BROWSER_LOGIN_TOKEN is required with V3_BROWSER_LOGIN_CALLER_ID"))?;
        Ok(Some(Self { token_hash, caller_id: caller_id.to_owned() }))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LoginRequest {
    caller_id: String,
}

pub(crate) async fn login(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<LoginRequest>,
) -> std::result::Result<Response, ApiError> {
    let denied = || ApiError(StatusCode::UNAUTHORIZED, "Diagnostics browser login unavailable or unauthorized".into());
    let bridge = app.browser_login_bridge.as_ref().ok_or_else(denied)?;
    // This route is for trusted server-to-server use, never browser JS/CORS.
    if headers.contains_key("origin")
        || !message_api_authorized(Some(&bridge.token_hash), &headers)
        || body.caller_id != bridge.caller_id
    {
        return Err(denied());
    }
    let _guard = app.gate.lock().await;
    let account = app.store.account_by_id("admin").await?.ok_or_else(denied)?;
    if account["is_approved"] == false || account["generation"].as_str().is_none() {
        return Err(denied());
    }
    let username = account["username"].as_str().ok_or_else(denied)?;
    let token = uuid();
    let created = now();
    let duration = 600;
    app.store.put(
        &format!("auth:{}", digest(&json!(token))),
        &json!({"id":uuid(),"user":username,"user_id":"admin","csrf":uuid(),
            "generation":account["generation"],"created_at":created,
            "user_agent":"Diagnostics v3 development browser","expires":created+duration}),
    ).await?;
    let mut response = Json(json!({"user_id":"admin","user":username,"expires_in":duration})).into_response();
    response.headers_mut().insert("set-cookie", HeaderValue::from_str(&format!(
        "v3_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={duration}{}",
        if app.secure { "; Secure" } else { "" }
    )).unwrap());
    response.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
    Ok(response)
}
