//! Runtime acceptance tests using isolated encrypted storage and a local provider fixture.
use super::*;
use axum::body::Body;
use futures_util::StreamExt;
use tempfile::TempDir;
use tower::ServiceExt;

async fn fixture() -> (TempDir, App) {
    let root = tempfile::tempdir().unwrap();
    let control_dir = root.path().join("control");
    std::fs::create_dir(&control_dir).unwrap();
    std::fs::write(control_dir.join("dispatch.lock"), b"").unwrap();
    std::fs::write(control_dir.join("shutoff"), b"0").unwrap();
    let store = Store::open(root.path()).await.unwrap();
    store.put("policy", &policy_default()).await.unwrap();
    store
        .put("settings", &json!({"model":"fixture"}))
        .await
        .unwrap();
    store
        .put("control", &json!({"shutoff":false}))
        .await
        .unwrap();
    store
        .put(
            "provider_tokens",
            &json!({"access_token":"fixture-only","expires_at":now()+3600}),
        )
        .await
        .unwrap();
    store
        .put(
            "admin",
            &json!({"username":"admin","generation":null,"password_hash":"fixture"}),
        )
        .await
        .unwrap();
    let app = App {
        store,
        gate: Arc::new(Mutex::new(())),
        provider: provider::Provider::new(),
        refresh: Arc::new(Mutex::new(())),
        login: Arc::new(RwLock::new(None)),
        active: Arc::new(Mutex::new(HashMap::new())),
        slots: Arc::new(Semaphore::new(2)),
        ready: Arc::new(AtomicBool::new(true)),
        stop: CancellationToken::new(),
        login_attempts: Arc::new(Mutex::new(Vec::new())),
        control_dir,
        secure: false,
    };
    (root, app)
}

#[tokio::test]
async fn default_agent_model_is_saved_independently_of_global_settings() {
    let (_root, app) = fixture().await;
    let initial = agent_model(State(app.clone()), Path("shared_default".into()))
        .await
        .unwrap()
        .0;
    assert_eq!(initial["model"], DEFAULT_AGENT_MODEL);
    let saved = agent_model_set(
        State(app.clone()),
        Path("shared_default".into()),
        Json(json!({"model":"gpt-6-sol"})),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(saved["provider"], "openai_subscription");
    assert_eq!(saved["model"], "gpt-6-sol");
    assert_eq!(
        app.store.get("settings").await.unwrap().unwrap()["model"],
        "fixture"
    );
    assert_eq!(
        agent_model(State(app.clone()), Path("shared_default".into()))
            .await
            .unwrap()
            .0["model"],
        "gpt-6-sol"
    );
    assert_eq!(
        agent_model(State(app), Path("unknown".into()))
            .await
            .unwrap_err()
            .0,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn old_default_model_is_migrated_once_and_new_choices_are_preserved() {
    let (_root, app) = fixture().await;
    app.store
        .put("agent:shared_default:model", &json!({"model":"gpt-5.4"}))
        .await
        .unwrap();
    migrate_default_agent_model(&app.store).await.unwrap();
    assert_eq!(
        app.store
            .get("agent:shared_default:model")
            .await
            .unwrap()
            .unwrap()["model"],
        DEFAULT_AGENT_MODEL
    );
    app.store
        .put("agent:shared_default:model", &json!({"model":"gpt-6-sol"}))
        .await
        .unwrap();
    migrate_default_agent_model(&app.store).await.unwrap();
    assert_eq!(
        app.store
            .get("agent:shared_default:model")
            .await
            .unwrap()
            .unwrap()["model"],
        "gpt-6-sol"
    );
}

async fn conversation(app: &App) -> String {
    create_session(
        State(app.clone()),
        Json(json!({"client_request_id":uuid(),"title":"Test"})),
    )
    .await
    .unwrap()
    .0["id"]
        .as_str()
        .unwrap()
        .to_owned()
}

async fn queued(app: &App, sid: &str) -> String {
    send(
        State(app.clone()),
        Path(sid.to_owned()),
        Json(json!({"client_message_id":uuid(),"text":"hello"})),
    )
    .await
    .unwrap()
    .1
     .0["run_id"]
        .as_str()
        .unwrap()
        .to_owned()
}

async fn running(app: &App, id: &str, generation: &str) {
    let mut run = app.run(id).await.unwrap();
    run["status"] = json!("running");
    run["generation"] = json!(generation);
    app.save(&mut run).await.unwrap();
}

#[tokio::test]
async fn message_receipts_are_idempotent_and_conflicting_reuse_is_rejected() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let body = json!({"client_message_id":"same","text":"hello"});
    let first = send(State(app.clone()), Path(sid.clone()), Json(body.clone()))
        .await
        .unwrap();
    let second = send(State(app.clone()), Path(sid.clone()), Json(body))
        .await
        .unwrap();
    assert_eq!(first.0, StatusCode::ACCEPTED);
    assert_eq!(first.1 .0, second.1 .0);
    let error = send(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"client_message_id":"same","text":"different"})),
    )
    .await
    .unwrap_err();
    assert_eq!(error.0, StatusCode::CONFLICT);
    let session = app.store.get(&key("session", &sid)).await.unwrap().unwrap();
    assert_eq!(session["messages"].as_array().unwrap().len(), 1);
    assert_eq!(app.store.list("run:").await.unwrap().len(), 1);
}

