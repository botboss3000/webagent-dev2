//! V2 registration policy adapted to independent encrypted v3 accounts.
//! Approval is not an entitlement: neither public signup nor approval grants tools.
use super::*;

// The app-owned file is authoritative; unknown fields survive registration edits.
impl Store {
    pub(crate) fn access_policy_path(&self) -> std::path::PathBuf {
        self.root.join("config/access-policy.json")
    }
    pub(crate) fn read_access_policy(&self) -> Result<Value> {
        let value: Value = serde_json::from_slice(&std::fs::read(self.access_policy_path())?)?;
        if !value.is_object() || !matches!(value["registration"].as_str(), Some("open"|"restricted")) {
            return Err(anyhow!("Invalid access-policy.json registration policy"));
        }
        Ok(value)
    }
    fn write_access_policy(&self, value: &Value) -> Result<()> {
        use std::io::Write;
        let path=self.access_policy_path();
        let directory=path.parent().ok_or_else(||anyhow!("Invalid policy path"))?;
        std::fs::create_dir_all(directory)?;
        use std::os::unix::fs::OpenOptionsExt;
        let temporary=directory.join(format!(".access-policy-{}.tmp",Uuid::new_v4()));
        let result=(|| -> Result<()> {
            let mut file=std::fs::OpenOptions::new().write(true).create_new(true)
                .mode(0o600).open(&temporary)?;
            file.write_all(&serde_json::to_vec_pretty(value)?)?;
            file.write_all(b"\n")?;
            file.sync_all()?;
            std::fs::rename(&temporary,&path)?;
            std::fs::File::open(directory)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() {let _=std::fs::remove_file(&temporary);}
        result?;
        Ok(())
    }
    pub(crate) async fn init_access_policy(&self) -> Result<()> {
        if self.access_policy_path().exists() {
            self.read_access_policy()?;
            return Ok(());
        }
        let legacy=self.get("auth:registration_policy").await?;
        let registration=if legacy.as_ref().and_then(|v|v["access_mode"].as_str())==Some("public_registered") {"open"} else {"restricted"};
        self.write_access_policy(&json!({"registration":registration}))
    }
    pub(crate) fn set_registration_policy(&self, registration: &str) -> Result<()> {
        if !matches!(registration,"open"|"restricted") {return Err(anyhow!("Invalid registration policy"));}
        let mut value=self.read_access_policy()?;
        value["registration"]=json!(registration);
        self.write_access_policy(&value)
    }
}

pub(crate) async fn mode(app: &App) -> Result<String> {
    Ok(if app.store.read_access_policy()?["registration"]=="open" {
        "public_registered"
    } else {"admin_approval"}.into())
}
pub(crate) async fn access_mode(State(app): State<App>) -> Api {
    Ok(Json(json!({"access_mode":mode(&app).await?, "guest_access":mode(&app).await?=="public_registered"})))
}
pub(crate) async fn set_mode(State(app): State<App>,
    axum::Extension(identity): axum::Extension<Value>, Json(body): Json<Value>) -> Api {
    phase3_api::administrator(&app,&identity).await?;
    let value=required(&body,"access_mode",32)?;
    if !matches!(value,"admin_approval"|"public_registered") {
        return Err(bad("Invalid access mode"));
    }
    let _guard=app.gate.lock().await;
    app.store.set_registration_policy(if value=="public_registered" {"open"} else {"restricted"})?;
    Ok(Json(json!({"access_mode":value,"guest_access":value=="public_registered"})))
}
pub(crate) async fn register(State(app): State<App>, headers: HeaderMap,
    Json(body): Json<Value>) -> std::result::Result<Response,ApiError> {
    if let Some(origin)=headers.get("origin").and_then(|v|v.to_str().ok()) {
        let host=headers.get("host").and_then(|v|v.to_str().ok()).unwrap_or("");
        if origin!=format!("https://{host}") && (app.secure || origin!=format!("http://{host}")) {
            return Err(bad("Invalid request origin"));
        }
    }
    if !app.store.has_admin_account().await? {return Err(conflict("Complete administrator setup first"));}
    let username=required(&body,"username",128)?.to_owned();
    let password=required(&body,"password",1024)?.to_owned();
    let display=body["display_name"].as_str().unwrap_or(&username).to_owned();
    if password.len()<8 || username.trim()!=username || username.chars().any(char::is_control)
        || display.trim()!=display || display.is_empty() || display.len()>128 || display.chars().any(char::is_control) {
        return Err(bad("Invalid registration input; password must be at least 8 characters"));
    }
    // Bound expensive public password hashing, including rejected duplicate signups.
    {
        let mut attempts=app.login_attempts.lock().await;
        attempts.retain(|t|now().saturating_sub(*t)<60);
        if attempts.len()>=5 {return Err(ApiError(StatusCode::TOO_MANY_REQUESTS,"Try again in one minute".into()));}
        attempts.push(now());
    }
    let hash_password=password.clone();
    let hash=tokio::task::spawn_blocking(move || {
        Argon2::default().hash_password(hash_password.as_bytes(),&SaltString::generate(&mut OsRng))
            .map(|hash|hash.to_string())
    }).await.map_err(|_|bad("Registration unavailable"))?.map_err(|_|bad("Password hashing failed"))?;
    let _guard=app.gate.lock().await;
    let public=mode(&app).await?=="public_registered";
    let created=app.store.create_user_account(&username,&hash).await
        .map_err(|_|conflict("Account creation rejected; username may already be registered"))?;
    let id=created["user_id"].as_str().ok_or_else(||bad("Account unavailable"))?;
    app.store.update_user_profile(id,&username,&display).await?;
    if !public {
        return Ok(Json(json!({"user_id":id,"username":username,"display_name":display,"pending_approval":true})).into_response());
    }
    app.store.update_account_access(id,1,access::AccountStatus::Active,access::Role::User,"basic").await?;
    drop(_guard);
    let mut response=login(State(app),headers,Json(json!({"username":username,"password":password}))).await?;
    // Login owns cookie/CSRF issuance; no passwordless token or admin fallback.
    response.headers_mut().insert("cache-control",HeaderValue::from_static("no-store"));
    Ok(response)
}

/// V3 uses an HttpOnly persistent cookie instead of V2's browser-local bearer.
/// A valid existing guest session recovers the identity and rotates its token.
/// Caller-supplied browser IDs are never accepted as proof of ownership.
pub(crate) async fn guest_login(State(app): State<App>, headers: HeaderMap)
    -> std::result::Result<Response,ApiError> {
    if let Some(origin)=headers.get("origin").and_then(|v|v.to_str().ok()) {
        let host=headers.get("host").and_then(|v|v.to_str().ok()).unwrap_or("");
        if origin!=format!("https://{host}") && (app.secure || origin!=format!("http://{host}")) {
            return Err(bad("Invalid request origin"));
        }
    }
    let _guard=app.gate.lock().await;
    if mode(&app).await?!="public_registered" {
        return Err(ApiError(StatusCode::FORBIDDEN,"Guest access is not enabled".into()));
    }
    if !app.store.has_admin_account().await? {return Err(conflict("Complete administrator setup first"));}
    let old_token=headers.get("cookie").and_then(|v|v.to_str().ok()).unwrap_or("")
        .split(';').find_map(|s|s.trim().strip_prefix("v3_session=")).unwrap_or("");
    let old_key=format!("auth:{}",digest(&json!(old_token)));
    let mut recovered=None;
    if old_token.len()==36 {
        if let Some(doc)=app.store.get(&old_key).await? {
            if doc["expires"].as_u64().unwrap_or(0)>now() {
                if let Some(id)=doc["user_id"].as_str() {
                    if let Some(account)=app.store.account_by_id(id).await? {
                        if account["role"]!="guest" {
                            return Err(conflict("Sign out before using guest access"));
                        }
                        if account["generation"]==doc["generation"] && account["status"]=="active" {
                            recovered=Some(account);
                        }
                    }
                }
            }
        }
    }
    let account=if let Some(account)=recovered { account } else {
        let mut attempts=app.login_attempts.lock().await;
        attempts.retain(|t|now().saturating_sub(*t)<60);
        if attempts.len()>=5 {return Err(ApiError(StatusCode::TOO_MANY_REQUESTS,"Try again in one minute".into()));}
        attempts.push(now());drop(attempts);
        app.store.create_guest_account().await?
    };
    let token=uuid();let csrf=uuid();let duration=30*86400;
    app.store.put(&format!("auth:{}",digest(&json!(token))),&json!({
        "id":uuid(),"user":account["username"],"user_id":account["user_id"],
        "generation":account["generation"],"csrf":csrf,"created_at":now(),
        "user_agent":"Guest browser","expires":now()+duration})).await?;
    if old_token.len()==36 {app.store.delete(&old_key).await?;}
    let mut response=Json(json!({"csrf_token":csrf,"user_id":account["user_id"],"is_guest":true})).into_response();
    response.headers_mut().insert("set-cookie",HeaderValue::from_str(&format!(
        "v3_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={duration}{}",
        if app.secure {"; Secure"} else {""})).map_err(|_|bad("Guest session unavailable"))?);
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn policy_file_defaults_preserves_extensions_and_rejects_invalid_values() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        assert_eq!(app.store.read_access_policy().unwrap()["registration"],"restricted");
        let path=app.store.access_policy_path();
        std::fs::write(&path,br#"{"registration":"open","pages":{"future":"restricted"}}"#).unwrap();
        assert_eq!(mode(&app).await.unwrap(),"public_registered");
        app.store.set_registration_policy("restricted").unwrap();
        assert_eq!(app.store.read_access_policy().unwrap()["pages"]["future"],"restricted");
        assert!(app.store.set_registration_policy("invalid").is_err());
        std::fs::write(&path,br#"{"registration":"invalid"}"#).unwrap();
        assert!(mode(&app).await.is_err());
        assert!(app.store.init_access_policy().await.is_err());
    }
    #[tokio::test]
    async fn policy_file_migrates_legacy_only_when_missing() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        app.store.put("auth:registration_policy",&json!({"access_mode":"public_registered"})).await.unwrap();
        std::fs::remove_file(app.store.access_policy_path()).unwrap();
        app.store.init_access_policy().await.unwrap();
        assert_eq!(app.store.read_access_policy().unwrap()["registration"],"open");
        app.store.set_registration_policy("restricted").unwrap();
        app.store.init_access_policy().await.unwrap();
        assert_eq!(mode(&app).await.unwrap(),"admin_approval");
    }
    #[tokio::test]
    async fn guest_cookie_rotates_and_policy_revokes_runtime_access() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        assert!(guest_login(State(app.clone()),HeaderMap::new()).await.is_err());
        app.store.set_registration_policy("open").unwrap();
        let response=guest_login(State(app.clone()),HeaderMap::new()).await.unwrap();
        let cookie=response.headers()["set-cookie"].to_str().unwrap().split(';').next().unwrap().to_owned();
        let token=cookie.strip_prefix("v3_session=").unwrap();
        let old_key=format!("auth:{}",digest(&json!(token)));
        let doc=app.store.get(&old_key).await.unwrap().unwrap();
        let id=doc["user_id"].as_str().unwrap();
        let account=app.store.account_by_id(id).await.unwrap().unwrap();
        assert_eq!(account["role"],"guest");
        assert!(account["password_hash"].is_null());
        assert!(!access::Principal::from_account(&account).unwrap().administrator());
        assert!(!app.store.live_access(id,"shared_default").await.unwrap().admitted());
        assert!(login(State(app.clone()),HeaderMap::new(),Json(json!({"username":id,"password":"anything"}))).await.is_err());
        let mut headers=HeaderMap::new();headers.insert("cookie",HeaderValue::from_str(&cookie).unwrap());
        let rotated=guest_login(State(app.clone()),headers).await.unwrap();
        let next=rotated.headers()["set-cookie"].to_str().unwrap().split(';').next().unwrap();
        assert_ne!(next,cookie);
        assert!(app.store.get(&old_key).await.unwrap().is_none());
        let next_doc=app.store.get(&format!("auth:{}",digest(&json!(next.strip_prefix("v3_session=").unwrap())))).await.unwrap().unwrap();
        assert_eq!(next_doc["user_id"],id);
        app.store.set_registration_policy("restricted").unwrap();
        assert!(app.store.live_access(id,"shared_default").await.is_err());
    }
    #[tokio::test]
    async fn registration_defaults_pending_and_never_grants_admin() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let response=register(State(app.clone()),HeaderMap::new(),Json(json!({"username":"new@example.test","password":"fixture-password","display_name":"New person"}))).await.unwrap();
        assert_eq!(response.status(),StatusCode::OK);
        assert!(!response.headers().contains_key("set-cookie"));
        let account=app.store.account_by_username("new@example.test").await.unwrap().unwrap();
        assert_eq!(account["status"],"pending");
        assert_eq!(account["role"],"user");
        assert_eq!(account["tier_id"],"basic");
        assert!(register(State(app),HeaderMap::new(),Json(json!({"username":"new@example.test","password":"fixture-password"}))).await.is_err());
    }
    #[tokio::test]
    async fn public_signup_uses_regular_cookie_but_has_no_agent_grant() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        app.store.set_registration_policy("open").unwrap();
        let response=register(State(app.clone()),HeaderMap::new(),Json(json!({"username":"second","password":"fixture-password"}))).await.unwrap();
        assert!(response.headers()["set-cookie"].to_str().unwrap().contains("HttpOnly"));
        let account=app.store.account_by_username("second").await.unwrap().unwrap();
        assert_eq!(account["status"],"active");
        assert!(!app.store.live_access(account["user_id"].as_str().unwrap(),"shared_default").await.unwrap().admitted());
    }
}
