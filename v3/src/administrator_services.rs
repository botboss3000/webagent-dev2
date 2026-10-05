//! Poll content-free repository notices and renew explicitly admitted host jobs.
//! This does not execute model instructions or create assistant replies.
use crate::*;
async fn host_jobs(app:&App)->Result<()> {
    for record in app.store.list("administrator_host:").await?.into_iter().take(32) {
        let Some(sid)=record["session_id"].as_str() else {continue;};
        let Some(rid)=record["run_id"].as_str() else {continue;};
        let Some(name)=record["tool_name"].as_str() else {continue;};
        let fresh=tools::administrator::host_fingerprint(app,sid,rid,name).await;
        let valid=fresh.as_ref().is_ok_and(|v|record["access_fingerprint"]==*v);
        let trusted=json!({"user_id":app.store.current_user_id(),"session_id":sid,
            "lease_until":now()+30,"access_fingerprint":record["access_fingerprint"]});
        let command=if valid{"administrator_host_lease"}else{"administrator_host_revoke"};
        let _=tokio::time::timeout(Duration::from_secs(2),tools::execute_trusted(command,&json!({}),sid,
            std::path::Path::new("/run/v3/build/build.sock"),Some(trusted),CancellationToken::new())).await;
        if !valid{app.store.delete(&format!("administrator_host:{sid}")).await?;}
    }
    Ok(())
}
async fn repository_notices(app:&App)->Result<()> {
    let mut sessions=app.store.list("session:").await?;
    sessions.sort_by_key(|s|std::cmp::Reverse(s["created_at"].as_u64().unwrap_or(0)));
    for session in sessions.into_iter().take(32) {
        let Some(sid)=session["id"].as_str().filter(|s|uuid::Uuid::parse_str(s).is_ok()) else {continue;};
        let agent=session["agent_id"].as_str().unwrap_or("shared_default");
        let mut scoped=app.clone();scoped.store=app.store.for_agent(agent).await?;
        let (policy,_,visibility,state)=ability_controls(&scoped,sid).await?;
        if visibility["settings"]["git_control"]["notify_repo_changes"]!=true {continue;}
        let mode=session_mode(&scoped,sid).await?;
        let access=scoped.store.live_access(scoped.store.current_user_id(),agent).await?;
        // Notifications are an opt-in automatic observation, never an approval
        // bypass for a caller whose Git tool remains gated.
        if !access.admitted() || live_tool_decision(&scoped,&access,"git_tool",&policy,&visibility,
            &state,&json!(["git_control"]),mode["mode"]=="read_only").permission!=access::Permission::Auto {continue;}
        let output=tokio::time::timeout(Duration::from_secs(5),tools::execute_trusted(
            "administrator_git_watch",&json!({}),sid,&app.tool_socket,None,CancellationToken::new())).await;
        let Ok(Ok(value))=output else {continue;};
        if value["status"]!="success" {continue;}
        let snapshot=&value["output"];
        let key=format!("administrator_notice:{sid}");
        let old=app.store.get(&key).await?.unwrap_or(Value::Null);
        if old["snapshot_sha256"]==snapshot["snapshot_sha256"] {continue;}
        // Recheck policy after I/O. Revocation can withdraw an observation.
        let current=scoped.store.live_access(scoped.store.current_user_id(),agent).await?;
        let (next_policy,_,next_visibility,next_state)=ability_controls(&scoped,sid).await?;
        let next_mode=session_mode(&scoped,sid).await?;
        if next_visibility["settings"]["git_control"]["notify_repo_changes"]!=true
            || live_tool_decision(&scoped,&current,"git_tool",&next_policy,&next_visibility,
                &next_state,&json!(["git_control"]),next_mode["mode"]=="read_only").permission!=access::Permission::Auto {continue;}
        let notice=json!({"session_id":sid,"agent_id":agent,"snapshot_sha256":snapshot["snapshot_sha256"],
            "dirty":snapshot["dirty"],"ahead":snapshot["ahead"],"observed_at":now(),
            "notification":if snapshot["notify"]==true {"Repository has new uncommitted changes or unpublished commits. No action was taken."}else{""}});
        scoped.store.atomic_docs_event(vec![(key,notice.clone())],sid,"repository_notice",&notice).await?;
    }
    Ok(())
}

async fn host_watch(app:App) {
    while app.ready.load(Ordering::Acquire) && !app.stop.is_cancelled() {
        if let Ok(users)=app.store.all_user_ids().await {
            for uid in users {
                if let Ok(store)=app.store.for_user(&uid).await {
                    let mut caller=app.clone();caller.store=store;let _=host_jobs(&caller).await;
                }
            }
        }
        tokio::select!{_=app.stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(5))=>{}}
    }
}
async fn notice_watch(app:App) {
    while app.ready.load(Ordering::Acquire) && !app.stop.is_cancelled() {
        if let Ok(users)=app.store.all_user_ids().await {
            for uid in users {
                if let Ok(store)=app.store.for_user(&uid).await {
                    let mut caller=app.clone();caller.store=store;let _=repository_notices(&caller).await;
                }
            }
        }
        tokio::select!{_=app.stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(10))=>{}}
    }
}
pub(crate) async fn watch(app:App) {tokio::join!(host_watch(app.clone()),notice_watch(app));}