#[tokio::test]
async fn authentication_and_csrf_protect_control_routes() {
    let (_root, app) = fixture().await;
    let response = router(app.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v3/status")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"expected","expires":now()+3600}),
        )
        .await
        .unwrap();
    let request = |csrf: &str| {
        Request::builder()
            .method("PUT")
            .uri("/api/v3/control")
            .header("cookie", format!("v3_session={token}"))
            .header("content-type", "application/json")
            .header("x-csrf-token", csrf)
            .body(Body::from(
                r#"{"client_request_id":"control","shutoff":true}"#,
            ))
            .unwrap()
    };
    let response = router(app.clone()).oneshot(request("wrong")).await.unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert!(!app.shutoff().await.unwrap());
    let response = router(app.clone())
        .oneshot(request("expected"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(app.shutoff().await.unwrap());
}

#[tokio::test]
async fn changed_policy_invalidates_pending_action_approval() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    let args = json!({"command":"printf hello"});
    let mut run = app.run(&id).await.unwrap();
    run["status"] = json!("waiting_approval");
    run["pending"] = json!({"id":"approval","name":"shell","arguments":args,"digest":digest(&args),
        "policy_version":1,"state":"waiting","decision":null,"call_id":"call"});
    app.save(&mut run).await.unwrap();
    let mut policy = policy_default();
    policy["version"] = json!(2);
    app.store.put("policy", &policy).await.unwrap();
    let error = approval(
        State(app.clone()),
        Path("approval".into()),
        Json(json!({"client_request_id":"decision","decision":"approve"})),
    )
    .await
    .unwrap_err();
    assert_eq!(error.0, StatusCode::CONFLICT);
    let run = app.run(&id).await.unwrap();
    assert_eq!(run["status"], "queued");
    assert!(run["pending"]["decision"].is_null());
    assert_eq!(run["pending"]["digest"], digest(&args));
}

#[tokio::test]
async fn recovery_holds_uncertain_writes_and_requeues_safe_reads() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "first").await;
    let mut run = app.run(&id).await.unwrap();
    run["phase"] = json!("tools");
    run["pending"] = json!({"name":"shell","state":"executing"});
    app.save(&mut run).await.unwrap();
    recover(&app, true).await.unwrap();
    let run = app.run(&id).await.unwrap();
    assert_eq!(run["status"], "recovery");
    assert_eq!(run["uncertain"], true);
    assert_eq!(run["pending"]["state"], "executing");
    let sid2 = conversation(&app).await;
    let id2 = queued(&app, &sid2).await;
    running(&app, &id2, "second").await;
    let mut read = app.run(&id2).await.unwrap();
    read["pending"] = json!({"name":"file_read","state":"executing"});
    app.save(&mut read).await.unwrap();
    recover(&app, true).await.unwrap();
    let read = app.run(&id2).await.unwrap();
    assert_eq!(read["status"], "queued");
    assert_eq!(read["pending"]["state"], "requested");
    assert_eq!(app.run(&id).await.unwrap()["status"], "recovery");
}

