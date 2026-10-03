//! Compiled Administrator contracts. Model arguments never choose identity,
//! agent, workspace, credentials, host origin, or a Docker command.
use crate::*;
use anyhow::{bail,Result};
use serde_json::{json,Value};
use tokio_util::sync::CancellationToken;

pub fn schemas()->Vec<Value> {
    serde_json::from_str(include_str!("administrator-schemas.json"))
        .expect("compiled Administrator schemas")
}
pub fn owns(name:&str)->bool {
    schemas().iter().any(|s|s["name"]==name)
}
fn request_id(args:&Value)->Result<&str> {
    let value=args["request_id"].as_str().unwrap_or("");
    if value.is_empty() || value.len()>80 || !value.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_'||b==b'-') {
        bail!("Canonical bounded request_id required");
    }
    Ok(value)
}
async fn browser_authority(app:&App,sid:&str)->Result<Value> {
    let uid=app.store.current_user_id();
    let account=app.store.account_by_id(uid).await?.ok_or_else(||anyhow!("Caller unavailable"))?;
    let key=format!("administrator_browser:{sid}");
    if let Some(old)=app.store.get(&key).await? {
        if old["generation"]==account["generation"] && old["expires_at"].as_u64().unwrap_or(0)>now()+30 {
            return Ok(old);
        }
    }
    let token=uuid(); let expires=now()+600;
    app.store.put(&format!("auth:{}",digest(&json!(token))),&json!({
        "id":uuid(),"user":account["username"],"user_id":uid,"csrf":uuid(),
        "generation":account["generation"],"created_at":now(),"expires":expires,
        "user_agent":"V3 current-caller Administrator browser"})).await?;
    let trusted=json!({"session_id":sid,"user_id":uid,"cookie":token,
        "expires_at":expires,"generation":account["generation"],"agent_id":session_agent_id(app,sid).await?,"allowed_session_ids":[sid]});
    app.store.put(&key,&trusted).await?;Ok(trusted)
}
async fn worker(app:&App,sid:&str,name:&str,args:&Value,trusted:Option<Value>,cancel:CancellationToken)->Result<Value> {
    tools::execute_trusted(name,args,sid,&app.tool_socket,trusted,cancel).await
}
pub(crate) async fn host_fingerprint(app:&App,sid:&str,run_id:&str,name:&str)->Result<String> {
    let agent=session_agent_id(app,sid).await?;
    let mut scoped=app.clone();scoped.store=app.store.for_agent(&agent).await?;
    let run=scoped.run(run_id).await?;
    if run["session_id"]!=sid {bail!("Host job run scope changed");}
    let (policy,revision,visibility,state)=ability_controls(&scoped,sid).await?;
    let mode=session_mode(&scoped,sid).await?;
    let access=scoped.store.live_access(scoped.store.current_user_id(),&agent).await?;
    if !access.principal.administrator() || !access.admitted() || live_tool_decision(&scoped,&access,name,
        &policy,&visibility,&state,&run["loaded_abilities"],mode["mode"]=="read_only").permission==access::Permission::Deny {
        bail!("Host build authority revoked");
    }
    Ok(digest(&json!([scoped.store.current_user_id(),sid,agent,access.context_fingerprint(),
        policy,revision,visibility,state,mode,run["loaded_abilities"],scoped.abilities.live_version()])))
}
async fn host(app:&App,sid:&str,run_id:&str,name:&str,args:&Value,cancel:CancellationToken)->Result<Value> {
    let fingerprint=host_fingerprint(app,sid,run_id,name).await?;
    let trusted=json!({"user_id":app.store.current_user_id(),"session_id":sid,
        "lease_until":now()+30,"access_fingerprint":fingerprint});
    if matches!(name,"v3_build_image"|"v3_worker_submit") {
        app.store.put(&format!("administrator_host:{sid}"),&json!({"session_id":sid,
            "run_id":run_id,"tool_name":name,"access_fingerprint":fingerprint})).await?;
    }
    tools::execute_trusted(name,args,sid,std::path::Path::new("/run/v3/build/build.sock"),Some(trusted),cancel).await
}

