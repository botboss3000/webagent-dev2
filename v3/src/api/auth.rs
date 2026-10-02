//! HTTP handlers for auth operations.
use super::*;
// Machine access is scoped to messaging only, never the browser/admin router.
pub(crate) fn message_api_token_hash(token: &str) -> Result<Option<[u8; 32]>> {
    if token.is_empty() {
        return Ok(None);
    }
    if token.len() < 32 || token.len() > 256 || !token.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(anyhow!("V3_MESSAGE_API_TOKEN must be 32-256 printable non-space ASCII characters"));
    }
    Ok(Some(Sha256::digest(token.as_bytes()).into()))
}
pub(crate) fn message_api_authorized(expected: Option<&[u8; 32]>, headers: &HeaderMap) -> bool {
    let Some(expected) = expected else { return false; };
    let Some(token) = headers.get("authorization").and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer ")) else { return false; };
    if token.len() > 256 { return false; }
    let actual: [u8; 32] = Sha256::digest(token.as_bytes()).into();
    // Compare fixed-length digests without early exit; never store/log the raw token.
    expected.iter().zip(actual.iter()).fold(0u8, |diff, (a, b)| diff | (a ^ b)) == 0
}
pub(crate) async fn message_api_auth(State(app): State<App>, mut request: Request, next: Next) -> Response {
    if !message_api_authorized(app.message_api_token_hash.as_ref(), request.headers()) {
        return ApiError(StatusCode::UNAUTHORIZED, "Messaging API authentication required".into())
            .into_response();
    }
    let authorized = app.store.account_by_id("admin").await.ok().flatten()
        .and_then(|account| access::Principal::from_account(&account).ok()
            .map(|principal| principal.active() && principal.administrator()
                && account["is_approved"] == true))
        .unwrap_or(false);
    if !authorized { return ApiError(StatusCode::UNAUTHORIZED, "Messaging API unavailable".into()).into_response(); }
    let mut caller = app.clone();
    caller.store = match app.store.for_user("admin").await {
        Ok(store) => store,
        Err(_) => return ApiError(StatusCode::UNAUTHORIZED, "Messaging API unavailable".into()).into_response(),
    };
    request.extensions_mut().insert(caller);
    let mut response = next.run(request).await;
    response.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
    response
}
pub(crate) async fn auth(State(app): State<App>, mut request: Request, next: Next) -> Response {
    let cookie = request
        .headers()
        .get("cookie")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let token = cookie
        .split(';')
        .find_map(|s| s.trim().strip_prefix("v3_session="))
        .unwrap_or("");
    if token.len() != 36 {
        return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response();
    }
    let auth_key = format!("auth:{}", digest(&json!(token)));
    let mut doc = match app.store.get(&auth_key).await {
        Ok(Some(v)) if v["expires"].as_u64().unwrap_or(0) > now() => v,
        _ => return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response(),
    };
    let user_id = doc["user_id"].as_str().unwrap_or("admin").to_owned();
    let account = match app.store.account_by_id(&user_id).await {
        Ok(Some(value)) => value,
        _ => return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response(),
    };
    let principal = match access::Principal::from_account(&account) {
        Ok(value) if value.active() && account["is_approved"] == true => value,
        _ => return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response(),
    };
    if principal.role == access::Role::Guest && registration::mode(&app).await.unwrap_or_default() != "public_registered" {
        return ApiError(StatusCode::UNAUTHORIZED, "Guest access is not enabled".into()).into_response();
    }
    let path=request.uri().path();
    let resource=if path.starts_with("/api/v3/sessions") || path.starts_with("/sessions") || path.starts_with("/api/v3/runs") || path.starts_with("/runs") || path.ends_with("/user/status") {Some("chat")}
        else if path.starts_with("/api/v3/agents/") || path.starts_with("/agents/") {Some("agents")} else {None};
    if let Some(page)=resource {
        if !app.store.read_access_policy().ok().is_some_and(|policy|registration::page_allowed(&policy,"main",page,&principal)) {
            return ApiError(StatusCode::FORBIDDEN,"Page access denied".into()).into_response();
        }
    }
    if doc["generation"] != account["generation"] {
        return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response();
    }
    if !matches!(request.method().as_str(), "GET" | "HEAD" | "OPTIONS") {
        let csrf = request
            .headers()
            .get("x-csrf-token")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if csrf != doc["csrf"].as_str().unwrap_or("") {
            return ApiError(StatusCode::FORBIDDEN, "Invalid request token".into()).into_response();
        }
        if let Some(origin) = request
            .headers()
            .get("origin")
            .and_then(|v| v.to_str().ok())
        {
            let host = request
                .headers()
                .get("host")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            if origin != format!("https://{host}")
                && (app.secure || origin != format!("http://{host}"))
            {
                return ApiError(StatusCode::FORBIDDEN, "Invalid request origin".into())
                    .into_response();
            }
        }
    }
    doc["_auth_key"] = json!(auth_key);
    doc["_user_id"] = json!(user_id);
    doc["_role"] = json!(principal.role);
    doc["_tier_id"] = json!(principal.tier_id);
    doc["_access_revision"] = json!(principal.access_revision);
    let mut caller = app.clone();
    caller.store = match app.store.for_user(&user_id).await {
        Ok(store) => store,
        Err(_) => return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response(),
    };
    request.extensions_mut().insert(caller);
    request.extensions_mut().insert(doc);
    next.run(request).await
}
pub(crate) async fn admin_auth(State(app): State<App>, request: Request, next: Next) -> Response {
    let Some(identity) = request.extensions().get::<Value>() else {
        return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response();
    };
    if let Err(error) = phase3_api::administrator(&app, identity).await {
        return error.into_response();
    }
    next.run(request).await
}
pub(crate) async fn login(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> std::result::Result<Response, ApiError> {
    if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
        let host = headers
            .get("host")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if origin != format!("https://{host}") && (app.secure || origin != format!("http://{host}"))
        {
            return Err(bad("Invalid request origin"));
        }
    }
    let username = required(&body, "username", 128)?;
    let password = required(&body, "password", 1024)?;
    {
        let mut attempts = app.login_attempts.lock().await;
        attempts.retain(|t| now() - t < 60);
        if attempts.len() >= 5 {
            return Err(ApiError(
                StatusCode::TOO_MANY_REQUESTS,
                "Try signing in again in one minute".into(),
            ));
        }
        attempts.push(now());
    }
    let account_exists = app.store.has_admin_account().await?;
    let credential = if account_exists {
        app.store
            .account_by_username(username)
            .await?
            .ok_or_else(|| ApiError(StatusCode::UNAUTHORIZED, "Invalid login".into()))?
    } else {
        app.store
            .get("admin")
            .await?
            .ok_or_else(|| bad("Admin unavailable"))?
    };
    if credential["role"] == "guest" {return Err(ApiError(StatusCode::UNAUTHORIZED,"Invalid login".into()));}
    let hash = credential["password_hash"].as_str().unwrap_or("").to_owned();
    let password = password.to_owned();
    let valid = tokio::task::spawn_blocking(move || {
        PasswordHash::new(&hash).ok().is_some_and(|h| {
            Argon2::default()
                .verify_password(password.as_bytes(), &h)
                .is_ok()
        })
    })
    .await
    .map_err(|_| bad("Sign in unavailable"))?;
    if username != credential["username"].as_str().unwrap_or("admin")
        || credential["is_approved"] == false
        || (account_exists && !access::Principal::from_account(&credential)
            .map(|principal| principal.active()).unwrap_or(false))
        || !valid
    {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "Invalid login".into()));
    }
    let user_id = credential["user_id"].as_str().unwrap_or("admin");
    if account_exists {
        app.store.record_user_login(user_id, now()).await?;
    }
    let token = uuid();
    let csrf = uuid();
    let created = now();
    let duration = app.store.session_days(user_id).await? * 86400;
    let user_agent = headers
        .get("user-agent")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("Unknown browser")
        .chars()
        .take(256)
        .collect::<String>();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"id":uuid(),"user":username,"user_id":user_id,"csrf":csrf,
                "generation":credential["generation"],"created_at":created,
                "user_agent":user_agent,"expires":created+duration}),
        )
        .await?;
    let mut response = Json(json!({"csrf_token":csrf,"user":username})).into_response();
    response.headers_mut().insert(
        "set-cookie",
        HeaderValue::from_str(&format!(
            "v3_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={duration}{}",
            if app.secure { "; Secure" } else { "" }
        ))
        .unwrap(),
    );
    Ok(response)
}
pub(crate) async fn setup_status(State(app): State<App>) -> Api {
    Ok(Json(
        json!({"initialized":app.store.has_admin_account().await?
        || app.store.get("admin").await?.is_some()}),
    ))
}
pub(crate) async fn setup_admin(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> std::result::Result<Response, ApiError> {
    if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
        let host = headers
            .get("host")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if origin != format!("https://{host}") && (app.secure || origin != format!("http://{host}"))
        {
            return Err(bad("Invalid request origin"));
        }
    }
    let username = body["username"].as_str().unwrap_or("admin").trim();
    let username = if username.is_empty() {
        "admin"
    } else {
        username
    };
    if username.len() > 128 {
        return Err(bad("Invalid username"));
    }
    let password = required(&body, "password", 1024)?;
    if password.len() < 6 {
        return Err(bad("Password must be at least 6 characters"));
    }
    let _guard = app.gate.lock().await;
    if app.store.has_admin_account().await? || app.store.get("admin").await?.is_some() {
        return Err(conflict("Admin already exists"));
    }
    let salt = SaltString::generate(&mut OsRng);
    let hash = Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map_err(|_| bad("Password hashing failed"))?
        .to_string();
    app.store
        .create_admin_account(username, &hash, &uuid())
        .await?;
    login(
        State(app.clone()),
        headers,
        Json(json!({"username":username,"password":password})),
    )
    .await
}
pub(crate) async fn me(State(app): State<App>, axum::Extension(doc): axum::Extension<Value>) -> Api {
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    let profile = app.store.user_profile(user_id).await?;
    let account = app.store.account_by_id(user_id).await?
        .ok_or_else(||ApiError(StatusCode::UNAUTHORIZED,"Sign in required".into()))?;
    let principal = access::Principal::from_account(&account).map_err(|_|bad("Account unavailable"))?;
    Ok(Json(
        json!({"user":account["username"],"user_id":user_id,
        "display_name":profile.as_ref().map(|p| &p["display_name"]),
        "is_admin":principal.administrator(),"role":account["role"],"status":account["status"],
        "tier_id":account["tier_id"],
        "csrf_token":doc["csrf"]}),
    ))
}
pub(crate) async fn update_profile(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
    Json(body): Json<Value>,
) -> Api {
    if doc["_role"] == "guest" {return Err(ApiError(StatusCode::FORBIDDEN,"Register an account to edit a profile".into()));}
    let display_name = required(&body, "display_name", 128)?.trim();
    let username = required(&body, "username", 128)?.trim();
    if display_name.is_empty() || display_name.chars().any(char::is_control) {
        return Err(bad("Invalid display name"));
    }
    if username.is_empty() || username.chars().any(char::is_control) {
        return Err(bad("Invalid username"));
    }
    let user_id=doc["_user_id"].as_str().ok_or_else(||bad("Account unavailable"))?;
    let profile = app
        .store
        .update_user_profile(user_id, username, display_name)
        .await?;
    Ok(Json(profile))
}
pub(crate) async fn session_policy(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
) -> Api {
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    Ok(Json(json!({"days":app.store.session_days(user_id).await?})))
}
pub(crate) async fn update_session_policy(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
    Json(body): Json<Value>,
) -> Api {
    if doc["_role"] == "guest" {return Err(ApiError(StatusCode::FORBIDDEN,"Guest sign-in duration is fixed".into()));}
    let days = body["days"]
        .as_u64()
        .ok_or_else(|| bad("Invalid sign-in duration"))?;
    if ![1, 7, 30, 90].contains(&days) {
        return Err(bad("Invalid sign-in duration"));
    }
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    app.store.set_session_days(user_id, days).await?;
    Ok(Json(json!({"days":days})))
}
pub(crate) async fn list_auth_sessions(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
) -> Api {
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    let account = app
        .store
        .account_by_id(user_id)
        .await?
        .ok_or_else(|| ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()))?;
    let generation = account["generation"].as_str().unwrap_or("");
    let current_key = doc["_auth_key"].as_str().unwrap_or("");
    let sessions = app
        .store
        .auth_sessions(user_id, generation, now())
        .await?
        .into_iter()
        .map(|s| {
            json!({"id":s["id"],"user_agent":s["user_agent"],
            "created_at":s["created_at"],"expires_at":s["expires_at"],
            "current":s["key"] == current_key})
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({"sessions":sessions})))
}
pub(crate) async fn revoke_auth_session(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
    Path(id): Path<String>,
) -> Api {
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    let account = app
        .store
        .account_by_id(user_id)
        .await?
        .ok_or_else(|| ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()))?;
    let generation = account["generation"].as_str().unwrap_or("");
    let session = app
        .store
        .auth_sessions(user_id, generation, now())
        .await?
        .into_iter()
        .find(|s| s["id"] == id)
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Sign-in session not found".into()))?;
    let current = session["key"] == doc["_auth_key"];
    app.store.delete(session["key"].as_str().unwrap()).await?;
    Ok(Json(json!({"ok":true,"current":current})))
}
pub(crate) async fn change_password(
    State(app): State<App>,
    axum::Extension(doc): axum::Extension<Value>,
    Json(body): Json<Value>,
) -> Api {
    let current = required(&body, "current_password", 1024)?.to_owned();
    let next = required(&body, "new_password", 1024)?;
    if next.len() < 6 {
        return Err(bad("New password must be at least 6 characters"));
    }
    let user_id = doc["_user_id"].as_str().unwrap_or("admin");
    let account = app
        .store
        .account_by_id(user_id)
        .await?
        .ok_or_else(|| ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()))?;
    let hash = account["password_hash"].as_str().unwrap_or("").to_owned();
    let valid = tokio::task::spawn_blocking(move || {
        PasswordHash::new(&hash).ok().is_some_and(|parsed| {
            Argon2::default()
                .verify_password(current.as_bytes(), &parsed)
                .is_ok()
        })
    })
    .await
    .map_err(|_| bad("Password verification unavailable"))?;
    if !valid {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "Current password is incorrect".into(),
        ));
    }
    let generation = account["generation"]
        .as_str()
        .ok_or_else(|| bad("Account unavailable"))?;
    let salt = SaltString::generate(&mut OsRng);
    let next_hash = Argon2::default()
        .hash_password(next.as_bytes(), &salt)
        .map_err(|_| bad("Password hashing failed"))?
        .to_string();
    let _guard = app.gate.lock().await;
    app.store
        .replace_user_password(user_id, generation, &next_hash, &uuid())
        .await
        .map_err(|_| conflict("Account changed; sign in again"))?;
    Ok(Json(json!({"ok":true,"sign_in_required":true})))
}
pub(crate) async fn logout(
    State(app): State<App>,
    headers: HeaderMap,
) -> std::result::Result<Response, ApiError> {
    let token = headers
        .get("cookie")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .find_map(|v| v.trim().strip_prefix("v3_session="))
        .unwrap_or("");
    app.store
        .delete(&format!("auth:{}", digest(&json!(token))))
        .await?;
    let mut r = Json(json!({"ok":true})).into_response();
    r.headers_mut().insert(
        "set-cookie",
        HeaderValue::from_static("v3_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"),
    );
    Ok(r)
}
pub(crate) async fn chat_ui_config(State(app): State<App>) -> Api {
    let bytes = tokio::fs::read(&app.chat_ui_config_path)
        .await
        .map_err(|_| {
            ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "Chat UI configuration unavailable".into(),
            )
        })?;
    let chat_ui: Value = serde_json::from_slice(&bytes).map_err(|_| {
        ApiError(StatusCode::INTERNAL_SERVER_ERROR, "Chat UI configuration invalid".into())
    })?;
    if !chat_ui["chat_common"].is_object() {
        return Err(ApiError(StatusCode::INTERNAL_SERVER_ERROR, "Chat UI configuration invalid".into()));
    }
    Ok(Json(json!({"chat_ui": chat_ui})))
}
