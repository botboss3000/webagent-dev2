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
        message_api_token_hash: None,
        browser_login_bridge: None,
        store,
        abilities: Arc::new(
            abilities::Catalog::load(&PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("plugins/abilities"))
                .unwrap(),
        ),
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
        tool_socket: root.path().join("worker.sock"),
        promotion_socket: root.path().join("promote.sock"),
        chat_ui_config_path: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("ui/chat/chat_ui.json"),
        secure: false,
    };
    (root, app)
}

#[tokio::test]
async fn chat_ui_config_reads_data_config_on_each_request_and_requires_auth() {
    let (root, mut app) = fixture().await;
    app.chat_ui_config_path = root.path().join("config/chat_ui.json");
    std::fs::create_dir_all(app.chat_ui_config_path.parent().unwrap()).unwrap();
    std::fs::write(
        &app.chat_ui_config_path,
        r#"{"chat_common":{"messages":{"pill_placeholder":"First"}}}"#,
    )
    .unwrap();
    let request = || {
        Request::builder()
            .uri("/api/v3/auth/ui-config")
            .body(Body::empty())
            .unwrap()
    };
    assert_eq!(
        router(app.clone()).oneshot(request()).await.unwrap().status(),
        StatusCode::UNAUTHORIZED
    );
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    for expected in ["First", "Updated"] {
        if expected == "Updated" {
            std::fs::write(
                &app.chat_ui_config_path,
                r#"{"chat_common":{"messages":{"pill_placeholder":"Updated"}}}"#,
            )
            .unwrap();
        }
        let response = router(app.clone())
            .oneshot(
                Request::builder()
                    .uri("/api/v3/auth/ui-config")
                    .header("cookie", format!("v3_session={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
        let config: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(config["chat_ui"]["chat_common"]["messages"]["pill_placeholder"], expected);
    }
}

async fn activate_codebase(app: &App, sid: &str) {
    let state = session_ability_set(
        State(app.clone()),
        Path((sid.to_owned(), "codebase_admin".into())),
        Json(SessionAbilityBody {
            state: "active".into(),
            expected_revision: 0,
        }),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(state["active"], json!(["codebase_admin"]));
}

#[tokio::test]
async fn agent_ability_off_is_persisted_and_conflicts_on_stale_revision() {
    let (_root, app) = fixture().await;
    let initial = agent_ability_catalog(State(app.clone()), Path("shared_default".into()))
        .await.unwrap().0;
    assert_eq!(initial["revision"], 0);
    let changed = agent_ability_visibility_set(
        State(app.clone()), Path(("shared_default".into(), "codebase_admin".into())),
        Json(AbilityVisibilityBody { visibility: "off".into(), expected_revision: 0 }),
    ).await.unwrap().0;
    assert_eq!(changed["revision"], 1);
    assert_eq!(changed["visibility"]["codebase_admin"], "off");
    let catalog = agent_ability_catalog(State(app.clone()), Path("shared_default".into()))
        .await.unwrap().0;
    assert_eq!(catalog["abilities"][0]["visibility"], "off");
    assert_eq!(app.store.agent_abilities().await.unwrap(), changed);
    let stale = agent_ability_visibility_set(
        State(app), Path(("shared_default".into(), "codebase_admin".into())),
        Json(AbilityVisibilityBody { visibility: "visible".into(), expected_revision: 0 }),
    ).await.unwrap_err();
    assert_eq!(stale.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn agent_tool_off_revokes_schema_and_survives_reopen() {
    let (root, app) = fixture().await;
    let result = agent_tool_mode_set(
        State(app.clone()),
        Path(("shared_default".into(), "codebase_admin".into(), "file_read".into())),
        Json(AbilityVisibilityBody { visibility: "off".into(), expected_revision: 0 }),
    ).await.unwrap().0;
    assert_eq!(result["tool_modes"]["file_read"], "off");
    let state = json!({"active":["codebase_admin"],"suppressed":[]});
    let policy = policy_default();
    assert!(!app.abilities.tool_available("file_read", &result, &state, &json!(["codebase_admin"])));
    assert!(!app.abilities.schemas_with(&policy, &json!(["codebase_admin"]), &result, &state)
        .iter().any(|schema| schema["name"] == "file_read"));
    assert_eq!(app.store.agent_abilities().await.unwrap()["tool_modes"]["file_read"], "off");
    app.store.app_pool.close().await;
    app.store.pool.close().await;
    app.store.agent_pool.close().await;
    let reopened = Store::open(root.path()).await.unwrap();
    assert_eq!(reopened.agent_abilities().await.unwrap()["tool_modes"]["file_read"], "off");
}

#[tokio::test]
async fn discoverable_tool_requires_loading_its_ability() {
    let (_root, app) = fixture().await;
    let result = agent_tool_mode_set(
        State(app.clone()),
        Path(("shared_default".into(), "codebase_admin".into(), "file_read".into())),
        Json(AbilityVisibilityBody { visibility: "discoverable".into(), expected_revision: 0 }),
    ).await.unwrap().0;
    let state = json!({"active":["codebase_admin"],"suppressed":[]});
    assert!(!app.abilities.tool_available("file_read", &result, &state, &json!([])));
    assert!(app.abilities.tool_available("file_read", &result, &state,
        &json!(["codebase_admin"])));
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
        .save_agent_model(&json!({"provider":"openai_subscription","model":"gpt-5.4"}))
        .await
        .unwrap();
    migrate_default_agent_model(&app.store).await.unwrap();
    assert_eq!(
        app.store
            .agent_model()
            .await
            .unwrap()["model"],
        DEFAULT_AGENT_MODEL
    );
    app.store
        .save_agent_model(&json!({"provider":"openai_subscription","model":"gpt-6-sol"}))
        .await
        .unwrap();
    migrate_default_agent_model(&app.store).await.unwrap();
    assert_eq!(
        app.store
            .agent_model()
            .await
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
    let (chat, _) = app.store.chat_snapshot(&sid).await.unwrap();
    assert_eq!(chat.len(), 1);
    assert_eq!(chat[0]["role"], "user");
    assert_eq!(chat[0]["content"], "hello");
    assert_eq!(app.store.context(&sid).await.unwrap().len(), 1);
    assert_eq!(app.store.list("run:").await.unwrap().len(), 1);
}

#[tokio::test]
async fn agent_proposal_is_visible_and_admin_can_promote_the_same_review() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let rid = uuid();
    let baseline = "b".repeat(40);
    let expected = hex::encode(Sha256::digest(format!("{baseline}\0patch").as_bytes()));
    let worker_listener = UnixListener::bind(&app.tool_socket).unwrap();
    let review_digest = expected.clone();
    let worker = tokio::spawn(async move {
        for _ in 0..2 {
            let (stream, _) = worker_listener.accept().await.unwrap();
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["name"], "source_review");
            let reply = json!({"id":call["id"],"status":"success","truncated":false,
                "review_sha256":review_digest,"baseline_commit":baseline,
                "status_output":" M src/main.rs","diff":"patch"});
            reader
                .get_mut()
                .write_all(format!("{reply}\n").as_bytes())
                .await
                .unwrap();
        }
    });
    let proposal = propose_promotion(
        &app,
        &sid,
        &rid,
        "run-test",
        &json!({"expected_review_sha256":expected,"summary":"Verified Rust change"}),
        1,
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(proposal["status"], "success");
    assert_eq!(proposal["state"], "proposed");
    assert_eq!(app.store.list("promotion:").await.unwrap().len(), 1);
    let record = app
        .store
        .get(&format!("promotion:{sid}:{rid}"))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(record["patch"], "patch");
    assert_eq!(record["proposed_by_run"], "run-test");
    let repeated = propose_promotion(
        &app,
        &sid,
        &rid,
        "run-test",
        &json!({"expected_review_sha256":expected,"summary":"Verified Rust change"}),
        1,
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(repeated["state"], "proposed");
    assert_eq!(app.store.list("promotion:").await.unwrap().len(), 1);

    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let listing = Request::builder()
        .uri(format!("/api/v3/sessions/{sid}/promotions"))
        .header("cookie", &cookie)
        .body(Body::empty())
        .unwrap();
    let response = router(app.clone()).oneshot(listing).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let listed: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(listed["promotions"][0]["state"], "proposed");
    assert!(listed["promotions"][0].get("patch").is_none());

    let review = Request::builder()
        .uri(format!("/api/v3/sessions/{sid}/promotions/{rid}/review"))
        .header("cookie", &cookie)
        .body(Body::empty())
        .unwrap();
    let response = router(app.clone()).oneshot(review).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let reviewed: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(reviewed["diff"], "patch");
    assert_eq!(reviewed["promotion"]["summary"], "Verified Rust change");

    let broker_listener = UnixListener::bind(&app.promotion_socket).unwrap();
    let broker = tokio::spawn(async move {
        let (stream, _) = broker_listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        let reply = json!({"id":call["id"],"status":"success",
            "patch_sha256":call["patch_sha256"],"paths":["src/main.rs"]});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let payload = json!({"client_request_id":rid,"expected_review_sha256":expected});
    let request = Request::builder()
        .method("POST")
        .uri(format!("/api/v3/sessions/{sid}/promotions"))
        .header("cookie", &cookie)
        .header("x-csrf-token", "fixture")
        .header("content-type", "application/json")
        .body(Body::from(payload.to_string()))
        .unwrap();
    let response = router(app.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let promoted: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(promoted["state"], "applied");
    assert_eq!(promoted["summary"], "Verified Rust change");
    worker.await.unwrap();
    broker.await.unwrap();
    let events = app.store.events(&sid, 0).await.unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "promotion_proposed")
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "promotion_requested")
            .count(),
        1
    );
}

#[tokio::test]
async fn proposal_respects_policy_and_rejects_stale_review() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let rid = uuid();
    let expected = "a".repeat(64);
    let args = json!({"expected_review_sha256":expected,"summary":"Test"});
    let mut policy = policy_default();
    policy["rules"]["promotion_propose"] = json!("deny");
    app.store.put("policy", &policy).await.unwrap();
    let denied = propose_promotion(
        &app,
        &sid,
        &rid,
        "run-test",
        &args,
        1,
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(denied["status"], "denied");
    assert!(app.store.list("promotion:").await.unwrap().is_empty());

    app.store.put("policy", &policy_default()).await.unwrap();
    let listener = UnixListener::bind(&app.tool_socket).unwrap();
    let worker = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        let reply = json!({"id":call["id"],"status":"success","truncated":false,
            "review_sha256":"b".repeat(64),"baseline_commit":"c".repeat(40),
            "diff":"patch"});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let stale = propose_promotion(
        &app,
        &sid,
        &rid,
        "run-test",
        &args,
        1,
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(stale["status"], "denied");
    assert!(app.store.list("promotion:").await.unwrap().is_empty());
    worker.await.unwrap();
}

#[tokio::test]
async fn promotion_requires_auth_and_csrf_and_reuses_durable_receipt() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let uri = format!("/api/v3/sessions/{sid}/promotions");
    let request_id = uuid();
    let baseline = "b".repeat(40);
    let expected = hex::encode(Sha256::digest(format!("{baseline}\0patch").as_bytes()));
    let payload = json!({"client_request_id":request_id,"expected_review_sha256":expected});
    let request = |cookie: Option<&str>, csrf: Option<&str>| {
        let mut builder = Request::builder()
            .method("POST")
            .uri(&uri)
            .header("content-type", "application/json");
        if let Some(cookie) = cookie {
            builder = builder.header("cookie", cookie);
        }
        if let Some(csrf) = csrf {
            builder = builder.header("x-csrf-token", csrf);
        }
        builder.body(Body::from(payload.to_string())).unwrap()
    };
    assert_eq!(
        router(app.clone())
            .oneshot(request(None, None))
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    assert_eq!(
        router(app.clone())
            .oneshot(request(Some(&cookie), None))
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );

    let worker_listener = UnixListener::bind(&app.tool_socket).unwrap();
    let broker_listener = UnixListener::bind(&app.promotion_socket).unwrap();
    let review_digest = expected.clone();
    let worker = tokio::spawn(async move {
        let (stream, _) = worker_listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(call["name"], "source_review");
        let reply = json!({"id":call["id"],"status":"success","truncated":false,
            "review_sha256":review_digest,"baseline_commit":baseline,
            "diff":"patch"});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let broker = tokio::spawn(async move {
        let (stream, _) = broker_listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(call["patch"], "patch");
        let reply = json!({"id":call["id"],"status":"success",
            "patch_sha256":call["patch_sha256"],"paths":["src/main.rs"]});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let response = router(app.clone())
        .oneshot(request(Some(&cookie), Some("fixture")))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let value: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(value["state"], "applied");
    assert_eq!(value["paths"][0], "src/main.rs");
    worker.await.unwrap();
    broker.await.unwrap();

    let retry = tokio::time::timeout(
        Duration::from_secs(2),
        router(app.clone()).oneshot(request(Some(&cookie), Some("fixture"))),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(retry.status(), StatusCode::OK);
    let body = axum::body::to_bytes(retry.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let repeated: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(repeated, value);
    assert_eq!(app.store.list("promotion:").await.unwrap().len(), 1);
    let events = app.store.events(&sid, 0).await.unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "promotion_requested")
            .count(),
        1
    );
}

#[tokio::test]
async fn admin_cannot_promote_a_stale_agent_proposal() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let rid = uuid();
    let baseline = "b".repeat(40);
    let expected = hex::encode(Sha256::digest(format!("{baseline}\0old").as_bytes()));
    let body = json!({"client_request_id":rid,"expected_review_sha256":expected});
    app.store
        .put(
            &format!("promotion:{sid}:{rid}"),
            &json!({"session_id":sid,"client_request_id":rid,"state":"proposed",
            "request_digest":digest(&body),"review_sha256":expected,
            "patch_sha256":hex::encode(Sha256::digest(b"old")),
            "patch":"old","created_at":now()}),
        )
        .await
        .unwrap();
    let listener = UnixListener::bind(&app.tool_socket).unwrap();
    let worker = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        let changed = hex::encode(Sha256::digest(format!("{baseline}\0new").as_bytes()));
        let reply = json!({"id":call["id"],"status":"success","truncated":false,
            "review_sha256":changed,"baseline_commit":baseline,"diff":"new"});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let error = promote(State(app.clone()), Path(sid.clone()), Json(body))
        .await
        .unwrap_err();
    assert_eq!(error.0, StatusCode::CONFLICT);
    assert_eq!(
        app.store
            .get(&format!("promotion:{sid}:{rid}"))
            .await
            .unwrap()
            .unwrap()["state"],
        "proposed"
    );
    worker.await.unwrap();
}

#[tokio::test]
async fn admin_rejects_proposal_once_and_csrf_is_required() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let rid = uuid();
    app.store
        .put(
            &format!("promotion:{sid}:{rid}"),
            &json!({"session_id":sid,"client_request_id":rid,"state":"proposed",
            "patch_sha256":"digest","created_at":now()}),
        )
        .await
        .unwrap();
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let uri = format!("/api/v3/sessions/{sid}/promotions/{rid}/reject");
    let request = |csrf: Option<&str>| {
        let mut builder = Request::builder()
            .method("POST")
            .uri(&uri)
            .header("cookie", &cookie);
        if let Some(csrf) = csrf {
            builder = builder.header("x-csrf-token", csrf);
        }
        builder.body(Body::empty()).unwrap()
    };
    let denied = router(app.clone()).oneshot(request(None)).await.unwrap();
    assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    for _ in 0..2 {
        let response = router(app.clone())
            .oneshot(request(Some("fixture")))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let value: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value["state"], "rejected");
    }
    let events = app.store.events(&sid, 0).await.unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "promotion_rejected")
            .count(),
        1
    );
    assert_eq!(
        app.store
            .get(&format!("promotion:{sid}:{rid}"))
            .await
            .unwrap()
            .unwrap()["state"],
        "rejected"
    );
}

#[tokio::test]
async fn interrupted_promotion_becomes_uncertain_without_dispatch() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let rid = uuid();
    app.store
        .put(
            &format!("promotion:{sid}:{rid}"),
            &json!({"session_id":sid,"client_request_id":rid,"state":"dispatching",
            "request_digest":"digest","patch_sha256":"patch","created_at":now()}),
        )
        .await
        .unwrap();
    recover_promotions(&app).await.unwrap();
    let record = app
        .store
        .get(&format!("promotion:{sid}:{rid}"))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(record["state"], "uncertain");
    recover_promotions(&app).await.unwrap();
    let events = app.store.events(&sid, 0).await.unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "promotion_recovered")
            .count(),
        1
    );
}

#[tokio::test]
async fn source_review_route_requires_auth_policy_and_existing_session() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let uri = format!("/api/v3/sessions/{sid}/source-review");
    let request = |path: &str, cookie: Option<&str>| {
        let mut builder = Request::builder().uri(path);
        if let Some(cookie) = cookie {
            builder = builder.header("cookie", cookie);
        }
        builder.body(Body::empty()).unwrap()
    };
    let response = router(app.clone())
        .oneshot(request(&uri, None))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let unknown = format!("/api/v3/sessions/{}/source-review", uuid());
    let response = router(app.clone())
        .oneshot(request(&unknown, Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);

    let mut policy = policy_default();
    policy["rules"]["source_review"] = json!("deny");
    app.store.put("policy", &policy).await.unwrap();
    let response = router(app.clone())
        .oneshot(request(&uri, Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);

    policy["rules"]["source_review"] = json!("require_approval");
    app.store.put("policy", &policy).await.unwrap();
    let response = router(app.clone())
        .oneshot(request(&uri, Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);

    app.store.put("policy", &policy_default()).await.unwrap();
    app.store
        .put("control", &json!({"shutoff":true}))
        .await
        .unwrap();
    let response = router(app.clone())
        .oneshot(request(&uri, Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::CONFLICT);
    app.store
        .put("control", &json!({"shutoff":false}))
        .await
        .unwrap();
    let listener = UnixListener::bind(&app.tool_socket).unwrap();
    let expected_sid = sid.clone();
    let worker = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(call["name"], "source_review");
        assert_eq!(call["workspace"], expected_sid);
        let reply = json!({"id":call["id"],"status":"success",
            "status_output":" M src/main.rs","diff":"patch","truncated":false});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let response = router(app.clone())
        .oneshot(request(&uri, Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    let review: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(review["diff"], "patch");
    worker.await.unwrap();
}

#[tokio::test]
async fn tool_reply_must_match_the_dispatched_request() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, app) = fixture().await;
    let listener = UnixListener::bind(&app.tool_socket).unwrap();
    let worker = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        reader
            .get_mut()
            .write_all(b"{\"id\":\"wrong\",\"status\":\"success\",\"diff\":\"wrong patch\"}\n")
            .await
            .unwrap();
    });
    let result = tools::execute(
        "source_review",
        &json!({}),
        &uuid(),
        &app.tool_socket,
        CancellationToken::new(),
    )
    .await;
    assert!(result.is_err());
    worker.await.unwrap();
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
async fn agent_loop_creates_reviewable_proposal_without_dispatching_broker() {
    use axum::routing::post;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    let (_root, mut app) = fixture().await;
    let baseline = "b".repeat(40);
    let expected = hex::encode(Sha256::digest(format!("{baseline}\0patch").as_bytes()));
    let arguments = json!({"expected_review_sha256":expected,"summary":"Verified coding change"});
    let captured = Arc::new(Mutex::new(Vec::<Value>::new()));
    let requests = captured.clone();
    let tool_args = arguments.to_string();
    let mock = Router::new().route(
        "/responses",
        post(move |Json(body): Json<Value>| {
            let requests = requests.clone();
            let tool_args = tool_args.clone();
            async move {
                let mut requests = requests.lock().await;
                requests.push(body);
                let output = if requests.len() == 1 {
                    json!([{"type":"function_call","call_id":"proposal-call",
                        "name":"promotion_propose","arguments":tool_args}])
                } else {
                    json!([{"type":"message","role":"assistant",
                        "content":[{"type":"output_text","text":"Proposal ready for review."}]}])
                };
                let event = json!({"type":"response.completed",
                    "response":{"status":"completed","output":output}});
                (
                    [("content-type", "text/event-stream")],
                    format!("data: {event}\n\n"),
                )
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));

    let worker_listener = UnixListener::bind(&app.tool_socket).unwrap();
    let worker_digest = expected.clone();
    let worker = tokio::spawn(async move {
        let (stream, _) = worker_listener.accept().await.unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(call["name"], "source_review");
        let reply = json!({"id":call["id"],"status":"success","truncated":false,
            "baseline_commit":baseline,"review_sha256":worker_digest,
            "status_output":" M src/main.rs","diff":"patch"});
        reader
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await
            .unwrap();
    });
    let sid = conversation(&app).await;
    activate_codebase(&app, &sid).await;
    let id = queued(&app, &sid).await;
    let mut run = app.run(&id).await.unwrap();
    run["loaded_abilities"] = json!(["codebase_admin"]);
    app.save(&mut run).await.unwrap();
    running(&app, &id, "proposal-generation").await;
    execute_run(
        app.clone(),
        id.clone(),
        "proposal-generation".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
    worker.await.unwrap();
    let proposals = app.store.list(&format!("promotion:{sid}:")).await.unwrap();
    assert_eq!(proposals.len(), 1);
    assert_eq!(proposals[0]["state"], "proposed");
    assert_eq!(proposals[0]["proposed_by_run"], id);
    let requests = captured.lock().await;
    assert_eq!(requests.len(), 2);
    assert!(requests[0]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "promotion_propose"));
    server.abort();
}

#[tokio::test]
async fn agent_loop_gates_tool_and_denial_continues_without_worker() {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    assert_eq!(
        settings(State(app.clone()), Json(json!({"model":"pinned-model"})))
            .await
            .unwrap()
            .0["provider"],
        "openai_subscription"
    );
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
    activate_codebase(&app, &sid).await;
    let id = queued(&app, &sid).await;
    let mut run = app.run(&id).await.unwrap();
    run["loaded_abilities"] = json!(["codebase_admin"]);
    app.save(&mut run).await.unwrap();
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
    assert_eq!(waiting["provider_selection"]["model"], "pinned-model");
    assert_eq!(
        settings(
            State(app.clone()),
            Json(json!({"model":"replacement-model","provider":"openai_subscription"}))
        )
        .await
        .unwrap()
        .0["model"],
        "replacement-model"
    );
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
    let completed = app.run(&id).await.unwrap();
    assert_eq!(completed["status"], "completed");
    let pre_tool = &completed["workflow"]["receipts"]["pre_tool"];
    let tool_result = &completed["workflow"]["receipts"]["tool_result"];
    assert_eq!(pre_tool["result"]["decision"], "reject");
    assert_eq!(tool_result["result"]["decision"], "reject");
    assert_eq!(pre_tool["generation"], "second");
    assert_eq!(tool_result["generation"], pre_tool["generation"]);
    assert_eq!(tool_result["policy_revision"], pre_tool["policy_revision"]);
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 2);
    assert!(requests
        .iter()
        .all(|request| request["model"] == "pinned-model"));
    let next_sid = conversation(&app).await;
    let next_id = queued(&app, &next_sid).await;
    assert_eq!(
        app.run(&next_id).await.unwrap()["provider_selection"]["model"],
        "replacement-model"
    );
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
async fn catalog_change_revokes_loaded_ability_before_pending_tool() {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    let mock = Router::new().route(
        "/responses",
        post(|| async {
            let event = json!({"type":"response.completed",
            "response":{"status":"completed","output":[{
                "type":"message","role":"assistant",
                "content":[{"type":"output_text","text":"Done."}]
            }]}});
            (
                [("content-type", "text/event-stream")],
                format!("data: {event}\n\n"),
            )
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
    let mut run = app.run(&id).await.unwrap();
    run["loaded_abilities"] = json!(["codebase_admin"]);
    run["ability_catalog_version"] = json!("old-catalog");
    run["phase"] = json!("tools");
    run["calls"] = json!([{"type":"function_call","call_id":"old-call",
        "name":"shell","arguments":"{\"command\":\"printf hello\"}"}]);
    app.save(&mut run).await.unwrap();
    running(&app, &id, "new-catalog").await;
    execute_run(
        app.clone(),
        id.clone(),
        "new-catalog".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let finished = app.run(&id).await.unwrap();
    assert_eq!(finished["status"], "completed");
    assert_eq!(finished["loaded_abilities"], json!([]));
    assert_eq!(finished["ability_catalog_version"], app.abilities.version());
    let output = finished["input"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["type"] == "function_call_output")
        .unwrap();
    assert!(output["output"].as_str().unwrap().contains("denied"));
    server.abort();
}

#[tokio::test]
async fn long_run_compacts_before_model_and_keeps_full_message_history() {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    let _saved = settings(State(app.clone()), Json(json!({"model":"summary-pinned"})))
        .await
        .unwrap();
    let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
    let captured = requests.clone();
    let mock = Router::new()
        .route(
            "/responses",
            post(
                |State(requests): State<Arc<Mutex<Vec<Value>>>>,
                 Json(body): Json<Value>| async move {
                    let mut requests = requests.lock().await;
                    requests.push(body);
                    let text = if requests.len() == 1 {
                        "Earlier work was completed; continue the current task."
                    } else {
                        "Current task completed."
                    };
                    let event = json!({"type":"response.completed",
                        "response":{"status":"completed","output":[{
                            "type":"message","role":"assistant",
                            "content":[{"type":"output_text","text":text}]
                        }]}});
                    (
                        [("content-type", "text/event-stream")],
                        format!("data: {event}\n\n"),
                    )
                },
            ),
        )
        .with_state(captured);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    let mut run = app.run(&id).await.unwrap();
    let mut input = Vec::new();
    for _ in 0..6 {
        input.push(json!({"role":"user","content":"x".repeat(50_000)}));
        input.push(json!({"type":"message","role":"assistant",
            "content":[{"type":"output_text","text":"done"}]}));
    }
    input.push(json!({"role":"user","content":"Current task"}));
    run["input"] = json!(input);
    app.save(&mut run).await.unwrap();
    let _saved = settings(
        State(app.clone()),
        Json(json!({"model":"summary-replacement"})),
    )
    .await
    .unwrap();
    running(&app, &id, "compact").await;
    execute_run(
        app.clone(),
        id.clone(),
        "compact".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();

    let finished = app.run(&id).await.unwrap();
    assert_eq!(finished["status"], "completed");
    assert_eq!(finished["compactions"], 1);
    assert!(serde_json::to_vec(&finished["input"]).unwrap().len() <= 256 * 1024);
    let (chat, _) = app.store.chat_snapshot(&sid).await.unwrap();
    assert_eq!(chat.len(), 2);
    assert_eq!(chat[0]["content"], "hello");
    assert_eq!(chat[1]["content"], "Current task completed.");
    let context = app.store.context(&sid).await.unwrap();
    assert!(context[0]["content"]
        .as_str()
        .unwrap()
        .contains("Earlier conversation summary"));
    let events = app.store.events(&sid, 0).await.unwrap();
    assert!(events
        .iter()
        .any(|event| event["kind"] == "context_compacted"));
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 2);
    assert!(requests
        .iter()
        .all(|request| request["model"] == "summary-pinned"));
    assert!(requests[0]["tools"].is_null());
    assert_eq!(requests[1]["input"][0]["role"], "user");
    assert!(requests[1]["input"][0]["content"]
        .as_str()
        .unwrap()
        .contains("Earlier conversation summary"));
    server.abort();
}

#[tokio::test]
async fn failed_context_summary_keeps_original_input_for_resume() {
    use axum::routing::post;
    let (_root, mut app) = fixture().await;
    let mock = Router::new().route(
        "/responses",
        post(|| async {
            let event = json!({"type":"response.completed",
                "response":{"status":"completed","output":[]}});
            (
                [("content-type", "text/event-stream")],
                format!("data: {event}\n\n"),
            )
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
    let mut original = Vec::new();
    for _ in 0..6 {
        original.push(json!({"role":"user","content":"x".repeat(50_000)}));
        original.push(json!({"type":"message","role":"assistant",
            "content":[{"type":"output_text","text":"done"}]}));
    }
    original.push(json!({"role":"user","content":"Current task"}));
    let mut run = app.run(&id).await.unwrap();
    run["input"] = json!(original);
    app.save(&mut run).await.unwrap();
    running(&app, &id, "failed-summary").await;
    execute_run(
        app.clone(),
        id.clone(),
        "failed-summary".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let paused = app.run(&id).await.unwrap();
    assert_eq!(paused["status"], "paused");
    assert_eq!(paused["input"], json!(original));
    let session = app.store.get(&key("session", &sid)).await.unwrap().unwrap();
    assert_eq!(session["checkpoint_through"], 0);
    assert_eq!(
        app.store.context(&sid).await.unwrap()[0]["content"],
        "hello"
    );
    let events = app.store.events(&sid, 0).await.unwrap();
    assert!(!events
        .iter()
        .any(|event| event["kind"] == "context_compacted"));
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
    let (chat, _) = app.store.chat_snapshot(&sid).await.unwrap();
    assert_eq!(chat.len(), 1);
    assert_eq!(chat[0]["role"], "user");
    server.abort();
}

// Run only through tests/run_real_worker_integration.py in a disposable container.
// The live worker rejects clients that are not the application UID.
#[tokio::test]
#[ignore = "requires the disposable real-worker integration container"]
async fn real_worker_coding_gates_are_end_to_end() {
    use axum::routing::post;

    let socket = PathBuf::from(std::env::var("V3_REAL_WORKER_SOCKET").unwrap());
    let workspace_root = PathBuf::from(std::env::var("V3_REAL_WORKSPACE_ROOT").unwrap());
    let control_dir = PathBuf::from(std::env::var("V3_REAL_CONTROL_DIR").unwrap());
    assert!(socket.exists(), "real worker socket is unavailable");

    async fn scenario(
        socket: &std::path::Path,
        workspace_root: &std::path::Path,
        control_dir: &std::path::Path,
        name: &str,
        arguments: Value,
        marker: &str,
        decision: &str,
    ) {
        let (_root, mut app) = fixture().await;
        app.tool_socket = socket.to_path_buf();
        app.control_dir = control_dir.to_path_buf();
        let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
        let captured = requests.clone();
        let call = json!({"type":"function_call","call_id":"real-worker-call",
            "name":name,"arguments":arguments.to_string()});
        let mock = Router::new().route(
            "/responses",
            post(move |Json(body): Json<Value>| {
                let captured = captured.clone();
                let call = call.clone();
                async move {
                    let mut requests = captured.lock().await;
                    requests.push(body);
                    let output = match requests.len() {
                        1 => json!([{"type":"function_call","call_id":"load-codebase",
                            "name":"load_ability",
                            "arguments":json!({"ability_id":"codebase_admin"}).to_string()}]),
                        2 => json!([call]),
                        _ => json!([{"type":"message","role":"assistant",
                            "content":[{"type":"output_text","text":"Done."}]}]),
                    };
                    let event = json!({"type":"response.completed",
                        "response":{"status":"completed","output":output}});
                    (
                        [("content-type", "text/event-stream")],
                        format!("data: {event}\n\n"),
                    )
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });
        app.provider = provider::Provider::for_test(format!("http://{address}/responses"));

        let sid = conversation(&app).await;
        let effect = workspace_root.join(&sid).join(marker);
        assert!(!effect.exists());
        let id = queued(&app, &sid).await;
        if decision == "read_only" {
            let _ = session_mode_set(
                State(app.clone()),
                Path(sid.clone()),
                Json(SessionModeBody {
                    mode: "read_only".into(),
                }),
            )
            .await
            .unwrap();
        }

        running(&app, &id, "first").await;
        execute_run(
            app.clone(),
            id.clone(),
            "first".into(),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        assert!(!effect.exists(), "the worker ran before approval");
        let first = app.run(&id).await.unwrap();
        if decision == "read_only" {
            assert_eq!(first["status"], "completed");
        } else {
            assert_eq!(
                first["status"],
                "waiting_approval",
                "tool={name}, requests={:?}, run={first}",
                requests.lock().await
            );
            assert_eq!(first["pending"]["name"], name);
            let pending = first["pending"]["id"].as_str().unwrap().to_owned();
            let chosen = if decision == "deny" {
                "deny"
            } else {
                "approve"
            };
            assert_eq!(
                approval(
                    State(app.clone()),
                    Path(pending),
                    Json(json!({"client_request_id":"real-worker-decision",
                        "decision":chosen})),
                )
                .await
                .unwrap()
                .0["status"],
                "queued"
            );
            assert!(!effect.exists(), "approval itself dispatched the worker");
            if decision == "shutoff" {
                assert_eq!(
                    control(
                        State(app.clone()),
                        Json(json!({"client_request_id":"real-worker-shutoff",
                            "shutoff":true})),
                    )
                    .await
                    .unwrap()
                    .0["shutoff"],
                    true
                );
                assert_eq!(app.run(&id).await.unwrap()["status"], "stopped");
                assert!(!effect.exists());
                server.abort();
                return;
            }
            running(&app, &id, "second").await;
            execute_run(
                app.clone(),
                id.clone(),
                "second".into(),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        }

        assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
        let requests = requests.lock().await;
        assert_eq!(requests.len(), 3);
        let outputs = requests[2]["input"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|value| {
                value["type"] == "function_call_output" && value["call_id"] == "real-worker-call"
            })
            .collect::<Vec<_>>();
        assert_eq!(outputs.len(), 1);
        let result: Value = serde_json::from_str(outputs[0]["output"].as_str().unwrap()).unwrap();
        if decision == "approve" {
            assert_eq!(result["status"], "success");
            assert_eq!(result["output"].as_str().unwrap().trim(), "1101");
            assert_eq!(std::fs::read_to_string(&effect).unwrap(), "once\n");
            drop(requests);
            execute_run(
                app.clone(),
                id.clone(),
                "second".into(),
                CancellationToken::new(),
            )
            .await
            .unwrap();
            assert_eq!(std::fs::read_to_string(&effect).unwrap(), "once\n");
        } else {
            assert_eq!(result["status"], "denied");
            assert!(!effect.exists(), "a denied tool changed the workspace");
        }
        server.abort();
    }

    scenario(
        &socket,
        &workspace_root,
        &control_dir,
        "shell",
        json!({"command":"printf 'once\\n' >> shell-effect.log; id -u"}),
        "shell-effect.log",
        "approve",
    )
    .await;
    scenario(
        &socket,
        &workspace_root,
        &control_dir,
        "python",
        json!({"code":"import os; open('python-effect.log','a').write('once'+chr(10)); print(os.getuid())"}),
        "python-effect.log",
        "approve",
    )
    .await;
    scenario(
        &socket,
        &workspace_root,
        &control_dir,
        "shell",
        json!({"command":"printf denied >> denied-effect.log"}),
        "denied-effect.log",
        "deny",
    )
    .await;
    scenario(
        &socket,
        &workspace_root,
        &control_dir,
        "shell",
        json!({"command":"printf read-only >> read-only-effect.log"}),
        "read-only-effect.log",
        "read_only",
    )
    .await;
    scenario(
        &socket,
        &workspace_root,
        &control_dir,
        "shell",
        json!({"command":"printf stopped >> stopped-effect.log"}),
        "stopped-effect.log",
        "shutoff",
    )
    .await;
}

#[tokio::test]
async fn private_events_never_appear_in_chat_snapshot_or_cursor() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    let before = snapshot(State(app.clone()), Path(sid.clone()))
        .await
        .unwrap()
        .0;
    assert_eq!(before["messages"].as_array().unwrap().len(), 1);
    let cursor = before["last_seq"].as_i64().unwrap();
    app.store
        .event(&sid, "delta", &json!({"run_id":id,"text":"draft"}))
        .await
        .unwrap();
    let after = snapshot(State(app.clone()), Path(sid.clone()))
        .await
        .unwrap()
        .0;
    assert_eq!(after["last_seq"], cursor);
    assert_eq!(after["messages"], before["messages"]);
    assert!(app
        .store
        .chat_events(&sid, cursor)
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn sse_last_event_id_replays_only_chat_after_reconnect_cursor() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    let cursor = app.store.chat_snapshot(&sid).await.unwrap().1;
    app.store
        .event(&sid, "delta", &json!({"run_id":id,"text":"draft"}))
        .await
        .unwrap();
    let mut run = app.run(&id).await.unwrap();
    run["status"] = json!("completed");
    run["turns"] = json!(1);
    app.store
        .save_run(
            &run,
            "model_output",
            &json!({
                "output":[{"type":"message","role":"assistant",
                    "content":[{"type":"output_text","text":"done"}]}],
                "terminal_text":"done"
            }),
        )
        .await
        .unwrap();
    let expected = app.store.chat_events(&sid, cursor).await.unwrap();
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
    assert_eq!(event["content"], "done");
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
    // The account table remains authoritative after the rollback-only document is gone.
    app.store.delete("admin").await.unwrap();
    assert_eq!(
        setup_status(State(app.clone())).await.unwrap().0["initialized"],
        true
    );
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
    let profile = app.store.user_profile("admin").await.unwrap().unwrap();
    assert_eq!(profile["display_name"], "operator");
    assert!(profile["last_login_at"].as_u64().unwrap() > 0);
    let cookie = valid
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap();
    let me_response = router(app.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v3/auth/me")
                .header("cookie", cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(me_response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(me_response.into_body(), 4096)
        .await
        .unwrap();
    let me: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(me["user_id"], "admin");
    assert_eq!(me["display_name"], "operator");
    assert_eq!(me["is_admin"], true);
}

#[tokio::test]
async fn account_panel_updates_profile_and_rotates_password_generation() {
    let (_root, app) = fixture().await;
    app.store.delete("admin").await.unwrap();
    setup_admin(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"first-secret"})),
    )
    .await
    .unwrap();
    let signed_in = login(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"first-secret"})),
    )
    .await
    .unwrap();
    let cookie = signed_in
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let old_generation =
        app.store.account_by_id("admin").await.unwrap().unwrap()["generation"].clone();
    let doc = json!({"_user_id":"admin","user":"operator"});
    assert_eq!(
        update_profile(
            State(app.clone()),
            axum::Extension(doc.clone()),
            Json(json!({"username":"operator2","display_name":"Operator One"})),
        )
        .await
        .unwrap()
        .0["display_name"],
        "Operator One"
    );
    assert_eq!(
        change_password(
            State(app.clone()),
            axum::Extension(doc.clone()),
            Json(json!({"current_password":"wrong","new_password":"second-secret"})),
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::FORBIDDEN
    );
    let _ = change_password(
        State(app.clone()),
        axum::Extension(doc),
        Json(json!({"current_password":"first-secret","new_password":"second-secret"})),
    )
    .await
    .unwrap();
    let new_account = app.store.account_by_id("admin").await.unwrap().unwrap();
    assert_ne!(new_account["generation"], old_generation);
    assert_eq!(
        app.store.get("admin").await.unwrap().unwrap()["generation"],
        new_account["generation"]
    );
    let revoked = router(app.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v3/auth/me")
                .header("cookie", cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(revoked.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        login(
            State(app.clone()),
            HeaderMap::new(),
            Json(json!({"username":"operator","password":"first-secret"}))
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        login(
            State(app),
            HeaderMap::new(),
            Json(json!({"username":"operator2","password":"second-secret"}))
        )
        .await
        .unwrap()
        .status(),
        StatusCode::OK
    );
}
#[tokio::test]
async fn sign_in_duration_and_session_revocation_use_the_app_store() {
    let (_root, app) = fixture().await;
    app.store.delete("admin").await.unwrap();
    setup_admin(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"first-secret"})),
    )
    .await
    .unwrap();
    let doc = json!({"_user_id":"admin"});
    assert_eq!(
        update_session_policy(
            State(app.clone()),
            axum::Extension(doc.clone()),
            Json(json!({"days":2})),
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::BAD_REQUEST
    );
    let _ = update_session_policy(
        State(app.clone()),
        axum::Extension(doc),
        Json(json!({"days":7})),
    )
    .await
    .unwrap();
    let response = login(
        State(app.clone()),
        HeaderMap::new(),
        Json(json!({"username":"operator","password":"first-secret"})),
    )
    .await
    .unwrap();
    let cookie_header = response
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(cookie_header.contains("Max-Age=604800"));
    let cookie = cookie_header.split(';').next().unwrap().to_owned();
    let body = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let login_body: Value = serde_json::from_slice(&body).unwrap();
    let listing = router(app.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v3/auth/sessions")
                .header("cookie", &cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(listing.status(), StatusCode::OK);
    let body = axum::body::to_bytes(listing.into_body(), 8192)
        .await
        .unwrap();
    let sessions: Value = serde_json::from_slice(&body).unwrap();
    let current = sessions["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["current"] == true)
        .unwrap();
    let id = current["id"].as_str().unwrap();
    let revoked = router(app.clone())
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/api/v3/auth/sessions/{id}"))
                .header("cookie", &cookie)
                .header("x-csrf-token", login_body["csrf_token"].as_str().unwrap())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(revoked.status(), StatusCode::OK);
    let denied = router(app)
        .oneshot(
            Request::builder()
                .uri("/api/v3/auth/me")
                .header("cookie", &cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(denied.status(), StatusCode::UNAUTHORIZED);
}
#[tokio::test]
async fn agent_model_settings_normalize_without_losing_selected_model() {
    let (_root, app) = fixture().await;
    app.store
        .save_agent_model(&json!({"provider":"openai_subscription","model":"legacy-chosen"}))
        .await
        .unwrap();
    migrate_provider_selection(&app.store).await.unwrap();
    migrate_provider_selection(&app.store).await.unwrap();
    let value = app
        .store
        .agent_model()
        .await
        .unwrap();
    assert_eq!(
        value,
        json!({"provider":"openai_subscription","model":"legacy-chosen"})
    );
    let read = agent_model(State(app.clone()), Path("shared_default".into()))
        .await
        .unwrap()
        .0;
    assert_eq!(read["provider"], "openai_subscription");
    assert_eq!(read["model"], "legacy-chosen");
}

#[tokio::test]
async fn unsupported_provider_is_rejected_without_changing_settings() {
    let (_root, app) = fixture().await;
    let original = json!({"model":"original","provider":"openai_subscription"});
    app.store
        .save_agent_model(&original)
        .await
        .unwrap();
    for provider in [json!("unsupported"), json!(null), json!(7)] {
        let body = json!({"model":"replacement","provider":provider});
        assert_eq!(
            settings(State(app.clone()), Json(body.clone()))
                .await
                .unwrap_err()
                .0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            agent_model_set(
                State(app.clone()),
                Path("shared_default".into()),
                Json(body)
            )
            .await
            .unwrap_err()
            .0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            app.store
                .agent_model()
                .await
                .unwrap(),
            original
        );
    }
}

#[tokio::test]
async fn legacy_run_binds_selection_once_and_invalid_selection_pauses_before_dispatch() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let id = queued(&app, &sid).await;
    let mut run = app.run(&id).await.unwrap();
    run.as_object_mut().unwrap().remove("provider_selection");
    // Exceeding the turn limit stops before HTTP, after the legacy binding checkpoint.
    run["turns"] = json!(50);
    app.save(&mut run).await.unwrap();
    running(&app, &id, "legacy").await;
    execute_run(
        app.clone(),
        id.clone(),
        "legacy".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let pinned = app.run(&id).await.unwrap()["provider_selection"].clone();
    assert_eq!(pinned["provider"], "openai_subscription");
    let sid2 = conversation(&app).await;
    let id2 = queued(&app, &sid2).await;
    let mut invalid = app.run(&id2).await.unwrap();
    invalid["provider_selection"] = json!({"provider":"unsupported","model":"valid"});
    app.save(&mut invalid).await.unwrap();
    running(&app, &id2, "invalid").await;
    execute_run(
        app.clone(),
        id2.clone(),
        "invalid".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let paused = app.run(&id2).await.unwrap();
    assert_eq!(paused["status"], "paused");
    assert_eq!(paused["attempt"], 0);
}

#[tokio::test]
async fn state_inspection_is_authenticated_policy_controlled_and_session_bounded() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let other = conversation(&app).await;
    let large = "x".repeat(800);
    for (session, content) in [(&sid, large.as_str()), (&other, "other session")] {
        sqlx::query(
            "INSERT INTO chat(id,session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
        )
        .bind(uuid())
        .bind(session)
        .bind("run-test")
        .bind("user")
        .bind(app.store.seal(&json!(content)).unwrap())
        .bind(1_i64)
        .execute(&app.store.pool)
        .await
        .unwrap();
    }

    let args: InspectArgs = serde_json::from_value(json!({"resource":"chat","limit":1})).unwrap();
    let page = inspect_state(&app, &sid, &args).await.unwrap();
    assert_eq!(page["messages"].as_array().unwrap().len(), 1);
    assert_eq!(
        page["messages"][0]["content_preview"]["text"]
            .as_str()
            .unwrap()
            .len(),
        512
    );
    assert_eq!(page["messages"][0]["content_preview"]["truncated"], true);
    assert!(page["messages"][0].get("content").is_none());
    assert!(!page.to_string().contains("other session"));
    assert!(serde_json::from_value::<InspectArgs>(
        json!({"resource":"session","session_id":other})
    )
    .is_err());
    for invalid in [
        json!({"resource":"chat","after":-1}),
        json!({"resource":"chat","limit":51}),
        json!({"resource":"runs","after":1}),
        json!({"resource":"unknown"}),
    ] {
        let parsed: InspectArgs = serde_json::from_value(invalid).unwrap();
        assert!(validate_inspect_args(&parsed).is_err());
    }

    let uri = format!("/api/v3/sessions/{sid}/state-inspect?resource=chat&limit=1");
    let request = |cookie: Option<&str>| {
        let mut builder = Request::builder().uri(&uri);
        if let Some(cookie) = cookie {
            builder = builder.header("cookie", cookie);
        }
        builder.body(Body::empty()).unwrap()
    };
    let response = router(app.clone()).oneshot(request(None)).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let response = router(app.clone())
        .oneshot(request(Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let returned: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        returned["messages"][0]["content_preview"]["truncated"],
        true
    );

    let mut policy = policy_default();
    policy["rules"]["state_inspect"] = json!("deny");
    app.store.put("policy", &policy).await.unwrap();
    let response = router(app.clone())
        .oneshot(request(Some(&cookie)))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}
#[tokio::test]
async fn agent_discovers_ability_then_inspects_only_its_current_session() {
    use axum::routing::post;

    let (_root, mut app) = fixture().await;
    let sid = conversation(&app).await;
    let other = conversation(&app).await;
    let private_other_text = "only in the other session";
    let _ = send(
        State(app.clone()),
        Path(other.clone()),
        Json(json!({"client_message_id":uuid(),"text":private_other_text})),
    )
    .await
    .unwrap();
    let long_text = "current session text ".repeat(40);
    let sent = send(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"client_message_id":uuid(),"text":long_text})),
    )
    .await
    .unwrap();
    let id = sent.1 .0["run_id"].as_str().unwrap().to_owned();
    let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
    let captured = requests.clone();
    let other_for_call = other.clone();
    let mock = Router::new().route(
        "/responses",
        post(move |Json(body): Json<Value>| {
            let captured = captured.clone();
            let other_for_call = other_for_call.clone();
            async move {
                let mut captured = captured.lock().await;
                captured.push(body);
                let output = match captured.len() {
                    1 => json!([{"type":"function_call","call_id":"discover",
                        "name":"list_abilities","arguments":"{}"}]),
                    2 => json!([{"type":"function_call","call_id":"load",
                        "name":"load_ability",
                        "arguments":"{\"ability_id\":\"codebase_admin\"}"}]),
                    3 => json!([{"type":"function_call","call_id":"inspect",
                        "name":"state_inspect",
                        "arguments":"{\"resource\":\"chat\",\"limit\":1}"}]),
                    4 => json!([{"type":"function_call","call_id":"cross-session",
                        "name":"state_inspect",
                        "arguments":json!({"resource":"chat","session_id":other_for_call}).to_string()}]),
                    5 => json!([{"type":"message","role":"assistant",
                        "content":[{"type":"output_text","text":"Inspection complete."}]}]),
                    _ => panic!("Unexpected provider turn"),
                };
                let event = json!({"type":"response.completed",
                    "response":{"status":"completed","output":output}});
                (
                    [("content-type", "text/event-stream")],
                    format!("data: {event}\n\n"),
                )
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    running(&app, &id, "discover-inspect").await;
    execute_run(
        app.clone(),
        id.clone(),
        "discover-inspect".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();

    assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
    assert_eq!(
        app.run(&id).await.unwrap()["loaded_abilities"],
        json!(["codebase_admin"])
    );
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 5);
    let names = |request: &Value| {
        request["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|tool| tool["name"].as_str().map(str::to_owned))
            .collect::<Vec<_>>()
    };
    for request in &requests[..2] {
        assert_eq!(names(request), vec!["list_abilities", "load_ability"]);
    }
    assert!(names(&requests[2]).contains(&"state_inspect".to_owned()));
    let result_for = |request: &Value, call_id: &str| -> Value {
        let item = request["input"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["type"] == "function_call_output" && item["call_id"] == call_id)
            .unwrap();
        serde_json::from_str(item["output"].as_str().unwrap()).unwrap()
    };
    let discovery = result_for(&requests[1], "discover");
    assert_eq!(discovery["status"], "ok");
    assert!(discovery["abilities"]
        .as_array()
        .unwrap()
        .iter()
        .any(|ability| ability["id"] == "codebase_admin"));
    let loaded = result_for(&requests[2], "load");
    assert_eq!(loaded["status"], "ok");
    assert!(loaded["ability"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "state_inspect"));
    let inspected = result_for(&requests[3], "inspect");
    assert_eq!(inspected["status"], "success");
    let messages = inspected["output"]["messages"].as_array().unwrap();
    assert_eq!(messages.len(), 1);
    assert_eq!(
        messages[0]["content_preview"]["text"]
            .as_str()
            .unwrap()
            .chars()
            .count(),
        512
    );
    assert_eq!(messages[0]["content_preview"]["truncated"], true);
    assert!(messages[0].get("content").is_none());
    assert!(!inspected.to_string().contains(private_other_text));
    assert_eq!(
        result_for(&requests[4], "cross-session")["status"],
        "denied"
    );
    server.abort();
}

#[tokio::test]
async fn prompt_slot_instructions_are_pinned_when_a_message_is_accepted() {
    use axum::routing::post;

    let (_root, mut app) = fixture().await;
    let original = app
        .store
        .upsert_prompt_slot("coding", "Use the first instruction.", None, None, None, 0)
        .await
        .unwrap();
    let sid = conversation(&app).await;
    let first_id = queued(&app, &sid).await;
    assert!(app.run(&first_id).await.unwrap()["instructions"]
        .as_str()
        .unwrap()
        .contains("Use the first instruction."));
    app.store
        .upsert_prompt_slot(
            "coding",
            "Use the second instruction.",
            None,
            None,
            None,
            original["revision"].as_i64().unwrap(),
        )
        .await
        .unwrap();

    let captured = Arc::new(Mutex::new(Vec::<Value>::new()));
    let requests = captured.clone();
    let mock = Router::new().route(
        "/responses",
        post(move |Json(body): Json<Value>| {
            let requests = requests.clone();
            async move {
                requests.lock().await.push(body);
                let event = json!({"type":"response.completed",
                "response":{"status":"completed","output":[{
                    "type":"message","role":"assistant",
                    "content":[{"type":"output_text","text":"Done."}]
                }]}});
                (
                    [("content-type", "text/event-stream")],
                    format!("data: {event}\n\n"),
                )
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, mock).await.unwrap();
    });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));

    running(&app, &first_id, "first-slot-run").await;
    execute_run(
        app.clone(),
        first_id.clone(),
        "first-slot-run".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&first_id).await.unwrap()["status"], "completed");
    let second_id = queued(&app, &sid).await;
    running(&app, &second_id, "second-slot-run").await;
    execute_run(
        app.clone(),
        second_id.clone(),
        "second-slot-run".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&second_id).await.unwrap()["status"], "completed");
    let requests = captured.lock().await;
    assert_eq!(requests.len(), 2);
    let first = requests[0]["instructions"].as_str().unwrap();
    let second = requests[1]["instructions"].as_str().unwrap();
    assert!(first.contains("Use the first instruction."));
    assert!(!first.contains("Use the second instruction."));
    assert!(second.contains("Use the second instruction."));
    server.abort();
}

#[tokio::test]
async fn prompt_slot_api_requires_auth_csrf_and_current_revision() {
    let (_root, app) = fixture().await;
    let uri = "/api/v3/agents/shared_default/slots/coding";
    let body = json!({"content":"Use bounded tools.","expected_revision":0}).to_string();
    let put_request = |cookie: Option<&str>, csrf: Option<&str>, body: &str| {
        let mut builder = Request::builder()
            .method("PUT")
            .uri(uri)
            .header("content-type", "application/json");
        if let Some(cookie) = cookie {
            builder = builder.header("cookie", cookie);
        }
        if let Some(csrf) = csrf {
            builder = builder.header("x-csrf-token", csrf);
        }
        builder.body(Body::from(body.to_owned())).unwrap()
    };
    let response = router(app.clone())
        .oneshot(put_request(None, None, &body))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let response = router(app.clone())
        .oneshot(put_request(Some(&cookie), None, &body))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let response = router(app.clone())
        .oneshot(put_request(Some(&cookie), Some("fixture"), &body))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let created: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(created["slot"]["revision"], 1);

    let response = router(app.clone())
        .oneshot(put_request(Some(&cookie), Some("fixture"), &body))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::CONFLICT);
    let get = Request::builder()
        .uri(uri)
        .header("cookie", &cookie)
        .body(Body::empty())
        .unwrap();
    let response = router(app.clone()).oneshot(get).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let fetched: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(fetched["slot"]["content"], "Use bounded tools.");
    assert_eq!(fetched["slot"]["revision"], 1);
}

#[tokio::test]
async fn prompt_slot_tool_waits_for_approval_then_writes_once() {
    use axum::routing::post;

    let (_root, mut app) = fixture().await;
    let args = json!({"action":"put","slot_name":"coding","content":"Use checked patches.","expected_revision":0});
    let call = json!({"type":"function_call","call_id":"slot-call",
        "name":"prompt_slot_write","arguments":args.to_string()});
    let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
    let captured = requests.clone();
    let mock = Router::new().route(
        "/responses",
        post(move |Json(body): Json<Value>| {
            let requests = captured.clone();
            let call = call.clone();
            async move {
                let mut requests = requests.lock().await;
                requests.push(body);
                let output = if requests.len() == 1 {
                    json!([call])
                } else {
                    json!([{"type":"message","role":"assistant",
                        "content":[{"type":"output_text","text":"Slot updated."}]}])
                };
                let event = json!({"type":"response.completed",
                    "response":{"status":"completed","output":output}});
                (
                    [("content-type", "text/event-stream")],
                    format!("data: {event}\n\n"),
                )
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
    activate_codebase(&app, &sid).await;
    let id = queued(&app, &sid).await;
    let mut run = app.run(&id).await.unwrap();
    run["loaded_abilities"] = json!(["codebase_admin"]);
    app.save(&mut run).await.unwrap();
    running(&app, &id, "slot-first").await;
    execute_run(
        app.clone(),
        id.clone(),
        "slot-first".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let waiting = app.run(&id).await.unwrap();
    assert_eq!(waiting["status"], "waiting_approval");
    assert!(app.store.get_prompt_slot("coding").await.unwrap().is_none());
    let pending_id = waiting["pending"]["id"].as_str().unwrap().to_owned();
    let approved = approval(
        State(app.clone()),
        Path(pending_id),
        Json(json!({"client_request_id":"slot-approve","decision":"approve"})),
    )
    .await
    .unwrap();
    assert_eq!(approved.0["status"], "queued");
    running(&app, &id, "slot-second").await;
    execute_run(
        app.clone(),
        id.clone(),
        "slot-second".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&id).await.unwrap()["status"], "completed");
    let slot = app.store.get_prompt_slot("coding").await.unwrap().unwrap();
    assert_eq!(slot["content"], "Use checked patches.");
    assert_eq!(slot["revision"], 1);
    let requests = requests.lock().await;
    assert_eq!(requests.len(), 2);
    let output = requests[1]["input"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["type"] == "function_call_output")
        .unwrap();
    let result: Value = serde_json::from_str(output["output"].as_str().unwrap()).unwrap();
    assert_eq!(result["status"], "success");
    assert_eq!(result["output"]["slot"]["revision"], 1);
    server.abort();
}

async fn ability_request(
    app: &App,
    method: axum::http::Method,
    uri: &str,
    body: Option<Value>,
    cookie: Option<&str>,
    csrf: Option<&str>,
) -> (StatusCode, Value) {
    let mut request = Request::builder().method(method).uri(uri);
    if let Some(cookie) = cookie {
        request = request.header("cookie", cookie);
    }
    if let Some(csrf) = csrf {
        request = request.header("x-csrf-token", csrf);
    }
    let payload = body.map(|body| body.to_string()).unwrap_or_default();
    if !payload.is_empty() {
        request = request.header("content-type", "application/json");
    }
    let response = router(app.clone())
        .oneshot(request.body(Body::from(payload)).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, body)
}

async fn ability_auth(app: &App) -> String {
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"ability-fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    format!("v3_session={token}")
}

#[tokio::test]
async fn ability_management_routes_enforce_auth_revisions_and_session_boundaries() {
    use axum::http::Method;
    let (_root, app) = fixture().await;
    let first = conversation(&app).await;
    let second = conversation(&app).await;
    let agent_uri = "/api/v3/agents/shared_default/abilities";
    let visibility_uri = "/api/v3/agents/shared_default/abilities/codebase_admin/visibility";
    let first_uri = format!("/api/v3/sessions/{first}/abilities");
    let first_set_uri = format!("{first_uri}/codebase_admin");
    let second_uri = format!("/api/v3/sessions/{second}/abilities");
    let second_set_uri = format!("{second_uri}/codebase_admin");
    let visible = json!({"visibility":"visible","expected_revision":0});
    let active = json!({"state":"active","expected_revision":0});

    assert_eq!(
        ability_request(&app, Method::GET, agent_uri, None, None, None)
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            visibility_uri,
            Some(visible.clone()),
            None,
            None
        )
        .await
        .0,
        StatusCode::UNAUTHORIZED
    );
    let cookie = ability_auth(&app).await;
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            visibility_uri,
            Some(visible.clone()),
            Some(&cookie),
            None
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &first_set_uri,
            Some(active.clone()),
            Some(&cookie),
            None
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );

    let (status, agent) =
        ability_request(&app, Method::GET, agent_uri, None, Some(&cookie), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(agent["revision"], 0);
    assert_eq!(agent["abilities"][0]["visibility"], "discoverable");
    let (status, first_state) =
        ability_request(&app, Method::GET, &first_uri, None, Some(&cookie), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        first_state["state"],
        json!({"revision":0,"active":[],"suppressed":[]})
    );

    let (status, result) = ability_request(
        &app,
        Method::PUT,
        visibility_uri,
        Some(visible.clone()),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(result["revision"], 1);
    assert_eq!(result["visibility"]["codebase_admin"], "visible");
    let (status, same) = ability_request(
        &app,
        Method::PUT,
        visibility_uri,
        Some(json!({"visibility":"visible","expected_revision":1})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(same["revision"], 1);
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            visibility_uri,
            Some(visible),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            visibility_uri,
            Some(json!({"visibility":"hidden","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            "/api/v3/agents/shared_default/abilities/missing/visibility",
            Some(json!({"visibility":"visible","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            "/api/v3/agents/missing/abilities/codebase_admin/visibility",
            Some(json!({"visibility":"visible","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );

    let (policy, _, visibility, state) = ability_controls(&app, &second).await.unwrap();
    assert!(app
        .abilities
        .schemas_with(&policy, &json!([]), &visibility, &state)
        .iter()
        .any(|tool| tool["name"] == "file_read"));
    assert!(!app
        .abilities
        .visible_guidance(&policy, &visibility, &state)
        .is_empty());

    let (status, activated) = ability_request(
        &app,
        Method::PUT,
        &first_set_uri,
        Some(active.clone()),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(activated["revision"], 1);
    assert_eq!(activated["active"], json!(["codebase_admin"]));
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &first_set_uri,
            Some(active),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &first_set_uri,
            Some(json!({"state":"active","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .1["revision"],
        1
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &first_set_uri,
            Some(json!({"state":"unknown","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &format!("{first_uri}/missing"),
            Some(json!({"state":"active","expected_revision":1})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        ability_request(
            &app,
            Method::PUT,
            &format!("/api/v3/sessions/{}/abilities/codebase_admin", uuid()),
            Some(json!({"state":"active","expected_revision":0})),
            Some(&cookie),
            Some("ability-fixture")
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    let (_, second_state) =
        ability_request(&app, Method::GET, &second_uri, None, Some(&cookie), None).await;
    assert_eq!(
        second_state["state"],
        json!({"revision":0,"active":[],"suppressed":[]})
    );

    let (status, suppressed) = ability_request(
        &app,
        Method::PUT,
        &first_set_uri,
        Some(json!({"state":"suppressed","expected_revision":1})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(suppressed["revision"], 2);
    let (policy, _, visibility, first_state) = ability_controls(&app, &first).await.unwrap();
    assert!(!app
        .abilities
        .schemas_with(
            &policy,
            &json!(["codebase_admin"]),
            &visibility,
            &first_state
        )
        .iter()
        .any(|tool| tool["name"] == "file_read"));
    assert!(app
        .abilities
        .visible_guidance(&policy, &visibility, &first_state)
        .is_empty());
    let (_, _, visibility, second_state) = ability_controls(&app, &second).await.unwrap();
    assert!(app
        .abilities
        .schemas_with(&policy, &json!([]), &visibility, &second_state)
        .iter()
        .any(|tool| tool["name"] == "file_read"));

    let (status, changed) = ability_request(
        &app,
        Method::PUT,
        visibility_uri,
        Some(json!({"visibility":"discoverable","expected_revision":1})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(changed["revision"], 2);
    let (policy, _, visibility, second_state) = ability_controls(&app, &second).await.unwrap();
    assert!(!app
        .abilities
        .schemas_with(&policy, &json!([]), &visibility, &second_state)
        .iter()
        .any(|tool| tool["name"] == "file_read"));
    assert!(app
        .abilities
        .visible_guidance(&policy, &visibility, &second_state)
        .is_empty());
    let (status, second_active) = ability_request(
        &app,
        Method::PUT,
        &second_set_uri,
        Some(json!({"state":"active","expected_revision":0})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(second_active["revision"], 1);
    let (policy, _, visibility, second_state) = ability_controls(&app, &second).await.unwrap();
    assert!(app
        .abilities
        .schemas_with(&policy, &json!([]), &visibility, &second_state)
        .iter()
        .any(|tool| tool["name"] == "file_read"));
    assert!(!app
        .abilities
        .visible_guidance(&policy, &visibility, &second_state)
        .is_empty());
}

#[tokio::test]
async fn ability_changes_revoke_waiting_approvals_for_both_scopes() {
    use axum::http::Method;
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let cookie = ability_auth(&app).await;
    let agent_uri = "/api/v3/agents/shared_default/abilities/codebase_admin/visibility";
    let session_uri = format!("/api/v3/sessions/{sid}/abilities/codebase_admin");
    let (status, _) = ability_request(
        &app,
        Method::PUT,
        agent_uri,
        Some(json!({"visibility":"visible","expected_revision":0})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let rid = queued(&app, &sid).await;
    let args = json!({"path":"notes.txt","content":"test"});
    let mut run = app.run(&rid).await.unwrap();
    run["status"] = json!("waiting_approval");
    run["phase"] = json!("tools");
    run["calls"] = json!([{"type":"function_call","call_id":"test-call",
        "name":"file_write","arguments":args.to_string()}]);
    run["pending"] = json!({"id":"old-visibility-approval","name":"file_write",
        "arguments":args,"call_id":"test-call","digest":digest(&args),
        "policy_version":1,"mode_revision":0,"visibility_revision":1,
        "ability_state_revision":0,"state":"waiting","decision":null});
    app.save(&mut run).await.unwrap();
    let (status, _) = ability_request(
        &app,
        Method::PUT,
        agent_uri,
        Some(json!({"visibility":"discoverable","expected_revision":1})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(app.run(&rid).await.unwrap()["status"], "queued");
    assert_eq!(
        approval(
            State(app.clone()),
            Path("old-visibility-approval".into()),
            Json(json!({"client_request_id":uuid(),"decision":"approve"}))
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::CONFLICT
    );

    let (status, _) = ability_request(
        &app,
        Method::PUT,
        &session_uri,
        Some(json!({"state":"active","expected_revision":0})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let mut run = app.run(&rid).await.unwrap();
    run["status"] = json!("waiting_approval");
    run["pending"]["id"] = json!("old-session-approval");
    run["pending"]["state"] = json!("waiting");
    run["pending"]["decision"] = Value::Null;
    run["pending"]["visibility_revision"] = json!(2);
    run["pending"]["ability_state_revision"] = json!(1);
    app.save(&mut run).await.unwrap();
    let (status, _) = ability_request(
        &app,
        Method::PUT,
        &session_uri,
        Some(json!({"state":"suppressed","expected_revision":1})),
        Some(&cookie),
        Some("ability-fixture"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(app.run(&rid).await.unwrap()["status"], "queued");
    assert_eq!(
        approval(
            State(app.clone()),
            Path("old-session-approval".into()),
            Json(json!({"client_request_id":uuid(),"decision":"approve"}))
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::CONFLICT
    );
}

#[tokio::test]
async fn session_mode_api_requires_auth_and_csrf_and_is_session_scoped() {
    let (_root, app) = fixture().await;
    let sid = conversation(&app).await;
    let other = conversation(&app).await;
    let uri = format!("/api/v3/sessions/{sid}/mode");
    let get = |cookie: Option<&str>, uri: &str| {
        let mut request = Request::builder().uri(uri);
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        request.body(Body::empty()).unwrap()
    };
    let put = |cookie: Option<&str>, csrf: Option<&str>, mode: &str| {
        let mut request = Request::builder()
            .method("PUT")
            .uri(&uri)
            .header("content-type", "application/json");
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        if let Some(csrf) = csrf {
            request = request.header("x-csrf-token", csrf);
        }
        request
            .body(Body::from(json!({"mode":mode}).to_string()))
            .unwrap()
    };
    let response = router(app.clone()).oneshot(get(None, &uri)).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let response = router(app.clone())
        .oneshot(put(None, None, "read_only"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":null}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let response = router(app.clone())
        .oneshot(put(Some(&cookie), None, "read_only"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let response = router(app.clone())
        .oneshot(get(Some(&cookie), &uri))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 65536)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body, json!({"mode":"write","revision":0}));

    for (mode, revision) in [("read_only", 1), ("read_only", 1), ("write", 2)] {
        let response = router(app.clone())
            .oneshot(put(Some(&cookie), Some("fixture"), mode))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body: Value = serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), 65536)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(body, json!({"mode":mode,"revision":revision}));
        let other_uri = format!("/api/v3/sessions/{other}/mode");
        let other_response = router(app.clone())
            .oneshot(get(Some(&cookie), &other_uri))
            .await
            .unwrap();
        let other_body: Value = serde_json::from_slice(
            &axum::body::to_bytes(other_response.into_body(), 65536)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(other_body, json!({"mode":"write","revision":0}));
    }
    let response = router(app)
        .oneshot(get(
            Some(&cookie),
            &format!("/api/v3/sessions/{}/mode", uuid()),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn read_only_revokes_old_approval_and_denies_write_at_execution() {
    use axum::routing::post;

    let (_root, mut app) = fixture().await;
    let sid = conversation(&app).await;
    activate_codebase(&app, &sid).await;
    let id = queued(&app, &sid).await;
    let args = json!({"command":"printf forbidden"});
    let mut run = app.run(&id).await.unwrap();
    run["loaded_abilities"] = json!(["codebase_admin"]);
    run["status"] = json!("waiting_approval");
    run["phase"] = json!("tools");
    run["calls"] = json!([{"type":"function_call","call_id":"old-call",
        "name":"shell","arguments":args.to_string()}]);
    run["pending"] = json!({"id":"old-approval","name":"shell","arguments":args,
        "call_id":"old-call","digest":digest(&args),"policy_version":1,
        "mode_revision":0,"state":"waiting","decision":null});
    app.save(&mut run).await.unwrap();

    let mode = session_mode_set(
        State(app.clone()),
        Path(sid.clone()),
        Json(SessionModeBody {
            mode: "read_only".into(),
        }),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(mode, json!({"mode":"read_only","revision":1}));
    let run = app.run(&id).await.unwrap();
    assert_eq!(run["status"], "queued");
    assert!(run["pending"]["decision"].is_null());
    assert_eq!(
        approval(
            State(app.clone()),
            Path("old-approval".into()),
            Json(json!({"client_request_id":"old-decision","decision":"approve"})),
        )
        .await
        .unwrap_err()
        .0,
        StatusCode::CONFLICT
    );
    let (policy, revision) = effective_policy(&app, &sid).await.unwrap();
    assert_eq!(revision, 1);
    assert_eq!(policy_decision(&policy, "shell"), "deny");
    assert_eq!(policy_decision(&policy, "state_inspect"), "allow");
    let schemas = app.abilities.schemas(&policy, &json!(["codebase_admin"]));
    assert!(!schemas.iter().any(|schema| schema["name"] == "shell"));
    assert!(schemas
        .iter()
        .any(|schema| schema["name"] == "state_inspect"));

    let mock = Router::new().route(
        "/responses",
        post(|| async {
            let event = json!({"type":"response.completed","response":{"status":"completed",
            "output":[{"type":"message","role":"assistant",
                "content":[{"type":"output_text","text":"Denied."}]}]}});
            (
                [("content-type", "text/event-stream")],
                format!("data: {event}\n\n"),
            )
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });
    app.provider = provider::Provider::for_test(format!("http://{address}/responses"));
    running(&app, &id, "read-only-retry").await;
    execute_run(
        app.clone(),
        id.clone(),
        "read-only-retry".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let finished = app.run(&id).await.unwrap();
    assert_eq!(finished["status"], "completed");
    let output = finished["input"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["type"] == "function_call_output")
        .unwrap();
    assert!(output["output"].as_str().unwrap().contains("denied"));
    server.abort();
}

#[test]
fn messaging_token_validation_and_auth_fail_closed() {
    assert!(message_api_token_hash("").unwrap().is_none());
    assert!(message_api_token_hash("short").is_err());
    assert!(message_api_token_hash(&" ".repeat(32)).is_err());
    let token = "fixture-messaging-token-0123456789";
    let hash = message_api_token_hash(token).unwrap();
    let mut headers = HeaderMap::new();
    assert!(!message_api_authorized(hash.as_ref(), &headers));
    headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
    assert!(message_api_authorized(hash.as_ref(), &headers));
    assert!(!message_api_authorized(None, &headers));
    headers.insert("authorization", HeaderValue::from_static("Bearer incorrect"));
    assert!(!message_api_authorized(hash.as_ref(), &headers));
}

#[tokio::test]
async fn messaging_api_is_scoped_idempotent_and_preserves_shutoff() {
    let (_root, mut app) = fixture().await;
    let token = "fixture-messaging-token-0123456789";
    let request = |method: &str, path: &str, body: Value, bearer: &str| {
        Request::builder().method(method).uri(path)
            .header("content-type", "application/json")
            .header("authorization", bearer)
            .body(Body::from(body.to_string())).unwrap()
    };
    let base = "/api/v3/admin/messaging/sessions";
    let body = json!({"client_request_id":"machine-session","title":"API chat"});
    let bearer = format!("Bearer {token}");
    assert_eq!(router(app.clone()).oneshot(request("POST", base, body.clone(), &bearer))
        .await.unwrap().status(), StatusCode::UNAUTHORIZED);
    app.message_api_token_hash = message_api_token_hash(token).unwrap();
    assert_eq!(router(app.clone()).oneshot(request("POST", base, body.clone(), "Bearer wrong"))
        .await.unwrap().status(), StatusCode::UNAUTHORIZED);
    let response = router(app.clone()).oneshot(request("POST", base, body.clone(), &bearer))
        .await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["cache-control"], "no-store");
    let created: Value = serde_json::from_slice(&axum::body::to_bytes(response.into_body(), 1048576).await.unwrap()).unwrap();
    let response = router(app.clone()).oneshot(request("POST", base, body, &bearer)).await.unwrap();
    let repeated: Value = serde_json::from_slice(&axum::body::to_bytes(response.into_body(), 1048576).await.unwrap()).unwrap();
    assert_eq!(created, repeated);
    let sid = created["id"].as_str().unwrap();
    let path = format!("{base}/{sid}/messages");
    let message = json!({"client_message_id":"machine-message","text":"Hello"});
    for _ in 0..2 {
        assert_eq!(router(app.clone()).oneshot(request("POST", &path, message.clone(), &bearer))
            .await.unwrap().status(), StatusCode::ACCEPTED);
    }
    let response = router(app.clone()).oneshot(request("GET", &format!("{base}/{sid}"), json!(null), &bearer)).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let state: Value = serde_json::from_slice(&axum::body::to_bytes(response.into_body(), 1048576).await.unwrap()).unwrap();
    assert_eq!(state["runs"].as_array().unwrap().len(), 1);
    assert_eq!(state["messages"].as_array().unwrap().len(), 1);
    app.store.put("control", &json!({"shutoff":true})).await.unwrap();
    assert_eq!(router(app.clone()).oneshot(request("POST", &path,
        json!({"client_message_id":"another","text":"Hello"}), &bearer))
        .await.unwrap().status(), StatusCode::CONFLICT);
    // The machine credential cannot authorize the general admin API.
    assert_eq!(router(app).oneshot(request("GET", "/api/v3/status", json!(null), &bearer))
        .await.unwrap().status(), StatusCode::UNAUTHORIZED);
}


#[tokio::test]
async fn workflow_policy_is_authenticated_validated_and_live() {
    let (_root, app) = fixture().await;
    let response=router(app.clone()).oneshot(Request::builder()
        .uri("/api/v3/workflow/policy").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(response.status(),StatusCode::UNAUTHORIZED);
    assert!(workflow_policy_set(State(app.clone()),Json(json!({"max_turns":51}))).await.is_err());
    assert!(app.store.get("workflow_policy").await.unwrap().is_none());
    let first=current_workflow_policy(&app).await.unwrap().revision().unwrap();
    workflow_policy_set(State(app.clone()),Json(json!({"max_turns":2}))).await.unwrap();
    let current=current_workflow_policy(&app).await.unwrap();
    assert_eq!(current.max_turns,2);
    assert_ne!(first,current.revision().unwrap());
    assert!(workflow_policy_set(State(app.clone()),Json(json!({"modules":{"publication":["uninstalled"]}}))).await.is_err());
    assert_eq!(current_workflow_policy(&app).await.unwrap().max_turns,2);
}

#[tokio::test]
async fn publication_commits_one_terminal_message_and_private_receipt() {
    let (_root,app)=fixture().await;
    let sid=uuid(); let id=uuid();
    app.store.put(&key("session",&sid),&json!({"id":sid,"title":"Publication test"})).await.unwrap();
    let mut run=json!({"id":id,"session_id":sid,"status":"running","phase":"tools",
        "generation":"g1","input":[],"turns":1,"call_index":0,"updated_at":now()});
    let policy=workflow::Policy::resolve(None).unwrap();
    assert!(publication::terminal(&app.store,&mut run,&[],"",&policy).await.is_err());
    assert_eq!(run["status"],"running");
    let output=vec![json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]})];
    publication::terminal(&app.store,&mut run,&output,"done",&policy).await.unwrap();
    publication::terminal(&app.store,&mut run,&output,"done",&policy).await.unwrap();
    let count:i64=sqlx::query_scalar("SELECT count(*) FROM chat WHERE terminal_run_id=?")
        .bind(&id).fetch_one(&app.store.pool).await.unwrap();
    assert_eq!(count,1);
    assert_eq!(app.run(&id).await.unwrap()["workflow"]["receipts"]["publication"]["result"]["decision"],"pass");
}


#[tokio::test]
async fn failed_publication_preserves_the_callers_run() {
    let (_root, app) = fixture().await;
    let mut run = json!({"id":uuid(),"session_id":uuid(),"status":"running",
        "phase":"tools","generation":"publication-failure","input":[],
        "turns":1,"call_index":0,"updated_at":now()});
    let before = run.clone();
    let policy = workflow::Policy::resolve(None).unwrap();
    // Close only this test fixture's pool; no live database is touched.
    app.store.pool.close().await;
    let output = vec![json!({"type":"message","role":"assistant",
        "content":[{"type":"output_text","text":"done"}]})];
    assert!(publication::terminal(&app.store, &mut run, &output, "done", &policy)
        .await.is_err());
    assert_eq!(run, before);
}

#[tokio::test]
async fn admission_records_policy_and_explicitly_rejects_attachments() {
    let (_root,app)=fixture().await;
    let Json(session)=create_session(State(app.clone()),Json(json!({"client_request_id":"workflow-admit","title":"Workflow"}))).await.unwrap();
    let sid=session["id"].as_str().unwrap().to_owned();
    let result=send(State(app.clone()),Path(sid.clone()),Json(json!({"client_message_id":"attachment","text":"hello","attachments":[{"id":"unsupported"}]}))).await;
    assert!(matches!(result,Err(ApiError(StatusCode::BAD_REQUEST,_))));
    let (_,Json(accepted))=send(State(app.clone()),Path(sid),Json(json!({"client_message_id":"text","text":"hello"}))).await.unwrap();
    let run=app.run(accepted["run_id"].as_str().unwrap()).await.unwrap();
    assert_eq!(run["workflow"]["receipts"]["admission"]["result"]["decision"],"pass");
    assert_eq!(run["workflow"]["receipts"]["attachment_preparation"]["result"]["decision"],"pass");
    assert_eq!(run["workflow"]["receipts"]["admission"]["policy_revision"],current_workflow_policy(&app).await.unwrap().revision().unwrap());
}

#[test]
fn tool_result_checkpoint_stays_private_and_binds_dispatch() {
    let policy=workflow::Policy::resolve(None).unwrap();
    let mut run=json!({"generation":"g1","turns":1,"call_index":0,"input":[],
        "pending":{"call_id":"c1"}});
    workflow::record(&mut run,workflow::Checkpoint::PreTool,&policy,&json!({"name":"read"}),workflow::Decision::Pass).unwrap();
    // Results must retain dispatch identity even if the caller's generation changes.
    run["generation"] = json!("g2");
    let result=json!({"status":"denied","output":"test"});
    append_tool(&mut run,&result);
    assert_eq!(run["workflow"]["receipts"]["tool_result"]["generation"],"g1");
    assert_eq!(run["workflow"]["receipts"]["tool_result"]["input_digest"],digest(&result));
    assert_eq!(run["workflow"]["receipts"]["tool_result"]["policy_revision"],policy.revision().unwrap());
    assert_eq!(run["workflow"]["receipts"]["tool_result"]["result"]["decision"],"reject");
    assert_eq!(run["workflow"]["receipts"]["pre_tool"]["result"]["decision"],"pass");
    assert!(run["input"][0].get("workflow").is_none());
    assert_eq!(run["input"][0]["type"],"function_call_output");
}

#[test]
fn successful_tool_result_records_pass_without_changing_dispatch_identity() {
    let policy = workflow::Policy::resolve(None).unwrap();
    let mut run = json!({"generation":"dispatch","turns":2,"call_index":3,
        "input":[],"pending":{"call_id":"success-call"}});
    workflow::record(&mut run, workflow::Checkpoint::PreTool, &policy,
        &json!({"name":"read"}), workflow::Decision::Pass).unwrap();
    let dispatch = run["workflow"]["receipts"]["pre_tool"].clone();
    run["generation"] = json!("later-generation");
    let result = json!({"status":"success","output":"fixture result"});
    append_tool(&mut run, &result);
    let receipt = &run["workflow"]["receipts"]["tool_result"];
    assert_eq!(receipt["result"]["decision"], "pass");
    for field in ["generation", "policy_revision", "turn", "call_index"] {
        assert_eq!(receipt[field], dispatch[field]);
    }
    assert_eq!(receipt["input_digest"], digest(&result));
    assert_eq!(run["workflow"]["receipts"]["pre_tool"], dispatch);
    assert_eq!(run["call_index"], 4);
    assert!(run["pending"].is_null());
    assert_eq!(run["input"][0]["call_id"], "success-call");
    assert_eq!(run["input"][0]["output"], result.to_string());
    assert!(run["input"][0].get("workflow").is_none());
}

// MODEL-SELECTION: agent-bound admission and dispatch safety, isolated fixtures only.
async fn selection_fixture(app: &App) -> model_selection::Catalog {
    use model_selection::*;
    use std::collections::{BTreeMap, BTreeSet};
    sqlx::query("INSERT INTO agents SELECT 'agent-a','Agent A',description,provider,model,status,metadata,revision,created_at,updated_at FROM agents WHERE id='shared_default'")
        .execute(&app.store.agent_pool).await.unwrap();
    let mut catalog = Catalog::default();
    for (id, model) in [("one", "model-one"), ("two", "model-two")] {
        let credential_ref = format!("credential:{id}");
        app.store.put(&credential_ref, &json!({"access_token":format!("token-{id}"),"expires_at":now()+3600})).await.unwrap();
        catalog.connections.insert(id.into(), Connection { id:id.into(), owner_id:"admin".into(), transport:Transport::OpenaiSubscription,
            credential_ref, allowed_agents:BTreeSet::new(), revision:1, revoked:false });
        catalog.entries.insert(id.into(), ModelEntry { id:id.into(), owner_id:"admin".into(), connection_id:id.into(),
            upstream_model:model.into(), display_name:model.into(), capabilities:BTreeSet::from(["text".into()]), allowed_agents:BTreeSet::new(), revision:1 });
    }
    catalog.groups.insert("backups".into(), BackupGroup { id:"backups".into(), owner_id:"admin".into(), entry_ids:vec!["one".into()], revision:1 });
    catalog.assignments = BTreeMap::from([
        ("shared_default".into(), Assignment { agent_id:"shared_default".into(), owner_id:"admin".into(), primary_entry_id:"one".into(), backup_group_id:None, revision:1 }),
        ("agent-a".into(), Assignment { agent_id:"agent-a".into(), owner_id:"admin".into(), primary_entry_id:"two".into(), backup_group_id:Some("backups".into()), revision:1 }),
    ]);
    app.store.save_model_catalog(&catalog,0).await.unwrap()
}

#[tokio::test]
async fn model_selection_admission_pins_agent_model_prompt_and_credentials() {
    let (_root, app) = fixture().await;
    let mut catalog = selection_fixture(&app).await;
    app.store.upsert_prompt_slot("selection-isolation", "DEFAULT PRIVATE PROMPT", Some(5), None, None, 0).await.unwrap();
    sqlx::query("INSERT INTO agent_prompts(id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at) VALUES('agent-a:selection-isolation','agent-a','selection-isolation',5,0,'replace',?,1,0)")
        .bind(app.store.seal(&json!("AGENT A PRIVATE PROMPT")).unwrap()).execute(&app.store.agent_pool).await.unwrap();
    for (agent_id, expected_model, expected_token, expected_prompt, excluded) in [
        ("shared_default","model-one","token-one","DEFAULT PRIVATE PROMPT","AGENT A PRIVATE PROMPT"),
        ("agent-a","model-two","token-two","AGENT A PRIVATE PROMPT","DEFAULT PRIVATE PROMPT"),
    ] {
        let Json(session) = create_session(State(app.clone()),Json(json!({"client_request_id":uuid(),"agent_id":agent_id}))).await.unwrap();
        let sid = session["id"].as_str().unwrap().to_owned();
        assert_eq!(session_agent_id(&app,&sid).await.unwrap(),agent_id);
        let (_,Json(accepted))=send(State(app.clone()),Path(sid.clone()),Json(json!({"client_message_id":uuid(),"text":"hello"}))).await.unwrap();
        let run=app.run(accepted["run_id"].as_str().unwrap()).await.unwrap();
        assert_eq!(run["agent_id"],agent_id);
        assert_eq!(run["provider_selection"]["model"],expected_model);
        assert!(run["instructions"].as_str().unwrap().contains(expected_prompt));
        assert!(!run["instructions"].as_str().unwrap().contains(excluded));
        let selection=provider::ProviderSelection::parse(&run["provider_selection"],"unused").unwrap();
        assert_eq!(app.tokens_for(&selection,&run).await.unwrap()["access_token"],expected_token);
        if agent_id=="agent-a" {
            catalog.assignments.get_mut(agent_id).unwrap().primary_entry_id="one".into();
            catalog=app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
            assert_eq!(app.run_candidate(&run).await.unwrap().unwrap().upstream_model,"model-two");
            assert_eq!(app.tokens_for(&selection,&run).await.unwrap()["access_token"],"token-two");
            let (policy,_)=effective_policy(&app,&sid).await.unwrap();
            assert_eq!(policy_decision(&policy,"prompt_slot_read"),"deny");
        }
    }
    assert_eq!(app.store.list_prompt_slots().await.unwrap().iter().filter(|p|p["slot_name"]=="selection-isolation").count(),1);
    assert_eq!(app.store.get_prompt_slot("selection-isolation").await.unwrap().unwrap()["content"],"DEFAULT PRIVATE PROMPT");
}

#[tokio::test]
async fn model_selection_failover_is_ordered_durable_and_revocation_is_live() {
    let (_root,app)=fixture().await;
    let mut catalog=selection_fixture(&app).await;
    let Json(session)=create_session(State(app.clone()),Json(json!({"client_request_id":uuid(),"agent_id":"agent-a"}))).await.unwrap();
    let (_,Json(accepted))=send(State(app.clone()),Path(session["id"].as_str().unwrap().into()),Json(json!({"client_message_id":uuid(),"text":"hello"}))).await.unwrap();
    let mut run=app.run(accepted["run_id"].as_str().unwrap()).await.unwrap();
    let failure=provider::typed_failure(model_selection::FailureKind::ModelUnavailable,false,"Model unavailable");
    model_failure(&mut run,&failure).unwrap();
    assert_eq!(run["status"],"queued");
    assert_eq!(run["provider_selection"]["model"],"model-one");
    assert_eq!(run["failover_state"]["history"][0]["to_entry_id"],"one");
    app.save(&mut run).await.unwrap();
    let mut restored=app.run(accepted["run_id"].as_str().unwrap()).await.unwrap();
    assert_eq!(restored["failover_state"],run["failover_state"]);
    let selection=provider::ProviderSelection::parse(&restored["provider_selection"],"unused").unwrap();
    assert_eq!(app.tokens_for(&selection,&restored).await.unwrap()["access_token"],"token-one");
    catalog.connections.get_mut("one").unwrap().revoked=true;
    app.store.save_model_catalog(&catalog,catalog.revision).await.unwrap();
    assert!(app.run_candidate(&restored).await.is_err());
    model_failure(&mut restored,&failure).unwrap();
    assert_eq!(restored["status"],"failed");
    assert!(restored["error"].as_str().unwrap().contains("no safe retry or eligible backup"));
}

#[tokio::test]
async fn model_selection_unsafe_failures_do_not_change_candidate() {
    let (_root,app)=fixture().await;
    let catalog=selection_fixture(&app).await;
    let plan=catalog.resolve("agent-a").unwrap();
    for (kind, started, uncertain) in [
        (model_selection::FailureKind::MalformedRequest,false,false),
        (model_selection::FailureKind::Cancelled,false,false),
        (model_selection::FailureKind::PolicyRejected,false,false),
        (model_selection::FailureKind::Transient,true,false),
        (model_selection::FailureKind::Transient,false,true),
    ] {
        let mut run=json!({"agent_id":"agent-a","selection_plan":plan,"failover_state":model_selection::FailoverState::default(),
            "provider_selection":{"provider":"openai_subscription","model":"model-two"},"uncertain":uncertain});
        model_failure(&mut run,&provider::typed_failure(kind,started,"Sanitized failure")).unwrap();
        assert_eq!(run["status"],"failed");
        assert_eq!(run["failover_state"]["candidate_index"],0);
        assert_eq!(run["provider_selection"]["model"],"model-two");
    }
}

#[tokio::test]
async fn model_catalog_api_uses_profile_admin_role_and_enforces_csrf_and_retries() {
    let (_root,app)=fixture().await;
    app.store.create_admin_account("catalog-admin","fixture-only","catalog-generation").await.unwrap();
    let token=uuid();
    app.store.put(&format!("auth:{}",digest(&json!(token))),&json!({"csrf":"catalog-csrf","expires":now()+3600,
        "generation":"catalog-generation","user_id":"admin"})).await.unwrap();
    let request=|method: &str,path: &str,cookie: bool,csrf: bool,body: Value| {
        let mut r=Request::builder().method(method).uri(path).header("content-type","application/json");
        if cookie { r=r.header("cookie",format!("v3_session={token}")); }
        if csrf { r=r.header("x-csrf-token","catalog-csrf"); }
        r.body(Body::from(body.to_string())).unwrap()
    };
    let response=router(app.clone()).oneshot(request("GET","/api/v3/models/catalog",false,false,Value::Null)).await.unwrap();
    assert_eq!(response.status(),StatusCode::UNAUTHORIZED);
    let response=router(app.clone()).oneshot(request("GET","/api/v3/models/catalog",true,false,Value::Null)).await.unwrap();
    assert_eq!(response.status(),StatusCode::OK);
    let new_agent=json!({"client_request_id":"catalog-agent","name":"Second Agent"});
    let response=router(app.clone()).oneshot(request("POST","/api/v3/models/agents",true,false,new_agent.clone())).await.unwrap();
    assert_eq!(response.status(),StatusCode::FORBIDDEN);
    let mut created=Value::Null;
    for _ in 0..2 {
        let response=router(app.clone()).oneshot(request("POST","/api/v3/models/agents",true,true,new_agent.clone())).await.unwrap();
        assert_eq!(response.status(),StatusCode::OK);
        let body: Value=serde_json::from_slice(&axum::body::to_bytes(response.into_body(),65536).await.unwrap()).unwrap();
        if created.is_null() { created=body.clone(); }
        assert_eq!(body,created);
    }
    let count: i64=sqlx::query_scalar("SELECT COUNT(*) FROM agents WHERE id=?").bind(created["id"].as_str().unwrap())
        .fetch_one(&app.store.agent_pool).await.unwrap();
    assert_eq!(count,1);
    let response=router(app.clone()).oneshot(request("POST","/api/v3/models/agents",true,true,
        json!({"client_request_id":"catalog-agent","name":"Different Agent"}))).await.unwrap();
    assert_eq!(response.status(),StatusCode::CONFLICT);
    let mut profile=app.store.user_profile("admin").await.unwrap().unwrap();
    profile["is_admin"]=json!(false);
    sqlx::query("UPDATE user_profiles SET value=? WHERE user_id='admin'")
        .bind(app.store.seal(&profile).unwrap()).execute(&app.store.app_pool).await.unwrap();
    let response=router(app.clone()).oneshot(request("GET","/api/v3/models/catalog",true,false,Value::Null)).await.unwrap();
    assert_eq!(response.status(),StatusCode::FORBIDDEN);
}