fn workspace_worker_arguments(args:&Value)->Result<Value> {
    let object=args.as_object().ok_or_else(||anyhow!("Object required"))?;
    const KNOWN:&[&str]=&["request_id","action","path","offset","limit","patch","expected_review_sha256","timeout_seconds","summary"];
    if object.keys().any(|key|!KNOWN.contains(&key.as_str())) {bail!("Unknown workspace action argument");}
    let action=args["action"].as_str().unwrap_or("");
    let relevant:&[&str]=match action {
        "seed"|"review"=>&[],
        "read"=>&["path","offset","limit"],
        "patch"=>&["patch","expected_review_sha256"],
        "check"|"test"=>&["expected_review_sha256","timeout_seconds"],
        "propose"=>&["expected_review_sha256","summary"],
        _=>bail!("Unknown fixed workspace action"),
    };
    let mut projected=json!({"action":action});
    for key in relevant {
        if let Some(value)=object.get(*key).filter(|value|!value.is_null()) {
            projected[*key]=value.clone();
        }
    }
    Ok(projected)
}

async fn receipt_action(app:&App,sid:&str,run_id:&str,args:&Value,pending_id:&str,policy_version:u64,cancel:CancellationToken)->Result<Value> {
    let forwarded=workspace_worker_arguments(args)?;
    let id=request_id(args)?;
    let key=format!("administrator_receipt:{sid}:{id}");
    let fingerprint=digest(args);
    {
        let _guard=app.gate.lock().await;
        if let Some(old)=app.store.get(&key).await? {
            if old["request_sha256"]!=fingerprint {bail!("request_id already used with different arguments");}
            let interrupted=old["status"]=="executing" || old["status"]=="uncertain";
            return Ok(json!({"status":if interrupted{"uncertain"}else{old["status"].as_str().unwrap_or("error")},
                "uncertain":interrupted,"output":old,"replayed_receipt":true}));
        }
        app.store.put(&key,&json!({"request_id":id,"session_id":sid,
            "request_sha256":fingerprint,"status":"executing","created_at":now(),
            "note":"If interrupted, reconcile this receipt; never replay the mutation automatically"})).await?;
    }
    let action=args["action"].as_str().unwrap_or("");
    let result=if action=="propose" {
        propose_promotion(app,sid,pending_id,run_id,&json!({
            "expected_review_sha256":args["expected_review_sha256"],"summary":args["summary"]}),policy_version,cancel).await
    } else if matches!(action,"check"|"test") {
        worker(app,sid,"v3_cargo_task",&json!({"task":action,
            "expected_review_sha256":args["expected_review_sha256"],
            "timeout_seconds":args.get("timeout_seconds").filter(|value|!value.is_null()).cloned().unwrap_or(json!(600))}),None,cancel).await
    } else {
        worker(app,sid,"v3_workspace_action",&forwarded,None,cancel).await
    };
    let value=match result {
        Ok(value)=>value,
        Err(_)=>json!({"status":"uncertain","output":"Operation interrupted; inspect source and receipt before another request","uncertain":true}),
    };
    let receipt=json!({"request_id":id,"session_id":sid,"request_sha256":fingerprint,
        "status":value["status"],"result":value,"finished_at":now()});
    app.store.put(&key,&receipt).await?;
    Ok(json!({"status":receipt["status"],"uncertain":receipt["result"]["uncertain"]==true,"output":receipt}))
}
fn diagnostics_target<'a>(args:&'a Value,current:&'a str)->Option<&'a str> {
    match args.get("run_id") {
        None|Some(Value::Null)=>Some(current),
        Some(Value::String(value)) if value.is_empty() || value=="current"=>Some(current),
        Some(Value::String(value))=>uuid::Uuid::parse_str(value).ok()
            .filter(|id|id.to_string()==*value).map(|_|value.as_str()),
        _=>None,
    }
}
fn diagnostics_response(selected:Option<&Value>,sid:&str)->Value {
    match selected.filter(|run|run["session_id"].as_str()==Some(sid)) {
        Some(run)=>json!({"status":"success","output":task_progress::project(run)}),
        None=>json!({"status":"error","error_kind":"diagnostics_unavailable",
            "output":"Diagnostics unavailable in this session; omit run_id or use current"}),
    }
}

