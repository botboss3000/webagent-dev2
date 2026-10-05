//! Separate operational archive; never reads encrypted application databases or keys.
use std::{path::Path, sync::{OnceLock, atomic::{AtomicU64, Ordering}}, time::Duration};
use serde_json::{json, Value};
use sqlx::{sqlite::{SqliteConnectOptions, SqlitePoolOptions}, SqlitePool};
use tokio::sync::mpsc;
use axum::{extract::Query, routing::get, Json, Router};
static RECORDER: OnceLock<Recorder> = OnceLock::new();
struct Recorder { pool: SqlitePool, tx: mpsc::Sender<Value>, dropped: AtomicU64, write_errors: AtomicU64 }
const MAX_ROWS: i64 = 200000;
const RETENTION_SECONDS: i64 = 7 * 86400;
pub(crate) async fn init(root: &Path) {
    if std::env::var("V3_DIAGNOSTICS_ENABLED").as_deref() == Ok("false") { return; }
    match open(root).await {
        Ok(pool) => {
            let (tx, mut rx) = mpsc::channel::<Value>(4096);
            if RECORDER.set(Recorder {pool:pool.clone(),tx,dropped:AtomicU64::new(0),write_errors:AtomicU64::new(0)}).is_err() {return;}
            tokio::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if persist(&pool, &event).await.is_err() {
                        RECORDER.get().unwrap().write_errors.fetch_add(1, Ordering::Relaxed);
                        tracing::error!("Diagnostic archive write failed; event lost");
                    }
                }
            });
        }
        Err(_) => tracing::error!("Diagnostic archive unavailable; application continues without capture"),
    }
}
async fn open(root: &Path) -> anyhow::Result<SqlitePool> {
    let dir=root.join("diagnostics"); tokio::fs::create_dir_all(&dir).await?;
    #[cfg(unix)] {
        use std::os::unix::fs::PermissionsExt;
        tokio::fs::set_permissions(&dir,std::fs::Permissions::from_mode(0o700)).await?;
    }
    let options=SqliteConnectOptions::new().filename(dir.join("logs.db")).create_if_missing(true)
        .journal_mode(sqlx::sqlite::SqliteJournalMode::Wal).busy_timeout(Duration::from_secs(2));
    let pool=SqlitePoolOptions::new().max_connections(2).connect_with(options).await?;
    sqlx::query("CREATE TABLE IF NOT EXISTS diagnostics (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, category TEXT NOT NULL, run_id TEXT, session_id TEXT, payload TEXT NOT NULL)").execute(&pool).await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS diagnostics_ts ON diagnostics(ts)").execute(&pool).await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS diagnostics_run ON diagnostics(run_id,id)").execute(&pool).await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS diagnostics_session ON diagnostics(session_id,id)").execute(&pool).await?;
    Ok(pool)
}
async fn persist(pool:&SqlitePool,event:&Value)->anyhow::Result<()> {
    let mut tx=pool.begin().await?;
    sqlx::query("INSERT INTO diagnostics(ts,category,run_id,session_id,payload) VALUES(?,?,?,?,?)")
        .bind(crate::now() as i64).bind(event["category"].as_str().unwrap_or("server"))
        .bind(event["run_id"].as_str()).bind(event["session_id"].as_str())
        .bind(event.to_string()).execute(&mut *tx).await?;
    // Short indexed deletes keep retention bounded across API and loop processes.
    sqlx::query("DELETE FROM diagnostics WHERE id IN (SELECT id FROM diagnostics WHERE ts < ? LIMIT 1000)")
        .bind(crate::now() as i64-RETENTION_SECONDS).execute(&mut *tx).await?;
    sqlx::query("DELETE FROM diagnostics WHERE id <= COALESCE((SELECT id FROM diagnostics ORDER BY id DESC LIMIT 1 OFFSET ?),0)")
        .bind(MAX_ROWS).execute(&mut *tx).await?;
    tx.commit().await?; Ok(())
}
fn emit(event:Value) {
    if let Some(r)=RECORDER.get() {
        if r.tx.try_send(event).is_err() {r.dropped.fetch_add(1,Ordering::Relaxed);}
    }
}
fn identifier(value:&Value)->Value {
    value.as_str().filter(|s|s.len()<=128 && !s.is_empty() && s.bytes().all(|b|b.is_ascii_alphanumeric() || b"_-:.".contains(&b)))
        .map(|s|json!(s)).unwrap_or(Value::Null)
}
fn failure_event(run:&Value,error:&anyhow::Error,action:&str)->Value {
    let failure=error.downcast_ref::<crate::provider::ProviderFailure>();
    json!({"category":"provider","level":"error","message":"Model request failed",
        "run_id":identifier(&run["id"]),"session_id":identifier(&run["session_id"]),
        "provider":serde_json::from_value::<crate::provider::ProviderId>(run["provider_selection"]["provider"].clone()).ok(),
        "model":identifier(&run["provider_selection"]["model"]),
        "kind":failure.map(|f|f.kind),"response_started":failure.map(|f|f.response_started),
        "http_status":failure.and_then(|f|f.http_status),
        "transport":failure.and_then(|f|f.transport.clone()),
        "action":match action {"retry"=>"retry","backup"=>"backup",_=>"terminal"},
        "failed_candidate_index":run["failed_candidate_index"].as_u64(),
        "next_candidate_index":run["failover_state"]["candidate_index"].as_u64(),
        "retries":run["failover_state"]["retries"].as_u64()})
}
pub(crate) fn failure(run:&Value,error:&anyhow::Error,action:&str) {emit(failure_event(run,error,action));}
pub(crate) fn backend_error() {emit(json!({"category":"server","level":"error","message":"Request storage or execution operation failed"}));}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Filter {run_id:Option<String>,session_id:Option<String>,before:Option<i64>,limit:Option<i64>}
async fn read(Query(filter):Query<Filter>)->crate::Api {
    let Some(r)=RECORDER.get() else {return Ok(Json(json!({"enabled":false,"records":[]})));};
    for id in [&filter.run_id,&filter.session_id].into_iter().flatten() {
        if identifier(&json!(id)).is_null() {return Err(crate::bad("Invalid diagnostic filter"));}
    }
    let rows:Vec<(i64,i64,String)>=sqlx::query_as("SELECT id,ts,payload FROM diagnostics WHERE (? IS NULL OR run_id=?) AND (? IS NULL OR session_id=?) AND id < ? ORDER BY id DESC LIMIT ?")
        .bind(&filter.run_id).bind(&filter.run_id).bind(&filter.session_id).bind(&filter.session_id)
        .bind(filter.before.unwrap_or(i64::MAX)).bind(filter.limit.unwrap_or(100).clamp(1,500))
        .fetch_all(&r.pool).await.map_err(|_|crate::ApiError(axum::http::StatusCode::SERVICE_UNAVAILABLE,"Diagnostic archive unavailable".into()))?;
    Ok(Json(json!({"enabled":true,"dropped_this_process":r.dropped.load(Ordering::Relaxed),
        "write_errors_this_process":r.write_errors.load(Ordering::Relaxed),
        "records":rows.into_iter().map(|(id,ts,payload)|json!({"id":id,"ts":ts,"event":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null)})).collect::<Vec<_>>()})))
}
// Merged only beneath the existing browser authentication and installation-admin layer.
pub(crate) fn router()->Router<crate::App> {Router::new().route("/admin/diagnostics",get(read))}
#[cfg(test)] mod tests {
    use super::*;
    #[tokio::test] async fn operational_archive_requires_installation_admin() {
        use tower::ServiceExt;
        use axum::{body::Body, http::{Request,StatusCode}};
        let (_root,app)=crate::runtime_tests::fixture().await;
        let uri="/api/v3/admin/diagnostics";
        let req=|cookie:Option<String>| {
            let mut b=Request::builder().uri(uri);
            if let Some(c)=cookie {b=b.header("cookie",c);}
            b.body(Body::empty()).unwrap()
        };
        assert_eq!(crate::api::router(app.clone()).oneshot(req(None)).await.unwrap().status(),StatusCode::UNAUTHORIZED);
        let member=app.store.create_user_account("diagnostics-member","fixture-password-hash").await.unwrap();
        app.store.update_account_access(member["user_id"].as_str().unwrap(),1,crate::access::AccountStatus::Active,crate::access::Role::User,"basic").await.unwrap();
        let member=app.store.account_by_id(member["user_id"].as_str().unwrap()).await.unwrap().unwrap();
        for (user,generation,expected) in [
            ("admin".to_owned(),"fixture-generation".to_owned(),StatusCode::OK),
            (member["user_id"].as_str().unwrap().to_owned(),member["generation"].as_str().unwrap().to_owned(),StatusCode::FORBIDDEN)] {
            let token=crate::uuid();
            app.store.put(&format!("auth:{}",crate::digest(&json!(token))),
                &json!({"user_id":user,"expires":crate::now()+3600,"generation":generation,"csrf":"fixture"})).await.unwrap();
            assert_eq!(crate::api::router(app.clone()).oneshot(req(Some(format!("v3_session={token}")))).await.unwrap().status(),expected);
        }
    }
    #[test] fn private_error_and_run_fields_never_escape() {
        let error=anyhow::anyhow!("SECRET https://provider.invalid/?token=SECRET");
        let run=json!({"id":"run-fixture","session_id":"session-fixture","input":"SECRET","provider_selection":{"provider":"invalid-SECRET","model":"SECRET with spaces"},"credential_ref":"SECRET"});
        let event=failure_event(&run,&error,"SECRET");
        assert!(!event.to_string().contains("SECRET"));assert_eq!(event["action"],"terminal");
    }
    #[tokio::test] async fn archive_survives_reopen_and_prunes() {
        let root=tempfile::tempdir().unwrap();let pool=open(root.path()).await.unwrap();
        persist(&pool,&json!({"category":"provider","run_id":"fixture","message":"fixed"})).await.unwrap();
        sqlx::query("UPDATE diagnostics SET ts=0").execute(&pool).await.unwrap();
        persist(&pool,&json!({"category":"server","message":"new"})).await.unwrap();
        pool.close().await;let pool=open(root.path()).await.unwrap();
        let count:i64=sqlx::query_scalar("SELECT COUNT(*) FROM diagnostics").fetch_one(&pool).await.unwrap();assert_eq!(count,1);
    }
}

// Only matched route templates, never raw paths, queries, headers or response bodies.
pub(crate) async fn http_errors(request:axum::extract::Request,next:axum::middleware::Next)->axum::response::Response {
    let route=request.extensions().get::<axum::extract::MatchedPath>().map(|p|p.as_str().to_owned());
    let method=request.method().as_str().to_owned();
    let response=next.run(request).await;
    if response.status().is_client_error() || response.status().is_server_error() {
        emit(json!({"category":"http","level":if response.status().is_server_error(){"error"}else{"warning"},
            "message":"HTTP request rejected or failed","status":response.status().as_u16(),
            "route":route,"method":match method.as_str(){"GET"|"POST"|"PUT"|"PATCH"|"DELETE"|"HEAD"|"OPTIONS"=>method.as_str(),_=>"other"}}));
    }
    response
}
