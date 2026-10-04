//! HTTP handlers for chat operations.
use super::*;
pub(crate) async fn workflow_policy_get(State(app): State<App>) -> Api {
    let policy = current_workflow_policy(&app).await?;
    Ok(Json(json!({"policy":policy,"revision":policy.revision()?})))
}
pub(crate) async fn workflow_policy_set(State(app): State<App>, Json(body): Json<Value>) -> Api {
    let _guard = app.gate.lock().await;
    let policy = workflow::Policy::resolve(Some(&body))
        .map_err(|_| bad("Invalid workflow policy, unsupported module, or ceiling exceeded"))?;
    let mut snapshot = app.runtime.write().await;
    let mut next = loop_runtime::Config::read(&app.runtime_path)?; next.apply_policy(&policy);
    next.persist(&app.runtime_path)?;
    *snapshot = next;
    Ok(Json(json!({"policy":policy,"revision":policy.revision()?})))
}
pub(crate) async fn current_workflow_policy(app: &App) -> Result<workflow::Policy> {
    loop_runtime::Config::read(&app.runtime_path)?.policy()
}

pub(crate) async fn send(
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
    let session = app.store.get(&key("session", &id)).await?
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Session not found".into()))?;
    if session["status"] == "recycled" { return Err(conflict("Session is recycled; restore it first")); }
    let agent_id = session_agent_id(&app, &id).await?;
    let access=app.store.live_access(app.store.current_user_id(),&agent_id).await
        .map_err(|_|ApiError(StatusCode::FORBIDDEN,"Agent unavailable".into()))?;
    if !access.admitted() { return Err(ApiError(StatusCode::FORBIDDEN,"Agent unavailable".into())); }
    let active = app.store.active_runs().await?;
    if active.len() >= app.runtime.read().await.scheduler.max_unresolved_runs {
        return Err(conflict("Run queue is full"));
    }
    if active.len() >= access.max_active_runs() as usize {
        return Err(conflict("Account run limit reached"));
    }
    if active
        .iter()
        .any(|r| r["session_id"] == id && !terminal(status(r)))
    {
        return Err(conflict("Resolve the existing run first"));
    }
    let workflow_policy = current_workflow_policy(&app).await?;
    let input: Vec<Value> = Vec::new();
    let run_id = uuid();
    // Attachment text is saved with this message, but remains untrusted user data
    // when the loop later hydrates context for the model.
    let prepared = attachments::prepare(body.get("attachments"), &id, &run_id)
        .map_err(|_| bad("Invalid or unsupported attachments"))?;
    let mut message = json!({"role":"user","content":text,"run_id":run_id});
    if !prepared.metadata.is_empty() {
        message["context_content"] = json!(prepared.context_content(text)?);
    }
    let agent_store=app.store.for_agent(&agent_id).await?;
    let catalog = app.store.model_catalog().await?;
    // The requested entry is a call-scoped override; it never appoints Standard.
    if body.get("model_role").is_some() && body["model_role"] != "standard" {
        return Err(bad("Chat supports only the Standard role"));
    }
    let mut plan = Some(match body.get("model_entry_id") {
        Some(Value::String(entry)) => catalog.resolve_custom(&agent_id,entry),
        Some(_) => return Err(bad("Invalid custom model entry")),
        None => catalog.resolve_role(&agent_id,model_selection::ModelRole::Standard),
    }.map_err(|_|conflict("Standard model required, or selected model is unavailable"))?);
    // Pin System separately now; compaction cannot borrow Standard or a later assignment.
    let mut system_plan = if catalog.agent_models.get(&agent_id)
        .is_some_and(|models|models.roles.contains_key(&model_selection::ModelRole::System)) {
        Some(catalog.resolve_role(&agent_id,model_selection::ModelRole::System)
            .map_err(|_|conflict("System model assignment is invalid or unauthorized"))?)
    } else {None};
    // Preserve the public forbidden response for live BYO entitlement denial.
    for candidate in plan.iter().chain(system_plan.iter()).flat_map(|p| &p.candidates) {
        if candidate.platform.is_none() && !access.model_allowed(&candidate.upstream_model) {
            return Err(ApiError(StatusCode::FORBIDDEN,"Model unavailable".into()));
        }
    }
    for plan in plan.iter_mut().chain(system_plan.iter_mut()) {
        app.pin_model_plan(plan).await.map_err(|_|conflict("Assigned model is no longer entitled or configured"))?;
    }
    let selection = match &plan {
        Some(plan) => provider::ProviderSelection { provider: plan.candidates[0].transport.provider_id(),
            model: plan.candidates[0].upstream_model.clone() },
        None => {
            let mut agent_app=app.clone();
            agent_app.store=agent_store.clone();
            agent_app.selection().await?
        },
    };
    if plan.as_ref().is_none_or(|p|p.candidates[0].platform.is_none()) && !access.model_allowed(&selection.model) {
        return Err(ApiError(StatusCode::FORBIDDEN,"Model unavailable".into()));
    }
    let credential_ref = plan.as_ref().map(|p| p.candidates[0].credential_ref.as_str()).unwrap_or("provider_tokens");
    if plan.as_ref().is_none_or(|p|p.candidates[0].platform.is_none()) && app.store.get(credential_ref).await?.is_none() {
        return Err(conflict("Assigned provider connection is not authorized"));
    }
    let instructions = assembled_agent_instructions(&agent_store, &agent_id).await?;
    let mut run = json!({"agent_id":agent_id,"selection_plan":plan,"failover_state":model_selection::FailoverState::default(),"instructions":instructions,"provider_selection":selection.value(),"id":run_id,"session_id":id,"status":"queued","phase":"model","context_hydration_pending":true,"input":input,"calls":[],"call_index":0,"loaded_abilities":[],"ability_catalog_version":app.abilities.version(),
        "turns":0,"attempt":0,"pending":null,"generation":"","uncertain":false,"error":null,"created_at":now(),"updated_at":now()});
    // Both role cursors survive recovery without changing one another on failover.
    run["system_selection_plan"] = json!(system_plan);
    run["system_failover_state"] = json!(model_selection::FailoverState::default());
    run["attachments"] = json!(prepared.metadata.clone());
    // Freeze memory at admission; later edits cannot rewrite queued context.
    run["memory_context"] = json!(memory::snapshot(app.store.list(&format!("memory:{id}:")).await?)?);
    workflow::record(&mut run, &workflow_policy,
        &workflow::Input::Admission { request_digest:digest(&body) }, workflow::Decision::Pass)?;
    workflow::record(&mut run, &workflow_policy,
        &workflow::Input::AttachmentPreparation { attachment_count:prepared.metadata.len() }, workflow::Decision::Pass)?;
    let result = json!({"run_id":run_id,"attachments":prepared.metadata});
    let mut docs = prepared.records;
    docs.push((key("run", &run_id), run));
    docs.push((receipt_key, json!({"digest":digest(&body),"result":result})));
    app.store
        .atomic_docs_event(
            docs,
            &id,
            "message",
            &message,
        )
        .await?;
    Ok((StatusCode::ACCEPTED, Json(result)))
}
// Only the owning session can read this encrypted attachment record. The
// model-facing copy was already saved with the message at admission.
pub(crate) async fn attachment_get(State(app): State<App>, Path((sid, aid)): Path<(String, String)>) -> Api {
    if Uuid::parse_str(&aid).is_err() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Attachment not found".into()));
    }
    let record = app.store.get(&key("attachment", &aid)).await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Attachment not found".into()))?;
    if record["session_id"] != sid || record["schema_version"] != 1 {
        return Err(ApiError(StatusCode::NOT_FOUND, "Attachment not found".into()));
    }
    Ok(Json(json!({"id":record["id"],"run_id":record["run_id"],
        "name":record["name"],"media_type":record["media_type"],
        "bytes":record["bytes"],"sha256":record["sha256"],"text":record["text"]})))
}
// Memory writes require a completed run in this same session. The API owns
// the decision; neither model text nor a Python tool can save memory directly.
pub(crate) async fn memory_list(State(app): State<App>, Path(sid): Path<String>) -> Api {
    let notes=app.store.list(&format!("memory:{sid}:")).await?;
    Ok(Json(json!({"memories":notes})))
}
pub(crate) async fn memory_save(State(app): State<App>, Path(sid): Path<String>,
    Json(body): Json<Value>) -> std::result::Result<(StatusCode, Json<Value>), ApiError> {
    let source=required(&body,"source_run_id",64)?;
    let id=required(&body,"client_memory_id",64)?;
    let text=required(&body,"text",1024)?;
    let _guard=app.gate.lock().await;
    let created_at=SystemTime::now().duration_since(UNIX_EPOCH)
        .map_err(|_|bad("System clock unavailable"))?.as_millis() as u64;
    let record=memory::record(&sid,source,id,text,created_at)
        .map_err(|_|bad("Invalid memory input"))?;
    let run=app.run(source).await
        .map_err(|_|ApiError(StatusCode::NOT_FOUND,"Source run not found".into()))?;
    if run["session_id"]!=sid {return Err(ApiError(StatusCode::NOT_FOUND,"Source run not found".into()));}
    if status(&run)!="completed" {return Err(conflict("Source run must be completed"));}
    let key=format!("memory:{sid}:{id}");
    if let Some(existing)=app.store.get(&key).await? {
        if existing["text"]!=text || existing["source_run_id"]!=source {
            return Err(conflict("Memory identifier reused with different input"));
        }
        return Ok((StatusCode::OK,Json(existing)));
    }
    if app.store.list(&format!("memory:{sid}:")).await?.len()>=memory::MAX_COUNT {
        return Err(conflict("Session memory limit reached"));
    }
    app.store.put(&key,&record).await?;
    Ok((StatusCode::CREATED,Json(record)))
}
pub(crate) async fn memory_delete(State(app): State<App>, Path((sid,id)): Path<(String,String)>) -> Api {
    if Uuid::parse_str(&id).is_err() {
        return Err(ApiError(StatusCode::NOT_FOUND,"Memory not found".into()));
    }
    let key=format!("memory:{sid}:{id}");
    let record=app.store.get(&key).await?
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Memory not found".into()))?;
    if record["session_id"]!=sid {return Err(ApiError(StatusCode::NOT_FOUND,"Memory not found".into()));}
    app.store.delete(&key).await?;
    Ok(Json(json!({"deleted":true})))
}
pub(crate) async fn events(
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
    let user_id=auth_doc["_user_id"].as_str().unwrap_or("admin").to_owned();
    let generation=auth_doc["generation"].clone();
    let agent_id=session_agent_id(&app,&id).await?;
    let access_revision=app.store.live_access(&user_id,&agent_id).await
        .map_err(|_|ApiError(StatusCode::FORBIDDEN,"Agent unavailable".into()))?.revision;
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
            user_id, generation, agent_id, access_revision,
        ),
        |(app, id, mut cursor, mut queue, start, auth_key, expires, mut checked,
            user_id, generation, agent_id, access_revision)| async move {
            loop {
                if now() - start > 3600 || app.stop.is_cancelled() || now() >= expires {
                    return None;
                }
                if now() - checked >= 15 {
                    let doc=app.store.get(&auth_key).await.ok().flatten();
                    let account=app.store.account_by_id(&user_id).await.ok().flatten();
                    let access=app.store.live_access(&user_id,&agent_id).await.ok();
                    let valid=doc.as_ref().is_some_and(|doc|doc["generation"]==generation
                            && doc["expires"].as_u64().unwrap_or(0)>now())
                        && account.as_ref().is_some_and(|account|
                            account["generation"]==generation
                            && account["is_approved"]==true
                            && access::Principal::from_account(account)
                                .is_ok_and(|principal|principal.active()))
                        && access.as_ref().is_some_and(|access|
                            access.admitted() && access.revision==access_revision);
                    if !valid {
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
                        (app, id, cursor, queue, start, auth_key, expires, checked,
                            user_id, generation, agent_id, access_revision),
                    ));
                }
                queue = match app.store.chat_events(&id, cursor).await {
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
pub(crate) async fn control(State(app): State<App>, Json(body): Json<Value>) -> Api {
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
        for user_app in user_apps(&app).await? {
            for view in user_app.store.active_runs().await? {
                if matches!(status(&view), "queued" | "running") {
                    let mut run = user_app.run(view["id"].as_str().unwrap()).await?;
                    run["status"] = json!("stopped");
                    run["error"] = json!("Stopped by global shutoff");
                    if (run["pending"]["state"] == "executing"
                        && !tools::is_read(run["pending"]["name"].as_str().unwrap_or("")))
                        || parallel_batch::executing_write(&run)
                    {
                        run["uncertain"] = json!(true);
                        parallel_batch::mark_uncertain_writes(&mut run);
                    }
                    user_app.save_override(&mut run).await?;
                }
            }
        }
    }
    Ok(Json(result))
}
pub(crate) async fn command(
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
        if action=="cancel" {
            if let Some(sid)=r["result"]["session_id"].as_str() {phase3_api::quiesce_session_resources(&app,sid).await?;}
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
                if (run["pending"]["state"] == "executing"
                    && !tools::is_read(run["pending"]["name"].as_str().unwrap_or("")))
                    || parallel_batch::executing_write(&run)
                {
                    run["status"] = json!("recovery");
                    run["uncertain"] = json!(true);
                    parallel_batch::mark_uncertain_writes(&mut run);
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
            if let Some(items)=run["batch"].as_array_mut() {
                for item in items { if item["state"]=="executing" && tools::is_read(item["pending"]["name"].as_str().unwrap_or("")) {item["state"]=json!("requested");} }
            }
        }
        "recovery" => {
            if run["uncertain"] != true {
                return Err(conflict("Run does not require recovery review"));
            }
            if run["batch"].is_array() {
                // RECOVERY-PER-CALL: the reviewer names the uncertain call when
                // more than one write was in flight. No other call is retried.
                let uncertain=run["batch"].as_array().unwrap().iter().enumerate()
                    .filter(|(_,item)|item["state"]=="uncertain").map(|(idx,_)|idx)
                    .collect::<Vec<_>>();
                let idx=if let Some(call_id)=body["call_id"].as_str() {
                    uncertain.iter().copied().find(|idx|run["batch"][*idx]["pending"]["call_id"]==call_id)
                        .ok_or_else(||bad("Unknown uncertain call ID"))?
                } else if uncertain.len()==1 {uncertain[0]}
                    else {return Err(bad("Specify call_id for uncertain parallel writes"));};
                match body["decision"].as_str() {
                    Some("abandon")=>{
                        run["status"]=json!("cancelled");
                        run["uncertain"]=json!(false);
                    },
                    Some("retry")=>{
                        if app.shutoff().await? {return Err(conflict("Clear global shutoff first"));}
                        // A retry is a new authorization attempt with a new ID.
                        run["batch"][idx]["state"]=json!("new");
                        run["batch"][idx]["pending"]=Value::Null;
                        run["batch"][idx]["result"]=Value::Null;
                    },
                    Some("accept")=>{
                        let output=required(&body,"output",1048576)?;
                        run["batch"][idx]["result"]=json!({"status":"reconciled","output":output});
                        run["batch"][idx]["state"]=json!("done");
                    },
                    _=>return Err(bad("Choose retry, accept, or abandon")),
                }
                if body["decision"]!="abandon" {
                    run["uncertain"]=json!(parallel_batch::uncertain_count(&run)>0);
                    run["status"]=json!(if run["uncertain"]==true {"recovery"}
                        else if app.shutoff().await? {"stopped"} else {"queued"});
                }
                run["pending"]=Value::Null;
            } else {
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
                    append_tool(&mut run, &json!({"status":"reconciled","output":output}))?;
                    run["status"] = json!(if app.shutoff().await? {
                        "stopped"
                    } else {
                        "queued"
                    });
                    run["uncertain"] = json!(false);
                }
                _ => return Err(bad("Choose retry, accept, or abandon")),

            }}
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
    if action=="cancel" {phase3_api::quiesce_session_resources(&app,result["session_id"].as_str().unwrap()).await?;}
    Ok(Json(result))
}
pub(crate) async fn approval(State(app): State<App>, Path(id): Path<String>, Json(body): Json<Value>) -> Api {
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
    let sid = run["session_id"].as_str().unwrap();
    let agent_id=run["agent_id"].as_str().unwrap_or("shared_default");
    let access=app.store.live_access(app.store.current_user_id(),agent_id).await
        .map_err(|_|conflict("Access changed; approval is invalid"))?;
    if !access.admitted() || !app.run_model_authorized(&run).await {
        run["status"]=json!("stopped");
        run["pending"]["decision"]=Value::Null;
        app.save(&mut run).await?;
        return Err(conflict("Access changed; approval is invalid"));
    }
    let (policy, mode_revision) = effective_policy(&app, sid).await?;
    let visibility = session_agent_abilities(&app, sid).await?;
    let ability_state = session_ability_state(&app, sid).await?;
    if run["pending"]["policy_version"] != policy["version"]
        || run["pending"]["access_revision"] != json!(access.revision)
        || run["pending"]["mode_revision"].as_u64().unwrap_or(0) != mode_revision
        || run["pending"]["visibility_revision"].as_u64().unwrap_or(0)
            != visibility["revision"].as_u64().unwrap()
        || run["pending"]["ability_state_revision"]
            .as_u64()
            .unwrap_or(0)
            != ability_state["revision"].as_u64().unwrap()
    {
        // BATCH-APPROVAL-REVOCATION: a changed policy invalidates only this
        // call. Its next pass gets a new pending ID and fresh revisions.
        let stale_id=run["pending"]["id"].clone();
        if let Some(items)=run["batch"].as_array_mut() {
            for item in items { if item["pending"]["id"]==stale_id {
                item["state"]=json!("new"); item["pending"]=Value::Null;
                item["result"]=Value::Null; break;
            } }
            run["pending"]=Value::Null;
        } else {
            run["pending"]["state"] = json!("requested");
            run["pending"]["decision"] = Value::Null;
        }
        run["status"] = json!("queued");
        app.save(&mut run).await?;
        return Err(conflict(
            "Policy or session mode changed; a new gate decision is required",
        ));
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
