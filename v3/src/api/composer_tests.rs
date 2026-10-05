//! Composer acceptance uses isolated storage and local HTTP provider fixtures only.
use super::*;
use crate::runtime_tests::{fixture, selection_fixture};
use axum::body::Body;
use base64::{engine::general_purpose::STANDARD, Engine};
use tower::ServiceExt;
async fn session(app: &App) -> String {
    create_session(
        State(app.clone()),
        Json(json!({"client_request_id":uuid()})),
    )
    .await
    .unwrap()
    .0["id"]
        .as_str()
        .unwrap()
        .into()
}
#[tokio::test]
async fn upload_composer_auth_ownership_retry_and_persistence() {
    let (_root, app) = fixture().await;
    let sid = session(&app).await;
    let other_sid = session(&app).await;
    let id = uuid();
    let bytes = vec![b'x'; 2 * 1024 * 1024 + 1];
    let body = json!({"client_upload_id":id,"name":"notes.md","media_type":"text/markdown","data":STANDARD.encode(&bytes)});
    let token = uuid();
    app.store
        .put(
            &format!("auth:{}", digest(&json!(token))),
            &json!({"csrf":"fixture","expires":now()+3600,"generation":"fixture-generation"}),
        )
        .await
        .unwrap();
    let cookie = format!("v3_session={token}");
    let uri = format!("/api/v3/sessions/{sid}/attachments");
    let req = |cookie: Option<&str>, csrf: Option<&str>| {
        let mut r = Request::builder()
            .method("POST")
            .uri(&uri)
            .header("content-type", "application/json");
        if let Some(v) = cookie {
            r = r.header("cookie", v);
        }
        if let Some(v) = csrf {
            r = r.header("x-csrf-token", v);
        }
        r.body(Body::from(body.to_string())).unwrap()
    };
    assert_eq!(
        router(app.clone())
            .oneshot(req(None, None))
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        router(app.clone())
            .oneshot(req(Some(&cookie), None))
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        router(app.clone())
            .oneshot(req(Some(&cookie), Some("fixture")))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let info = uploads::upload(State(app.clone()), Path(sid.clone()), Json(body.clone()))
        .await
        .unwrap()
        .0;
    assert_eq!(info["bytes"], bytes.len());
    assert!(info.get("data").is_none());
    assert_eq!(
        app.store.get("attachment_quota").await.unwrap().unwrap()["bytes"],
        bytes.len()
    );
    assert!(uploads::lookup(&app, &other_sid, &id).await.is_err());
    let account = app
        .store
        .create_user_account("composerforeign", "fixture-only-password-hash")
        .await
        .unwrap();
    let mut foreign = app.clone();
    foreign.store = app
        .store
        .for_user(account["user_id"].as_str().unwrap())
        .await
        .unwrap();
    assert!(uploads::lookup(&foreign, &sid, &id).await.is_err());
    let mut changed = body.clone();
    changed["name"] = json!("other.md");
    assert!(
        uploads::upload(State(app.clone()), Path(sid.clone()), Json(changed))
            .await
            .is_err()
    );
    let (_, Json(sent)) = send(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"text":"Summarize","client_message_id":uuid(),"attachment_ids":[id]})),
    )
    .await
    .unwrap();
    let snapshot = snapshot(State(app.clone()), Path(sid.clone()))
        .await
        .unwrap()
        .0;
    assert_eq!(snapshot["messages"][0]["content"], "Summarize");
    assert_eq!(snapshot["messages"][0]["attachments"][0]["id"], id);
    let record = attachment_get(State(app.clone()), Path((sid.clone(), id.clone())))
        .await
        .unwrap()
        .0;
    assert_eq!(record["media_type"], "text/markdown");
    assert_eq!(record["text"].as_str().unwrap().len(), bytes.len());
    assert!(record.get("data").is_none());
    let mut events = app.store.chat_events(&sid, 0).await.unwrap();
    uploads::message_files(&app, &mut events).await.unwrap();
    assert_eq!(events[0]["attachments"][0]["id"], id);
    assert!(
        uploads::upload_delete(State(app.clone()), Path((sid.clone(), id.clone())))
            .await
            .is_err()
    );
    assert_eq!(sent["attachments"][0]["id"], id);
    let pending = uuid();
    let pending_body = json!({"client_upload_id":pending,"name":"document.pdf","media_type":"application/pdf","data":STANDARD.encode(b"%PDF-fixture")});
    let _ = uploads::upload(
        State(app.clone()),
        Path(other_sid.clone()),
        Json(pending_body),
    )
    .await
    .unwrap();
    let _ = uploads::upload_delete(
        State(app.clone()),
        Path((other_sid.clone(), pending.clone())),
    )
    .await
    .unwrap();
    let _ = uploads::upload_delete(State(app.clone()), Path((other_sid, pending)))
        .await
        .unwrap();
    assert_eq!(
        app.store.get("attachment_quota").await.unwrap().unwrap()["bytes"],
        bytes.len()
    );
}
#[tokio::test]
async fn voice_composer_requires_matching_confirmation_and_publishes_one_turn() {
    let (_root, app) = fixture().await;
    let sid = session(&app).await;
    let route = |text: &str, proposal: Value| json!({"utterance":text,"proposal_id":proposal});
    assert!(voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("yes", Value::Null))
    )
    .await
    .is_err());
    let Json(offer) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("Create a report", Value::Null)),
    )
    .await
    .unwrap();
    assert_eq!(offer["action"], "dispatch_work");
    assert!(app.store.chat_snapshot(&sid).await.unwrap().0.is_empty());
    let ticket = offer["ticket"].clone();
    let Json(sent) = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":ticket})),
    )
    .await
    .unwrap();
    let replay = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":ticket})),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(replay["run_id"], sent["run_id"]);
    assert_eq!(app.store.chat_snapshot(&sid).await.unwrap().0.len(), 1);
    let Json(status) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("What is the status?", Value::Null)),
    )
    .await
    .unwrap();
    assert_eq!(status["action"], "answer");
    assert_eq!(app.store.chat_snapshot(&sid).await.unwrap().0.len(), 1);
    let Json(change) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("Instead make it shorter", Value::Null)),
    )
    .await
    .unwrap();
    assert_eq!(change["action"], "request_confirmation");
    assert!(voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":change["proposal"]["id"]}))
    )
    .await
    .is_err());
    assert!(voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("yes", json!(uuid())))
    )
    .await
    .is_err());
    let _ = session_mode_set(
        State(app.clone()),
        Path(sid.clone()),
        Json(SessionModeBody {
            mode: "plan".into(),
            expected_revision: Some(0),
            confirm_write: false,
        }),
    )
    .await
    .unwrap();
    assert!(voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("yes", change["proposal"]["id"].clone()))
    )
    .await
    .is_err());
    let Json(change) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("Instead make it shorter", Value::Null)),
    )
    .await
    .unwrap();
    let Json(confirmed) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("yes", change["proposal"]["id"].clone())),
    )
    .await
    .unwrap();
    let Json(replacement) = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":confirmed["ticket"]})),
    )
    .await
    .unwrap();
    assert_ne!(replacement["run_id"], sent["run_id"]);
    assert_eq!(
        app.run(sent["run_id"].as_str().unwrap()).await.unwrap()["status"],
        "cancelled"
    );
    assert_eq!(app.store.chat_snapshot(&sid).await.unwrap().0.len(), 2);
    let Json(pause) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("pause", Value::Null)),
    )
    .await
    .unwrap();
    let _ = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":pause["ticket"]})),
    )
    .await
    .unwrap();
    assert_eq!(
        app.run(replacement["run_id"].as_str().unwrap())
            .await
            .unwrap()["status"],
        "paused"
    );
    let Json(resume) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(route("resume", Value::Null)),
    )
    .await
    .unwrap();
    let _ = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":resume["ticket"]})),
    )
    .await
    .unwrap();
    assert_eq!(
        app.run(replacement["run_id"].as_str().unwrap())
            .await
            .unwrap()["status"],
        "queued"
    );
    assert_eq!(app.store.chat_snapshot(&sid).await.unwrap().0.len(), 2);
}
#[tokio::test]
async fn voice_composer_audio_and_quick_answers_remain_ephemeral() {
    let (_root, mut app) = fixture().await;
    let mut catalog = selection_fixture(&app).await;
    catalog.connections.get_mut("one").unwrap().transport = model_selection::Transport::OpenaiApi;
    catalog
        .entries
        .get_mut("one")
        .unwrap()
        .capabilities
        .extend(["voice".into(), "image_in".into()]);
    for role in [
        model_selection::ModelRole::Voice,
        model_selection::ModelRole::ImageIn,
    ] {
        catalog
            .agent_models
            .get_mut("shared_default")
            .unwrap()
            .roles
            .insert(
                role,
                model_selection::RoleAssignment {
                    entry_id: "one".into(),
                    backup_entry_ids: vec![],
                },
            );
    }
    app.store
        .save_model_catalog(&catalog, catalog.revision)
        .await
        .unwrap();
    app.store
        .put("credential:one", &json!({"api_key":"fixture-audio-key"}))
        .await
        .unwrap();
    let captured = Arc::new(Mutex::new(Vec::<Value>::new()));
    let capture = captured.clone();
    let mock=Router::new().route("/provider",post(move|headers:HeaderMap,body:axum::body::Bytes|{let capture=capture.clone();async move{
  assert_eq!(headers["authorization"],"Bearer fixture-audio-key");
  if headers["content-type"].to_str().unwrap().starts_with("multipart/"){return Json(json!({"text":"What is the weather?"})).into_response();}
  let value:Value=serde_json::from_slice(&body).unwrap();capture.lock().await.push(value);
  ([("content-type","text/event-stream")],format!("data: {}\n\n",json!({"choices":[{"delta":{"content":"A quick answer."},"finish_reason":"stop"}]}))).into_response()
 }}));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });
    app.provider = provider::Provider::for_test(format!("http://{address}/provider"));
    let sid = session(&app).await;
    let Json(start) = voice_audio(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"op":"start"})),
    )
    .await
    .unwrap();
    let id = start["capture_id"].clone();
    let frame: Vec<u8> = (0..8000).flat_map(|_| 10000i16.to_le_bytes()).collect();
    let chunk = json!({"op":"chunk","capture_id":id,"seq":1,"data":STANDARD.encode(&frame)});
    assert_eq!(
        voice_audio(State(app.clone()), Path(sid.clone()), Json(chunk.clone()))
            .await
            .unwrap()
            .0["speaking"],
        true
    );
    assert_eq!(
        voice_audio(State(app.clone()), Path(sid.clone()), Json(chunk))
            .await
            .unwrap()
            .0["replayed"],
        true
    );
    assert!(voice_audio(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"op":"chunk","capture_id":id,"seq":3,"data":STANDARD.encode(&frame)}))
    )
    .await
    .is_err());
    let Json(final_text) = voice_audio(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"op":"finish","capture_id":id})),
    )
    .await
    .unwrap();
    assert_eq!(final_text["text"], "What is the weather?");
    assert_eq!(
        voice_audio(
            State(app.clone()),
            Path(sid.clone()),
            Json(json!({"op":"finish","capture_id":id}))
        )
        .await
        .unwrap()
        .0["replayed"],
        true
    );
    let Json(answer) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"utterance":"Explain this briefly"})),
    )
    .await
    .unwrap();
    assert_eq!(answer["answer"], "A quick answer.");
    assert!(app.store.chat_snapshot(&sid).await.unwrap().0.is_empty());
    assert!(app.store.active_runs().await.unwrap().is_empty());
    let requests = captured.lock().await;
    assert!(requests[0].get("tools").is_none());
    drop(requests);
    let aid = uuid();
    let _ = uploads::upload(State(app.clone()),Path(sid.clone()),Json(json!({"client_upload_id":aid,"name":"photo.png","media_type":"image/png","data":STANDARD.encode(b"fixture-raster")}))).await.unwrap();
    let _ = uploads::upload_describe(State(app.clone()), Path((sid.clone(), aid.clone())))
        .await
        .unwrap();
    assert_eq!(
        uploads::lookup(&app, &sid, &aid).await.unwrap()["text"],
        "A quick answer."
    );
    let requests = captured.lock().await;
    assert!(requests.last().unwrap()["messages"][1]["content"]
        .as_array()
        .unwrap()
        .iter()
        .any(|p| p["type"] == "image_url"));
    assert!(app.store.chat_snapshot(&sid).await.unwrap().0.is_empty());
    drop(requests);
    let Json(offer) = voice_route(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"utterance":"Create a short summary"})),
    )
    .await
    .unwrap();
    let Json(sent) = voice_control(
        State(app.clone()),
        Path(sid.clone()),
        Json(json!({"ticket":offer["ticket"]})),
    )
    .await
    .unwrap();
    let rid = sent["run_id"].as_str().unwrap().to_owned();
    let mut run = app.run(&rid).await.unwrap();
    assert!(app
        .store
        .claim_run(&mut run, "fixture-loop", "voice-e2e")
        .await
        .unwrap());
    crate::execute_run(
        app.clone(),
        rid.clone(),
        "voice-e2e".into(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(app.run(&rid).await.unwrap()["status"], "completed");
    let messages = app.store.chat_snapshot(&sid).await.unwrap().0;
    assert_eq!(
        messages.iter().filter(|m| m["role"] == "assistant").count(),
        1
    );
    assert_eq!(messages.iter().filter(|m| m["role"] == "user").count(), 1);
    assert_eq!(messages.last().unwrap()["content"], "A quick answer.");
    server.abort();
}

#[tokio::test]
async fn attachment_limits_follow_chat_json_and_fail_closed() {
    let (root, mut app) = fixture().await;
    let sid = session(&app).await;
    app.chat_ui_config_path = root.path().join("chat.json");
    let defaults = uploads::limits(&app).await.unwrap();
    assert_eq!((defaults.max_file_size_mb,defaults.max_per_message),(None,None));
    let write = |mb: Value,count: Value| std::fs::write(&app.chat_ui_config_path,
        json!({"chat_limits":{"attachments":{"max_file_size_mb":mb,"max_per_message":count}}}).to_string()).unwrap();
    write(json!(1),json!(1));
    let Json(controls) = chat_controls_get(State(app.clone()),Path(sid.clone())).await.unwrap();
    assert_eq!(controls["composer"]["attachment_limits"],json!({"max_file_size_mb":1,"max_per_message":1}));
    let make = |id: &str,bytes: usize| json!({"client_upload_id":id,"name":"limits.txt","media_type":"text/plain","data":STANDARD.encode(vec![b'x';bytes])});
    let id = uuid();
    let _ = upload(State(app.clone()),Path(sid.clone()),Json(make(&id,1024*1024))).await.unwrap();
    assert!(upload(State(app.clone()),Path(sid.clone()),Json(make(&uuid(),1024*1024+1))).await.is_err());
    let request = Request::builder().header("content-type","application/json").body(Body::from(vec![b' ';2*1024*1024])).unwrap();
    assert_eq!(uploads::request_body(&app,request).await.unwrap_err().0,StatusCode::PAYLOAD_TOO_LARGE);
    let second=uuid();
    let _ = upload(State(app.clone()),Path(sid.clone()),Json(make(&second,1))).await.unwrap();
    let ids=json!([id,second]);
    let mut prepared=attachments::prepare(None,&sid,"limits-run").unwrap();
    assert!(uploads::prepare_uploads(&app,&sid,"limits-run",Some(&ids),&mut prepared).await.is_err());
    write(json!(30),json!(2));
    let limits=uploads::limits(&app).await.unwrap();
    assert_eq!((limits.max_file_size_mb,limits.max_per_message),(Some(30),Some(2)));
    uploads::prepare_uploads(&app,&sid,"limits-run",Some(&ids),&mut prepared).await.unwrap();
    assert_eq!(prepared.metadata.len(),2);
    // Raising the file limit also raises transport admission; 25 MiB is no longer hard-coded.
    let large_id=uuid();
    let large=make(&large_id,26*1024*1024);
    let request=Request::builder().header("content-type","application/json").body(Body::from(large.to_string())).unwrap();
    let parsed=uploads::request_body(&app,request).await.unwrap();
    let _ = upload(State(app.clone()),Path(sid.clone()),Json(parsed)).await.unwrap();
    write(json!(1),json!(2));
    let mut lowered=attachments::prepare(None,&sid,"lowered").unwrap();
    assert!(uploads::prepare_uploads(&app,&sid,"lowered",Some(&json!([large_id])),&mut lowered).await.is_err());
    for (mb,count) in [(json!(0),json!(2)),(json!(30),json!("2")),(json!(-1),json!(2))] {
        write(mb,count);
        assert_eq!(uploads::limits(&app).await.err().unwrap().0,StatusCode::SERVICE_UNAVAILABLE);
    }
}

#[tokio::test]
async fn unrestricted_attachment_defaults_remove_size_count_and_storage_caps() {
    let (root, mut app) = fixture().await;
    let sid = session(&app).await;
    app.chat_ui_config_path = root.path().join("chat.json");
    for config in [json!({}),json!({"chat_limits":{"attachments":{}}}),
        json!({"chat_limits":{"attachments":{"max_file_size_mb":null,"max_per_message":null}}})] {
        std::fs::write(&app.chat_ui_config_path,config.to_string()).unwrap();
        let limit = uploads::limits(&app).await.unwrap();
        assert_eq!((limit.max_file_size_mb,limit.max_per_message),(None,None));
    }
    let Json(controls)=chat_controls_get(State(app.clone()),Path(sid.clone())).await.unwrap();
    assert_eq!(controls["composer"]["attachment_limits"]["max_file_size_mb"],Value::Null);
    assert_eq!(controls["composer"]["attachment_limits"]["max_per_message"],Value::Null);
    // Existing storage accounting above the former quota cannot block another upload.
    app.store.put("attachment_quota",&json!({"bytes":300*1024*1024u64})).await.unwrap();
    let aid=uuid();
    let body=json!({"client_upload_id":aid,"name":"unrestricted.txt","media_type":"text/plain",
        "data":STANDARD.encode(vec![b'x';26*1024*1024])});
    let request=Request::builder().header("content-type","application/json").body(Body::from(body.to_string())).unwrap();
    let parsed=uploads::request_body(&app,request).await.unwrap();
    let _=upload(State(app.clone()),Path(sid.clone()),Json(parsed)).await.unwrap();
    assert!(app.store.get("attachment_quota").await.unwrap().unwrap()["bytes"].as_u64().unwrap()>300*1024*1024u64);
    let items=json!((0..12).map(|n|json!({"name":format!("{n}.txt"),"media_type":"text/plain","text":"source"})).collect::<Vec<_>>());
    let mut prepared=attachments::prepare(Some(&items),&sid,"unrestricted-run").unwrap();
    uploads::prepare_uploads(&app,&sid,"unrestricted-run",Some(&json!([aid])),&mut prepared).await.unwrap();
    assert_eq!(prepared.metadata.len(),13);
    // Configuration is a refinement, without the former 256 MiB/100-file ceilings.
    std::fs::write(&app.chat_ui_config_path,json!({"chat_limits":{"attachments":{"max_file_size_mb":512,"max_per_message":1000}}}).to_string()).unwrap();
    let limits=uploads::limits(&app).await.unwrap();
    assert_eq!((limits.max_file_size_mb,limits.max_per_message),(Some(512),Some(1000)));
}