#[tokio::test]
async fn shutoff_survives_storage_reopen_and_clear_requires_explicit_resume() {
    let (root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    assert_eq!(
        control(
            State(app.clone()),
            Json(json!({"client_request_id":"off","shutoff":true}))
        )
        .await
        .unwrap()
        .0["shutoff"],
        true
    );
    let reopened = Store::open(root.path()).await.unwrap();
    assert_eq!(
        reopened.get("control").await.unwrap().unwrap()["shutoff"],
        true
    );
    assert_eq!(
        reopened.get(&key("run", &id)).await.unwrap().unwrap()["status"],
        "stopped"
    );
    assert_eq!(
        std::fs::read(app.control_dir.join("shutoff")).unwrap(),
        b"1"
    );
    assert_eq!(
        control(
            State(app.clone()),
            Json(json!({"client_request_id":"on","shutoff":false}))
        )
        .await
        .unwrap()
        .0["shutoff"],
        false
    );
    assert_eq!(app.run(&id).await.unwrap()["status"], "stopped");
    assert_eq!(
        command(
            State(app.clone()),
            Path((id.clone(), "resume".into())),
            Json(json!({"client_request_id":"resume"}))
        )
        .await
        .unwrap()
        .0["status"],
        "queued"
    );
    assert_eq!(app.run(&id).await.unwrap()["status"], "queued");
}

#[tokio::test]
async fn stale_generation_cannot_execute_or_commit_failure() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "current").await;
    execute_run(
        app.clone(),
        id.clone(),
        "stale".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let run = app.run(&id).await.unwrap();
    assert_eq!(run["generation"], "current");
    assert_eq!(run["status"], "running");
    assert_eq!(run["attempt"], 0);
    assert!(run["error"].is_null());
}

