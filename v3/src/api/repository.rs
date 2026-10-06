//! Tenant repository connections. Private tokens never enter API projections.
use crate::{App, Api, ApiError};
use axum::{extract::{Path, State}, http::StatusCode, Extension, Json};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Connection {
    id: String, url: String, username: String, credential_ref: String,
    owner_id: String, revoked: bool, revision: u64,
}
#[derive(Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Catalog {revision: u64, connections: BTreeMap<String, Connection>}
fn denied() -> ApiError {ApiError(StatusCode::FORBIDDEN,"Repository configuration unavailable".into())}
fn doc(agent: &str) -> String {format!("agent:{agent}:repository_connections")}
async fn authority(app: &App, identity: &Value, agent: &str, write: bool) -> Result<String, ApiError> {
    let id=identity["_user_id"].as_str().ok_or_else(denied)?;
    let account=app.store.account_by_id(id).await?.ok_or_else(denied)?;
    let principal=crate::access::Principal::from_account(&account)?;
    if !principal.active() {return Err(denied());}
    let rights=app.store.agent_authority(id,agent).await.map_err(|_|denied())?;
    if write && rights["can_manage"]!=true || !write && rights["can_manage"]!=true
        && !app.store.live_access(id,agent).await.is_ok_and(|a|a.admitted()) {return Err(denied());}
    Ok(id.to_owned())
}
fn normalized_url(raw: &str) -> Result<String, ApiError> {
    if raw.len()>2048 || raw.trim()!=raw || raw.chars().any(char::is_control) {return Err(crate::bad("Invalid repository URL"));}
    let mut url=reqwest::Url::parse(raw).map_err(|_|crate::bad("Invalid repository URL"))?;
    if url.scheme()!="https" || url.host_str().is_none() || !url.username().is_empty()
        || url.password().is_some() || url.query().is_some() || url.fragment().is_some()
        || url.path()=="/" || url.path().contains('%') {return Err(crate::bad("Use an HTTPS repository URL without credentials, query or fragment"));}
    let path=url.path().trim_end_matches('/').to_owned();
    url.set_path(&path);
    Ok(url.to_string())
}
async fn catalog(app: &App, agent: &str) -> Result<Catalog, ApiError> {
    let store=app.store.for_agent(agent).await?;
    let value=store.get(&doc(agent)).await?;
    value.map(serde_json::from_value).transpose().map_err(anyhow::Error::from).map_err(Into::into)
        .map(|v|v.unwrap_or_default())
}
async fn projection(app: &App, agent: &str, value: &Catalog, caller: &str) -> Result<Value, ApiError> {
    let rights=app.store.agent_authority(caller,agent).await?;
    let mut connections=Vec::new();
    for connection in value.connections.values() {
        let configured=!connection.revoked && (connection.credential_ref.is_empty()
            || app.store.get(&connection.credential_ref).await?.is_some());
        connections.push(json!({"id":connection.id,"url":connection.url,"username":connection.username,
            "owner_id":connection.owner_id,"revoked":connection.revoked,"revision":connection.revision,
            "configured":configured,"authenticated":!connection.credential_ref.is_empty() && configured,
            "can_edit":rights["can_manage"]==true && connection.owner_id==caller}));
    }
    Ok(json!({"agent_id":agent,"revision":value.revision,"can_manage":rights["can_manage"],"connections":connections}))
}
pub(crate) async fn read(State(app): State<App>, Extension(identity): Extension<Value>, Path(agent): Path<String>) -> Api {
    let caller=authority(&app,&identity,&agent,false).await?;
    Ok(Json(projection(&app,&agent,&catalog(&app,&agent).await?,&caller).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Body {
    expected_revision: u64, url: String, username: Option<String>, token: Option<String>, revoked: bool,
}
pub(crate) async fn put(State(app): State<App>, Extension(identity): Extension<Value>,
    Path((agent,id)): Path<(String,String)>, Json(body): Json<Body>) -> Api {
    let _guard=app.gate.lock().await;
    let caller=authority(&app,&identity,&agent,true).await?;
    if id.is_empty() || id.len()>80 || !id.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_') {
        return Err(crate::bad("Invalid repository connection ID"));
    }
    let url=normalized_url(&body.url)?;
    let username=body.username.unwrap_or_else(||"git".to_owned());
    if username.is_empty() || username.len()>128 || !username.bytes().all(|b|b.is_ascii_graphic() && !b"'\"\\@:".contains(&b)) {
        return Err(crate::bad("Invalid repository username"));
    }
    if body.token.as_ref().is_some_and(|v|v.len()<8 || v.len()>512 || !v.bytes().all(|b|b.is_ascii_graphic())) {
        return Err(crate::bad("Use an 8–512 character printable repository token"));
    }
    let store=app.store.for_agent(&agent).await?;
    // Capture the exact sealed value, not just a decrypted revision, to fence
    // other processes performing writes to this agent's encrypted catalog.
    let old_blob: Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
        .bind(doc(&agent)).fetch_optional(&store.agent_pool).await.map_err(anyhow::Error::from)?;
    let mut next: Catalog=old_blob.as_ref().map(|b|store.unseal(b)).transpose()?
        .map(serde_json::from_value).transpose().map_err(anyhow::Error::from)?.unwrap_or_default();
    if next.revision!=body.expected_revision {return Err(crate::conflict("Repository connections changed; reload before saving"));}
    let previous=next.connections.get(&id);
    if previous.is_some_and(|c|c.owner_id!=caller) {return Err(denied());}
    if previous.is_none() && next.connections.len()>=8 {return Err(crate::bad("Agent repository connection limit reached"));}
    let mut reference=previous.filter(|c|c.url==url && c.username==username).map(|c|c.credential_ref.clone()).unwrap_or_default();
    if let Some(token)=body.token {
        reference=format!("repository_credential:{}",crate::uuid());
        // The private app credential is minted before the agent CAS. A race
        // can leave an unreachable encrypted secret, never a visible partial
        // connection or a token linked to another agent's catalog.
        app.store.put(&reference,&json!({"token":token,"agent_id":agent,"connection_id":id,"owner_id":caller})).await?;
    }
    let revision=previous.map_or(Ok(1),|c|c.revision.checked_add(1).ok_or_else(||crate::bad("Repository revision exhausted")))?;
    next.connections.insert(id.clone(),Connection {id:id.clone(),url,username,credential_ref:reference,
        owner_id:caller.clone(),revoked:body.revoked,revision});
    next.revision=next.revision.checked_add(1).ok_or_else(||crate::bad("Repository revision exhausted"))?;
    let sealed=store.seal(&serde_json::to_value(&next).map_err(anyhow::Error::from)?)?;
    let mut tx=store.agent_pool.begin().await.map_err(anyhow::Error::from)?;
    let affected=if let Some(old)=old_blob {
        sqlx::query("UPDATE docs SET value=? WHERE key=? AND value=?").bind(sealed).bind(doc(&agent)).bind(old)
            .execute(&mut *tx).await.map_err(anyhow::Error::from)?.rows_affected()
    } else {
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING").bind(doc(&agent)).bind(sealed)
            .execute(&mut *tx).await.map_err(anyhow::Error::from)?.rows_affected()
    };
    if affected!=1 {return Err(crate::conflict("Repository connections changed; reload before saving"));}
    sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
        .bind(format!("agent:{agent}:repository_audit:{}",next.revision))
        .bind(store.seal(&json!({"actor_id":caller,"connection_id":id,"revision":next.revision,"revoked":body.revoked}))?)
        .execute(&mut *tx).await.map_err(anyhow::Error::from)?;
    tx.commit().await.map_err(anyhow::Error::from)?;
    crate::phase3_api::quiesce_agent(&app,&agent).await?;
    Ok(Json(projection(&app,&agent,&next,&caller).await?))
}
/// Called only after Rust's live tool decision. This trusted payload is not a
/// model result or durable history item. The worker matches the exact remote.
pub(crate) async fn transport(app: &App, sid: &str) -> anyhow::Result<Value> {
    let agent=crate::session_agent_id(app,sid).await?;
    transport_for_agent(app,&agent).await
}
/// The administrator File Manager uses the default agent's independent V3
/// repository connections for a live-checkout Git operation.
pub(crate) async fn transport_for_agent(app: &App, agent: &str) -> anyhow::Result<Value> {
    let caller=app.store.current_user_id();
    let access=app.store.live_access(caller,agent).await?;
    anyhow::ensure!(access.admitted(),"Repository transport access revoked");
    let value=catalog(app,agent).await.map_err(|_|anyhow::anyhow!("Repository catalog unavailable"))?;
    let mut connections=Vec::new();
    for record in value.connections.values().filter(|c|!c.revoked) {
        let token=if record.credential_ref.is_empty() {None} else {
            let secret=app.store.get(&record.credential_ref).await?.ok_or_else(||anyhow::anyhow!("Repository credential unavailable"))?;
            anyhow::ensure!(secret["agent_id"]==agent && secret["connection_id"]==record.id && secret["owner_id"]==record.owner_id,
                "Repository credential scope changed");
            Some(secret["token"].as_str().ok_or_else(||anyhow::anyhow!("Repository credential invalid"))?.to_owned())
        };
        connections.push(json!({"id":record.id,"url":record.url,"username":record.username,
            "token":token,"credential_ref":record.credential_ref,"revision":record.revision}));
    }
    Ok(json!({"agent_id":agent,"revision":value.revision,"connections":connections}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn urls_are_https_and_do_not_embed_credentials() {
        assert_eq!(normalized_url("https://example.test/team/repo/").unwrap(),"https://example.test/team/repo");
        for raw in ["http://example.test/repo","https://u:p@example.test/repo","https://example.test/repo?token=x",
            "https://example.test/repo#x","ssh://example.test/repo","https://example.test/","https://example.test/a%2fb"] {
            assert!(normalized_url(raw).is_err(),"{raw}");
        }
    }
    #[tokio::test]
    async fn credentials_are_agent_scoped_rotated_and_never_projected() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let identity=json!({"_user_id":"admin"});
        let body=|revision,token:Option<&str>|Body {expected_revision:revision,url:"https://example.test/team/repo".into(),
            username:Some("git".into()),token:token.map(str::to_owned),revoked:false};
        let saved=put(State(app.clone()),Extension(identity.clone()),Path(("shared_default".into(),"origin".into())),Json(body(0,Some("fixture-token-only")))).await.unwrap();
        assert!(!saved.0.to_string().contains("fixture-token-only"));
        assert!(!saved.0.to_string().contains("credential_ref"));
        let old=catalog(&app,"shared_default").await.unwrap();
        let old_ref=old.connections["origin"].credential_ref.clone();
        assert_eq!(app.store.get(&old_ref).await.unwrap().unwrap()["token"],"fixture-token-only");
        assert!(matches!(put(State(app.clone()),Extension(identity.clone()),Path(("shared_default".into(),"origin".into())),Json(body(0,None))).await,
            Err(ApiError(StatusCode::CONFLICT,_))));
        let _ = put(State(app.clone()),Extension(identity),Path(("shared_default".into(),"origin".into())),Json(body(1,Some("rotated-fixture-token")))).await.unwrap();
        assert_ne!(catalog(&app,"shared_default").await.unwrap().connections["origin"].credential_ref,old_ref);
        let mut revoked=body(2,None);revoked.revoked=true;
        let _ = put(State(app.clone()),Extension(json!({"_user_id":"admin"})),Path(("shared_default".into(),"origin".into())),Json(revoked)).await.unwrap();
        let sid=crate::uuid();
        app.store.put(&format!("session:{sid}"),&json!({"id":sid,"agent_id":"shared_default"})).await.unwrap();
        assert_eq!(transport(&app,&sid).await.unwrap()["connections"],json!([]));
    }
    #[tokio::test]
    async fn delegated_manager_owns_only_created_connection_members_cannot_write() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let user=app.store.create_user_account("repository_delegate","fixture-password-hash").await.unwrap();
        let uid=user["user_id"].as_str().unwrap();
        app.store.update_account_access(uid,1,crate::access::AccountStatus::Active,crate::access::Role::User,"basic").await.unwrap();
        let agent = "repository-agent";
        let pool = crate::agent_storage::open_agent_pool(&app.store.root,agent).await.unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,'','openai_subscription','','active',?,1,1,1)")
            .bind(agent).bind(agent).bind(app.store.seal(&json!({"owner_id":"admin"})).unwrap())
            .execute(&pool).await.unwrap();
        pool.close().await;
        app.store.register_agent(agent,"admin").await.unwrap();
        app.store.set_agent_membership("admin",agent,crate::access::Relationship::Owner,0).await.unwrap();
        app.store.assign_agent_membership("admin",uid,"repository-agent",crate::access::Relationship::Member,true,0).await.unwrap();
        let body=|revision|Body {expected_revision:revision,url:"https://example.test/team/repo".into(),
            username:Some("git".into()),token:Some("fixture-token-only".into()),revoked:false};
        let _ = put(State(app.clone()),Extension(json!({"_user_id":"admin"})),Path(("repository-agent".into(),"origin".into())),Json(body(0))).await.unwrap();
        assert!(matches!(put(State(app.clone()),Extension(json!({"_user_id":uid})),Path(("repository-agent".into(),"origin".into())),Json(body(1))).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
        let saved=put(State(app.clone()),Extension(json!({"_user_id":uid})),Path(("repository-agent".into(),"delegate".into())),Json(body(1))).await.unwrap();
        assert!(!saved.0.to_string().contains("fixture-token-only"));
        assert!(!saved.0.to_string().contains("credential_ref"));
        let membership=app.store.agent_membership(uid,"repository-agent").await.unwrap().unwrap();
        app.store.assign_agent_membership("admin",uid,"repository-agent",crate::access::Relationship::Member,false,membership["revision"].as_u64().unwrap()).await.unwrap();
        assert!(read(State(app.clone()),Extension(json!({"_user_id":uid})),Path("repository-agent".into())).await.is_ok());
        assert!(matches!(put(State(app),Extension(json!({"_user_id":uid})),Path(("repository-agent".into(),"delegate".into())),Json(body(2))).await,
            Err(ApiError(StatusCode::FORBIDDEN,_))));
    }

}