pub async fn execute(app:&App,sid:&str,run_id:&str,name:&str,args:&Value,pending_id:&str,policy_version:u64,cancel:CancellationToken)->Result<Value> {
    if cancel.is_cancelled(){bail!("Tool execution cancelled");}
    let agent=session_agent_id(app,sid).await?;
    let run=app.run(run_id).await?;
    if run["session_id"]!=sid || run["agent_id"].as_str().unwrap_or("shared_default")!=agent {bail!("Current run/session mismatch");}
    match name {
        "repository_access"=>repository_access(app,sid,args,cancel).await,
        "read_diagnostics"=>{
            match diagnostics_target(args,run_id) {
                Some(target) if target==run_id=>Ok(diagnostics_response(Some(&run),sid)),
                Some(target)=>{
                    let selected=app.run(target).await.ok();
                    Ok(diagnostics_response(selected.as_ref(),sid))
                },
                None=>Ok(diagnostics_response(None,sid)),
            }
        },
        "dev_browser_url"=>{
            let path=args["path"].as_str().filter(|value|!value.is_empty()).map(str::to_owned).unwrap_or(format!("/app?session_id={sid}"));
            if !valid_development_path(&path,sid,&agent){bail!("Development path unavailable");}
            Ok(json!({"status":"success","output":{"url":format!("https://dev.webagent.live{path}"),"authentication_required":true}}))
        },
        "login_as_current_user"|"login_as_current_user_v3"|"dev_browser_action"|"dev_browser_record"=>{
            worker(app,sid,name,args,Some(browser_authority(app,sid).await?),cancel).await
        },
        "v3_workspace_receipt"=>{
            let id=request_id(args)?;
            let receipt=app.store.get(&format!("administrator_receipt:{sid}:{id}")).await?
                .ok_or_else(||anyhow!("Receipt not found in current session"))?;
            Ok(json!({"status":receipt["status"],"uncertain":receipt["result"]["uncertain"]==true,"output":receipt}))
        },
        "v3_workspace_action"=>receipt_action(app,sid,run_id,args,pending_id,policy_version,cancel).await,
        "v3_build_status"|"v3_build_image"|"v3_worker_submit"|"v3_worker_job"=>host(app,sid,run_id,name,args,cancel).await,
        "git_tool"|"resolve_conflict"|"commit_and_push"=>{
            let trusted=crate::api::repository::transport(app,sid).await?;
            worker(app,sid,name,args,Some(trusted),cancel).await
        },
        _=>worker(app,sid,name,args,None,cancel).await,
    }
}
pub fn valid_development_path(path:&str,sid:&str,agent:&str)->bool {
    if path.len()>1024 || !path.starts_with('/') || path.starts_with("//") || path.contains('\\') || path.chars().any(char::is_control) {return false;}
    if path=="/app" || path.starts_with("/app?") {
        return reqwest::Url::parse(&format!("https://dev.webagent.live{path}")).is_ok_and(|url|
            url.query_pairs().all(|(key,value)|(key!="session_id"||value==sid)&&(key!="agent"||value==agent)));
    }
    path=="/api/v3/auth/me" || path.starts_with(&format!("/api/v3/sessions/{sid}/"))
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn routes_are_current_session_and_fixed_origin(){
        assert!(valid_development_path("/app?session_id=own","own","agent-a"));
        assert!(!valid_development_path("/app?session_id=other","own","agent-a"));
        assert!(!valid_development_path("//evil/app","own","agent-a"));
        assert!(!valid_development_path("/api/v3/sessions/other/","own","agent-a"));
        assert!(!valid_development_path("/app?session_id=own&session_id=other","own","agent-a"));
    }
}

