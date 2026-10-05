//! Installation catalog lifecycle. Browser authentication and CSRF are inherited from api::router.
use crate::{App, Api, ApiError, platform_catalog::{Entry, Mutation}};
use axum::{extract::{State, Path}, Extension, Json, Router, routing::{get, post}};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Draft { expected_revision:u64, name:String, entries:BTreeMap<String,Entry> }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fence { expected_revision:u64 }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Rollback { expected_revision:u64, revision:u64 }
async fn list(State(app):State<App>, Extension(identity):Extension<Value>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    Ok(Json(json!({"rosters":app.store.platform_rosters().await?})))
}
async fn history(State(app):State<App>, Extension(identity):Extension<Value>, Path(id):Path<String>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let roster=app.store.platform_roster(&id).await?
        .ok_or_else(||ApiError(axum::http::StatusCode::NOT_FOUND,"Catalog not found".into()))?;
    Ok(Json(json!({"history":roster.history,"published_revision":roster.published_revision})))
}
async fn read(State(app):State<App>, Extension(identity):Extension<Value>, Path(id):Path<String>) -> Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let roster=app.store.platform_roster(&id).await?
        .ok_or_else(||ApiError(axum::http::StatusCode::NOT_FOUND,"Catalog not found".into()))?;
    Ok(Json(json!({"roster":roster})))
}
async fn mutate(app:&App, identity:&Value, id:&str, expected:u64, operation:Mutation)->Api {
    let _guard=app.gate.lock().await;
    crate::phase3_api::administrator(app,identity).await?;
    if !crate::access::valid_id(id) {return Err(crate::bad("Invalid catalog identity"));}
    let current=app.store.platform_roster(id).await?;
    if current.as_ref().map_or(0,|r|r.revision)!=expected {
        return Err(crate::conflict("Catalog revision changed; reload before saving"));
    }
    // Validate on a private copy before persistence; immutable history is never client writable.
    match &operation {
        Mutation::Draft{name,entries} => {
            let mut probe=current.clone().unwrap_or(crate::platform_catalog::Roster{
                id:id.into(),name:name.clone(),revision:1,draft:BTreeMap::new(),
                published_revision:None,history:vec![],retired:false});
            if probe.retired {return Err(crate::bad("Catalog retired"));}
            probe.name=name.clone();probe.draft=entries.clone();
            probe.validate().map_err(|_|crate::bad("Invalid catalog metadata"))?;
        }
        Mutation::Publish{rollback} => {
            let mut probe=current.clone().ok_or_else(||crate::bad("Save a draft first"))?;
            probe.publish(expected,*rollback).map_err(|_|crate::bad("Publication unavailable"))?;
        }
        Mutation::Retire => {
            if id=="base" || current.is_none() {return Err(crate::bad("Catalog cannot retire"));}
        }
    }
    let roster=app.store.mutate_platform_roster(id,expected,operation,
        identity["_user_id"].as_str().unwrap_or("")).await?;
    Ok(Json(json!({"roster":roster})))
}
async fn draft(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(input):Json<Draft>)->Api {
    mutate(&app,&identity,&id,input.expected_revision,Mutation::Draft{name:input.name,entries:input.entries}).await
}
async fn publish(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(input):Json<Fence>)->Api {
    mutate(&app,&identity,&id,input.expected_revision,Mutation::Publish{rollback:None}).await
}
async fn rollback(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(input):Json<Rollback>)->Api {
    mutate(&app,&identity,&id,input.expected_revision,Mutation::Publish{rollback:Some(input.revision)}).await
}
async fn retire(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(input):Json<Fence>)->Api {
    mutate(&app,&identity,&id,input.expected_revision,Mutation::Retire).await
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyUpdate { expected_revision:u64, api_key:Option<String> }
async fn credential(State(app):State<App>,Extension(identity):Extension<Value>,
    Path((id,entry_id)):Path<(String,String)>,Json(input):Json<KeyUpdate>)->Api {
    let _guard=app.gate.lock().await;
    crate::phase3_api::administrator(&app,&identity).await?;
    if !crate::access::valid_id(&id) || !crate::access::valid_id(&entry_id) {
        return Err(crate::bad("Invalid catalog identity"));
    }
    let roster=app.store.platform_roster(&id).await?
        .ok_or_else(||crate::bad("Catalog not found"))?;
    if roster.revision!=input.expected_revision {return Err(crate::conflict("Catalog revision changed"));}
    let entry=roster.draft.get(&entry_id).ok_or_else(||crate::bad("Draft entry not found"))?;
    if roster.retired || !entry.transport.uses_api_key()
        || input.api_key.as_ref().is_some_and(|key|key.is_empty() || key.len()>4096
            || !key.bytes().all(|b|b.is_ascii_graphic())) {
        return Err(crate::bad("Invalid credential input or unavailable transport"));
    }
    app.store.platform_credential_set(&id,&entry_id,input.expected_revision,
        input.api_key.as_deref(),identity["_user_id"].as_str().unwrap_or("")).await?;
    Ok(Json(json!({"saved":true,"revision":input.expected_revision+1})))
}
async fn validate(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let roster=app.store.platform_roster(&id).await?
        .ok_or_else(||crate::bad("Catalog not found"))?;
    let mut entries=Vec::new();
    for entry in roster.draft.values() {
        let configured=app.store.platform_credential_configured(&id,entry).await?;
        entries.push(json!({"entry_id":entry.id,"configured":configured,
            "warning":if configured {Value::Null} else {json!("Credential not configured")}}));
    }
    Ok(Json(json!({"revision":roster.revision,"entries":entries,
        "provider_acceptance_verified":false})))
}
/// Preview compares metadata only; it neither publishes nor validates provider acceptance.
async fn preview(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    crate::phase3_api::administrator(&app,&identity).await?;
    let roster=app.store.platform_roster(&id).await?.ok_or_else(||crate::bad("Catalog not found"))?;
    let published=roster.published().map(|snapshot|&snapshot.entries);
    let mut added=Vec::new();let mut changed=Vec::new();let mut removed=Vec::new();
    for (id,entry) in &roster.draft {
        match published.and_then(|entries|entries.get(id)) {
            None=>added.push(id.clone()),Some(old) if old!=entry=>changed.push(id.clone()),_=>{}
        }
    }
    if let Some(entries)=published {for id in entries.keys() {if !roster.draft.contains_key(id) {removed.push(id.clone());}}}
    let tiers:Vec<_>=app.store.entitlement_tiers().await?.into_iter()
        .filter(|tier|tier.model_policy.as_ref().is_some_and(|policy|policy.roster_id.as_deref()==Some(&roster.id)))
        .map(|tier|tier.id).collect();
    Ok(Json(json!({"revision":roster.revision,"published_revision":roster.published_revision,
        "retired":roster.retired,"added_entry_ids":added,"changed_entry_ids":changed,
        "removed_entry_ids":removed,"bound_tier_ids":tiers,"provider_acceptance_verified":false})))
}
pub fn router()->Router<App> {
    Router::new().route("/",get(list)).route("/{id}",get(read).put(draft))
        .route("/{id}/history",get(history))
        .route("/{id}/validate",post(validate))
        .route("/{id}/preview",post(preview))
        .route("/{id}/entries/{entry_id}/credential",axum::routing::put(credential))
        .route("/{id}/publish",post(publish)).route("/{id}/rollback",post(rollback))
        .route("/{id}/retire",post(retire))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn preview_is_metadata_only_and_does_not_publish() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let admin=json!({"_user_id":"admin"});
        let entry=Entry{id:"model".into(),display_name:"First".into(),upstream_model:"deepseek-chat".into(),
            transport:crate::model_selection::Transport::Deepseek,capabilities:std::collections::BTreeSet::from(["text".into()])};
        let entries=BTreeMap::from([("model".into(),entry.clone())]);
        let _=mutate(&app,&admin,"team",0,Mutation::Draft{name:"Team".into(),entries}).await.unwrap();
        let Json(initial)=preview(State(app.clone()),Extension(admin.clone()),Path("team".into())).await.unwrap();
        assert_eq!(initial["added_entry_ids"],json!(["model"]));
        assert!(initial["published_revision"].is_null());
        let _=mutate(&app,&admin,"team",1,Mutation::Publish{rollback:None}).await.unwrap();
        let mut changed=entry;changed.display_name="Changed".into();
        let _=mutate(&app,&admin,"team",2,Mutation::Draft{name:"Team".into(),entries:BTreeMap::from([("model".into(),changed)])}).await.unwrap();
        let Json(result)=preview(State(app.clone()),Extension(admin),Path("team".into())).await.unwrap();
        assert_eq!(result["changed_entry_ids"],json!(["model"]));
        assert_eq!(result["published_revision"],2);
        assert_eq!(app.store.platform_roster("team").await.unwrap().unwrap().revision,3);
        assert!(!result.to_string().contains("credential_ref"));
        assert!(preview(State(app),Extension(json!({"_user_id":"unknown"})),Path("team".into())).await.is_err());
    }
    #[tokio::test]
    async fn lifecycle_requires_live_admin_and_fences_history() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let admin=json!({"_user_id":"admin"});
        let Json(saved)=mutate(&app,&admin,"base",0,Mutation::Draft{name:"Base".into(),entries:BTreeMap::new()}).await.unwrap();
        assert_eq!(saved["roster"]["revision"],1);
        assert!(mutate(&app,&admin,"base",0,Mutation::Draft{name:"Stale".into(),entries:BTreeMap::new()}).await.is_err());
        let _ = mutate(&app,&admin,"base",1,Mutation::Publish{rollback:None}).await.unwrap();
        assert!(mutate(&app,&admin,"base",2,Mutation::Retire).await.is_err());
        assert!(read(State(app.clone()),Extension(json!({"_user_id":"unknown"})),Path("base".into())).await.is_err());
        let Json(loaded)=read(State(app),Extension(admin),Path("base".into())).await.unwrap();
        assert_eq!(loaded["roster"]["published_revision"],2);
        assert!(serde_json::from_value::<Draft>(json!({"expected_revision":2,"name":"Base","entries":{},"history":[]})).is_err());
    }
}
