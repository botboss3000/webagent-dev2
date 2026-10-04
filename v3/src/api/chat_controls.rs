//! Composer choices belong to the user session; Rust retains model and mode authority.
use super::*;
async fn choices(app: &App, sid: &str) -> Result<Value, ApiError> {
    let agent = session_agent_id(app, sid).await?;
    let catalog = app.store.model_catalog().await?;
    let saved = app
        .store
        .get(&format!("session_chat_controls:{sid}"))
        .await?
        .unwrap_or(json!({"entry_id":null,"revision":0}));
    let mut models = Vec::new();
    let mut options = vec![(
        None,
        catalog.resolve_role(&agent, model_selection::ModelRole::Standard),
    )];
    if let Some(roster) = catalog.agent_models.get(&agent) {
        for id in &roster.entry_ids {
            options.push((Some(id.as_str()), catalog.resolve_custom(&agent, id)));
        }
    }
    // Labels are public; connection documents and credential references are not.
    for (id, result) in options {
        let Ok(mut plan) = result else { continue };
        if app.pin_model_plan(&mut plan).await.is_err() {
            continue;
        }
        let Some(first) = plan.candidates.first() else {
            continue;
        };
        let connected =
            first.platform.is_some() || app.store.get(&first.credential_ref).await?.is_some();
        let display = catalog
            .entries
            .get(&first.entry_id)
            .map(|entry| entry.display_name.as_str())
            .unwrap_or(&first.upstream_model);
        models.push(json!({"entry_id":id,"label":if id.is_none(){format!("Standard · {display}")}else{display.to_owned()},
            "model":first.upstream_model,"connected":connected}));
    }
    let config = crate::execution_modes::config(&app.store, &agent).await?;
    let modes: Vec<Value> = config["execution_modes"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|mode| {
            json!({"id":mode["id"],"label":mode["label"],"description":mode["description"],
            "permission_policy":mode["permission_policy"]})
        })
        .collect();
    let mode = session_mode(app, sid).await?;
    let attachment_limits = uploads::limits(app).await?;
    Ok(
        json!({"composer":{"attachments":true,"voice":true,"attachment_limits":{"max_file_size_mb":attachment_limits.max_file_size_mb,"max_per_message":attachment_limits.max_per_message}},"agent_id":agent,"model":saved,"models":models,"mode":{"mode":mode["mode"],"execution_mode":mode["execution_mode"],"revision":mode["revision"]},
        "execution_modes":modes}),
    )
}
pub(crate) async fn chat_usage_get(State(app):State<App>,Path(sid):Path<String>)->Api{
    let totals=usage::public(&app.store.get(&format!("session_usage:{sid}")).await?.unwrap_or(Value::Null));
    let views=app.store.recent_runs(Some(&sid)).await?;
    let runs=public_runs(&app,views.into_iter().take(1).collect()).await?;
    let latest=runs.first().map(|run|json!({"id":run["id"],"status":run["status"],"model":run["model"],
        "context_summary":run["context_summary"],"context_controls":run["context_controls"],"usage":run["usage"],"compactions":run["compactions"]}));
    Ok(Json(json!({"usage":totals,"latest":latest})))
}
pub(crate) async fn chat_controls_get(State(app): State<App>, Path(sid): Path<String>) -> Api {
    let _guard = app.gate.lock().await;
    Ok(Json(choices(&app, &sid).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ChatModelBody {
    pub entry_id: Option<String>,
    pub expected_revision: u64,
    pub client_request_id: String,
}
pub(crate) async fn chat_model_set(
    State(app): State<App>,
    Path(sid): Path<String>,
    Json(body): Json<ChatModelBody>,
) -> Api {
    if body.client_request_id.is_empty() || body.client_request_id.len() > 128 {
        return Err(bad("Invalid request identifier"));
    }
    let _guard = app.gate.lock().await;
    let session = app
        .store
        .get(&key("session", &sid))
        .await?
        .ok_or_else(|| bad("Session not found"))?;
    if session["status"] == "recycled" {
        return Err(conflict(
            "Restore this conversation before changing its model",
        ));
    }
    let options = choices(&app, &sid).await?;
    let fingerprint = digest(&json!([sid, body.entry_id, body.expected_revision]));
    let receipt = format!("receipt:chat_controls:{sid}:{}", body.client_request_id);
    if let Some(saved) = app.store.get(&receipt).await? {
        if saved["digest"] != fingerprint {
            return Err(conflict("Request identifier reused"));
        }
        return Ok(Json(saved["result"].clone()));
    }
    if options["model"]["revision"].as_u64() != Some(body.expected_revision) {
        return Err(conflict("Conversation model changed; reload the picker"));
    }
    if !options["models"]
        .as_array()
        .into_iter()
        .flatten()
        .any(|entry| entry["entry_id"] == json!(body.entry_id) && entry["connected"] == true)
    {
        return Err(conflict("Selected model is unavailable or not connected"));
    }
    let revision = body
        .expected_revision
        .checked_add(1)
        .ok_or_else(|| bad("Model revision exhausted"))?;
    let saved = json!({"entry_id":body.entry_id,"revision":revision});
    // Later admissions read this preference; accepted runs retain their immutable plans.
    app.store
        .atomic_docs_event(
            vec![
                (format!("session_chat_controls:{sid}"), saved.clone()),
                (receipt, json!({"digest":fingerprint,"result":saved})),
            ],
            &sid,
            "chat_model_changed",
            &saved,
        )
        .await?;
    Ok(Json(saved))
}
// Composer revisions are consumed only at the next model-call boundary.
// The current call and System plan stay pinned; live grants are checked anew.
pub(crate) async fn apply_chat_model_preference(app:&App,run:&mut Value)->Result<bool>{
    let sid=run["session_id"].as_str().ok_or_else(||anyhow!("Session missing"))?;
    let saved=app.store.get(&format!("session_chat_controls:{sid}")).await?
        .unwrap_or(json!({"entry_id":null,"revision":0}));
    let revision=saved["revision"].as_u64().ok_or_else(||anyhow!("Invalid model revision"))?;
    if revision==run["chat_model_revision"].as_u64().unwrap_or(0){return Ok(false);}
    let agent=run["agent_id"].as_str().ok_or_else(||anyhow!("Agent missing"))?;
    let catalog=app.store.model_catalog().await?;
    let mut plan=match saved["entry_id"].as_str(){
        Some(entry)=>catalog.resolve_custom(agent,entry)?,
        None=>catalog.resolve_role(agent,model_selection::ModelRole::Standard)?,
    };
    app.pin_model_plan(&mut plan).await?;
    let candidate=&plan.candidates[0];
    if candidate.platform.is_none() && app.store.get(&candidate.credential_ref).await?.is_none(){
        return Err(anyhow!("Selected model connection is unavailable"));
    }
    run["provider_selection"]=provider::ProviderSelection{
        provider:candidate.transport.provider_id(),model:candidate.upstream_model.clone()
    }.value();
    run["selection_plan"]=json!(plan);
    run["failover_state"]=json!(model_selection::FailoverState::default());
    run["chat_model_revision"]=json!(revision);
    run["attempt"]=json!(0);
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_tests::{fixture, selection_fixture};
    #[tokio::test]
    async fn composer_model_is_private_fenced_and_pinned_without_changing_standard() {
        let (_root, app) = fixture().await;
        let catalog = selection_fixture(&app).await;
        let Json(session) = create_session(
            State(app.clone()),
            Json(json!({"client_request_id":uuid()})),
        )
        .await
        .unwrap();
        let sid = session["id"].as_str().unwrap().to_owned();
        let Json(options) = chat_controls_get(State(app.clone()), Path(sid.clone()))
            .await
            .unwrap();
        assert_eq!(options["models"].as_array().unwrap().len(), 3);
        assert!(!options.to_string().contains("credential:"));
        assert!(!options.to_string().contains("token-"));
        let request = uuid();
        let body = || ChatModelBody {
            entry_id: Some("two".into()),
            expected_revision: 0,
            client_request_id: request.clone(),
        };
        let Json(saved) = chat_model_set(State(app.clone()), Path(sid.clone()), Json(body()))
            .await
            .unwrap();
        assert_eq!(saved["revision"], 1);
        assert_eq!(
            chat_model_set(State(app.clone()), Path(sid.clone()), Json(body()))
                .await
                .unwrap()
                .0,
            saved
        );
        assert!(chat_model_set(
            State(app.clone()),
            Path(sid.clone()),
            Json(ChatModelBody {
                entry_id: Some("one".into()),
                expected_revision: 0,
                client_request_id: uuid()
            })
        )
        .await
        .is_err());
        let (_, Json(sent)) = send(
            State(app.clone()),
            Path(sid.clone()),
            Json(json!({"text":"hello","client_message_id":uuid()})),
        )
        .await
        .unwrap();
        let run = app.run(sent["run_id"].as_str().unwrap()).await.unwrap();
        assert_eq!(run["selection_plan"]["custom_entry_id"], "two");
        assert_eq!(run["provider_selection"]["model"], "model-two");
        assert_eq!(
            app.store.model_catalog().await.unwrap().agent_models["shared_default"].roles,
            catalog.agent_models["shared_default"].roles
        );
    }
    #[tokio::test]
    async fn composer_model_choice_applies_at_next_loop_boundary_without_a_message(){
        let (_root,app)=fixture().await;
        let catalog=selection_fixture(&app).await;
        let Json(session)=create_session(State(app.clone()),Json(json!({"client_request_id":uuid()}))).await.unwrap();
        let sid=session["id"].as_str().unwrap().to_owned();
        let (_,Json(sent))=send(State(app.clone()),Path(sid.clone()),Json(json!({"text":"hi","client_message_id":uuid()}))).await.unwrap();
        let mut run=app.run(sent["run_id"].as_str().unwrap()).await.unwrap();
        let original=run["selection_plan"].clone();
        let system=run["system_selection_plan"].clone();
        chat_model_set(State(app.clone()),Path(sid.clone()),Json(ChatModelBody{entry_id:Some("two".into()),expected_revision:0,client_request_id:uuid()})).await.unwrap();
        assert_eq!(app.run(sent["run_id"].as_str().unwrap()).await.unwrap()["selection_plan"],original);
        assert!(apply_chat_model_preference(&app,&mut run).await.unwrap());
        assert_eq!(run["provider_selection"]["model"],"model-two");
        assert_eq!(run["chat_model_revision"],1);
        assert_eq!(run["system_selection_plan"],system);
        assert!(!apply_chat_model_preference(&app,&mut run).await.unwrap());
        assert_eq!(app.store.chat_snapshot(&sid).await.unwrap().0.len(),1);
        assert_eq!(app.store.model_catalog().await.unwrap().agent_models["shared_default"].roles,catalog.agent_models["shared_default"].roles);
    }

    #[tokio::test]
    async fn active_run_picks_up_composer_model_and_mode_on_next_call(){
        use axum::{routing::post,Router};
        use std::sync::Arc;
        use tokio::sync::{Mutex,Notify};
        struct Shared{calls:Mutex<Vec<Value>>,started:Notify,release:Notify}
        let shared=Arc::new(Shared{calls:Mutex::new(vec![]),started:Notify::new(),release:Notify::new()});
        let handler=shared.clone();
        let router=Router::new().route("/responses",post(move|Json(body):Json<Value>|{
            let shared=handler.clone();
            async move{
                let number={let mut calls=shared.calls.lock().await;calls.push(body);calls.len()};
                let response=if number==1{
                    shared.started.notify_one();shared.release.notified().await;
                    json!({"status":"completed","usage":{"input_tokens":12,"output_tokens":1},
                        "output":[{"type":"function_call","call_id":"discover","name":"list_abilities","arguments":"{}"}]})
                }else{
                    json!({"status":"completed","usage":{"input_tokens":23,"output_tokens":2},
                        "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"boundary complete"}]}]})
                };
                ([( "content-type","text/event-stream")],format!("data: {}\n\n",json!({"type":"response.completed","response":response})))
            }
        }));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move{axum::serve(listener,router).await.unwrap();});
        let (_root,mut app)=fixture().await;selection_fixture(&app).await;
        app.provider=provider::Provider::for_test(format!("http://{addr}/responses"));
        let Json(session)=create_session(State(app.clone()),Json(json!({"client_request_id":uuid()}))).await.unwrap();
        let sid=session["id"].as_str().unwrap().to_owned();
        let (_,Json(sent))=send(State(app.clone()),Path(sid.clone()),Json(json!({"text":"discover and finish","client_message_id":uuid()}))).await.unwrap();
        let rid=sent["run_id"].as_str().unwrap().to_owned();
        let mut run=app.run(&rid).await.unwrap();
        assert!(app.store.claim_run(&mut run,"fixture-loop","boundary-test").await.unwrap());
        let runner=tokio::spawn(crate::execute_run(app.clone(),rid.clone(),"boundary-test".into(),CancellationToken::new()));
        tokio::time::timeout(Duration::from_secs(10),shared.started.notified()).await.unwrap();
        chat_model_set(State(app.clone()),Path(sid.clone()),Json(ChatModelBody{entry_id:Some("two".into()),expected_revision:0,client_request_id:uuid()})).await.unwrap();
        session_mode_set(State(app.clone()),Path(sid.clone()),Json(SessionModeBody{mode:"plan".into(),expected_revision:Some(0),confirm_write:false})).await.unwrap();
        shared.release.notify_one();
        tokio::time::timeout(Duration::from_secs(15),runner).await.unwrap().unwrap().unwrap();
        let calls=shared.calls.lock().await;
        assert_eq!(calls.len(),2);assert_eq!(calls[0]["model"],"model-one");assert_eq!(calls[1]["model"],"model-two");
        let mut run=app.run(&rid).await.unwrap();
        assert_eq!(run["dispatch_mode"]["execution_mode"],"plan");
        assert_eq!(run["system_selection_plan"]["candidates"][0]["upstream_model"],"model-one");
        assert_eq!(run["usage"]["input_tokens"],35);
        app.save(&mut run).await.unwrap();app.save(&mut run).await.unwrap();
        let totals=app.store.get(&format!("session_usage:{sid}")).await.unwrap().unwrap();
        assert_eq!(totals["input_tokens"],35);assert_eq!(totals["output_tokens"],3);assert_eq!(totals["reported_calls"],2);
        let (messages,_)=app.store.chat_snapshot(&sid).await.unwrap();
        assert_eq!(messages.iter().filter(|row|row["role"]=="user").count(),1);
        assert_eq!(messages.iter().filter(|row|row["role"]=="assistant").count(),1);
        assert_eq!(messages.last().unwrap()["content"],"boundary complete");
        server.abort();
    }

    #[tokio::test]
    async fn mode_control_requires_current_revision_and_confirmation_to_widen() {
        let (_root, app) = fixture().await;
        let Json(session) = create_session(
            State(app.clone()),
            Json(json!({"client_request_id":uuid()})),
        )
        .await
        .unwrap();
        let sid = session["id"].as_str().unwrap().to_owned();
        let change = |mode: &str, revision, confirmed| SessionModeBody {
            mode: mode.into(),
            expected_revision: Some(revision),
            confirm_write: confirmed,
        };
        assert!(session_mode_set(
            State(app.clone()),
            Path(sid.clone()),
            Json(change("auto", 0, false))
        )
        .await
        .is_err());
        assert_eq!(session_mode(&app, &sid).await.unwrap()["revision"], 0);
        let _ = session_mode_set(
            State(app.clone()),
            Path(sid.clone()),
            Json(change("plan", 0, false)),
        )
        .await
        .unwrap();
        assert!(session_mode_set(
            State(app.clone()),
            Path(sid.clone()),
            Json(change("auto", 0, true))
        )
        .await
        .is_err());
        let _ = session_mode_set(
            State(app.clone()),
            Path(sid.clone()),
            Json(change("auto", 1, true)),
        )
        .await
        .unwrap();
        assert_eq!(session_mode(&app, &sid).await.unwrap()["mode"], "write");
    }
}