async fn repository_access(app:&App,sid:&str,args:&Value,cancel:CancellationToken)->Result<Value> {
    if args.as_object().is_none_or(|m|m.keys().any(|k|k!="verify_remote") || m.get("verify_remote").is_some_and(|v|!v.is_boolean())) {
        bail!("repository_access accepts only verify_remote boolean");
    }
    let trusted=crate::api::repository::transport(app,sid).await?;
    let fingerprint=digest(&trusted);
    let verify=args["verify_remote"].as_bool().unwrap_or(true);
    let client=reqwest::Client::builder().timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none()).build()?;
    let mut rows=Vec::new();
    for connection in trusted["connections"].as_array().ok_or_else(||anyhow!("Repository configuration invalid"))? {
        let raw=connection["url"].as_str().unwrap_or("");
        let url=reqwest::Url::parse(raw)?;
        let parts=url.path().trim_matches('/').split('/').collect::<Vec<_>>();
        let mut metadata=json!({"verified":false,"reason":if verify{"provider_metadata_unavailable"}else{"not_requested"},
            "reported_roles":Value::Null,"push_permission":"unverified"});
        if verify && url.host_str()==Some("github.com") && parts.len()==2
            && parts.iter().all(|p|!p.is_empty() && p.bytes().all(|b|b.is_ascii_alphanumeric()||b"_.-".contains(&b))) {
            let repo=parts[1].strip_suffix(".git").unwrap_or(parts[1]);
            let mut request=client.get(format!("https://api.github.com/repos/{}/{}",parts[0],repo))
                .header("user-agent","WebAgent-v3-repository-metadata").header("accept","application/vnd.github+json");
            if let Some(token)=connection["token"].as_str(){request=request.bearer_auth(token);}
            let response=tokio::select!{_=cancel.cancelled()=>bail!("Repository metadata cancelled"),r=request.send()=>r};
            if let Ok(response)=response {
                let status=response.status().as_u16();
                if response.content_length().is_some_and(|v|v>65536){metadata["reason"]=json!("metadata_too_large");}
                else {
                    let mut response=response;let mut bytes=Vec::new();
                    loop {
                        let chunk=tokio::select!{_=cancel.cancelled()=>bail!("Repository metadata cancelled"),r=response.chunk()=>r};
                        match chunk {Ok(Some(chunk)) if bytes.len()+chunk.len()<=65536=>bytes.extend_from_slice(&chunk),Ok(None)=>break,_=>{bytes.clear();break;}}
                    }
                    if status==200 {
                        if let Ok(value)=serde_json::from_slice::<Value>(&bytes) {
                            let roles=json!({"pull":value["permissions"]["pull"].as_bool(),"push":value["permissions"]["push"].as_bool(),"admin":value["permissions"]["admin"].as_bool()});
                            metadata=json!({"verified":true,"http_status":status,"reported_roles":roles,
                                "push_permission":"unverified","note":"Repository roles do not establish token write scope or branch protection permission"});
                        }
                    } else {metadata["http_status"]=json!(status);metadata["reason"]=json!("metadata_read_unverified");}
                }
            }
        }
        rows.push(json!({"connection_id":connection["id"],"url":raw,"credential_present":connection["token"].is_string(),"revision":connection["revision"],"metadata":metadata}));
    }
    if digest(&crate::api::repository::transport(app,sid).await?)!=fingerprint{bail!("Repository grants or credential references changed during read");}
    Ok(json!({"status":"success","output":{"agent_id":trusted["agent_id"],"revision":trusted["revision"],"connections":rows,
        "mutations_performed":false,"credentials_exposed":false}}))
}

#[cfg(test)]
mod workspace_argument_tests {
    use super::*;
    #[test]
    fn seed_projects_schema_optional_defaults_without_changing_receipt_input() {
        let raw=json!({"action":"seed","expected_review_sha256":"0".repeat(64),"limit":1,"offset":1,
            "patch":"","path":"","request_id":"seed-exact","summary":"","timeout_seconds":1});
        let before=digest(&raw);
        assert_eq!(workspace_worker_arguments(&raw).unwrap(),json!({"action":"seed"}));
        assert_eq!(digest(&raw),before);
        assert_eq!(workspace_worker_arguments(&json!({"action":"review","path":null})).unwrap(),json!({"action":"review"}));
    }
    #[test]
    fn read_preserves_pagination_and_rejects_unknown_keys() {
        assert_eq!(workspace_worker_arguments(&json!({"action":"read","path":"README.md","offset":2,"limit":1,"patch":""})).unwrap(),
            json!({"action":"read","path":"README.md","offset":2,"limit":1}));
        assert_eq!(workspace_worker_arguments(&json!({"action":"read","path":"README.md","offset":null,"limit":null})).unwrap(),
            json!({"action":"read","path":"README.md"}));
        assert!(workspace_worker_arguments(&json!({"action":"seed","command":"id"})).is_err());
    }
}

#[cfg(test)]
mod diagnostics_tests {
    use super::*;
    #[test]
    fn current_aliases_and_canonical_run_ids() {
        let current="11111111-1111-4111-8111-111111111111";
        for args in [json!({}),json!({"run_id":null}),json!({"run_id":""}),json!({"run_id":"current"})] {
            assert_eq!(diagnostics_target(&args,current),Some(current));
        }
        let explicit=json!({"run_id":"22222222-2222-4222-8222-222222222222"});
        assert_eq!(diagnostics_target(&explicit,current),explicit["run_id"].as_str());
        for args in [json!({"run_id":"not-a-run"}),json!({"run_id":42}),json!({"run_id":" current "})] {
            assert!(diagnostics_target(&args,current).is_none());
        }
    }
    #[test]
    fn unavailable_and_cross_session_responses_do_not_disclose_run_data() {
        let missing=diagnostics_response(None,"current-session");
        let other=json!({"session_id":"other-session","private":"never-return"});
        assert_eq!(diagnostics_response(Some(&other),"current-session"),missing);
        assert_eq!(missing["status"],"error");
        assert_eq!(missing["error_kind"],"diagnostics_unavailable");
        assert!(!missing.to_string().contains("never-return"));
    }
}
