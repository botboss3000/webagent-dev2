//! HTTP handlers for promotions operations.
use super::*;
pub(crate) fn valid_review_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
pub(crate) fn verified_review<'a>(
    review: &'a Value,
    expected: &str,
) -> std::result::Result<(&'a str, &'a str), ApiError> {
    if !valid_review_digest(expected)
        || review["status"] != "success"
        || review["truncated"] != false
    {
        return Err(conflict("Source changed since review"));
    }
    let patch = review["diff"]
        .as_str()
        .ok_or_else(|| conflict("Review has no patch"))?;
    let baseline = review["baseline_commit"]
        .as_str()
        .ok_or_else(|| conflict("Review baseline is missing"))?;
    if !matches!(baseline.len(), 40 | 64)
        || !baseline
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(conflict("Review baseline is invalid"));
    }
    let calculated = hex::encode(Sha256::digest(format!("{baseline}\0{patch}").as_bytes()));
    if review["review_sha256"] != expected || calculated != expected {
        return Err(conflict("Source changed since review"));
    }
    if patch.is_empty() || patch.len() > 1024 * 1024 {
        return Err(bad("Review patch is empty or exceeds limit"));
    }
    Ok((patch, baseline))
}
pub(crate) async fn propose_promotion(
    app: &App,
    sid: &str,
    request_id: &str,
    run_id: &str,
    args: &Value,
    policy_version: u64,
    cancel: CancellationToken,
) -> Result<Value> {
    let Some(expected) = args["expected_review_sha256"].as_str() else {
        return Ok(json!({"status":"denied","output":"Review digest is required"}));
    };
    let Some(summary) = args["summary"].as_str() else {
        return Ok(json!({"status":"denied","output":"Change summary is required"}));
    };
    if !valid_review_digest(expected) || summary.trim().is_empty() || summary.len() > 4096 {
        return Ok(json!({"status":"denied","output":"Invalid review digest or summary"}));
    }
    let receipt_key = format!("promotion:{sid}:{request_id}");
    let body = json!({"client_request_id":request_id,"expected_review_sha256":expected});
    let request_digest = digest(&body);
    {
        let _guard = app.gate.lock().await;
        if let Some(existing) = app.store.get(&receipt_key).await? {
            if existing["request_digest"] == request_digest {
                return Ok(json!({"status":"success","proposal_id":request_id,
                    "state":existing["state"],"review_sha256":expected}));
            }
            return Ok(json!({"status":"denied","output":"Proposal identifier conflict"}));
        }
        let policy = app
            .store
            .get("policy")
            .await?
            .unwrap_or_else(policy_default);
        if app.shutoff().await?
            || policy["version"] != policy_version
            || policy_decision(&policy, "source_review") != "allow"
            || policy_decision(&policy, "promotion_propose") == "deny"
            || app.store.get(&key("session", sid)).await?.is_none()
        {
            return Ok(
                json!({"status":"denied","output":"Proposal unavailable under current controls"}),
            );
        }
    }
    let review = tools::execute(
        "source_review",
        &json!({}),
        sid,
        &app.tool_socket,
        cancel.clone(),
    )
    .await?;
    let (patch, baseline) = match verified_review(&review, expected) {
        Ok(value) => value,
        Err(error) => return Ok(json!({"status":"denied","output":error.1})),
    };
    let patch_sha256 = hex::encode(Sha256::digest(patch.as_bytes()));
    let record = json!({"session_id":sid,"client_request_id":request_id,
        "request_digest":request_digest,"state":"proposed","review_sha256":expected,
        "baseline_commit":baseline,"patch_sha256":patch_sha256,
        "patch":patch,"status_output":review["status_output"],"summary":summary,
        "proposed_by_run":run_id,"paths":[],"created_at":now(),"updated_at":now()});
    let _guard = app.gate.lock().await;
    if let Some(existing) = app.store.get(&receipt_key).await? {
        if existing["request_digest"] == request_digest {
            return Ok(json!({"status":"success","proposal_id":request_id,
                "state":existing["state"],"review_sha256":expected}));
        }
        return Ok(json!({"status":"denied","output":"Proposal identifier conflict"}));
    }
    let policy = app
        .store
        .get("policy")
        .await?
        .unwrap_or_else(policy_default);
    if cancel.is_cancelled()
        || app.shutoff().await?
        || policy["version"] != policy_version
        || policy_decision(&policy, "source_review") != "allow"
        || policy_decision(&policy, "promotion_propose") == "deny"
    {
        return Ok(
            json!({"status":"denied","output":"Proposal unavailable under current controls"}),
        );
    }
    app.store
        .atomic_docs_event(
            vec![(receipt_key, record)],
            sid,
            "promotion_proposed",
            &json!({"client_request_id":request_id,"review_sha256":expected,
            "patch_sha256":patch_sha256}),
        )
        .await?;
    Ok(
        json!({"status":"success","proposal_id":request_id,"state":"proposed",
        "review_sha256":expected,"patch_sha256":patch_sha256}),
    )
}
pub(crate) fn public_promotion(record: &Value) -> Value {
    json!({"session_id":record["session_id"],"client_request_id":record["client_request_id"],
        "state":record["state"],"review_sha256":record["review_sha256"],
        "patch_sha256":record["patch_sha256"],"paths":record["paths"],
        "created_at":record["created_at"],"updated_at":record["updated_at"],
        "summary":record["summary"],"proposed_by_run":record["proposed_by_run"]})
}
pub(crate) async fn promotions(State(app): State<App>, Path(id): Path<String>) -> Api {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let records = app.store.list(&format!("promotion:{id}:")).await?;
    Ok(Json(
        json!({"promotions":records.iter().map(public_promotion).collect::<Vec<_>>()}),
    ))
}
pub(crate) async fn promotion_review(
    State(app): State<App>,
    Path((id, request_id)): Path<(String, String)>,
) -> Api {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let record = app
        .store
        .get(&format!("promotion:{id}:{request_id}"))
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Promotion not found".into()))?;
    Ok(Json(json!({"promotion":public_promotion(&record),
        "baseline_commit":record["baseline_commit"],"status_output":record["status_output"],
        "diff":record["patch"]})))
}
pub(crate) async fn promotion_receipt(
    State(app): State<App>,
    Path((id, request_id)): Path<(String, String)>,
) -> Api {
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let record = app
        .store
        .get(&format!("promotion:{id}:{request_id}"))
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Promotion not found".into()))?;
    Ok(Json(public_promotion(&record)))
}
pub(crate) async fn promotion_reject(
    State(app): State<App>,
    Path((id, request_id)): Path<(String, String)>,
) -> Api {
    if Uuid::parse_str(&request_id).is_err() {
        return Err(bad("Invalid promotion identifier"));
    }
    let _guard = app.gate.lock().await;
    if app.store.get(&key("session", &id)).await?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
    }
    let receipt_key = format!("promotion:{id}:{request_id}");
    let mut record = app
        .store
        .get(&receipt_key)
        .await?
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "Promotion not found".into()))?;
    if record["state"] == "rejected" {
        return Ok(Json(public_promotion(&record)));
    }
    if record["state"] != "proposed" {
        return Err(conflict("Only a proposed promotion can be rejected"));
    }
    record["state"] = json!("rejected");
    record["updated_at"] = json!(now());
    app.store
        .atomic_docs_event(
            vec![(receipt_key, record.clone())],
            &id,
            "promotion_rejected",
            &json!({"client_request_id":request_id,
            "patch_sha256":record["patch_sha256"]}),
        )
        .await?;
    Ok(Json(public_promotion(&record)))
}
pub(crate) async fn promote(State(app): State<App>, Path(id): Path<String>, Json(body): Json<Value>) -> Api {
    let request_id = required(&body, "client_request_id", 128)?.to_owned();
    if Uuid::parse_str(&request_id).is_err() {
        return Err(bad("client_request_id must be a UUID"));
    }
    let expected = required(&body, "expected_review_sha256", 64)?.to_owned();
    if !valid_review_digest(&expected) {
        return Err(bad("Invalid review digest"));
    }
    let receipt_key = format!("promotion:{id}:{request_id}");
    let request_digest = digest(&body);
    {
        let _guard = app.gate.lock().await;
        if let Some(record) = app.store.get(&receipt_key).await? {
            if record["request_digest"] != request_digest {
                return Err(conflict("Promotion identifier reused with different input"));
            }
            if record["state"] != "proposed" {
                return Ok(Json(public_promotion(&record)));
            }
        }
        if app.store.get(&key("session", &id)).await?.is_none() {
            return Err(ApiError(StatusCode::NOT_FOUND, "Session not found".into()));
        }
        let policy = app
            .store
            .get("policy")
            .await?
            .unwrap_or_else(policy_default);
        if policy_decision(&policy, "source_review") != "allow" {
            return Err(ApiError(
                StatusCode::FORBIDDEN,
                "Source review is unavailable under current policy".into(),
            ));
        }
        if app.shutoff().await? {
            return Err(conflict("Global shutoff is active"));
        }
    }
    let review = tools::execute(
        "source_review",
        &json!({}),
        &id,
        &app.tool_socket,
        CancellationToken::new(),
    )
    .await?;
    let (patch, _baseline) = verified_review(&review, &expected)?;
    let patch_sha256 = hex::encode(Sha256::digest(patch.as_bytes()));
    let mut record = json!({"session_id":id,"client_request_id":request_id,
        "request_digest":request_digest,"state":"dispatching","review_sha256":expected,
        "baseline_commit":review["baseline_commit"],"patch_sha256":patch_sha256,
        "patch":patch,"paths":[],"created_at":now(),"updated_at":now()});
    {
        let _guard = app.gate.lock().await;
        if let Some(existing) = app.store.get(&receipt_key).await? {
            if existing["request_digest"] != request_digest {
                return Err(conflict("Promotion identifier reused with different input"));
            }
            if existing["state"] != "proposed" {
                return Ok(Json(public_promotion(&existing)));
            }
            if existing["patch_sha256"] != patch_sha256 || existing["review_sha256"] != expected {
                return Err(conflict("Proposed patch changed since review"));
            }
            record = existing;
            record["state"] = json!("dispatching");
            record["updated_at"] = json!(now());
        }
        if app.shutoff().await? {
            return Err(conflict("Global shutoff is active"));
        }
        let policy = app
            .store
            .get("policy")
            .await?
            .unwrap_or_else(policy_default);
        if policy_decision(&policy, "source_review") != "allow" {
            return Err(ApiError(
                StatusCode::FORBIDDEN,
                "Source review is unavailable under current policy".into(),
            ));
        }
        app.store
            .atomic_docs_event(
                vec![(receipt_key.clone(), record.clone())],
                &id,
                "promotion_requested",
                &json!({"client_request_id":request_id,
                "review_sha256":expected,"patch_sha256":patch_sha256}),
            )
            .await?;
    }
    let result = promotion::apply(&app.promotion_socket, patch, app.stop.child_token()).await;
    record["state"] = json!(match &result {
        Ok(reply) if reply["status"] == "success" => "applied",
        Ok(reply) if reply["status"] == "denied" => "denied",
        _ => "uncertain",
    });
    if let Ok(reply) = result {
        record["paths"] = reply["paths"].clone();
    }
    record["updated_at"] = json!(now());
    {
        let _guard = app.gate.lock().await;
        app.store
            .atomic_docs_event(
                vec![(receipt_key, record.clone())],
                &id,
                "promotion_result",
                &json!({"client_request_id":request_id,
                "state":record["state"],"patch_sha256":patch_sha256}),
            )
            .await?;
    }
    Ok(Json(public_promotion(&record)))
}
pub(crate) async fn recover_promotions(app: &App) -> Result<()> {
    let _guard = app.gate.lock().await;
    for mut record in app.store.list("promotion:").await? {
        if record["state"] != "dispatching" {
            continue;
        }
        record["state"] = json!("uncertain");
        record["updated_at"] = json!(now());
        let sid = record["session_id"]
            .as_str()
            .ok_or_else(|| anyhow!("Invalid promotion session"))?;
        let rid = record["client_request_id"]
            .as_str()
            .ok_or_else(|| anyhow!("Invalid promotion request"))?;
        app.store
            .atomic_docs_event(
                vec![(format!("promotion:{sid}:{rid}"), record.clone())],
                sid,
                "promotion_recovered",
                &json!({"client_request_id":rid,
                "state":"uncertain","patch_sha256":record["patch_sha256"]}),
            )
            .await?;
    }
    Ok(())
}
