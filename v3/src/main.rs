mod cache;
mod provider;
#[cfg(test)]
mod runtime_tests;
mod store;
mod tools;
use anyhow::{anyhow, Result};
use argon2::{
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Argon2,
};
use axum::{
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{
        sse::{Event, KeepAlive},
        IntoResponse, Response, Sse,
    },
    routing::{get, post, put},
    Json, Router,
};
use fs2::FileExt;
use rand::rngs::OsRng;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    convert::Infallible,
    fs::{File, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use store::Store;
use tokio::sync::{Mutex, RwLock, Semaphore};
use tokio_util::sync::CancellationToken;
use tower_http::services::{ServeDir, ServeFile};
use uuid::Uuid;

const DEFAULT_AGENT_MODEL: &str = "gpt-6-luna";

#[derive(Clone)]
struct App {
    store: Store,
    gate: Arc<Mutex<()>>,
    provider: provider::Provider,
    refresh: Arc<Mutex<()>>,
    login: Arc<RwLock<Option<Value>>>,
    active: Arc<Mutex<HashMap<String, CancellationToken>>>,
    slots: Arc<Semaphore>,
    ready: Arc<AtomicBool>,
    stop: CancellationToken,
    login_attempts: Arc<Mutex<Vec<u64>>>,
    control_dir: PathBuf,
    secure: bool,
}
#[derive(Debug)]
struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
impl From<anyhow::Error> for ApiError {
    fn from(_e: anyhow::Error) -> Self {
        tracing::error!("Request storage or execution operation failed");
        Self(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Operation failed; check development logs".into(),
        )
    }
}
type Api = std::result::Result<Json<Value>, ApiError>;
fn bad(s: &str) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, s.into())
}
fn conflict(s: &str) -> ApiError {
    ApiError(StatusCode::CONFLICT, s.into())
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}
fn uuid() -> String {
    Uuid::new_v4().to_string()
}
fn digest(value: &Value) -> String {
    hex::encode(Sha256::digest(serde_json::to_vec(value).unwrap()))
}
fn key(prefix: &str, id: &str) -> String {
    format!("{prefix}:{id}")
}
fn terminal(status: &str) -> bool {
    matches!(status, "completed" | "cancelled" | "failed")
}
fn status(run: &Value) -> &str {
    run["status"].as_str().unwrap_or("failed")
}
fn public_run(run: &Value) -> Value {
    json!({"id":run["id"],"session_id":run["session_id"],"status":run["status"],"error":run["error"],
        "pending":run["pending"],"uncertain":run["uncertain"],"turns":run["turns"]})
}
fn required<'a>(body: &'a Value, name: &str, max: usize) -> std::result::Result<&'a str, ApiError> {
    let s = body[name]
        .as_str()
        .ok_or_else(|| bad(&format!("{name} is required")))?;
    if s.is_empty() || s.len() > max {
        return Err(bad(&format!("{name} has invalid length")));
    }
    Ok(s)
}
fn policy_default() -> Value {
    json!({"version":1,"rules":{"file_list":"allow","file_read":"allow","file_search":"allow","file_write":"require_approval","shell":"require_approval","python":"require_approval"}})
}
fn policy_decision<'a>(policy: &'a Value, name: &str) -> &'a str {
    policy["rules"][name].as_str().unwrap_or("deny")
}
fn marker(app: &App, shutoff: bool) -> Result<()> {
    let lock = File::open(app.control_dir.join("dispatch.lock"))?;
    lock.lock_exclusive()?;
    let result = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(app.control_dir.join("shutoff"))?;
        f.write_all(if shutoff { b"1" } else { b"0" })?;
        f.sync_all()?;
        Ok(())
    })();
    FileExt::unlock(&lock)?;
    result
}
impl App {
    async fn run(&self, id: &str) -> Result<Value> {
        self.store
            .get(&key("run", id))
            .await?
            .ok_or_else(|| anyhow!("Run not found"))
    }
    async fn shutoff(&self) -> Result<bool> {
        Ok(self
            .store
            .get("control")
            .await?
            .unwrap_or(json!({"shutoff":true}))["shutoff"]
            .as_bool()
            .unwrap_or(true))
    }
    async fn save(&self, run: &mut Value) -> Result<()> {
        run["updated_at"] = json!(now());
        self.store
            .save_run(run, "run_status", &public_run(run))
            .await
    }
    async fn tokens(&self) -> Result<Value> {
        let _lock = self.refresh.lock().await;
        let mut tokens = self
            .store
            .get("provider_tokens")
            .await?
            .ok_or_else(|| anyhow!("Connect your ChatGPT subscription first"))?;
        if tokens["expires_at"].as_f64().unwrap_or(0.) < now() as f64 + 60. {
            tokens = self.provider.refresh(&tokens).await?;
            self.store.put("provider_tokens", &tokens).await?;
        }
        Ok(tokens)
    }
}
async fn auth(State(app): State<App>, mut request: Request, next: Next) -> Response {
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
    let admin = match app.store.get("admin").await {
        Ok(Some(value)) => value,
        _ => return ApiError(StatusCode::UNAUTHORIZED, "Sign in required".into()).into_response(),
    };
    if doc["generation"] != admin["generation"] {
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
    request.extensions_mut().insert(doc);
    next.run(request).await
}
async fn login(
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
    let credential = app
        .store
        .get("admin")
        .await?
        .ok_or_else(|| bad("Admin unavailable"))?;
    let hash = credential["password_hash"].as_str().unwrap().to_owned();
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
    if username != credential["username"].as_str().unwrap_or("admin") || !valid {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "Invalid login".into()));
    }
    let token = uuid();
    let csrf = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"user":username,"csrf":csrf,"generation":credential["generation"],"expires":now()+86400}),
        )
        .await?;
    let mut response = Json(json!({"csrf_token":csrf,"user":username})).into_response();
    response.headers_mut().insert(
        "set-cookie",
        HeaderValue::from_str(&format!(
            "v3_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400{}",
            if app.secure { "; Secure" } else { "" }
        ))
        .unwrap(),
    );
    Ok(response)
}
async fn setup_status(State(app): State<App>) -> Api {
    Ok(Json(
        json!({"initialized":app.store.get("admin").await?.is_some()}),
    ))
}
async fn setup_admin(
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
    if app.store.get("admin").await?.is_some() {
        return Err(conflict("Admin already exists"));
    }
    let salt = SaltString::generate(&mut OsRng);
    let hash = Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map_err(|_| bad("Password hashing failed"))?
        .to_string();
    app.store
        .put(
            "admin",
            &json!({"username":username,"password_hash":hash,"generation":uuid()}),
        )
        .await?;
    login(
        State(app.clone()),
        headers,
        Json(json!({"username":username,"password":password})),
    )
    .await
}
async fn me(axum::Extension(doc): axum::Extension<Value>) -> Json<Value> {
    Json(json!({"user":doc["user"],"csrf_token":doc["csrf"]}))
}
async fn logout(
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
async fn overview(State(app): State<App>) -> Api {
    let login=app.login.read().await.clone().map(|v|json!({"verification_url":v["verification_url"],"user_code":v["user_code"],"status":v["status"]}));
    let mut runs = app.store.active_runs().await?;
    runs.extend(app.store.recent_runs(None).await?);
    let runs = runs.iter().map(public_run).collect::<Vec<_>>();
    Ok(Json(
        json!({"shutoff":app.shutoff().await?,"provider":{"connected":app.store.get("provider_tokens").await?.is_some(),"login":login},
        "model":app.store.get("agent:shared_default:model").await?.unwrap_or(json!({"model":DEFAULT_AGENT_MODEL}))["model"],"policy":app.store.get("policy").await?,"runs":runs}),
    ))
}
async fn settings(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let model = required(&body, "model", 128)?;
    if !model
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "-._".contains(c))
    {
        return Err(bad("Invalid model"));
    }
    let _guard = app.gate.lock().await;
    let value = json!({"model":model});
    app.store.put("agent:shared_default:model", &value).await?;
    Ok(Json(value))
}
// The first v3 agent uses its own model setting; the subscription credential
// remains in the independent v3 vault and can later be shared by more agents.
async fn agent_model(State(app): State<App>, Path(id): Path<String>) -> Api {
    if id != "shared_default" {
        return Err(ApiError(StatusCode::NOT_FOUND, "Agent not found".into()));
    }
    let value = app
        .store
        .get("agent:shared_default:model")
        .await?
        .unwrap_or(json!({"model":DEFAULT_AGENT_MODEL}));
    Ok(Json(
        json!({"id":id,"provider":"openai_subscription","model":value["model"]}),
    ))
}
async fn agent_model_set(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    if id != "shared_default" {
        return Err(ApiError(StatusCode::NOT_FOUND, "Agent not found".into()));
    }
    let model = required(&body, "model", 128)?;
    if !model
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "-._".contains(c))
    {
        return Err(bad("Invalid model"));
    }
    let value = json!({"model":model});
    app.store.put("agent:shared_default:model", &value).await?;
    Ok(Json(
        json!({"id":id,"provider":"openai_subscription","model":model}),
    ))
}
// The original v3 seed used gpt-5.4. Move that exact old default once,
// while leaving any other explicitly selected model untouched.
async fn migrate_default_agent_model(store: &Store) -> Result<()> {
    const MARKER: &str = "migration:default_model_gpt_6_luna";
    if store.get(MARKER).await?.is_some() {
        return Ok(());
    }
    if store
        .get("agent:shared_default:model")
        .await?
        .is_some_and(|value| value["model"] == "gpt-5.4")
    {
        store
            .put(
                "agent:shared_default:model",
                &json!({"model":DEFAULT_AGENT_MODEL}),
            )
            .await?;
    }
    store.put(MARKER, &json!({"complete":true})).await?;
    Ok(())
}
async fn policy_set(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let rules = body["rules"]
        .as_object()
        .ok_or_else(|| bad("rules required"))?;
    let schemas = tools::schemas();
    if rules.len() != schemas.len() {
        return Err(bad("Specify every tool policy"));
    }
    for schema in &schemas {
        if !matches!(
            rules
                .get(schema["name"].as_str().unwrap())
                .and_then(Value::as_str),
            Some("allow" | "deny" | "require_approval")
        ) {
            return Err(bad("Invalid policy rule"));
        }
    }
    let _guard = app.gate.lock().await;
    let old = app.store.get("policy").await?.unwrap();
    let value = json!({"version":old["version"].as_u64().unwrap()+1,"rules":rules});
    app.store.put("policy", &value).await?;
    app.store
        .event("", "policy_changed", &json!({"version":value["version"]}))
        .await?;
    Ok(Json(value))
}
async fn provider_start(State(app): State<App>) -> Api {
    let mut login = app.login.write().await;
    if login.as_ref().is_some_and(|v| v["status"] == "pending") {
        return Err(conflict("Authorization already pending"));
    }
    let mut device = app.provider.device_start().await?;
    device["status"] = json!("pending");
    let answer =
        json!({"verification_url":device["verification_url"],"user_code":device["user_code"]});
    *login = Some(device.clone());
    drop(login);
    tokio::spawn(async move {
        let expires = now() + 900;
        let interval = device["interval"].as_u64().unwrap_or(5).max(5);
        loop {
            tokio::select! {_=app.stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(interval))=>{}}
            let current = app.login.read().await.clone();
            if current.as_ref().is_none_or(|v| {
                v["device_auth_id"] != device["device_auth_id"] || v["status"] != "pending"
            }) {
                break;
            }
            if now() > expires {
                if let Some(v) = app.login.write().await.as_mut() {
                    v["status"] = json!("expired");
                }
                break;
            }
            match app.provider.device_poll(&device).await {
                Ok(Some(tokens)) => {
                    let _guard = app.refresh.lock().await;
                    let mut login = app.login.write().await;
                    if login.as_ref().is_some_and(|v| {
                        v["device_auth_id"] == device["device_auth_id"] && v["status"] == "pending"
                    }) {
                        if app.store.put("provider_tokens", &tokens).await.is_ok() {
                            if let Some(v) = login.as_mut() {
                                v["status"] = json!("connected");
                            }
                        }
                    }
                    break;
                }
                Ok(None) => {}
                Err(_) => {
                    let mut login = app.login.write().await;
                    if let Some(v) = login.as_mut() {
                        if v["device_auth_id"] == device["device_auth_id"] {
                            v["status"] = json!("failed");
                        }
                    }
                    break;
                }
            }
        }
    });
    Ok(Json(answer))
}
async fn provider_logout(State(app): State<App>) -> Api {
    let _guard = app.refresh.lock().await;
    *app.login.write().await = None;
    app.store.delete("provider_tokens").await?;
    for token in app.active.lock().await.values() {
        token.cancel();
    }
    Ok(Json(json!({"ok":true})))
}
async fn sessions(State(app): State<App>) -> Api {
    let list = app
        .store
        .list("session:")
        .await?
        .into_iter()
        .map(|v| json!({"id":v["id"],"title":v["title"]}))
        .collect::<Vec<_>>();
    Ok(Json(json!({"sessions":list})))
}
async fn create_session(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let request_id = required(&body, "client_request_id", 128)?;
    let title = body["title"].as_str().unwrap_or("New chat");
    if title.len() > 200 {
        return Err(bad("Title too long"));
    }
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:session:{request_id}");
    if let Some(receipt) = app.store.get(&receipt_key).await? {
        if receipt["digest"] != digest(&body) {
            return Err(conflict("Request identifier reused with different input"));
        }
        return Ok(Json(receipt["result"].clone()));
    }
    let id = uuid();
    let value = json!({"id":id,"title":title,"messages":[],"context":[],"created_at":now()});
    let result = json!({"id":id,"title":title});
    app.store
        .atomic_docs_event(
            vec![
                (key("session", &id), value),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            &id,
            "session_created",
            &result,
        )
        .await?;
    Ok(Json(result))
}
async fn snapshot(State(app): State<App>, Path(id): Path<String>) -> Api {
    let _guard = app.gate.lock().await;
    let session = app
        .store
        .get(&key("session", &id))
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Session not found".into()))?;
    let runs = app
        .store
        .recent_runs(Some(&id))
        .await?
        .iter()
        .map(public_run)
        .collect::<Vec<_>>();
    let last_seq =
        sqlx::query_scalar::<_, i64>("SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?")
            .bind(&id)
            .fetch_one(&app.store.pool)
            .await
            .map_err(anyhow::Error::from)?;
    let current = app
        .store
        .active_runs()
        .await?
        .into_iter()
        .find(|r| r["session_id"] == id && status(r) == "running" && r["phase"] == "model");
    let mut partial = Value::Null;
    if let Some(view) = current {
        let run = app.run(view["id"].as_str().unwrap()).await?;
        let rows=sqlx::query_scalar::<_,Vec<u8>>("SELECT payload FROM events WHERE session_id=? AND kind='delta' AND seq>? AND seq<=? ORDER BY seq")
            .bind(&id).bind(run["stream_after"].as_i64().unwrap_or(0)).bind(last_seq).fetch_all(&app.store.pool).await.map_err(anyhow::Error::from)?;
        let mut text = String::new();
        for row in rows {
            if text.len() >= 1024 * 1024 {
                break;
            }
            let item = app.store.unseal(&row)?;
            if item["run_id"] == run["id"] {
                text.push_str(item["text"].as_str().unwrap_or(""));
            }
        }
        if !text.is_empty() {
            partial = json!({"run_id":run["id"],"text":text});
        }
    }
    Ok(Json(
        json!({"session":{"id":session["id"],"title":session["title"]},"messages":session["messages"],"runs":runs,"last_seq":last_seq,"partial":partial}),
    ))
}
async fn send(
    State(app): State<App>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> std::result::Result<(StatusCode, Json<Value>), ApiError> {
    let text = required(&body, "text", 65536)?;
    let request_id = required(&body, "client_message_id", 128)?;
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:message:{id}:{request_id}");
    if let Some(receipt) = app.store.get(&receipt_key).await? {
        if receipt["digest"] != digest(&body) {
            return Err(conflict("Message identifier reused with different input"));
        }
        return Ok((StatusCode::ACCEPTED, Json(receipt["result"].clone())));
    }
    if app.shutoff().await? {
        return Err(conflict("Global shutoff is active"));
    }
    if app.store.get("provider_tokens").await?.is_none() {
        return Err(conflict("Connect your ChatGPT subscription first"));
    }
    let mut session = app
        .store
        .get(&key("session", &id))
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Session not found".into()))?;
    let active = app.store.active_runs().await?;
    if active.len() >= 200 {
        return Err(conflict("Run queue is full"));
    }
    if active
        .iter()
        .any(|r| r["session_id"] == id && !terminal(status(r)))
    {
        return Err(conflict("Resolve the existing run first"));
    }
    let mut input = session["context"].as_array().unwrap().clone();
    input.push(json!({"role":"user","content":text}));
    let run_id = uuid();
    let message = json!({"role":"user","content":text,"run_id":run_id});
    session["messages"]
        .as_array_mut()
        .unwrap()
        .push(message.clone());
    let run = json!({"id":run_id,"session_id":id,"status":"queued","phase":"model","input":input,"calls":[],"call_index":0,
        "turns":0,"attempt":0,"pending":null,"generation":"","uncertain":false,"error":null,"created_at":now(),"updated_at":now()});
    let result = json!({"run_id":run_id});
    app.store
        .atomic_docs_event(
            vec![
                (key("session", &id), session),
                (key("run", &run_id), run),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            &id,
            "message",
            &message,
        )
        .await?;
    Ok((StatusCode::ACCEPTED, Json(result)))
}
async fn events(
    State(app): State<App>,
    Path(id): Path<String>,
    headers: HeaderMap,
    axum::Extension(auth_doc): axum::Extension<Value>,
    Query(params): Query<HashMap<String, String>>,
) -> std::result::Result<
    Sse<impl futures_util::Stream<Item = std::result::Result<Event, Infallible>>>,
    ApiError,
> {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let after = params
        .get("after")
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0)
        .max(0)
        .max(
            headers
                .get("last-event-id")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<i64>().ok())
                .unwrap_or(0),
        );
    let auth_key = auth_doc["_auth_key"].as_str().unwrap_or("").to_owned();
    let expires = auth_doc["expires"].as_u64().unwrap_or(0);
    let stream = futures_util::stream::unfold(
        (
            app,
            id,
            after,
            Vec::<Value>::new(),
            now(),
            auth_key,
            expires,
            0u64,
        ),
        |(app, id, mut cursor, mut queue, start, auth_key, expires, mut checked)| async move {
            loop {
                if now() - start > 3600 || app.stop.is_cancelled() || now() >= expires {
                    return None;
                }
                if now() - checked >= 15 {
                    if !matches!(app.store.get(&auth_key).await, Ok(Some(_))) {
                        return None;
                    }
                    checked = now();
                }
                if !queue.is_empty() {
                    let item = queue.remove(0);
                    cursor = item["seq"].as_i64().unwrap();
                    let event = Event::default()
                        .id(cursor.to_string())
                        .data(item.to_string());
                    return Some((
                        Ok(event),
                        (app, id, cursor, queue, start, auth_key, expires, checked),
                    ));
                }
                queue = match app.store.events(&id, cursor).await {
                    Ok(v) => v,
                    Err(_) => return None,
                };
                if queue.is_empty() {
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
            }
        },
    );
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}
async fn control(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let req = required(&body, "client_request_id", 128)?;
    let shutoff = body["shutoff"]
        .as_bool()
        .ok_or_else(|| bad("shutoff must be boolean"))?;
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:control:{req}");
    if let Some(r) = app.store.get(&receipt_key).await? {
        if r["digest"] != digest(&body) {
            return Err(conflict("Identifier reused"));
        }
        return Ok(Json(r["result"].clone()));
    }
    // Fail closed: enabling marker precedes commit, disabling follows commit.
    if shutoff {
        marker(&app, true)?;
    }
    let result = json!({"shutoff":shutoff});
    app.store
        .atomic_docs_event(
            vec![
                ("control".into(), result.clone()),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            "",
            "control_changed",
            &result,
        )
        .await?;
    if !shutoff {
        marker(&app, false)?;
    } else {
        for token in app.active.lock().await.values() {
            token.cancel();
        }
        for view in app.store.active_runs().await? {
            if matches!(status(&view), "queued" | "running") {
                let mut run = app.run(view["id"].as_str().unwrap()).await?;
                run["status"] = json!("stopped");
                run["error"] = json!("Stopped by global shutoff");
                if run["pending"]["state"] == "executing"
                    && !tools::is_read(run["pending"]["name"].as_str().unwrap_or(""))
                {
                    run["uncertain"] = json!(true);
                }
                app.save(&mut run).await?;
            }
        }
    }
    Ok(Json(result))
}
async fn command(
    State(app): State<App>,
    Path((id, action)): Path<(String, String)>,
    Json(body): Json<Value>,
) -> Api {
    let req = required(&body, "client_request_id", 128)?;
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:run:{id}:{action}:{req}");
    if let Some(r) = app.store.get(&receipt_key).await? {
        if r["digest"] != digest(&body) {
            return Err(conflict("Identifier reused"));
        }
        return Ok(Json(r["result"].clone()));
    }
    let mut run = app
        .run(&id)
        .await
        .map_err(|_| ApiError(StatusCode::NOT_FOUND, "Run not found".into()))?;
    match action.as_str() {
        "cancel" => {
            if let Some(token) = app.active.lock().await.get(&id) {
                token.cancel();
            }
            if !terminal(status(&run)) {
                if run["pending"]["state"] == "executing"
                    && !tools::is_read(run["pending"]["name"].as_str().unwrap_or(""))
                {
                    run["status"] = json!("recovery");
                    run["uncertain"] = json!(true);
                } else {
                    run["status"] = json!("cancelled");
                }
                run["error"] = json!("Cancelled by user");
            }
        }
        "resume" => {
            if app.shutoff().await? {
                return Err(conflict("Clear global shutoff first"));
            }
            if run["uncertain"] == true {
                return Err(conflict("Review the uncertain tool outcome first"));
            }
            if !matches!(status(&run), "stopped" | "paused" | "failed") {
                return Err(conflict("Run cannot be resumed"));
            }
            if app.store.active_runs().await?.iter().any(|r| {
                r["id"] != id && r["session_id"] == run["session_id"] && !terminal(status(r))
            }) {
                return Err(conflict("Another run owns this conversation"));
            }
            run["status"] = json!("queued");
            run["error"] = Value::Null;
            run["attempt"] = json!(0);
            run["turns"] = json!(0);
            if run["pending"]["state"] == "executing" {
                run["pending"]["state"] = json!("requested");
            }
        }
        "recovery" => {
            if run["uncertain"] != true {
                return Err(conflict("Run does not require recovery review"));
            }
            match body["decision"].as_str() {
                Some("abandon") => {
                    run["status"] = json!("cancelled");
                    run["uncertain"] = json!(false);
                }
                Some("retry") => {
                    if app.shutoff().await? {
                        return Err(conflict("Clear global shutoff first"));
                    }
                    run["pending"]["state"] = json!("requested");
                    run["pending"]["decision"] = Value::Null;
                    run["status"] = json!("queued");
                    run["uncertain"] = json!(false);
                }
                Some("accept") => {
                    let output = required(&body, "output", 1048576)?;
                    append_tool(&mut run, &json!({"status":"reconciled","output":output}));
                    run["status"] = json!(if app.shutoff().await? {
                        "stopped"
                    } else {
                        "queued"
                    });
                    run["uncertain"] = json!(false);
                }
                _ => return Err(bad("Choose retry, accept, or abandon")),
            }
        }
        _ => return Err(ApiError(StatusCode::NOT_FOUND, "Unknown run action".into())),
    }
    let result = public_run(&run);
    app.store
        .atomic_docs_event(
            vec![
                (key("run", &id), run),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            result["session_id"].as_str().unwrap(),
            "run_status",
            &result,
        )
        .await?;
    Ok(Json(result))
}
async fn approval(State(app): State<App>, Path(id): Path<String>, Json(body): Json<Value>) -> Api {
    let req = required(&body, "client_request_id", 128)?;
    let decision = required(&body, "decision", 20)?;
    if !matches!(decision, "approve" | "deny") {
        return Err(bad("Invalid approval decision"));
    }
    let _guard = app.gate.lock().await;
    let receipt_key = format!("receipt:approval:{id}:{req}");
    if let Some(r) = app.store.get(&receipt_key).await? {
        if r["digest"] != digest(&body) {
            return Err(conflict("Identifier reused"));
        }
        return Ok(Json(r["result"].clone()));
    }
    let view = app
        .store
        .active_runs()
        .await?
        .into_iter()
        .find(|r| r["pending"]["id"] == id)
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Approval not found".into()))?;
    let mut run = app.run(view["id"].as_str().unwrap()).await?;
    if status(&run) != "waiting_approval" {
        return Err(conflict("Approval is no longer pending"));
    }
    let policy = app.store.get("policy").await?.unwrap();
    if run["pending"]["policy_version"] != policy["version"] {
        run["pending"]["state"] = json!("requested");
        run["pending"]["decision"] = Value::Null;
        run["status"] = json!("queued");
        app.save(&mut run).await?;
        return Err(conflict("Policy changed; a new gate decision is required"));
    }
    run["pending"]["decision"] = json!(decision);
    run["status"] = json!(if app.shutoff().await? {
        "stopped"
    } else {
        "queued"
    });
    let result = public_run(&run);
    app.store
        .atomic_docs_event(
            vec![
                (key("run", run["id"].as_str().unwrap()), run.clone()),
                (receipt_key, json!({"digest":digest(&body),"result":result})),
            ],
            run["session_id"].as_str().unwrap(),
            "run_status",
            &result,
        )
        .await?;
    Ok(Json(result))
}
fn append_tool(run: &mut Value, result: &Value) {
    let call_id = run["pending"]["call_id"].clone();
    run["input"]
        .as_array_mut()
        .unwrap()
        .push(json!({"type":"function_call_output","call_id":call_id,"output":result.to_string()}));
    run["call_index"] = json!(run["call_index"].as_u64().unwrap() + 1);
    run["pending"] = Value::Null;
}
async fn recover(app: &App, startup: bool) -> Result<()> {
    let _guard = app.gate.lock().await;
    for view in app.store.active_runs().await? {
        let id = view["id"].as_str().unwrap();
        if status(&view) != "running" || (!startup && app.active.lock().await.contains_key(id)) {
            continue;
        }
        let mut run = app.run(id).await?;
        if run["pending"]["state"] == "executing"
            && !tools::is_read(run["pending"]["name"].as_str().unwrap_or(""))
        {
            run["status"] = json!("recovery");
            run["uncertain"] = json!(true);
            run["error"] = json!("Tool outcome is uncertain after interruption");
        } else {
            run["status"] = json!(if app.shutoff().await? {
                "stopped"
            } else {
                "queued"
            });
            if run["pending"]["state"] == "executing" {
                run["pending"]["state"] = json!("requested");
            }
            run["error"] = json!("Recovered after interruption");
        }
        app.save(&mut run).await?;
    }
    Ok(())
}
async fn scheduler(app: App) {
    let mut last_recovery = 0;
    loop {
        if app.stop.is_cancelled() {
            break;
        }
        if now() - last_recovery >= 10 {
            if recover(&app, false).await.is_err() {
                tracing::error!("Recovery scan failed");
            }
            last_recovery = now();
        }
        let _guard = app.gate.lock().await;
        if let Ok(false) = app.shutoff().await {
            if let Ok(runs) = app.store.active_runs().await {
                for view in runs {
                    if status(&view) != "queued" {
                        continue;
                    }
                    let id = view["id"].as_str().unwrap().to_owned();
                    let mut run = match app.run(&id).await {
                        Ok(r) => r,
                        Err(_) => continue,
                    };
                    let mut active = app.active.lock().await;
                    if active.contains_key(&id) {
                        continue;
                    }
                    let permit = match app.slots.clone().try_acquire_owned() {
                        Ok(p) => p,
                        Err(_) => break,
                    };
                    let cancel = app.stop.child_token();
                    active.insert(id.clone(), cancel.clone());
                    drop(active);
                    run["status"] = json!("running");
                    run["generation"] = json!(uuid());
                    if app.save(&mut run).await.is_err() {
                        app.active.lock().await.remove(&id);
                        continue;
                    }
                    let cloned = app.clone();
                    tokio::spawn(async move {
                        let generation = run["generation"].as_str().unwrap().to_owned();
                        if let Err(error) =
                            execute_run(cloned.clone(), id.clone(), generation, cancel).await
                        {
                            tracing::warn!(run_id=%id,"Run interrupted: {}",error);
                        }
                        cloned.active.lock().await.remove(&id);
                        drop(permit);
                    });
                }
            }
        }
        drop(_guard);
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}
async fn execute_run(
    app: App,
    id: String,
    generation: String,
    cancel: CancellationToken,
) -> Result<()> {
    loop {
        let _guard = app.gate.lock().await;
        let mut run = app.run(&id).await?;
        if run["generation"] != generation || status(&run) != "running" || cancel.is_cancelled() {
            return Ok(());
        }
        if app.shutoff().await? {
            run["status"] = json!("stopped");
            app.save(&mut run).await?;
            return Ok(());
        }
        let sid = run["session_id"].as_str().unwrap().to_owned();
        if run["phase"] == "model" {
            if run["turns"].as_u64().unwrap() >= 50 || run["attempt"].as_u64().unwrap() >= 3 {
                run["status"] = json!("paused");
                run["error"] = json!("Run limit reached; explicit continuation required");
                app.save(&mut run).await?;
                return Ok(());
            }
            // Bound context reconstruction. Provider-specific token tuning follows measured support.
            let encoded = serde_json::to_vec(&run["input"])?;
            if encoded.len() > 1024 * 1024 {
                run["status"] = json!("paused");
                run["error"] = json!("Context limit reached");
                app.save(&mut run).await?;
                return Ok(());
            }
            run["stream_after"] = json!(
                sqlx::query_scalar::<_, i64>(
                    "SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?"
                )
                .bind(&sid)
                .fetch_one(&app.store.pool)
                .await?
            );
            run["attempt"] = json!(run["attempt"].as_u64().unwrap() + 1);
            app.save(&mut run).await?;
            let policy = app.store.get("policy").await?.unwrap();
            let schemas = tools::schemas()
                .into_iter()
                .filter(|t| policy_decision(&policy, t["name"].as_str().unwrap()) != "deny")
                .collect();
            let model_settings = app
                .store
                .get("agent:shared_default:model")
                .await?
                .unwrap_or(json!({"model":DEFAULT_AGENT_MODEL}));
            let model = model_settings["model"]
                .as_str()
                .unwrap_or(DEFAULT_AGENT_MODEL)
                .to_owned();
            let input = run["input"].as_array().unwrap().clone();
            drop(_guard);
            let tokens = match app.tokens().await {
                Ok(v) => v,
                Err(_) => {
                    let _guard = app.gate.lock().await;
                    let mut run = app.run(&id).await?;
                    if run["generation"] == generation && status(&run) == "running" {
                        run["status"] = json!(if run["attempt"].as_u64().unwrap() < 3 {
                            "queued"
                        } else {
                            "paused"
                        });
                        run["error"]=json!("Provider authorization unavailable; connect your subscription and resume");
                        app.save(&mut run).await?;
                    }
                    drop(_guard);
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    return Ok(());
                }
            };
            let (sender, mut receiver) = tokio::sync::mpsc::channel::<String>(32);
            let collector_app = app.clone();
            let collector_id = id.clone();
            let collector_generation = generation.clone();
            let collector_sid = sid.clone();
            let collector = tokio::spawn(async move {
                while let Some(text) = receiver.recv().await {
                    let _guard = collector_app.gate.lock().await;
                    if let Ok(run) = collector_app.run(&collector_id).await {
                        if run["generation"] == collector_generation && status(&run) == "running" {
                            let _ = collector_app
                                .store
                                .event(
                                    &collector_sid,
                                    "delta",
                                    &json!({"run_id":collector_id,"text":text}),
                                )
                                .await;
                        }
                    }
                }
            });
            // First poll initiates local dispatch while holding the same gate as shutoff.
            let dispatch_guard = app.gate.lock().await;
            let current = app.run(&id).await?;
            if current["generation"] != generation
                || status(&current) != "running"
                || app.shutoff().await?
                || cancel.is_cancelled()
            {
                drop(sender);
                drop(dispatch_guard);
                let _ = collector.await;
                return Ok(());
            }
            let mut completion = Box::pin(app.provider.complete(
                &tokens,
                &model,
                input,
                schemas,
                &sid,
                sender,
                cancel.clone(),
            ));
            let mut immediate = None;
            futures_util::future::poll_fn(|cx| {
                use std::future::Future;
                if let std::task::Poll::Ready(value) = completion.as_mut().poll(cx) {
                    immediate = Some(value);
                }
                std::task::Poll::Ready(())
            })
            .await;
            drop(dispatch_guard);
            let response = if let Some(value) = immediate {
                value
            } else {
                tokio::select! {
                    biased;
                    _=cancel.cancelled()=>Err(anyhow!("Execution cancelled")),
                    r=&mut completion=>r
                }
            };
            drop(completion);
            let _ = collector.await;
            let _guard = app.gate.lock().await;
            let mut run = app.run(&id).await?;
            if run["generation"] != generation || status(&run) != "running" {
                return Ok(());
            }
            match response {
                Err(_) => {
                    if cancel.is_cancelled() {
                        run["status"] = json!("stopped");
                        run["error"] = json!("Provider request interrupted");
                    } else if run["attempt"].as_u64().unwrap() < 3 {
                        run["status"] = json!("queued");
                        run["error"] = json!("Provider request interrupted; retrying");
                    } else {
                        run["status"] = json!("failed");
                        run["error"] = json!("Provider request failed after three attempts");
                    }
                    app.save(&mut run).await?;
                    drop(_guard);
                    if !cancel.is_cancelled() {
                        tokio::time::sleep(Duration::from_secs(2)).await;
                    }
                    return Ok(());
                }
                Ok(response) => {
                    let output = response["output"]
                        .as_array()
                        .ok_or_else(|| anyhow!("Missing provider output"))?;
                    let calls = output
                        .iter()
                        .filter(|v| v["type"] == "function_call")
                        .cloned()
                        .collect::<Vec<_>>();
                    for item in output {
                        run["input"].as_array_mut().unwrap().push(item.clone());
                    }
                    let text = output
                        .iter()
                        .filter(|v| v["type"] == "message")
                        .flat_map(|v| v["content"].as_array().into_iter().flatten())
                        .filter_map(|v| v["text"].as_str())
                        .collect::<Vec<_>>()
                        .join("");
                    run["turns"] = json!(run["turns"].as_u64().unwrap() + 1);
                    run["attempt"] = json!(0);
                    run["calls"] = json!(calls);
                    run["call_index"] = json!(0);
                    run["phase"] = json!("tools");
                    if !text.is_empty() {
                        let mut session = app.store.get(&key("session", &sid)).await?.unwrap();
                        let message = json!({"role":"assistant","content":text,"run_id":id});
                        session["messages"]
                            .as_array_mut()
                            .unwrap()
                            .push(message.clone());
                        app.store
                            .atomic_docs_event(
                                vec![
                                    (key("session", &sid), session),
                                    (key("run", &id), run.clone()),
                                ],
                                &sid,
                                "message",
                                &message,
                            )
                            .await?;
                    }
                    if calls.is_empty() {
                        run["status"] = json!("completed");
                        let mut session = app.store.get(&key("session", &sid)).await?.unwrap();
                        session["context"] = run["input"].clone();
                        app.store
                            .atomic_docs_event(
                                vec![
                                    (key("session", &sid), session),
                                    (key("run", &id), run.clone()),
                                ],
                                &sid,
                                "run_status",
                                &public_run(&run),
                            )
                            .await?;
                        return Ok(());
                    } else {
                        app.save(&mut run).await?;
                    }
                }
            }
        } else {
            let idx = run["call_index"].as_u64().unwrap() as usize;
            let calls = run["calls"].as_array().unwrap();
            if idx >= calls.len() {
                run["phase"] = json!("model");
                app.save(&mut run).await?;
                continue;
            }
            let call = calls[idx].clone();
            let name = call["name"].as_str().unwrap_or("").to_owned();
            let args = match call["arguments"]
                .as_str()
                .map(serde_json::from_str::<Value>)
            {
                Some(Ok(v)) if v.is_object() => v,
                _ => json!({"invalid_arguments":true}),
            };
            let policy = app.store.get("policy").await?.unwrap();
            let decision = policy_decision(&policy, &name).to_owned();
            if run["pending"].is_null() {
                run["pending"] = json!({"id":uuid(),"name":name,"arguments":args,"call_id":call["call_id"],"digest":digest(&args),
                    "policy_version":policy["version"],"state":"requested","decision":null});
            }
            if run["pending"]["policy_version"] != policy["version"] {
                run["pending"]["id"] = json!(uuid());
                run["pending"]["decision"] = Value::Null;
                run["pending"]["policy_version"] = policy["version"].clone();
            }
            if decision == "deny"
                || run["pending"]["decision"] == "deny"
                || args.get("invalid_arguments").is_some()
            {
                let result = json!({"status":"denied","output":"Tool invocation denied by policy or invalid arguments"});
                append_tool(&mut run, &result);
                app.store
                    .save_run(
                        &run,
                        "tool_result",
                        &json!({"run_id":id,"name":name,"result":result}),
                    )
                    .await?;
                continue;
            }
            if decision == "require_approval" && run["pending"]["decision"] != "approve" {
                run["status"] = json!("waiting_approval");
                run["pending"]["state"] = json!("waiting");
                let mut payload = run["pending"].clone();
                payload["run_id"] = json!(id);
                app.store
                    .save_run(&run, "approval_required", &payload)
                    .await?;
                return Ok(());
            }
            if run["pending"]["digest"] != digest(&args) {
                return Err(anyhow!("Tool argument digest mismatch"));
            }
            run["pending"]["state"] = json!("executing");
            run["pending"]["decision"] = json!("consumed");
            app.save(&mut run).await?;
            drop(_guard);
            let result = tools::execute(&name, &args, &sid, cancel.clone()).await;
            let _guard = app.gate.lock().await;
            let mut run = app.run(&id).await?;
            if run["generation"] != generation {
                return Ok(());
            }
            if status(&run) != "running" {
                // An already committed result can resolve uncertainty without restarting the run.
                if run["uncertain"] == true {
                    if let Ok(value) = result {
                        if !matches!(
                            value["status"].as_str(),
                            Some("cancelled" | "timeout" | "uncertain")
                        ) {
                            append_tool(&mut run, &value);
                            run["uncertain"] = json!(false);
                            app.save(&mut run).await?;
                        }
                    }
                }
                return Ok(());
            }
            match result {
                Ok(value) => {
                    let uncertain = matches!(
                        value["status"].as_str(),
                        Some("cancelled" | "timeout" | "uncertain")
                    );
                    if uncertain && !tools::is_read(&name) {
                        run["uncertain"] = json!(true);
                        run["status"] = json!("recovery");
                        run["error"] = json!("Tool interrupted; review its outcome");
                        app.save(&mut run).await?;
                        return Ok(());
                    }
                    append_tool(&mut run, &value);
                    app.store
                        .save_run(
                            &run,
                            "tool_result",
                            &json!({"run_id":id,"name":name,"result":value}),
                        )
                        .await?;
                }
                Err(_) => {
                    if tools::is_read(&name) {
                        run["pending"]["state"] = json!("requested");
                        run["status"] = json!("failed");
                        run["error"] = json!("Read tool interrupted");
                    } else {
                        run["uncertain"] = json!(true);
                        run["status"] = json!("recovery");
                        run["error"] = json!("Tool outcome unknown; review before continuing");
                    }
                    app.save(&mut run).await?;
                    return Ok(());
                }
            }
        }
    }
}
fn router(app: App) -> Router {
    let api = Router::new()
        .route("/auth/me", get(me))
        .route("/auth/logout", post(logout))
        .route("/status", get(overview))
        .route("/settings", put(settings))
        .route("/agents/{id}/model", get(agent_model).put(agent_model_set))
        .route("/policy", put(policy_set))
        .route("/provider/login", post(provider_start))
        .route("/provider/logout", post(provider_logout))
        .route("/sessions", get(sessions).post(create_session))
        .route("/sessions/{id}", get(snapshot))
        .route("/sessions/{id}/messages", post(send))
        .route("/sessions/{id}/events", get(events))
        .route("/runs/{id}/{action}", post(command))
        .route("/approvals/{id}/decision", post(approval))
        .route("/control", put(control))
        .route_layer(middleware::from_fn_with_state(app.clone(), auth));
    let ui = std::env::var("V3_UI_ROOT").unwrap_or_else(|_| "/opt/v3/ui".into());
    let shared = std::env::var("V3_SHARED_UI_ROOT").unwrap_or_else(|_| "/opt/v3/shared-ui".into());
    Router::new().route("/api/v3/auth/login",post(login))
        .route("/api/v3/auth/setup-status",get(setup_status))
        .route("/api/v3/auth/setup-admin",post(setup_admin))
        .nest("/api/v3",api)
        .route("/health/live",get(||async{Json(json!({"status":"alive"}))}))
        .route("/health/ready",get(|State(app):State<App>|async move{
            let healthy=app.ready.load(Ordering::Acquire)&&sqlx::query("SELECT 1").execute(&app.store.pool).await.is_ok();
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
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("webagent_v3=info".parse()?),
        )
        .init();
    let root = PathBuf::from(std::env::var("V3_STATE_ROOT").unwrap_or_else(|_| "/state".into()));
    std::fs::create_dir_all(&root)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(root.join("runtime.lock"))?;
    lock.try_lock_exclusive()
        .map_err(|_| anyhow!("Another v3 application owns this state"))?;
    let store = Store::open(&root).await?;
    // The temporary development bootstrap credential had no generation. Retire it
    // once so this installation follows the same first-run setup flow as v2.
    if store
        .get("admin")
        .await?
        .is_some_and(|v| v["generation"].is_null())
    {
        store.delete("admin").await?;
    }
    for (k, v) in [
        ("policy", policy_default()),
        ("settings", json!({"model":DEFAULT_AGENT_MODEL})),
        ("control", json!({"shutoff":false})),
    ] {
        if store.get(k).await?.is_none() {
            store.put(k, &v).await?;
        }
    }
    migrate_default_agent_model(&store).await?;
    let app = App {
        store,
        gate: Arc::new(Mutex::new(())),
        provider: provider::Provider::new(),
        refresh: Arc::new(Mutex::new(())),
        login: Arc::new(RwLock::new(None)),
        active: Arc::new(Mutex::new(HashMap::new())),
        slots: Arc::new(Semaphore::new(2)),
        ready: Arc::new(AtomicBool::new(false)),
        stop: CancellationToken::new(),
        login_attempts: Arc::new(Mutex::new(Vec::new())),
        control_dir: PathBuf::from(
            std::env::var("V3_CONTROL_ROOT").unwrap_or_else(|_| "/run/v3/control".into()),
        ),
        secure: std::env::var("V3_SECURE_COOKIE").unwrap_or_else(|_| "true".into()) != "false",
    };
    marker(&app, app.shutoff().await?)?;
    recover(&app, true).await?;
    app.ready.store(true, Ordering::Release);
    let scheduler = tokio::spawn(scheduler(app.clone()));
    let listener = tokio::net::TcpListener::bind(
        std::env::var("V3_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into()),
    )
    .await?;
    tracing::info!("WebAgent v3 ready");
    let shutdown = app.clone();
    axum::serve(listener, router(app.clone()))
        .with_graceful_shutdown(async move {
            let mut signal =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("signal handler");
            tokio::select! {_=signal.recv()=>{},_=tokio::signal::ctrl_c()=>{}}
            shutdown.ready.store(false, Ordering::Release);
            {
                let _guard = shutdown.gate.lock().await;
                let _ = marker(&shutdown, true);
                shutdown.stop.cancel();
                for c in shutdown.active.lock().await.values() {
                    c.cancel();
                }
            }
        })
        .await?;
    let _ = scheduler.await;
    tokio::time::sleep(Duration::from_secs(1)).await;
    app.store.pool.close().await;
    drop(lock);
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_tool_policy_is_denied() {
        assert_eq!(policy_decision(&policy_default(), "unknown"), "deny");
    }
    #[test]
    fn policy_defaults_gate_all_side_effects() {
        let p = policy_default();
        for name in ["file_write", "shell", "python"] {
            assert_eq!(policy_decision(&p, name), "require_approval");
        }
        assert_eq!(policy_decision(&p, "file_read"), "allow");
    }
    #[test]
    fn arguments_digest_changes_with_action() {
        assert_ne!(
            digest(&json!({"command":"echo hello"})),
            digest(&json!({"command":"rm file"}))
        );
    }
    #[test]
    fn consumed_tool_result_advances_once() {
        let mut r = json!({"input":[],"pending":{"call_id":"c"},"call_index":0});
        append_tool(&mut r, &json!({"output":"done"}));
        assert_eq!(r["call_index"], 1);
        assert!(r["pending"].is_null());
        assert_eq!(r["input"][0]["call_id"], "c");
    }
}