#[tokio::test]
async fn agent_loop_gates_tool_and_denial_continues_without_worker() {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
    let captured = requests.clone();
    let mock = Router::new().route("/responses", post(|State(requests): State<Arc<Mutex<Vec<Value>>>>, Json(body): Json<Value>| async move {
        let mut requests = requests.lock().await;
        requests.push(body);
        let output = if requests.len() == 1 {
            json!([{"type":"function_call","call_id":"call","name":"shell","arguments":"{\"command\":\"printf hello\"}"}])
        } else {
            json!([{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Action denied."}]}])
        };
        let event = json!({"type":"response.completed","response":{"status":"completed","output":output}});
        ([("content-type","text/event-stream")], format!("data: {event}\n\n"))
    })).with_state(captured);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "first").await;
    execute_run(
        app.clone(),
        id.clone(),
        "first".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let waiting = app.run(&id).await.unwrap();
    assert_eq!(waiting["status"], "waiting_approval");
    assert_eq!(waiting["pending"]["arguments"]["command"], "printf hello");
    let approval_id = waiting["pending"]["id"].as_str().unwrap().to_owned();
    assert_eq!(
        approval(
            State(app.clone()),
            Path(approval_id),
            Json(json!({"client_request_id":"deny","decision":"deny"}))
        )
        .await
        .unwrap()
        .0["status"],
        "queued"
    );
    running(&app, &id, "second").await;
    execute_run(
        app.clone(),
        id.clone(),
        "second".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 2);
    let tool_output = requests[1]["input"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["type"] == "function_call_output")
        .unwrap();
    assert_eq!(tool_output["call_id"], "call");
    assert!(tool_output["output"].as_str().unwrap().contains("denied"));
    server.abort();
}

#[tokio::test]
async fn shutoff_cancels_midstream_model_without_waiting_for_eof() {
    use axum::{
        response::{sse::Event, Sse},
        routing::post,
    };
    let (_root, mut app) = fixture().await;
    let started = Arc::new(tokio::sync::Notify::new());
    let signal = started.clone();
    let mock = Router::new().route(
        "/responses",
        post(move || {
            let signal = signal.clone();
            async move {
                signal.notify_one();
                let first = futures_util::stream::once(async {
                    Ok::<Event, Infallible>(
                        Event::default()
                            .data(r#"{"type":"response.output_text.delta","delta":"partial"}"#),
                    )
                });
                Sse::new(first.chain(futures_util::stream::pending()))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "generation").await;
    let cancel = CancellationToken::new();
    app.active.lock().await.insert(id.clone(), cancel.clone());
    let executing = tokio::spawn(execute_run(
        app.clone(),
        id.clone(),
        "generation".into(),
        cancel,
    ));
    tokio::time::timeout(Duration::from_secs(2), started.notified())
        .await
        .unwrap();
    assert_eq!(
        control(
            State(app.clone()),
            Json(json!({"client_request_id":"stop","shutoff":true}))
        )
        .await
        .unwrap()
        .0["shutoff"],
        true
    );
    tokio::time::timeout(Duration::from_secs(2), executing)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(app.run(&id).await.unwrap()["status"], "stopped");
    assert!(
        app.store.get(&key("session", &sid)).await.unwrap().unwrap()["messages"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["role"] != "assistant")
    );
    server.abort();
}

async fn approved_worker_tool(name: &str, arguments: Value, marker_text: &str) {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
    let captured = requests.clone();
    let tool_call = json!({"type":"function_call","call_id":"approved-call","name":name,"arguments":arguments.to_string()});
    let call = tool_call.clone();
    let mock = Router::new().route("/responses",post(move |Json(body):Json<Value>| {
        let requests = captured.clone();
        let call = call.clone();
        async move {
            let mut requests = requests.lock().await;
            requests.push(body);
            let output = if requests.len()==1 {json!([call])}
                else {json!([{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Executed."}]}])};
            let event=json!({"type":"response.completed","response":{"status":"completed","output":output}});
            ([("content-type","text/event-stream")],format!("data: {event}\n\n"))
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "first").await;
    execute_run(
        app.clone(),
        id.clone(),
        "first".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let waiting = app.run(&id).await.unwrap();
    assert_eq!(waiting["status"], "waiting_approval");
    assert_eq!(waiting["pending"]["arguments"], arguments);
    let pending_id = waiting["pending"]["id"].as_str().unwrap().to_owned();
    assert_eq!(
        approval(
            State(app.clone()),
            Path(pending_id.clone()),
            Json(json!({"client_request_id":"approve","decision":"approve"}))
        )
        .await
        .unwrap()
        .0["status"],
        "queued"
    );
    running(&app, &id, "second").await;
    execute_run(
        app.clone(),
        id.clone(),
        "second".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
    // Re-entering the completed generation must not dispatch the action twice.
    execute_run(
        app.clone(),
        id.clone(),
        "second".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 2);
    let outputs = requests[1]["input"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| v["type"] == "function_call_output")
        .collect::<Vec<_>>();
    assert_eq!(outputs.len(), 1);
    assert_eq!(outputs[0]["call_id"], "approved-call");
    let result: Value = serde_json::from_str(outputs[0]["output"].as_str().unwrap()).unwrap();
    assert_eq!(result["status"], "success");
    assert_eq!(result["output"].as_str().unwrap().trim(), marker_text);
    server.abort();
}

#[tokio::test]
#[ignore = "requires running v3 worker"]
async fn approved_shell_executes_once_and_returns_result() {
    approved_worker_tool(
        "shell",
        json!({"command":"printf v3-approved-execution"}),
        "v3-approved-execution",
    )
    .await;
}

#[tokio::test]
#[ignore = "requires running v3 worker"]
async fn approved_python_executes_once_and_returns_result() {
    approved_worker_tool(
        "python",
        json!({"code":"print('v3-approved-python')"}),
        "v3-approved-python",
    )
    .await;
}

#[tokio::test]
async fn snapshot_partial_and_cursor_cover_exact_committed_prefix() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    running(&app, &id, "stream").await;
    let mut run = app.run(&id).await.unwrap();
    run["stream_after"] = json!(0);
    app.save(&mut run).await.unwrap();
    app.store
        .event(&sid, "delta", &json!({"run_id":id,"text":"first "}))
        .await
        .unwrap();
    app.store
        .event(
            &sid,
            "delta",
            &json!({"run_id":"other-run","text":"ignore"}),
        )
        .await
        .unwrap();
    app.store
        .event(&sid, "delta", &json!({"run_id":id,"text":"second"}))
        .await
        .unwrap();
    let snap = snapshot(State(app.clone()), Path(sid.clone()))
        .await
        .unwrap()
        .0;
    assert_eq!(snap["partial"]["run_id"], id);
    assert_eq!(snap["partial"]["text"], "first second");
    let cursor = snap["last_seq"].as_i64().unwrap();
    assert!(cursor > 0);
    app.store
        .event(&sid, "delta", &json!({"run_id":id,"text":" third"}))
        .await
        .unwrap();
    let remaining = app.store.events(&sid, cursor).await.unwrap();
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0]["payload"]["text"], " third");
    assert!(remaining[0]["seq"].as_i64().unwrap() > cursor);
}

#[tokio::test]
async fn sse_last_event_id_replays_only_events_after_reconnect_cursor() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    app.store
        .event(&sid, "fixture", &json!({"position":"before"}))
        .await
        .unwrap();
    let cursor = app.store.events(&sid, 0).await.unwrap().last().unwrap()["seq"]
        .as_i64()
        .unwrap();
    app.store
        .event(&sid, "fixture", &json!({"position":"after"}))
        .await
        .unwrap();
    let expected = app.store.events(&sid, cursor).await.unwrap();
    assert_eq!(expected.len(), 1);
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600}),
        )
        .await
        .unwrap();
    let request = Request::builder()
        .uri(format!("/api/v3/sessions/{sid}/events?after=0"))
        .header("cookie", format!("v3_session={token}"))
        .header("last-event-id", cursor.to_string())
        .body(Body::empty())
        .unwrap();
    let response = router(app.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    let chunk = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let text = std::str::from_utf8(&chunk).unwrap();
    let data = text
        .lines()
        .find_map(|line| line.strip_prefix("data:").map(str::trim))
        .unwrap();
    let event: Value = serde_json::from_str(data).unwrap();
    assert_eq!(event, expected[0]);
    assert_eq!(event["payload"]["position"], "after");
    assert!(text
        .lines()
        .any(|line| line == format!("id: {}", expected[0]["seq"])));
    drop(stream);
}

#[tokio::test]
async fn expired_and_revoked_sessions_cannot_open_authenticated_routes() {
    let (_root, app) = fixture().await;
    let token = uuid();
    let auth_key = format!("auth:{}", digest(&json!(token)));
    app.store
        .put(&auth_key, &json!({"csrf":"fixture","expires":now()-1}))
        .await
        .unwrap();
    let request = || {
        Request::builder()
            .uri("/api/v3/status")
            .header("cookie", format!("v3_session={token}"))
            .body(Body::empty())
            .unwrap()
    };
    assert_eq!(
        router(app.clone())
            .oneshot(request())
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    app.store
        .put(&auth_key, &json!({"csrf":"fixture","expires":now()+3600}))
        .await
        .unwrap();
    assert_eq!(
        router(app.clone())
            .oneshot(request())
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    app.store.delete(&auth_key).await.unwrap();
    assert_eq!(
        router(app.clone())
            .oneshot(request())
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn first_run_setup_is_single_claim_and_revokes_old_sessions() {
    let (_root, app) = fixture().await;
    let old_token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(old_token))),
            &json!({"user":"admin","csrf":"old","generation":null,"expires":now()+3600}),
        )
        .await
        .unwrap();
    app.store.delete("admin").await.unwrap();
    assert_eq!(
        setup_status(State(app.clone())).await.unwrap().0["initialized"],
        false
    );
    let response = setup_admin(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"new-secret"})),
    )
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(response.headers().get("set-cookie").is_some());
    assert_eq!(
        setup_status(State(app.clone())).await.unwrap().0["initialized"],
        true
    );
    let repeated = setup_admin(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"other","password":"another-secret"})),
    )
    .await
    .unwrap_err();
    assert_eq!(repeated.0, StatusCode::CONFLICT);
    let old = router(app.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v3/status")
                .header("cookie", format!("v3_session={old_token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(old.status(), StatusCode::UNAUTHORIZED);
    let wrong = login(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"admin","password":"new-secret"})),
    )
    .await
    .unwrap_err();
    assert_eq!(wrong.0, StatusCode::UNAUTHORIZED);
    let valid = login(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"new-secret"})),
    )
    .await
    .unwrap();
    assert_eq!(valid.status(), StatusCode::OK);
}
