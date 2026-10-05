//! Private artifacts are read through authenticated session ownership only.
use super::*;
pub(crate) async fn artifact(axum::Extension(app):axum::Extension<App>,Path((sid,id)):Path<(String,String)>)->std::result::Result<Response,ApiError> {
    uuid::Uuid::parse_str(&id).map_err(|_|ApiError(StatusCode::BAD_REQUEST,"Invalid artifact identifier".into()))?;
    let (before,trusted)=artifact_authority(&app,&sid).await?;
    let output=tools::execute_trusted("administrator_artifact_get",&json!({"artifact_id":id}),&sid,
        &app.tool_socket,Some(trusted),CancellationToken::new()).await?;
    // Worker I/O is not an authorization lease. A concurrent restriction or
    // credential replacement withdraws the bytes before publication.
    let (after,_)=artifact_authority(&app,&sid).await?;
    if before!=after {return Err(artifact_denied());}
    let encoded=output["png_base64"].as_str().filter(|v|v.len()<=1400000)
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Artifact unavailable".into()))?;
    use base64::Engine;
    let data=base64::engine::general_purpose::STANDARD.decode(encoded)
        .map_err(|_|ApiError(StatusCode::INTERNAL_SERVER_ERROR,"Invalid artifact".into()))?;
    if data.len()>1024*1024 || !data.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Err(ApiError(StatusCode::INTERNAL_SERVER_ERROR,"Invalid artifact".into()));
    }
    let mut response=( [("content-type","image/png"),("cache-control","no-store")],data).into_response();
    response.headers_mut().insert("content-disposition",HeaderValue::from_static("inline; filename=administrator.png"));
    Ok(response)
}

fn artifact_denied()->ApiError {
    ApiError(StatusCode::FORBIDDEN,"Artifact access unavailable under current policy".into())
}
async fn artifact_authority(app:&App,sid:&str)->std::result::Result<(String,Value),ApiError> {
    let session=app.store.get(&key("session",sid)).await?
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Session not found".into()))?;
    let agent=session["agent_id"].as_str().unwrap_or("shared_default");
    let mut scoped=app.clone();scoped.store=app.store.for_agent(agent).await?;
    let (policy,revision,visibility,state)=ability_controls(&scoped,sid).await?;
    let mode=session_mode(&scoped,sid).await?;
    let access=scoped.store.live_access(scoped.store.current_user_id(),agent).await?;
    if !access.admitted() || live_tool_decision(&scoped,&access,"dev_browser_action",
        &policy,&visibility,&state,&json!(["diagnostics"]),mode["mode"]=="read_only").permission==access::Permission::Deny {
        return Err(artifact_denied());
    }
    let trusted=app.store.get(&format!("administrator_browser:{sid}")).await?
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Artifact not found".into()))?;
    let account=app.store.account_by_id(app.store.current_user_id()).await?
        .ok_or_else(artifact_denied)?;
    if trusted["session_id"]!=sid || trusted["user_id"]!=app.store.current_user_id()
        || trusted["agent_id"]!=agent || trusted["generation"]!=account["generation"]
        || trusted["expires_at"].as_u64().unwrap_or(0)<=now() {
        return Err(artifact_denied());
    }
    let fingerprint=digest(&json!([app.store.current_user_id(),sid,agent,policy,revision,
        visibility,state,mode,access.context_fingerprint(),trusted,app.abilities.live_version()]));
    Ok((fingerprint,trusted))
}

pub(crate) async fn notices(axum::Extension(app):axum::Extension<App>,Path(sid):Path<String>)->Api {
    let session=app.store.get(&key("session",&sid)).await?
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Session not found".into()))?;
    let agent=session["agent_id"].as_str().unwrap_or("shared_default");
    let mut scoped=app.clone();scoped.store=app.store.for_agent(agent).await?;
    let (policy,_,visibility,state)=ability_controls(&scoped,&sid).await?;
    let mode=session_mode(&scoped,&sid).await?;
    let access=scoped.store.live_access(scoped.store.current_user_id(),agent).await?;
    if !access.admitted(){return Err(ApiError(StatusCode::FORBIDDEN,"Agent access unavailable".into()));}
    if visibility["settings"]["git_control"]["notify_repo_changes"]!=true
        || live_tool_decision(&scoped,&access,"git_tool",&policy,&visibility,&state,
            &json!(["git_control"]),mode["mode"]=="read_only").permission!=access::Permission::Auto {
        return Ok(Json(json!({"notice":Value::Null})));
    }
    let value=app.store.get(&format!("administrator_notice:{sid}")).await?;
    let (next_policy,_,next_visibility,next_state)=ability_controls(&scoped,&sid).await?;
    let next_mode=session_mode(&scoped,&sid).await?;
    let current=scoped.store.live_access(scoped.store.current_user_id(),agent).await?;
    if !current.admitted() || next_visibility["settings"]["git_control"]["notify_repo_changes"]!=true
        || live_tool_decision(&scoped,&current,"git_tool",&next_policy,&next_visibility,&next_state,
            &json!(["git_control"]),next_mode["mode"]=="read_only").permission!=access::Permission::Auto {
        return Ok(Json(json!({"notice":Value::Null})));
    }
    Ok(Json(json!({"notice":value})))
}
