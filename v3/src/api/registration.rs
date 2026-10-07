//! V2 registration policy adapted to independent encrypted v3 accounts.
//! Approval is not an entitlement: neither public signup nor approval grants tools.
use super::*;
use sqlx::Row;

// The app-owned file is authoritative; unknown fields survive registration edits.
impl Store {
    pub(crate) fn access_policy_path(&self) -> std::path::PathBuf {
        self.root.join("config/access-policy.json")
    }
    pub(crate) fn policy_lock(&self) -> Result<std::fs::File> {
        use fs2::FileExt;
        let directory=self.root.join("config");
        std::fs::create_dir_all(&directory)?;
        let lock=std::fs::OpenOptions::new().create(true).read(true).write(true).open(directory.join("access-policy.lock"))?;
        lock.lock_exclusive()?;
        Ok(lock)
    }
    pub(crate) fn read_access_policy(&self) -> Result<Value> {
        let value: Value = serde_json::from_slice(&std::fs::read(self.access_policy_path())?)?;
        if !value.is_object() || !matches!(value["registration"].as_str(), Some("open"|"restricted")) {
            return Err(anyhow!("Invalid access-policy.json registration policy"));
        }
        if value.get("revision").is_some_and(|v|v.as_u64().is_none_or(|n|n==0)) {return Err(anyhow!("Invalid policy revision"));}
        if let Some(tiers)=value.get("tiers") {
            let map=tiers.as_object().ok_or_else(||anyhow!("Invalid policy tiers"))?;
            for (id,definition) in map {let tier:access::Tier=serde_json::from_value(definition.clone())?;tier.validate()?;if tier.id!=*id {return Err(anyhow!("Tier identity mismatch"));}}
        }
        if let Some(pages)=value.get("pages") {validate_pages(pages)?;}
        Ok(value)
    }
    pub(crate) fn save_page_policy(&self, pages:Value, expected:u64) -> Result<Value> {
        validate_pages(&pages)?;
        let _lock=self.policy_lock()?;
        let mut policy=self.read_access_policy()?;
        if policy["revision"].as_u64().unwrap_or(0)!=expected {return Err(anyhow!("Policy revision changed"));}
        policy["pages"]=pages;
        policy["revision"]=json!(expected.checked_add(1).ok_or_else(||anyhow!("Policy revision overflow"))?);
        self.write_access_policy(&policy)?;
        Ok(json!({"pages":policy["pages"],"revision":policy["revision"]}))
    }
    pub(crate) async fn init_policy_tiers(&self) -> Result<()> {
        if self.read_access_policy()?.get("tiers").is_some() {return Ok(());}
        let rows=sqlx::query("SELECT value FROM entitlement_tiers ORDER BY tier_id").fetch_all(&self.app_pool).await?;
        let mut tiers=serde_json::Map::new();
        for row in rows {let value=self.unseal(&row.get::<Vec<u8>,_>("value"))?;tiers.insert(value["id"].as_str().ok_or_else(||anyhow!("Invalid legacy tier"))?.to_owned(),value);}
        let _lock=self.policy_lock()?;
        let mut policy=self.read_access_policy()?;
        if policy.get("tiers").is_none() {policy["tiers"]=Value::Object(tiers);policy["revision"]=json!(policy["revision"].as_u64().unwrap_or(0)+1);self.write_access_policy(&policy)?;}
        Ok(())
    }
    pub(crate) fn write_access_policy(&self, value: &Value) -> Result<()> {
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
        let _lock=self.policy_lock()?;
        if self.access_policy_path().exists() {self.read_access_policy()?;return Ok(());}
        self.write_access_policy(&json!({"schema_version":1,"revision":1,"registration":registration}))
    }
    pub(crate) fn set_registration_policy(&self, registration: &str) -> Result<()> {
        self.set_registration_policy_revision(registration,None)
    }
    pub(crate) fn set_registration_policy_revision(&self, registration: &str, expected:Option<u64>) -> Result<()> {
        if !matches!(registration,"open"|"restricted") {return Err(anyhow!("Invalid registration policy"));}
        let _lock=self.policy_lock()?;
        let mut value=self.read_access_policy()?;
        if expected.is_some_and(|revision|revision!=value["revision"].as_u64().unwrap_or(0)) {return Err(anyhow!("Policy revision changed"));}
        value["revision"]=json!(value["revision"].as_u64().unwrap_or(0).checked_add(1).ok_or_else(||anyhow!("Policy revision overflow"))?);
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
    let policy=app.store.read_access_policy()?;
    let open=policy["registration"]=="open";
    Ok(Json(json!({"access_mode":if open {"public_registered"} else {"admin_approval"}, "guest_access":open,"revision":policy["revision"].as_u64().unwrap_or(0)})))
}
pub(crate) async fn set_mode(State(app): State<App>,
    axum::Extension(identity): axum::Extension<Value>, Json(body): Json<Value>) -> Api {
    phase3_api::administrator(&app,&identity).await?;
    let value=required(&body,"access_mode",32)?;
    if !matches!(value,"admin_approval"|"public_registered") {
        return Err(bad("Invalid access mode"));
    }
    let _guard=app.gate.lock().await;
    let expected=body["expected_revision"].as_u64().ok_or_else(||bad("Expected policy revision required"))?;
    app.store.set_registration_policy_revision(if value=="public_registered" {"open"} else {"restricted"},Some(expected))
        .map_err(|_|conflict("Policy save rejected; reload the current policy"))?;
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
        // Extension preservation is separate from the now-validated page-policy schema.
        std::fs::write(&path,br#"{"registration":"open","extension":{"future":"restricted"}}"#).unwrap();
        assert_eq!(mode(&app).await.unwrap(),"public_registered");
        app.store.set_registration_policy("restricted").unwrap();
        assert_eq!(app.store.read_access_policy().unwrap()["extension"]["future"],"restricted");
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

// Page audiences are a global ceiling; administrative APIs retain their role gate.
pub(crate) fn validate_pages(pages:&Value)->Result<()> {
    let scopes=pages.as_object().ok_or_else(||anyhow!("Invalid page policy"))?;
    for (scope,entries) in scopes {
        if !matches!(scope.as_str(),"main"|"app") {return Err(anyhow!("Invalid page scope"));}
        let entries=entries.as_object().ok_or_else(||anyhow!("Invalid page entries"))?;
        if entries.len()>128 {return Err(anyhow!("Too many page policies"));}
        for (id,audience) in entries {
            if !access::valid_agent_id(id) || !matches!(audience.as_str(),Some("everyone"|"registered"|"admin")) {return Err(anyhow!("Invalid page audience"));}
        }
    }
    Ok(())
}
pub(crate) fn page_allowed(policy:&Value, scope:&str, id:&str, principal:&access::Principal)->bool {
    if !principal.active() {return false;}
    if principal.administrator() {return true;} // protected administration recovery
    if scope=="app" && matches!(id,"settings"|"users"|"user-policies"|"page-access") {return false;}
    match policy["pages"][scope][id].as_str().unwrap_or("everyone") {
        "everyone"=>true,"registered"=>principal.role!=access::Role::Guest,_=>false,
    }
}

#[cfg(test)]
mod advanced_policy_tests {
    use super::*;
    #[tokio::test]
    async fn policy_cas_and_file_tiers_preserve_extensions() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let before=app.store.read_access_policy().unwrap();
        let revision=before["revision"].as_u64().unwrap();
        app.store.set_registration_policy_revision("open",Some(revision)).unwrap();
        assert!(app.store.set_registration_policy_revision("restricted",Some(revision)).is_err());
        assert_eq!(app.store.entitlement_tier("basic").await.unwrap().unwrap().id,"basic");
        let mut policy=app.store.read_access_policy().unwrap();policy["extension"]=json!({"future":true});app.store.write_access_policy(&policy).unwrap();
        app.store.save_page_policy(json!({"main":{"chat":"admin"}}),policy["revision"].as_u64().unwrap()).unwrap();
        let current=app.store.read_access_policy().unwrap();assert_eq!(current["extension"]["future"],true);
        let user=app.store.create_user_account("fixture-user","long-hash-placeholder").await.unwrap();
        app.store.update_account_access(user["user_id"].as_str().unwrap(),1,access::AccountStatus::Active,access::Role::User,"basic").await.unwrap();
        let account=app.store.account_by_id(user["user_id"].as_str().unwrap()).await.unwrap().unwrap();
        assert!(!page_allowed(&current,"main","chat",&access::Principal::from_account(&account).unwrap()));
        assert!(validate_pages(&json!({"main":{"chat":"invalid"}})).is_err());
    }
    #[tokio::test]
    async fn file_edits_change_live_access_fingerprint() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let before=app.store.live_access("admin","shared_default").await.unwrap();
        let mut policy=app.store.read_access_policy().unwrap();policy["extension"]=json!("changed");
        app.store.write_access_policy(&policy).unwrap();
        let after=app.store.live_access("admin","shared_default").await.unwrap();
        assert_ne!(before.revision.policy,after.revision.policy);
    }
}
